"""Workflow A — Driver Eco Dashboard V1 publication and e-mail delivery job.

`run(client, run_id, params)`, the platform's standard job contract, so the
existing scheduler can invoke it later without a second scheduling framework.
**It is deliberately not registered in any schedule.** Every Eco schedule is
disabled today and enabling one is a separate, separately authorized decision.

It is safe for scheduler retry and restart semantics because it is re-entrant
by construction: each invocation reads the durable delivery state, performs THE
one safe next action, and returns. Firing it twice concurrently is a supported
input — the second invocation observes that another owns the delivery and does
nothing.

MODES

    render_only  (default)  builds the canonical snapshot and the message from
                            a synthetic placeholder capability, prints the link
                            in REDACTED form, and performs no publication, no
                            provider call and no remote transition. Nothing
                            leaves the host.
    execute                 runs the real lifecycle against the configured
                            secure-delivery endpoint and e-mail provider.

`execute` fails closed on missing configuration rather than guessing: no
default dashboard domain, no default publisher endpoint, no default machine
credential. A dry run that quietly became a real send would be exactly the
failure this design exists to prevent.

WHAT THIS JOB NEVER DOES

It never sends the recipient address, the driver identity key, the client code
or the period label to the delivery boundary; it never puts a capability in a
query string, a log line or a job result; and it never marks the remote
operation `DELIVERED` on the strength of an attempted request.
"""

from __future__ import annotations

import os
import socket
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from jobs.ecodriving_dashboard import publisher as pub
from jobs.ecodriving_dashboard import secure_delivery_client as sdc
from jobs.ecodriving_dashboard.dashboard_email import (
    DashboardEmailContext,
    build_capability_url,
    build_message,
    build_message_id,
    redact_capability_url,
)
from jobs.ecodriving_dashboard.delivery_contract import (
    DeliveryIdentity,
    derive_operation_id,
    derive_provider_idempotency_key,
    derive_recipient_identity,
    derive_subject_ref,
    normalise_email,
)
from jobs.ecodriving_dashboard.delivery_ledger import DeliveryLedger, LedgerConflict
from jobs.ecodriving_dashboard.email_provider import (
    ProviderConfigurationError,
    SmtpEmailProvider,
)
from jobs.ecodriving_dashboard.job_eco_dashboard_snapshot import (
    _client_business_pg_conn,
    _load_client_account_config,
    build_delivery_snapshot,
)
from jobs.ecodriving_dashboard.publication import PrivacyContext
from jobs.ecodriving_dashboard.snapshot_contract import (
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
)

JOB_SOURCE = "jobs.ecodriving_dashboard.job_eco_dashboard_publish"
DATASET_NAME = "eco_dashboard_publish"

MODE_RENDER_ONLY = "render_only"
MODE_EXECUTE = "execute"

SNAPSHOT_UNAVAILABLE = "SNAPSHOT_UNAVAILABLE"
#: A snapshot that is not `OK` is not published. There is no partial dashboard,
#: and a link to one would be worse than no link.
PUBLISHABLE_STATUS = "OK"

ENV_DASHBOARD_BASE_URL = "ECO_DASHBOARD_BASE_URL"
ENV_PUBLISHER_ENDPOINT = "ECO_DASHBOARD_PUBLISHER_URL"
ENV_PUBLISHER_TOKEN = "ECO_DASHBOARD_PUBLISHER_TOKEN"
ENV_MESSAGE_ID_DOMAIN = "ECO_DASHBOARD_MESSAGE_ID_DOMAIN"

#: Shape-only placeholder used by `render_only` so the message can be built
#: without a publication existing. It is not a capability and cannot be one:
#: no grant is ever minted with this value, and the rendered link is printed
#: only in redacted form.
_PLACEHOLDER_CAPABILITY = "0" * 43


class PublishConfigurationError(RuntimeError):
    """Execute mode is missing something it must not guess."""


def _require(params: dict, name: str) -> str:
    value = str(params.get(name) or "").strip()
    if not value:
        raise ValueError(f"Missing required param: {name}")
    return value


def _setting(params: dict, key: str, env_name: str) -> str:
    return str(params.get(key) or os.getenv(env_name) or "").strip()


#: One definition of the lease owner, shared with every other caller of the
#: publisher, so two entry points can never disagree about what "somebody else
#: is working on this" means.
owner_token = pub.owner_token


def _delivery_identity(*, client_id: str, identity_key: str, period_type: str,
                       start: date, end_exclusive: date, send_scope: str) -> DeliveryIdentity:
    return DeliveryIdentity(
        client_id=str(client_id),
        identity_key=identity_key,
        period_type=period_type,
        period_start_date=start,
        period_end_date=end_exclusive,
        send_scope=send_scope,
    )


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = _require(params, "client_id")
    identity_key = _require(params, "identity_key")
    recipient_email = normalise_email(_require(params, "recipient_email"))

    period_type = str(params.get("period_type") or PERIOD_TYPE_WEEKLY).strip().lower()
    if period_type not in (PERIOD_TYPE_WEEKLY, PERIOD_TYPE_MONTHLY):
        raise ValueError("period_type must be 'weekly' or 'monthly'")

    mode = str(params.get("mode") or MODE_RENDER_ONLY).strip().lower()
    if mode not in (MODE_RENDER_ONLY, MODE_EXECUTE):
        raise ValueError("mode must be 'render_only' or 'execute'")
    send_scope = str(params.get("send_scope") or "normal").strip().lower()

    dashboard_base_url = _setting(params, "dashboard_base_url", ENV_DASHBOARD_BASE_URL)
    message_id_domain = _setting(params, "message_id_domain", ENV_MESSAGE_ID_DOMAIN) \
        or "eco-dashboard.local"

    result: dict[str, Any] = {
        "job_name": DATASET_NAME,
        "client_id": client_id,
        "period_type": period_type,
        "mode": mode,
        "send_scope": send_scope,
        "snapshot_status": SNAPSHOT_UNAVAILABLE,
        "published": False,
        "message_prepared": False,
        "provider_called": False,
        "invocation": None,
        "state": None,
        "next_action": None,
    }

    # --- canonical snapshot, through the one supported host path --------------
    built = build_delivery_snapshot(
        client_id=client_id,
        identity_key=identity_key,
        period_type=period_type,
        params=params,
        privacy=PrivacyContext(
            identity_key=identity_key,
            client_code=str(params.get("client_code") or "") or _client_code(client_id),
            # The recipient address is declared so the value-level privacy
            # sweep would catch it if it ever appeared in the payload. It is
            # never written into the document.
            email_addresses=(recipient_email,),
        ),
    )
    if built is None:
        client.log("WARNING", "SCRIPT", JOB_SOURCE,
                   "No Eco Driving stats row for the requested closed period",
                   run_id=run_id, context={"period_type": period_type})
        return result

    identity = _delivery_identity(
        client_id=client_id, identity_key=identity_key, period_type=period_type,
        start=built.current_identity.period_start_date,
        end_exclusive=built.current_identity.period_end_date_exclusive,
        send_scope=send_scope)

    result.update({
        "period_label": built.current_identity.period_label,
        "period_start_date": identity.period_start_date.isoformat(),
        "period_end_date_exclusive": identity.period_end_date.isoformat(),
        "snapshot_status": built.snapshot_status,
        "payload_bytes": len(built.payload),
        "payload_digest": built.payload_digest,
        "operation_id": derive_operation_id(identity),
        "subject_ref": derive_subject_ref(identity),
        "recipient_identity": derive_recipient_identity(identity, recipient_email),
    })

    if built.snapshot_status != PUBLISHABLE_STATUS:
        # Fail closed: a period that could not produce a complete score is not
        # published and no link is sent.
        client.log("WARNING", "SCRIPT", JOB_SOURCE,
                   "Snapshot is not publishable; no dashboard link will be delivered",
                   run_id=run_id,
                   context={"snapshot_status": built.snapshot_status,
                            "period_label": built.current_identity.period_label})
        result["next_action"] = "NONE"
        return result

    if mode == MODE_RENDER_ONLY:
        return _render_only(client, run_id, result, identity,
                            recipient_email=recipient_email,
                            dashboard_base_url=dashboard_base_url,
                            message_id_domain=message_id_domain,
                            expires_at=None)

    # --- execute --------------------------------------------------------------
    endpoint = _setting(params, "publisher_endpoint", ENV_PUBLISHER_ENDPOINT)
    token = _setting(params, "publisher_token", ENV_PUBLISHER_TOKEN)
    if not endpoint:
        raise PublishConfigurationError(f"{ENV_PUBLISHER_ENDPOINT} is not configured")
    if not token:
        raise PublishConfigurationError(f"{ENV_PUBLISHER_TOKEN} is not configured")
    if not dashboard_base_url:
        raise PublishConfigurationError(f"{ENV_DASHBOARD_BASE_URL} is not configured")

    # PREFLIGHT, BEFORE ANY EXTERNAL EFFECT. The publisher state machine runs
    # the same checks again — this is the job-level fail-closed, so an execute
    # run with an unusable destination or an unconfigured provider ends here,
    # having published nothing, issued no capability and contacted nobody.
    #
    # The endpoint is validated before a `SecureDeliveryClient` exists to hold
    # the machine credential, so a destination that is not permitted to receive
    # plaintext never has one attached to a request aimed at it.
    try:
        sdc.validate_publisher_endpoint(endpoint)
    except sdc.SecureDeliveryError as error:
        raise PublishConfigurationError(
            f"{ENV_PUBLISHER_ENDPOINT} is not usable: {error.code}") from None
    provider = SmtpEmailProvider()
    try:
        backend = provider.preflight()
    except ProviderConfigurationError as error:
        # A definite local failure. Deliberately NOT a delivery state: nothing
        # was attempted, so there is nothing to reconcile.
        result["invocation"] = pub.INVOCATION.PREFLIGHT_FAILED
        result["conflict_code"] = error.code
        result["next_action"] = "OPERATOR_INVESTIGATION"
        client.log("ERROR", "SCRIPT", JOB_SOURCE,
                   "Provider configuration is missing or invalid; nothing was published",
                   run_id=run_id, context={"code": error.code,
                                           "operation_id": result["operation_id"]})
        return result
    result["provider_backend_id"] = backend.stable_id()

    cfg = _load_client_account_config(client_id=client_id)
    # Autocommit at construction: the helper sets the business timezone with a
    # statement, so a session opened without it is already INTRANS and psycopg
    # refuses `conn.autocommit = True` from that point on.
    conn = _client_business_pg_conn(cfg, autocommit=True)
    try:
        ledger = DeliveryLedger(conn)
        services = pub.PublisherServices(
            ledger=ledger,
            client=sdc.SecureDeliveryClient(
                sdc.HttpSecureDeliveryTransport(endpoint), publisher_token=token),
            provider=provider,
            config=pub.PublisherConfig(
                dashboard_base_url=dashboard_base_url,
                message_id_domain=message_id_domain),
            logger=lambda level, event, context: client.log(
                level, "SCRIPT", JOB_SOURCE, event, run_id=run_id, context=dict(context)),
        )
        try:
            outcome = pub.advance_delivery(
                services,
                identity=identity,
                payload_digest=built.payload_digest,
                recipient_email=recipient_email,
                body=built.payload,
                owner=owner_token(run_id),
                run_id=run_id,
            )
        except LedgerConflict as conflict:  # pragma: no cover - defensive
            result["invocation"] = pub.INVOCATION.CONFLICT
            result["conflict_code"] = conflict.code
            return result
    finally:
        conn.close()

    summary = outcome.summary()
    result.update({
        "invocation": summary["invocation"],
        "state": summary["state"],
        "next_action": summary["next_action"],
        "steps": summary["steps"],
        "conflict_code": summary.get("conflict_code"),
        "delivery": summary.get("delivery"),
        "published": summary["state"] not in ("PREPARED",),
        "message_prepared": summary["state"] not in ("PREPARED", "BEARER_RECOVERY_REQUIRED",
                                                     "CAPABILITY_PERSISTED"),
        "provider_called": bool((summary.get("delivery") or {}).get("provider_attempts")),
    })
    return result


def _client_code(client_id: str) -> str:
    cfg = _load_client_account_config(client_id=client_id)
    return str(getattr(cfg, "client_code", "") or "")


def _render_only(client, run_id, result: dict, identity: DeliveryIdentity, *,
                 recipient_email: str, dashboard_base_url: str,
                 message_id_domain: str, expires_at: Optional[datetime]) -> dict:
    """Prove message construction without publishing anything.

    The placeholder capability makes the link's SHAPE observable while
    guaranteeing the printed value is not a real one; the link is redacted
    anyway, because a dry run is not an exception to the rule that a capability
    is never printed.
    """
    base = dashboard_base_url or "https://eco-dashboard.invalid"
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    url = build_capability_url(base, _PLACEHOLDER_CAPABILITY)
    message = build_message(
        DashboardEmailContext(
            recipient_email=recipient_email,
            period_type=identity.period_type,
            period_start_date=identity.period_start_date,
            period_end_date=identity.period_end_date,
            capability_url=url,
            expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=14)),
        ),
        message_id=build_message_id(key, domain=message_id_domain),
    )
    result.update({
        "message_prepared": True,
        "provider_idempotency_key": key,
        "message_id": message.message_id,
        "email_subject": message.subject,
        "capability_url_redacted": redact_capability_url(url),
        "dashboard_base_url_configured": bool(dashboard_base_url),
        "invocation": "RENDER_ONLY",
        "state": "NOT_STARTED",
        "next_action": "PUBLISH",
    })
    client.log("INFO", "SCRIPT", JOB_SOURCE,
               "Eco Dashboard delivery rendered (no publication, no provider call)",
               run_id=run_id,
               context={"operation_id": result["operation_id"],
                        "recipient_identity": result["recipient_identity"],
                        "email_subject": message.subject,
                        "capability_url_redacted": result["capability_url_redacted"]})
    return result
