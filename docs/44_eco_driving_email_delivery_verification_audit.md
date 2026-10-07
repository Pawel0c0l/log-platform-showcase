# ECO_DRIVING_EMAIL_DELIVERY_VERIFICATION_AUDIT

Read-only audit of what the platform can currently **prove** about the delivery of
Eco Driving weekly and monthly customer e-mails, and of which stronger levels of
verification are reachable on the existing example.invalid SMTP infrastructure.

- **Executed:** 2026-08-30
- **Repository / branch / HEAD:** `log-platform` / `main` / `e00aabd7acacebdf289fafb72cd4075b18b560c4`
- **Method:** static reading of the four mailer jobs, their shared SMTP/idempotency
  modules, the applied client-business migrations and the operational runbooks;
  execution of the existing deterministic safety suite; read-only aggregate SQL
  against the platform database and all five enabled client-business databases
  (`SET TRANSACTION READ ONLY` on every session).
- **Not done:** no live SMTP connection, no test or customer e-mail, no production
  mutation, no migration, no schedule change, no service restart, no commit, no push.
  No recipient address, driver identity or credential is reproduced below.
- **Scope note:** AUDIT ONLY. §11 offers options for an owner decision; none of them
  is implemented, and nothing in the sending or retry path was changed.

> **Status note (added after implementation).** §3.3's "largest cheap loss" and
> §11 Option 1 have since been implemented for all four mailers: the relay's final
> reply (code, verbatim text, queue id when example.invalid emits one) is now retained in
> `provider_response` + `metadata_json.smtp_acceptance`, and the exact transmitted
> MIME message is appended to the sender mailbox's Sent folder wherever that
> sender's `{PREFIX}_IMAP_*` namespace is configured. Everything else below —
> bounces, DSNs, delivery state — remains unimplemented and out of scope.
> See `docs/07_operations.md` § "Eco e-mail senders, SMTP acceptance and the
> Sent-folder copy".

---

## 1. Executive conclusion

### What we can currently prove

For a normal production send, the strongest provable statement today is:

> **This host opened an authenticated SMTP session to the example.invalid relay, transmitted
> exactly one message addressed to exactly one recipient, and `smtplib` returned
> from `sendmail`/`send_message` without raising — which means the relay answered
> the terminating dot with a `250`.**

That is level **B** in the taxonomy of §5: *example.invalid accepted the message for relay*.
It is a genuine, protocol-grounded fact, not merely "no exception was thrown", because
`smtplib.SMTP.sendmail` raises `SMTPDataError` on any final reply other than `250`
(CPython 3.12 `smtplib.py`, `sendmail` → `(code, resp) = self.data(msg); if code != 250: raise`).

Everything downstream of the relay — whether the recipient's mail server accepted it,
whether it reached a mailbox, whether it landed in Inbox or Junk, whether anyone read
it — is **not observed anywhere in this system**. There is no bounce ingestion, no DSN
request, no return-path processing and no INBOX reader for any of the four mailers.
`sent` therefore terminates the lifecycle: no code path mutates a send-log row after it.

Three qualifications matter and are developed below:

1. **The `250` text itself is thrown away.** `smtplib` discards the final reply on
   success, and neither mailer asks for it. So the relay's queue identifier — the one
   token that would let an operator ask example.invalid about a specific message later — is
   accepted and immediately lost (§3.3).
2. **Per-recipient durable evidence exists and is good, but it is young and it has a
   hole.** The four `eco_*_email_send_log` tables are proper per-recipient ledgers with
   DB-enforced idempotency. However, the ALPHA driver logs contain **no row older than
   2026-08-10**, while `public.runs` and `public.logs` show driver sends from
   2026-07-21 onwards, including the 1 137-message monthly production send of
   2026-08-07. For those earlier periods only batch counters survive (§4.4).
3. **The ambiguous-send contract is real and correctly implemented.** An SMTP outcome
   that cannot be proven to be a refusal freezes the reservation, is never retried by
   any automatic path — `force_resend` included — and can only be cleared by an
   operator attestation (§3.2, §6). `SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO`
   is enforced in code, in SQL and in a passing deterministic test suite.

### What appears technically achievable on the existing example.invalid SMTP architecture

Without changing transport, without an external provider, and without weakening the
ambiguous-retry constraint:

| | Achievable | How |
|---|---|---|
| **Capture the relay's final `250` text and queue ID** | **Yes, application-only** | Replace the single `sendmail` call with the explicit `mail()`/`rcpt()`/`data()` sequence, whose `data()` return value is exactly `(250, b'2.0.0 Ok: queued as ...')`. No provider capability needed; example.invalid already sends this line. |
| **Shrink the ambiguity window** | **Yes, application-only** | The queue ID above is captured *inside* the same `data()` call, so a row that has one is provably accepted. It does not eliminate the window (a socket can still die before the reply is read) but it makes every accepted send individually addressable in the provider's own logs. |
| **Bounce / NDR ingestion** | **Yes for BRAVO00016, application-only; ALPHA needs one mailbox decision** | The BRAVO weekly sender already holds working IMAP credentials for the *same* mailbox it sends from and already searches it by `Message-ID`. Reading `INBOX` instead of only appending to `Sent`, parsing `multipart/report; report-type=delivery-status` and correlating on the original `Message-ID`, is a pure application addition. The ALPHA sender address has no IMAP credentials configured. |
| **RFC 3461 DSN (`NOTIFY=`, `RET=`, `ORCPT=`)** | **Unknown — provider confirmation required** | Nothing in the repository records whether the example.invalid submission service advertises the `DSN` ESMTP keyword. This is a one-command read-only check (§8.2). |
| **Destination-server acceptance / mailbox delivery / open tracking** | **Not provable over ordinary SMTP submission** | Relay-and-forward hides the downstream conversation. Only negative evidence (a bounce) crosses back. Open tracking would require a tracking pixel or link instrumentation, which is a product and privacy decision, not an SMTP one. |

---

## 2. Current architecture

### 2.1 The four mailers

There are **four** Eco Driving customer mailers, in two families. Both families send
weekly and monthly. They are structurally the same program.

| Family | Weekly job | Monthly job | Recipient unit | Send log | Subject column |
|---|---|---|---|---|---|
| ALPHA driver | `jobs.ecodriving.job_eco_driving_weekly_email_notifications` | `jobs.ecodriving.job_eco_driving_monthly_email_notifications` | driver in `eco_drivers_id_chart` | `eco_driving_{weekly,monthly}_email_send_log` | `assigned_id` |
| BRAVO person | `jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications` | `jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications` | physical person | `eco_person_{weekly,monthly}_email_send_log` | `person_name_group_key` |

A `diff` of the weekly and monthly driver jobs (normalising the period words) shows
they differ only in template map, stats columns, period aliases and one comment block.
**The weekly and monthly delivery paths are identical.** One consequence is worth
recording: the monthly driver job reads the *weekly* SMTP environment namespace
(`ECO_WEEKLY_EMAIL_SMTP_*`) — see `job_eco_driving_monthly_email_notifications.py:316-354`.
That is deliberate reuse of one sender account, not a defect, but it means weekly and
monthly ALPHA mail share one transport identity.

### 2.2 Execution flow (identical for weekly and monthly)

```
trigger (operator ops/runner.py, or dispatcher fire)
  -> resolve_execution_contract(params)              jobs/ecodriving/email_safety.py
       render_only | test_send | normal_send | force_resend
  -> validate_template_inventory / link placeholders
  -> load_smtp_settings_from_env(dry_run=False)      fail-closed on missing secrets
  -> authorize_dashboard_mailing(...)                ops/eco_dashboard_mailing_rollout.json
  -> select_period_for_send(fetch_period_candidates(...))
       half-open Europe/Warsaw period; SNAPSHOT_NOT_FINALIZED / PERIOD_NOT_CLOSED
  -> _fetch_candidates(...)                          one row per recipient
  for each candidate:
       classify_candidate(...)                       -> skip / INVALID_RANKING_SNAPSHOT / send
       unresolved_ambiguous_send(...)                PRE-FLIGHT GATE, before any remote effect
       render_with_dashboard_link(...)               may publish snapshot + mint capability
       reserve_send(...)                             INSERT status='pending', COMMIT
       ---------- remote-effect boundary ----------
       send_html_email(...) / send_prepared_email(...)   ONE smtplib session, ONE submission
       ---------- remote-effect boundary ----------
       mark_send_sent(...) | mark_send_ambiguous(...) | mark_send_failed(...)   COMMIT
       [BRAVO weekly only] archive_sent_message(...) -> IMAP APPEND to Sent, verify by Message-ID
  -> summary dict returned; run_context marks the run SUCCESS
```

Authoritative anchors:

- SMTP session and failure classification: `jobs/common/eco_smtp_submission.py:211` (`run_smtp_session`), `:167` (`classify_transmit_exception`), `:97` (`_proves_protocol_rejection`).
- Reservation / idempotency: `jobs/ecodriving/email_idempotency.py` (`reserve_send`), `jobs/ecodriving_person/email_idempotency.py:172`.
- Ambiguity persistence and operator exit: `jobs/common/eco_email_reconciliation.py:126` (`mark_send_ambiguous`), `:171` (`unresolved_ambiguous_send`), `:222` (`resolve_ambiguous_send`).
- Driver weekly send loop: `jobs/ecodriving/job_eco_driving_weekly_email_notifications.py:1292-1400`.
- Driver monthly send loop: `jobs/ecodriving/job_eco_driving_monthly_email_notifications.py:1306-1400`.
- Person weekly send loop incl. archive: `jobs/ecodriving_person/job_eco_driving_person_weekly_email_notifications.py:1354-1525`.
- Person monthly send loop: `jobs/ecodriving_person/job_eco_driving_person_monthly_email_notifications.py:1283-1400`.
- Message construction: `job_eco_driving_weekly_email_notifications.py:533` (`build_email_message`), `jobs/ecodriving_person/email_delivery.py:227` (`prepare_email`).

### 2.3 Message shape, attachments, links

No mailer attaches a file. `grep` for `add_attachment` across the mailers and
`jobs/ecodriving_person/email_delivery.py` returns nothing. Every message is a
`multipart/alternative` with a generated plain-text part and the rendered HTML part.

Dashboard links are the only externally-reachable payload, and they are gated twice —
by the `--with-dashboard` runner option and by `ops/eco_dashboard_mailing_rollout.json`.
Current declared rollout: **BRAVO00016 enabled, ALPHA00001 disabled.** ALPHA production
mail therefore carries no dashboard link at all today.

### 2.4 Triggering — what actually fires

| | ALPHA driver weekly/monthly | BRAVO person weekly/monthly |
|---|---|---|
| registered in `workflow_a_control.dataset_registry` | **No** (verified: registry holds only the two `eco_person_driving_*_email_notifications` rows) | Yes (`db/migrations/046_workflow_a_eco_person_registry.sql`) |
| schedule rows | none | present for all five clients, **all `enabled = false`** (verified) |
| declared in `ops/eco_mailing_production_schedule.json` | no | BRAVO00016 **weekly** only, `normal_send`; monthly deliberately undeclared |
| therefore triggerable by | manual `ops/runner.py` / `log-job-runner.sh` only | manual only, until a schedule row is enabled |

**No Eco Driving customer e-mail is currently sent by a timer.** Every production send
so far was a manual invocation. This is directly relevant to §7: batch reconciliation
today has no scheduled producer to attach to.

---

## 3. What "email sent" currently means

### 3.1 The precise success point

`status = 'sent'` is written at `job_eco_driving_weekly_email_notifications.py:1336-1344`
(and the three structural twins), immediately after `send_html_email` returns. Decomposed
against the states the audit asked about:

| Candidate meaning | Is that what `sent` means? |
|---|---|
| message object created | necessary but not sufficient — `sent` is written after transmission |
| connection + STARTTLS + AUTH succeeded | necessary; a failure here is `DEFINITE_NOT_SUBMITTED` (`eco_smtp_submission.py:229-246`) |
| `MAIL FROM` accepted | necessary; `SMTPSenderRefused` is `DEFINITE_NOT_SUBMITTED` |
| every `RCPT TO` accepted | necessary; `SMTPRecipientsRefused` is `DEFINITE_NOT_SUBMITTED`. There is exactly one recipient per message, so partial recipient acceptance is unreachable in the driver path |
| body accepted after `DATA` | **yes** |
| relay returned a final `2xx` | **yes** — this is the load-bearing fact. `smtplib` raises unless the terminating dot is answered `250` |
| job finished without exception | **no** — the job completes regardless; per-recipient outcome is per-row |
| some persistent record was created | **yes, but that is a consequence, not the definition** |

So `sent` = *"example.invalid answered `250` to the terminating dot for this one recipient"*. That
is stronger than "no exception" and materially weaker than "delivered". The schema says
so in its own words:

```sql
COMMENT ON TABLE public.eco_driving_weekly_email_send_log IS
  '... status=sent means the SMTP server accepted the message without raising an
   exception; it does not guarantee inbox delivery.';
```
(`db/client_business/032_eco_driving_weekly_email_notifications.sql`)

`provider_response` is **not** the server's words. It is the constant literal
`'SMTP accepted message without raising an exception'`, written by the job
(`:1343`; person path returns the same literal from `email_delivery.py:294`). Verified
in production: the ALPHA weekly log holds exactly two distinct values of that column —
that literal (3 441 rows) and `NULL` (284 rows).

### 3.2 What a failure means

`classify_exception` returns one of two values, and **the default is ambiguity**:

- `DEFINITE_NOT_SUBMITTED` — connect/STARTTLS/login failure by construction (nothing was
  written), or a transmit exception carrying protocol evidence: `SMTPHeloError`,
  `SMTPNotSupportedError`, `SMTPSenderRefused`, `SMTPRecipientsRefused`, or an
  `SMTPDataError` whose `smtp_code` parses into `400 ≤ code < 600`.
- `AMBIGUOUS_SUBMISSION` — everything else, explicitly including
  `SMTPDataError(-1, b'garbled')`, socket death mid-`DATA`, and a timeout reading the
  final reply.

The `SMTPDataError` split at `eco_smtp_submission.py:97-166` is the subtle and correct
part: CPython raises that one type from two structurally different places (a non-354
answer to `DATA`, where nothing was written; and a non-250 answer to the terminating dot,
where everything was written), so the **reply code, not the exception type**, is what may
be treated as evidence.

`quit()` failures are swallowed (`:255-258`) — a relay that accepted and then dropped
during `QUIT` has the message, and turning that into a failure would be a false negative
in exactly the direction that produces duplicate customer mail.

### 3.3 What SMTP evidence is available versus retained

| Evidence | Available from the library? | Retained today? |
|---|---|---|
| SMTP client | `smtplib` (CPython 3.12), `SMTP` or `SMTP_SSL` per `*_USE_SSL` | n/a |
| connection/TLS/auth outcome | yes, as exceptions | only as `error_message` text on a `failed` row |
| `MAIL FROM` refusal code/text | yes (`SMTPSenderRefused.smtp_code/.smtp_error`) | only inside the `str(exc)` in `error_message` |
| per-recipient `RCPT TO` refusals | yes — `sendmail` returns `{addr: (code, msg)}` for partial refusal | driver path: return value **discarded** (`send_html_email` ignores `run_smtp_session`'s result). Person path: `email_delivery.py:264-272` inspects it and raises, which classifies AMBIGUOUS — correct, since a non-empty map means the message *was* transmitted |
| final `250` reply text and **queue ID** | **not exposed by `sendmail`** — `smtplib` reads it and discards it on success | **not retained. This is the single largest cheap loss.** |
| queue/message identifier from the server | would be inside that discarded `250` line | no |
| `Message-ID` header | generated **by this host** — `make_msgid(domain=<from-domain>)` at `:563` / `email_delivery.py:236` | **yes**, stored in `smtp_message_id`. Verified: 100 % of `sent` rows across all four production logs carry one |
| deterministic / recoverable `Message-ID` | random per attempt, so **not deterministic**, but durably recorded before the row leaves `pending`→`sent` | yes, recoverable from the send log |
| exact MIME bytes | reconstructible | **only BRAVO weekly**: `sent_mime_bytes` + `sent_mime_sha256` (migration `041`). Verified: 212 BRAVO weekly rows carry MIME; the 39 BRAVO **monthly** rows carry none |
| connection timeout | `*_TIMEOUT_SECONDS`, default 30 s, applied to the whole session | value not persisted per row |
| any SMTP transaction metadata beyond the above | no | no |

### 3.4 The six-level ladder

| Level | Statement | Can the current system prove it? |
|---|---|---|
| 1 | the application attempted a send | **Yes.** A committed `pending` reservation exists *before* the socket is opened, and `summary.smtp_attempt_count` counts the boundary crossings. |
| 2 | SMTP submission was confirmed (session completed, message transmitted) | **Yes**, for `status='sent'`. |
| 3 | the example.invalid relay accepted responsibility for the message | **Yes**, but only as the inference "`250` was returned". The relay's own acceptance token is not kept, so the claim cannot be re-verified against example.invalid afterwards. |
| 4 | the recipient's mail server accepted it | **No.** Nothing downstream of the relay is observed. |
| 5 | mailbox delivery | **No.** |
| 6 | the human opened/read it | **No.** No pixel, no link-click attribution, no read receipt. |

---

## 4. Current evidence model

### 4.1 Per-recipient persistence — the send logs

All four tables are genuine per-recipient ledgers. Columns (union across the family):

`send_log_id, client_id, run_id, <subject>, recipient_email, original_recipient_email,
ranking_type, report_type, send_scope, idempotency_key, parent_send_log_id, template_type,
template_filename, qualification_status, ranking_included, template_variant,
period_start_date, period_end_date, ecodriving_rating_type, email_subject, status,
smtp_message_id, provider_response, error_message, force_resend_reason, force_resend_at,
attempted_at, sent_at, created_at, updated_at, metadata_json` — plus, on the two person
tables, `sent_mime_bytes, sent_mime_sha256, sent_archive_status, sent_archive_mailbox,
sent_archive_message_id, sent_archive_attempted_at, sent_archive_verified_at, sent_archive_error`.

Status vocabulary: `pending`, `sent`, `failed`, `dry_run_rendered`,
`skipped_already_sent`, `skipped_missing_email`, `skipped_unknown_rating_type`
(person tables add `skipped`, `skipped_existing_reservation`).
Scope vocabulary: `normal`, `test`, `forced`, `render_only`/`dry_run`, `skipped`.

**The durable per-recipient record the audit asked about exists.** Mapping:

| Required element | Present as |
|---|---|
| report instance | `(report_type, period_start_date, period_end_date)` + `run_id` → `public.runs` |
| customer | `client_id` (+ `client_code` via `workflow_a_control.client_account`) |
| recipient | `<subject>` and `recipient_email` / `original_recipient_email` |
| intended message | `email_subject`, `template_type`, `template_filename`, `template_variant`; **exact bytes only for BRAVO weekly** |
| send attempt | one row per attempt; `attempted_at`, `send_scope`, `idempotency_key`, `parent_send_log_id` |
| SMTP outcome | `status`, `sent_at`, `smtp_message_id`, `error_message`, `metadata_json.smtp_submission_result/_phase/_detail` |

What is **missing** from that record:

1. the relay's final reply text and queue ID (§3.3);
2. a typed failure classification column — the reason lives in free-text `error_message`
   and in `metadata_json`, not in a constrained column that could be indexed or counted;
3. any post-submission state at all — there is no column that could ever hold
   `BOUNCED_PERMANENT`, `DELIVERED` or similar, and no writer that would set one;
4. exact MIME bytes for three of the four mailers.

### 4.2 DB-enforced idempotency

Migration `044_eco_email_fail_closed_idempotency.sql` installs, per table:

```sql
CREATE UNIQUE INDEX uq_<table>_normal_identity
  ON public.<table> (client_id, <subject>, report_type, period_start_date, period_end_date)
  WHERE send_scope = 'normal' AND status IN ('pending','sent');
CREATE UNIQUE INDEX uq_<table>_normal_idempotency
  ON public.<table> (idempotency_key)
  WHERE send_scope = 'normal' AND status IN ('pending','sent');
```

Two properties follow, and both are load-bearing:

- **Template and rating are deliberately excluded from identity.** A re-render with a
  different template cannot manufacture a second normal delivery for the same period.
- **A `pending` row holds the identity slot.** The reservation is committed *before* SMTP
  (`:1330`), so a concurrent worker, a restart, or a later run is refused by PostgreSQL
  and not merely by application logic.

The migration is conflict-gated: it aborts with `ECO_EMAIL_IDEMPOTENCY_CONFLICT` rather
than rewriting history if the pre-existing rows cannot satisfy the new identity.

**Application state is heterogeneous across clients.** Verified read-only:
`send_scope`/`idempotency_key`/`parent_send_log_id` are present on BRAVO00016 and
ALPHA00001; on FOXTROT00001, DELTA00001 and ECHO00001 the driver send-log tables still have
only the pre-`044` shape and the two person tables are absent entirely. Those three
clients have never sent an Eco e-mail (all logs empty), so the gap is latent rather
than active — but a first send for one of them would run without the fail-closed
identity guarantee. **This is the one finding in this audit that is a live risk rather
than an observability gap.**

### 4.3 Other persistence in the neighbourhood

- **`public.runs` / `public.logs`** (platform DB). Every mailer logs its complete summary
  dict at INFO on completion, and an ERROR line per failed/ambiguous recipient carrying
  `send_log_id`, `smtp_submission_result` and `operator_action_required`.
- **`public.eco_dashboard_delivery_operation`** (migrations `049`/`050`/`051`) — a
  per-recipient *delivery* ledger with a real state machine
  (`PREPARED → CAPABILITY_PERSISTED → EXTERNAL_MAILER_HANDOFF`, plus the unreachable-here
  `DELIVERY_INTENT_RECORDED / PROVIDER_SUBMISSION_PENDING / PROVIDER_ACCEPTED /
  PROVIDER_AMBIGUOUS / PROVIDER_REJECTED / REMOTE_DELIVERED / FINALIZED`), an immutable
  submission identity (`provider_idempotency_key`, `provider_backend_id`,
  `provider_message_fingerprint`, `provider_bound_capability_id`,
  `provider_bound_bearer_generation`), `provider_message_id`, `provider_accepted_at`,
  `remote_delivered_at`, leases, attempt counts and an `operator_action_required` flag.
  **This machinery is deliberately not used for the actual send.** Migration `050`
  introduces `external_mailer` precisely so that a row whose link was handed to the Eco
  mailer terminates at `EXTERNAL_MAILER_HANDOFF` and the ledger's own provider lifecycle
  becomes physically unreachable — otherwise it would send the driver a *second*,
  dashboard-specific message. It is nevertheless the closest existing model of the state
  machine §10 would need, and it only exists for dashboard-enabled sends (BRAVO00016).
- **Sent-folder archive** — BRAVO weekly only. After SMTP acceptance the exact MIME bytes
  are stored in the client DB and `IMAP APPEND`ed to the sender's `\Sent` mailbox, then
  verified by searching `HEADER Message-ID` (`email_delivery.py:389-441`). Archive failure
  is tracked independently of `status` and is repairable by an `archive_only` run that
  never opens SMTP.
- **No outbox table, no retry queue, no incident table for customer mail.** The
  `suspected_bug` outbox with its `dead_letter` state applies to *operator alert* mail
  only, not to customer mail.

### 4.4 Can an operator answer "did customer X receive the report for period Y?"

**For a period whose rows still exist, the honest answer is:**

> "For customer X, recipient R, period Y, the send log holds a `normal`-scope row with
> `status='sent'`, `sent_at=<ts>`, `smtp_message_id=<id>`, written by run `<run_id>`. That
> means the example.invalid relay accepted that message. We do not know whether it was delivered,
> and if it bounced we would not know."

Confidence in the *acceptance* claim: **high**. Confidence in *delivery*: **none — the
system holds no evidence either way.**

**For BRAVO00016 weekly specifically, one step more is available:** the message is also
present, byte-identical, in the sender's Sent folder, verified by `Message-ID`
(212 of 216 rows `sent_archive_status='appended'`). That corroborates what was sent, not
that it arrived.

**And there is a hole.** Verified read-only against ALPHA00001:

| Log | Rows | Oldest `attempted_at` | Newest |
|---|---|---|---|
| `eco_driving_weekly_email_send_log` | 3 725 | 2026-08-12 14:12 UTC | 2026-08-26 10:59 UTC |
| `eco_driving_monthly_email_send_log` | 1 255 | 2026-08-10 22:09 UTC | 2026-08-10 22:09 UTC |

But `public.runs` records ALPHA driver weekly sends from **2026-07-21** and `public.logs`
holds the completion summary of run `1fa5f596-7df5-4770-80ff-cb136cbff633`
(2026-08-07, monthly, `normal_send`): `sent_count=1137`, `failed_count=105`,
`skipped_missing_email=10`, `smtp_accepted_count=1137`, `smtp_failed_count=1`.
The monthly log's only surviving content is the render-only rehearsal of 2026-08-10.

**So for ALPHA periods before 2026-08-10, the per-recipient question cannot be answered at
all — only the batch counts survive, in `public.logs`.** The repository contains no
record of a deliberate send-log reset, and the client runtime role holds only
`SELECT, INSERT, UPDATE` (migration `033`), so the job itself cannot have deleted them.
The cause is **not established by repository evidence** and is recorded here as an open
item, not as an accusation. Its consequence for this audit is concrete: *recipient-level
historical reconciliation for ALPHA is currently possible only from 2026-08-10 onward.*

A second, structural limit: `public.logs` is the only place the earlier batch counters
live, and it is subject to platform retention. Batch-level history is therefore not
durable indefinitely either.

---

## 5. SMTP state model

Submission and delivery are different questions, and the boundary is the relay.

```
        HOST                              example.invalid relay                    destination MX            mailbox
          |                                    |                              |                       |
  reserve (committed 'pending')                |                              |                       |
          |---- TCP + STARTTLS + AUTH -------->|                              |                       |
          |---- MAIL FROM / RCPT TO ---------->|                              |                       |
          |---- DATA + body + "." ------------>|                              |                       |
          |<--- 250 2.0.0 Ok: queued as XXXX --|                              |                       |
  mark 'sent'                                  |---- relay forwards --------->|                       |
          |                                    |<--- 250 / 4xx / 5xx ---------|---- filter/deliver -->|
          |                    (a 5xx here becomes a bounce message, sent back to MAIL FROM)          |
          |<==== NOBODY IS LISTENING FOR THAT BOUNCE TODAY ======================================     |
```

- **Everything left of the `250` is observable and is observed.**
- **The `250` itself is observed but its text is dropped.**
- **Everything right of the `250` is invisible** — except that a failure there generates
  a bounce addressed to the envelope sender, and no process reads that mailbox.

Envelope-sender control: the driver path uses `smtp.send_message(msg)`, so the envelope
sender is derived from the `From:` header (`no-reply.eco-alpha@example.invalid` by default).
The person path passes it explicitly (`prepared.envelope_sender = settings.from_email`,
`email_delivery.py:242`). **Neither sets a distinct return-path, and neither uses VERP**,
so a bounce could be correlated only by parsing the returned original headers, not by the
envelope address. Nothing anywhere sends `NOTIFY=`, `RET=` or `ORCPT=`.

---

## 6. Failure and ambiguity matrix

`P` = definite pre-submission phase (nothing written). `A` = ambiguous. Automatic retry
here means "a subsequent ordinary run of the same period will submit again", since the
identity index only reserves `pending`/`sent`.

| # | Outcome | Classification | Row state after | Retry on next run | Duplication risk | Auto-retry safe? |
|---|---|---|---|---|---|---|
| 1 | connection refused / DNS / TCP timeout | `DEFINITE_NOT_SUBMITTED` (`PHASE_CONNECT`) | `failed`, `error_message` | **yes, automatic** | none | **yes** |
| 2 | STARTTLS failure | `DEFINITE_NOT_SUBMITTED` (`PHASE_STARTTLS`) | `failed` | yes, automatic | none | yes |
| 3 | authentication failure | `DEFINITE_NOT_SUBMITTED` (`PHASE_LOGIN`) | `failed` | yes, automatic | none | yes |
| 4 | EHLO/greeting refused (`SMTPHeloError`) | `DEFINITE_NOT_SUBMITTED` | `failed` | yes, automatic | none | yes |
| 5 | `MAIL FROM` rejected (`SMTPSenderRefused`) | `DEFINITE_NOT_SUBMITTED` | `failed` | yes, automatic | none | yes |
| 6 | **all** recipients rejected (`SMTPRecipientsRefused`) | `DEFINITE_NOT_SUBMITTED` | `failed` | yes, automatic | none | yes — but this is a *hard address failure* that will recur every period, and nothing marks the address bad |
| 7 | **some** recipients rejected | one recipient per message ⇒ unreachable in the driver path. Person path raises a plain `RuntimeError` ⇒ **`AMBIGUOUS`** (`email_delivery.py:264-272`) | `pending` + ambiguous marker | **never** | would be a duplicate | **no — correctly refused** |
| 8 | connection dies before `DATA` is answered 354 → `SMTPDataError(4xx/5xx)` | `DEFINITE_NOT_SUBMITTED` (code is evidence) | `failed` | yes, automatic | none | yes |
| 9 | connection dies **during** body transmission | `AMBIGUOUS_SUBMISSION` | `pending` + marker, `sent_at` stays NULL | **never** | high if retried | **no — correctly refused** |
| 10 | timeout/disconnect **after** the dot, before the final reply is read | `AMBIGUOUS_SUBMISSION` — the core case | `pending` + marker | **never** | high | **no — correctly refused** |
| 11 | garbled final reply → `SMTPDataError(-1, ...)` | `AMBIGUOUS_SUBMISSION` (code is not evidence) | `pending` + marker | **never** | high | **no — correctly refused** |
| 12 | explicit `4xx` to the terminating dot | `DEFINITE_NOT_SUBMITTED` | `failed` | yes, automatic | none | yes (RFC 5321 transient refusal of the whole message) |
| 13 | explicit `5xx` to the terminating dot | `DEFINITE_NOT_SUBMITTED` | `failed` | yes, automatic | none | yes — but a permanent refusal will recur every period, unremarked |
| 14 | **final `250`** | success | `sent`, `sent_at`, `smtp_message_id` | never (identity index) | none | n/a |
| 15 | process crash **before** submission | — | `pending`, no marker | **no** — blocks; `existing` inside 120 min, then `STALE_PENDING_REQUIRES_RECONCILIATION` | none | n/a, but see below |
| 16 | process crash **after** the relay accepted, before `mark_send_sent` commits | — | `pending`, **no ambiguous marker** | **no** — same stale-pending block | none while blocked | n/a, but see below |
| 17 | DB error while writing the outcome | `classify_exception` ⇒ `AMBIGUOUS` (the message may already be gone) | attempted marker; if that write also fails, the row stays `pending` | never | none while blocked | **no** |
| 18 | dashboard link unavailable | product decision | `failed`, `send_scope='skipped'`, `dashboard_link_blocked_count` | yes, automatic | none — SMTP was never opened | yes |
| 19 | `INVALID_RANKING_SNAPSHOT` | product decision | `failed` | yes, automatic | none — SMTP was never opened | yes |
| 20 | IMAP Sent-archive failure after acceptance (BRAVO weekly) | archive-only | `status` stays `sent`; `sent_archive_status='failed'` | archive-only rerun; **never** resubmits SMTP | none | yes (archive only) |

### 6.1 The ambiguous-send window, precisely

Rows 9–11 and 17 are the window. The contract implemented in
`jobs/common/eco_email_reconciliation.py` handles them as follows, and the design choice
is worth restating because it is unusual and correct:

**The row does not move.** It stays `pending` — which is already what the partial unique
indexes reserve and what blocks a new reservation — and gains
`metadata_json.smtp_submission_result = 'AMBIGUOUS'` plus phase, detail and
`requires_operator_reconciliation`. It is *not* downgraded to `failed`, because `failed`
means "safe to send again", which is precisely what the host cannot claim. `sent_at`
stays NULL because nothing was established.

**Nothing automatic can clear it.** The gate is applied twice per candidate:
a cheap pre-flight `unresolved_ambiguous_send(...)` *before* any remote effect —
before the dashboard snapshot is published or a capability is rotated
(`job_...weekly...py:1164-1210`) — and again authoritatively inside `reserve_send`,
which is what makes it correct under concurrency. `force_resend` is gated too:
`ExecutionContract.blocks_on_unresolved_ambiguous_send` returns True for both
`NORMAL_SEND` and `FORCE_RESEND`, on the stated ground that force exists to override an
established `sent` (a fact), never the absence of a fact. `test_send` is the one
exemption and is bounded by construction — it requires an explicit
`test_recipient_email` and runs under `send_scope='test'`, so it cannot reach the
customer.

**Only a human ends it.** `ops/reconcile_eco_email_ambiguous_send.py --resolve <id>
--as delivered|not-delivered --operator <who> --reason <why>`; `delivered` marks the row
`sent` (never resent), `not-delivered` marks it `failed` (retryable, mailed exactly once
next run). Both attestations are stored. The `UPDATE` is guarded by the same
`UNRESOLVED_AMBIGUOUS_PREDICATE`, so resolving a row that was never ambiguous is a no-op
returning `False` — a state edit, not a reconciliation, and refused as such.

**Verdict on the constraint:** `SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO` **is
correctly enforced today**, in application code, in SQL predicates, and in tests. No code
path in the four mailers can automatically resubmit an ambiguous message.

Deterministic evidence, executed for this audit:

```
$ PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_eco_email_ambiguous_smtp_safety.py
### TIER A — no infrastructure
PASS  test_only_protocol_evidence_makes_a_failure_retryable
PASS  test_an_smtp_data_error_is_read_from_its_reply_code_not_its_type
PASS  test_a_garbled_final_reply_travels_the_ambiguous_path_end_to_end
PASS  test_the_session_says_where_a_failure_happened_and_what_it_proves
PASS  test_all_four_mailers_share_one_safety_contract
PASS  test_the_eligibility_check_precedes_the_dashboard_capability
PASS  test_both_production_modes_share_one_ambiguity_gate
PASS  test_an_unresolved_ambiguity_stops_both_production_modes_before_the_dashboard
PASS  test_the_gate_blocks_only_what_it_must
PASS  test_a_forced_resend_still_overrides_an_established_sent
PASS  test_the_bravo_mime_and_archive_path_is_unchanged
### TIER B — the REAL eco send logs on a disposable PostgreSQL
SKIPPED: export ECO_EMAIL_AMBIGUITY_TEST_DSN ...
11 checks passed — Eco e-mail ambiguous-SMTP safety
```

Tier B — the durable half, against real send-log DDL on a disposable PostgreSQL — is
**skipped without a DSN and is therefore not yet evidence**. Running it (loopback,
disposable) is a cheap and worthwhile closure step; it needs no production access.

### 6.2 Two gaps in the ambiguity contract

1. **Rows 15/16 — the crash-shaped stale `pending` has no operator exit.**
   `reserve_send` correctly refuses to reclaim it and returns
   `STALE_PENDING_REQUIRES_RECONCILIATION`, so it never duplicates. But
   `reconcile_eco_email_ambiguous_send.py` can only act on rows carrying the *ambiguous
   marker*, and a crash between the relay's `250` and the outcome commit leaves no such
   marker. Such a row is durably blocked with **no tooling path out**; the only exit is
   a hand-written `UPDATE`, which is exactly the kind of unattested state edit the rest
   of the design refuses. Row 16 is also the one case where the customer *has* the mail
   and the ledger will never say so.
2. **Rows 6 and 13 — a permanently bad address is invisible.** A `5xx` refusal is
   correctly recorded as `failed` and correctly retried next period, because for *this*
   message the retry is safe. But nothing accumulates the fact that the address is dead,
   so a hard-failing recipient is re-attempted every week forever with no signal.

---

## 7. Weekly / monthly reconciliation assessment

### 7.1 Batch success semantics

`ops/runner.py` wraps every job in `api/client.py:297 run_context`, which marks the run
`SUCCESS` iff the body did not raise. The mailers do not raise on per-recipient failure
unless `fail_fast=true` (default **false**). Therefore:

- **one failed customer does not fail the batch** — correct behaviour for a fleet mailer;
- **but a batch with terminal per-recipient failures is still recorded `SUCCESS`**, with
  nothing in the run status distinguishing "1 130 sent, 0 problems" from "1 130 sent,
  89 failed, 17 with no address".

This is `P0-6` in `docs/17_production_hardening_roadmap.md` §5.2, still open. Confirmed
unchanged by runtime evidence: run `2f37ac23` (1 137 sent / 105 failed / 10 skipped /
1 SMTP failure) is `status = SUCCESS` in `public.runs`; every one of the 11 ALPHA weekly
and 3 ALPHA monthly runs is `SUCCESS`, including those with 64–89 failures each.

One success does not *mask* a failure in the data — every outcome gets its own row and
its own counter — but it does mask it in the **run status**, which is what a scheduler,
a watchdog or an operator dashboard reads first.

### 7.2 Counters actually recorded

The summary dict, logged at INFO on completion and returned by `run`, carries:
`candidates_count`, `source_recipient_count`, `rendered_count`, `would_send_count`,
`sent_count`, `failed_count`, `failed_before_smtp_count`, `skipped_missing_email`,
`skipped_unknown_rating_type`, `skipped_already_sent`, `invalid_ranking_snapshot_count`,
`dashboard_link_blocked_count`, `smtp_attempt_count`, `smtp_accepted_count`,
`smtp_failed_count`, `smtp_ambiguous_count`, `ambiguous_reconciliation_blocked_count`,
`stale_pending_count`, `idempotency_blocked_count`, `sent_archive_*`, plus the full
`period_selection` diagnostics including every rejected period candidate.

Against the audit's checklist: intended (`candidates_count`), attempted
(`smtp_attempt_count`), SMTP-accepted (`smtp_accepted_count`), explicit failures
(`smtp_failed_count`, `failed_before_smtp_count`), ambiguous (`smtp_ambiguous_count`),
unresolved/blocked (`ambiguous_reconciliation_blocked_count`, `stale_pending_count`) —
**all six are already counted.** "Later bounced" is the only one with no counter, because
there is no producer for it.

Skipped is fully distinguishable from successful: distinct `status` values, distinct
counters, and `sent_at` non-NULL only for `sent` (CHECK-enforced).

### 7.3 Duplicate sends across retries and restarts

Structurally prevented, at three layers:

1. `_already_sent(...)` pre-check → `skipped_already_sent`;
2. `reserve_send` refuses to reserve over an existing `normal` `pending`/`sent` row;
3. the partial unique indexes make a duplicate reservation a PostgreSQL error, not an
   application race.

Verified in production data: across all four logs and every period, **the count of
`normal`-scope `sent` rows per (client, subject, period) is at most one** — the identity
index guarantees it, and the migration's conflict gate proved it held before the index
was created.

Duplicates remain possible only by explicit human action (`force_resend`, which requires
a reason and is refused over an unresolved ambiguity), which is the intended escape hatch.

### 7.4 Can a run be "successful" with unresolved deliveries?

**Yes.** `smtp_ambiguous_count > 0`, `stale_pending_count > 0` and
`ambiguous_reconciliation_blocked_count > 0` all leave the run `SUCCESS`. Each does emit
an ERROR-level log line with `operator_action_required: true`, so the evidence is there —
but it is in the log stream, not in the run's terminal status. Today the population of
unresolved rows is **zero** across all four production logs (verified), so this is a
latent rather than an active problem.

### 7.5 Runtime evidence summary (read-only, aggregate only)

| Client / log | Period | scope | status | rows | with `Message-ID` | ambiguous |
|---|---|---|---|---|---|---|
| ALPHA weekly | 2026-08-01→08-10 | normal | sent | 1 020 | 1 020 | 0 |
| | | normal | failed | 64 | 0 | 0 |
| | | normal | skipped_missing_email | 20 | 0 | 0 |
| | 2026-08-01→08-17 | normal | sent / failed / skipped | 1 091 / 76 / 18 | 1 091 | 0 |
| | 2026-08-01→08-24 | normal | sent / failed / skipped | 1 130 / 89 / 17 | 1 130 | 0 |
| ALPHA monthly | 2026-07-01→08-01 | render_only | rendered / failed / skipped | 1 141 / 104 / 10 | 0 | 0 |
| BRAVO person weekly | 2026-08-01→08-10 | normal | sent | 33 | 33 | 0 |
| | 2026-08-01→08-17 | normal | sent | 35 | 35 | 0 |
| | 2026-08-01→08-24 | normal | sent | 36 | 36 | 0 |
| BRAVO person monthly | 2026-07-01→08-01 | normal | sent | 39 | 39 | 0 |

All 229 ALPHA `failed` rows carry `error_message = 'INVALID_RANKING_SNAPSHOT'` — a
pre-SMTP product refusal, not a transport failure. **No SMTP-transport failure and no
ambiguous row exists in any current send log.** BRAVO weekly: 212 of 216 rows archived
to Sent (`appended`), 212 with preserved MIME; BRAVO monthly: 0 archived, 0 MIME.

---

## 8. Bounce / DSN / post-submission feedback

### 8.1 What exists in the repository

| Capability | Present? | Evidence |
|---|---|---|
| DSN request (`NOTIFY=`, `RET=`, `ORCPT=`, RFC 3461) | **No** | no occurrence anywhere in the tree |
| bounce / NDR parsing | **No** | no occurrence |
| dedicated bounce mailbox | **No** | no such configuration key |
| return-path / envelope-sender override | **No** | envelope sender is the `From` address; no VERP |
| IMAP/POP mailbox processing | **Yes, but only two unrelated uses** | (a) Workflow B Stage 1 `jobs/mail/fetch_reports.py` reads an inbound *report* mailbox (`IMAP_*`); (b) `jobs/ecodriving_person/email_delivery.py` `APPEND`s to `\Sent` and `SEARCH`es it by `Message-ID` — **it never selects `INBOX`** |
| mail-server log / API / control-panel integration | **No** | none |
| delayed-delivery or permanent-failure handling | **No** | none |

`docs/17_production_hardening_roadmap.md` §5.3 states the same conclusion independently
and classifies it as an open P1: *"No code anywhere reads a mailbox for delivery-status
notifications. `BRAVO_ECO_WEEKLY_EMAIL_IMAP_*` is used only to APPEND sent MIME to the Sent
folder; it never opens INBOX. `sent` therefore means 'accepted for relay', the lifecycle
ends there, and a hard-bouncing address keeps receiving every future period."*
§6 of the same document adds: *"bounce/DSN ingestion is obtainable and is entirely
absent."* **This audit confirms both statements against current code.**

### 8.2 What is genuinely favourable

The BRAVO weekly sender already has **working, credentialled IMAP access to the same
mailbox it sends from** (`BRAVO_ECO_WEEKLY_EMAIL_IMAP_HOST/PORT/USERNAME/PASSWORD/USE_SSL`,
all present in the host `.env`), and the codebase already contains a tested IMAP client
that logs in, lists mailboxes, discovers a special-use folder, selects a mailbox
read-only and searches by `HEADER Message-ID`. Reading `INBOX` for bounces is therefore
**a smaller change than the Sent-archive feature that already shipped** — it reuses the
same settings loader, the same connection helper and the same correlation key.

The ALPHA sender (`no-reply.eco-alpha@example.invalid`) has **no IMAP credentials configured**.
Bounces for the 1 100-recipient weekly fleet are currently going to a mailbox nothing
reads. Whether that mailbox even exists and is retained is an owner/provider question.

### 8.3 External capability requiring verification

Classified **`REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION`**. To be checked with the SMTP
administrator or by one read-only probe (no message sent):

1. **Does the submission service advertise `DSN` in its EHLO response?** Determines
   whether RFC 3461 `NOTIFY=SUCCESS,FAILURE,DELAY` / `ORCPT=` are usable at all.
   *Read-only experiment:* connect, `STARTTLS`, `EHLO`, authenticate, read
   `smtp.esmtp_features`, `QUIT`. **No `MAIL FROM`, no `RCPT TO`, no `DATA`, no message.**
   Also record whether `SMTPUTF8`, `PIPELINING`, `SIZE` and `8BITMIME` are advertised.
2. **What exactly does the `250` to the terminating dot look like?** Specifically whether
   it contains a queue identifier (Postfix-style `Ok: queued as ABC123`, Exim-style
   `id=1a2b3c-...`). *This cannot be established without transmitting a message*, so the
   experiment is deferred: §11 Option 1 captures whatever the line says, and its shape
   becomes known from the first real production send after the change — no test message
   to a customer is required.
3. **Does example.invalid generate RFC 3464 DSNs for downstream failures, and to which address?**
   Envelope sender, `From`, or a configured bounce address.
4. **Does example.invalid expose delivery logs, a queue view or a control-panel/API report,** and
   with what retention? This is the difference between "we can ask about a message
   later" and "the queue ID is only useful in a support ticket".
5. **Does the sending account's mailbox actually receive and retain NDRs,** and is there a
   mailbox at all behind `no-reply.eco-alpha@example.invalid`?
6. **Are there submission rate/volume limits** relevant to the 1 100-message weekly ALPHA
   batch (the last run spanned 05:31–10:59 UTC), and does the relay defer under load —
   which would surface as row 12 (`4xx`) and be silently retried a week later.

Nothing in the repository answers 1–6. Do not assume any of them.

---

## 9. Delivery-verification capability matrix

Legend: `AVAILABLE_NOW` · `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE` (application change
only) · `REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION` · `REQUIRES_ADDITIONAL_INFRASTRUCTURE` ·
`NOT_RELIABLY_PROVABLE`.

| State | Today | Classification | Confidence once implemented | Principal edge cases |
|---|---|---|---|---|
| `NOT_ATTEMPTED` | **yes** — candidate with no row, or `skipped_*` | `AVAILABLE_NOW` | high | a candidate the query never returned leaves no trace at all; only `candidates_count` bounds it |
| `ATTEMPT_STARTED` | **yes** — committed `pending` before the socket opens | `AVAILABLE_NOW` | high | none material; the reservation is committed, so it survives a crash |
| `SMTP_REJECTED` | **yes** — `failed` + `DEFINITE_NOT_SUBMITTED` | `AVAILABLE_NOW` | high | the reason is free text, so counting reasons requires parsing; a `4xx` and a `5xx` are indistinguishable without reading `error_message` |
| `SMTP_ACCEPTED` | **yes** — `sent` | `AVAILABLE_NOW` | high for the claim, **low for re-verifiability**: the relay's own token is not kept | upgrading to "provably the same message example.invalid logged" needs the queue ID |
| `SMTP_ACCEPTED` **+ relay queue ID** | no | `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE` (capture) / `REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION` (what the ID is worth) | high | the `250` text is free-form; parse defensively and store the whole line verbatim |
| `SEND_OUTCOME_AMBIGUOUS` | **yes** — frozen `pending` + marker + operator tool | `AVAILABLE_NOW` | high | the crash-shaped variant (§6.2.1) is blocked but not *marked*, and has no tooling exit |
| `BOUNCE_PENDING` | no | `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE` | medium — it is a timeout, not an observation | choosing the window; a delayed-delivery DSN can precede success by days |
| `BOUNCED_TEMPORARY` (4.x.x DSN / delay) | no | `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE` for BRAVO; **+ mailbox decision** for ALPHA | medium-high | a delay notice is not a failure; must not be allowed to imply non-delivery |
| `BOUNCED_PERMANENT` (5.x.x DSN / NDR) | no | as above | high **when a DSN arrives**; silent drops produce false negatives | non-RFC-3464 NDRs need heuristic parsing; some receivers never bounce at all |
| `DELIVERED` (destination MX accepted) | no | `REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION` — only if example.invalid exposes relay logs or honours `NOTIFY=SUCCESS` | medium at best; success DSNs are widely suppressed | acceptance by the MX still is not mailbox placement |
| `DELIVERED` (in the mailbox / Inbox vs Junk) | no | **`NOT_RELIABLY_PROVABLE`** over ordinary SMTP | — | spam foldering is invisible by design |
| `OPENED` | no | `REQUIRES_ADDITIONAL_INFRASTRUCTURE` (pixel/link instrumentation, hosting, and a privacy decision) | **low as evidence** — image blocking, prefetch and privacy proxies produce both false negatives and false positives | the existing dashboard-link infrastructure could yield a *click* signal for BRAVO at near-zero cost; a click proves reading, its absence proves nothing |

Against the audit's lettered questions:

- **A — our application attempted the send: `AVAILABLE_NOW`, high confidence.**
- **B — example.invalid accepted the message: `AVAILABLE_NOW`, high confidence** as an inference
  from `250`; `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE` to make it independently
  re-verifiable by capturing the queue ID.
- **C — immediate SMTP rejection: `AVAILABLE_NOW`**, with a typed-classification
  improvement available application-side.
- **D — later hard/soft bounce: `IMPLEMENTABLE_WITH_CURRENT_INFRASTRUCTURE`** for
  BRAVO00016 (credentials already exist); requires a mailbox decision for ALPHA00001;
  RFC 3461-based *requesting* of DSNs is `REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION`.
- **E — destination mail server accepted: `REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION`**,
  and realistically only as *absence of a bounce*, which is weaker than proof.
- **F — reached the mailbox/inbox: `NOT_RELIABLY_PROVABLE`.**
- **G — recipient opened/read: `REQUIRES_ADDITIONAL_INFRASTRUCTURE`, and inherently
  low-confidence as evidence.**

---

## 10. Identifiers and the correlation chain

### 10.1 What exists

```
public.runs.run_id
   └─ eco_*_email_send_log.run_id                          (TEXT, no FK — platform DB vs client DB)
        ├─ send_log_id                (UUID PK)            THE per-attempt identity
        ├─ idempotency_key            "eco_driver|weekly|<client>|assigned_sha256:<hex>|<start>|<end>"
        ├─ (client_id, <subject>, report_type, period_*)   THE logical delivery identity
        ├─ parent_send_log_id         forced resend → the row it overrides
        ├─ smtp_message_id            RFC 5322 Message-ID, host-generated
        ├─ metadata_json.smtp_submission_{result,phase,detail,ambiguous_at}
        ├─ [BRAVO weekly] sent_mime_sha256, sent_archive_message_id
        └─ [dashboard sends] eco_dashboard_delivery_operation, keyed by
             (client_id, identity_key, period_type, period_*, send_scope)
```

### 10.2 What is missing

| Link | Status |
|---|---|
| `report run → customer → recipient → message` | **complete** |
| `message → SMTP transaction` | **partial** — the host's `Message-ID` is stored, but the relay's own transaction identity (queue ID) is not, so the two sides cannot be joined in example.invalid's logs |
| `SMTP transaction → later bounce/DSN` | **absent** — no bounce exists to link; when one does, `Message-ID` inside the returned original headers is the natural join key and it is **already durably stored** |
| `send_log.run_id → public.runs.run_id` | present as a value, unenforced across databases; sufficient for reconciliation, not referentially guaranteed |
| `eco_dashboard_delivery_operation ↔ send log` | joinable only by `(client_id, identity_key/subject, period, scope)`; there is no `send_log_id` on the ledger and no `delivery_id` on the send log |

**The good news for §11: `smtp_message_id` is already present on 100 % of `sent` rows in
every production send log.** Bounce correlation needs no new identifier and no schema
change on the send side — only somewhere to put the result.

### 10.3 Introducing observability without weakening the retry constraint

The constraint is about **what may cause a second submission**, not about what may be
recorded. Every option in §11 is therefore compatible with it, provided three rules hold:

1. **No new state may make an ambiguous row reservable.** The predicate
   `send_scope='normal' AND status='pending' AND metadata_json->>'smtp_submission_result'='AMBIGUOUS'`
   is the single authority; it composes with any table and must keep composing.
2. **A bounce is evidence about the past, never an instruction to resend.** A
   `BOUNCED_PERMANENT` row must be terminal-and-informational. Auto-resending on a soft
   bounce would reintroduce exactly the duplicate this contract prevents — with the
   aggravating factor that the recipient *did* get the first copy in the delay case.
3. **Post-submission state belongs in new columns or a new table, never by overloading
   `status`.** `status` is what the partial unique indexes read. Widening its vocabulary
   would silently change what reserves an identity slot.

Restated as a retry policy — no change proposed, current behaviour only:

| Safe to retry automatically | Operator action required |
|---|---|
| pre-SMTP product refusals (`INVALID_RANKING_SNAPSHOT`, dashboard-link blocked, missing address) | any `AMBIGUOUS_SUBMISSION` (rows 9, 10, 11, 17) |
| `DEFINITE_NOT_SUBMITTED` at connect / STARTTLS / login / EHLO / `MAIL FROM` / all-recipients-refused | stale `pending` past the window (rows 15, 16) |
| explicit `4xx`/`5xx` to `DATA` or to the terminating dot | anything the operator has attested |
| IMAP Sent-archive failure — archive step only, never SMTP | a `5xx` that recurs every period (address is dead, not transiently broken) |

---

## 11. Options for the owner

Ordered least → most invasive. **None of these is implemented.** Each is scoped so it can
be adopted independently.

### Option 0 — Close the gaps that need no new capability at all
*Prerequisite housekeeping rather than an architecture choice.*

- **Proves:** nothing new by itself; it makes the existing proofs trustworthy.
- **Still does not prove:** anything downstream of the relay.
- **Application changes:** run the Tier-B half of `test_eco_email_ambiguous_smtp_safety.py`
  against a disposable loopback PostgreSQL, so the durable half of the contract stops
  being untested; extend `ops/reconcile_eco_email_ambiguous_send.py` to also list and
  resolve **stale `pending`** rows (§6.2.1), which today have no tooling exit; establish
  and record what happened to the pre-2026-08-10 ALPHA send-log rows (§4.4); decide
  whether migration `044` should be applied to FOXTROT00001 / DELTA00001 / ECHO00001 before
  they ever send (§4.2).
- **Provider capability:** none.
- **Duplicate-send implications:** strictly reduces risk. The stale-pending tool must
  take the same attestation as the ambiguous one — it is the same class of decision.
- **Complexity:** low. **Risks:** the migration item is a production DDL change and needs
  the usual conflict pre-flight; everything else is local.

### Option 1 — Capture what the relay already tells us
*Smallest change with a genuine increase in provable state.*

- **Proves:** that example.invalid accepted the message **and what it called it** — the final `250`
  line, including a queue identifier if the relay emits one. Turns "we believe it was
  accepted" into "here is the relay's own receipt, quote it in a support ticket".
- **Still does not prove:** anything about the destination MX or the mailbox.
- **Application changes:** in `run_smtp_session`, replace the single `sendmail` /
  `send_message` call with the explicit `mail()` / `rcpt()` / `data()` sequence and return
  the `(code, resp)` from `data()`. Persist `resp` verbatim into `provider_response`
  (which today holds a constant literal, so no new column is strictly required) and, if a
  queue ID can be parsed, into a new nullable column. Also worth doing here: record the
  per-recipient refusal map from `rcpt()`, and replace the free-text failure reason with a
  typed classification column.
- **Provider capability:** none to *capture*. Confirming what the queue ID is worth is
  §8.3 items 2 and 4.
- **Duplicate-send implications:** **must be done carefully.** Decomposing `sendmail`
  means reimplementing its error handling; if any branch stops raising, an ambiguous
  outcome could be misread as definite. The classification module is the safety net and
  its default is already `AMBIGUOUS`, but the change belongs in main-session work with
  the existing safety suite extended to cover the new call sequence.
- **Complexity:** low-medium, one module. **Risks:** as above — this is the one option
  that touches the remote-effect boundary itself.

### Option 2 — Ingest bounces from the mailbox we already own
*The highest-value option, and the one the roadmap already calls obtainable.*

- **Proves:** **D** — that a message which was accepted for relay later failed
  downstream, distinguishing permanent (5.x.x) from transient/delayed (4.x.x), per
  recipient, per period, correlated by the `Message-ID` already stored on every `sent`
  row. Also yields, as a by-product, a durable list of dead addresses.
- **Still does not prove:** positive delivery. Absence of a bounce is weak evidence — a
  receiver may drop silently, and some NDRs never arrive. `DELIVERED` must not be
  inferred from silence; the honest resulting state is `NO_BOUNCE_OBSERVED`.
- **Application changes:** a new read-only mailbox worker (not a mailer) that selects
  `INBOX`, finds `multipart/report; report-type=delivery-status` parts plus common
  non-RFC NDR shapes, extracts the original `Message-ID` and the status code, and writes
  a **new post-submission table** keyed by `smtp_message_id` — never touching `status`
  (§10.3 rule 3). Reuse `load_sent_archive_settings_from_env` and the existing IMAP
  helpers wholesale. Idempotency by IMAP UID plus `Message-ID`.
- **Mail-server capability:** for **BRAVO00016, none** — credentials for the sending
  mailbox already exist and already work. For **ALPHA00001**, an owner/provider decision:
  either give `no-reply.eco-alpha@example.invalid` a real, IMAP-reachable mailbox, or set a
  distinct return-path pointing at one that already is. §8.3 items 3 and 5 must be
  answered first.
- **Duplicate-send implications:** **none, if and only if the worker never writes
  `status` and never resends.** It is an observer. A soft bounce must not trigger a
  resend — the customer may well have received the message after the delay.
- **Complexity:** medium — NDR parsing is genuinely messy in the tail; budget for
  "unparsed, quarantined for an operator" as a first-class outcome.
- **Risks:** mis-parsing a delay notice as a permanent failure; the mailbox filling up;
  correlating a bounce to the wrong period if a recipient receives several messages —
  mitigated because `Message-ID` is unique per attempt.

### Option 3 — Request DSNs explicitly (RFC 3461)
*Only after §8.3 item 1 is answered.*

- **Proves:** with `NOTIFY=FAILURE,DELAY` the bounce signal becomes reliable rather than
  best-effort, and `ORCPT=` makes correlation exact instead of header-parsed. With
  `NOTIFY=SUCCESS` — **if** example.invalid and the downstream chain honour it, which most do not —
  something approaching **E**.
- **Still does not prove:** **F** or **G**. Success DSNs are widely suppressed, so
  planning on them would be planning on a courtesy.
- **Application changes:** pass `mail_options=['NOTIFY=FAILURE,DELAY', 'RET=HDRS']` and
  `rcpt_options=['ORCPT=rfc822;<addr>']`. Small — but only legal if the server advertises
  `DSN`; `smtplib` raises `SMTPNotSupportedError` otherwise, which is classified
  `DEFINITE_NOT_SUBMITTED`, so a wrong assumption fails closed and safe.
- **Provider capability:** **`REQUIRES_EXAMPLE.INVALID_CAPABILITY_CONFIRMATION`.**
- **Duplicate-send implications:** none — it changes the envelope, not the retry logic.
- **Complexity:** low once confirmed. **Risks:** `RET=FULL` would mail the whole customer
  message back on failure, which is a data-exposure question; prefer `RET=HDRS`.
- **Dependency:** worth little without Option 2, which is what reads the results.

### Option 4 — A per-recipient delivery state machine, reusing the model that already exists
*The strongest option, and the most invasive.*

- **Proves:** a single per-recipient row that carries the whole lifecycle —
  `NOT_ATTEMPTED → ATTEMPT_STARTED → SMTP_ACCEPTED(+queue id) | SMTP_REJECTED |
  SEND_OUTCOME_AMBIGUOUS → BOUNCE_PENDING → BOUNCED_{TEMPORARY,PERMANENT} |
  NO_BOUNCE_OBSERVED` — making the operator table in §12 a query rather than a
  reconstruction, and giving `A`–`D` durable, indexed, reportable state.
- **Still does not prove:** **E**, **F**, **G**. No amount of host-side modelling creates
  evidence the protocol does not return.
- **Application changes:** substantial. The design does not need inventing:
  `public.eco_dashboard_delivery_operation` (migrations `049`/`050`/`051`) is already
  exactly this shape — immutable submission identity, `PROVIDER_ACCEPTED` /
  `PROVIDER_AMBIGUOUS` / `REMOTE_DELIVERED`, `operator_action_required`, leases, a guard
  trigger that refuses retroactive edits — and is already applied on every enabled client
  database. It is currently short-circuited at `EXTERNAL_MAILER_HANDOFF` **on purpose**,
  so that the ledger's provider lifecycle cannot send a second message alongside the Eco
  mailer. Two honest routes: extend the send log with post-submission columns and a
  status-independent lifecycle column, or make the Eco mailer the ledger's provider
  adapter so one row owns the whole delivery. The second is architecturally cleaner and
  operationally riskier, because it moves the authority for "was this sent" from the
  table that currently holds it.
- **Provider capability:** none beyond Options 1–3.
- **Duplicate-send implications:** **this is where the risk concentrates.** The ledger's
  own lifecycle was deliberately made unreachable for externally-mailed rows precisely to
  prevent a second message. Any move in this direction must keep the identity indexes as
  the single authority on what may be submitted, and must not let a new lifecycle column
  become a second, competing opinion.
- **Complexity:** high — migrations, backfill decisions, reconciliation of the two
  existing models. **Risks:** the highest of any option; it should not be attempted before
  Options 1 and 2 have produced real data about what the states actually look like in
  production.

### Suggested sequencing

`0 → 1 → 2 → (3 if example.invalid confirms DSN) → 4 only if the reporting need survives 0–3.`
Options 0–2 are individually small, individually useful, and together move the system
from "we know we submitted it" to "we know we submitted it, example.invalid's own receipt says so,
and we find out when it fails" — which is most of the practical value, without touching
the transport or the retry contract.

---

## 12. Operational usefulness

### 12.1 The reconciliation table the audit asked about

| Column | Today | Source |
|---|---|---|
| Customer | **yes** | `client_id` → `workflow_a_control.client_account.client_code` |
| Period | **yes** | `period_start_date`, `period_end_date` (half-open, Europe/Warsaw) |
| Recipient | **yes** | `<subject>`, `recipient_email`, `original_recipient_email` |
| Report generated | **yes** | `status='dry_run_rendered'`, or `rendered_count`; implicit for any row past rendering |
| SMTP accepted | **yes** | `status='sent'` + `sent_at` + `smtp_message_id` |
| Bounce | **no — no producer exists** | would come from Option 2 |
| Final status | **partial** | derivable from `status` + `metadata_json`, but it stops at submission and has no post-submission dimension |

Five of seven columns are answerable **today**, with one SQL query per client, for periods
whose rows still exist. The missing two are exactly what Option 2 supplies. Note the
practical wrinkle: the query must be run **per client business database**, because the
send logs live in the client databases while runs and logs live in the platform database.

### 12.2 Batch-level reconciliation

| Question | Answerable today? |
|---|---|
| intended recipients | **yes** — `candidates_count` / `source_recipient_count` |
| attempted | **yes** — `smtp_attempt_count`, and per-row `pending` reservations |
| SMTP accepted | **yes** — `smtp_accepted_count` / rows with `status='sent'` |
| explicit failures | **yes** — `smtp_failed_count`, `failed_before_smtp_count`, and per-row `error_message` |
| ambiguous | **yes** — `smtp_ambiguous_count`, and the `UNRESOLVED_AMBIGUOUS_PREDICATE` listing |
| later bounced | **no** |
| unresolved | **yes** — `ambiguous_reconciliation_blocked_count`, `stale_pending_count`, `idempotency_blocked_count` |

Six of seven, **but with two caveats**: the counters live in `public.logs` under platform
retention rather than in a durable per-run summary table, and the run's terminal status is
`SUCCESS` regardless of how many of them are non-zero (§7.1, `P0-6`). An operator asking
"did last week's send go cleanly?" must read the log payload; the run status will say
`SUCCESS` either way.

### 12.3 Existing operator tooling

- `ops/inspect_eco_email_safety.py` — read-only, identity-attested period/conflict inspection.
- `ops/inspect_eco_mailing_schedule_contract.py` — read-only; what a scheduled fire would resolve to.
- `ops/reconcile_eco_email_ambiguous_send.py --list / --resolve` — the ambiguous-send exit.
- `ops/recover_eco_dashboard_operator_required_delivery.py` — dashboard delivery states.
- `archive_only` run mode — Sent-archive repair without any SMTP resend.

There is **no** operator command that answers "show me the delivery status of period Y for
customer X" as a table. Building one is a read-only reporting task and needs nothing that
does not already exist.

---

## 13. Findings summary

| # | Finding | Severity |
|---|---|---|
| F1 | The delivery lifecycle terminates at `sent` = "example.invalid returned `250`". Nothing downstream of the relay is observed, and no bounce/DSN ingestion exists for any of the four mailers. | **High** — this is the audit's central answer |
| F2 | The relay's final `250` text, and any queue ID in it, is read by `smtplib` and discarded. `provider_response` holds a constant literal instead. Cheapest available improvement. | **High** |
| F3 | ALPHA driver send logs hold no row older than 2026-08-10, while `public.runs`/`public.logs` show sends from 2026-07-21 including the 1 137-message monthly send of 2026-08-07. Recipient-level history for those periods is unavailable; the cause is not established by repository evidence. | **High** |
| F4 | Migration `044` (fail-closed identity, `send_scope`, reservation columns) is applied on BRAVO00016 and ALPHA00001 only. FOXTROT00001, DELTA00001 and ECHO00001 still carry the pre-`044` driver send-log shape and have no person send logs. Latent — those clients have never sent. | **Medium-High** |
| F5 | A crash between the relay's `250` and the outcome commit leaves a stale `pending` with no ambiguous marker: correctly blocked from resend, but with no operator tooling exit and no record that the customer has the mail. | **Medium** |
| F6 | A batch with terminal per-recipient failures is still recorded `SUCCESS` (`P0-6`, open). Confirmed unchanged: every ALPHA run with 64–105 failures is `SUCCESS`. | **Medium** |
| F7 | A permanently rejected address (`5xx` / all-recipients-refused) is retried every period with no accumulation and no signal. | **Medium** |
| F8 | Tier B of the ambiguous-SMTP safety suite is skipped without a DSN, so the durable half of the contract is asserted but not yet demonstrated. | **Medium** |
| F9 | Exact sent MIME bytes are preserved only for BRAVO weekly. BRAVO monthly and both ALPHA mailers keep no reproducible copy of what was sent. *(Partly closed since this audit: BRAVO monthly now preserves the bytes in `eco_person_monthly_email_send_log` as well, and all four mailers file the transmitted message in the sender's Sent folder wherever that sender's `{PREFIX}_IMAP_*` namespace is configured — which for the ALPHA mailbox it is not yet. The two ALPHA driver send logs still hold no MIME bytes.)* | **Low-Medium** |
| F10 | ALPHA driver email jobs are not registered as dispatcher datasets, so all ALPHA customer sending is manual (`P1`, open). | **Low** — by design today, but it means no batch reconciliation has a scheduled producer |
| **P1** | **Positive:** `SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO` is correctly enforced in code, SQL and passing tests, including against `force_resend`, and including before any dashboard remote effect. | — |
| **P2** | **Positive:** duplicate normal sends are prevented by PostgreSQL partial unique indexes, not merely by application logic, with template and rating deliberately excluded from identity. | — |
| **P3** | **Positive:** `smtp_message_id` is present on 100 % of `sent` rows in every production log — the join key bounce correlation would need already exists. | — |

---

## 14. Verification performed

| Check | Result |
|---|---|
| `ops/tests_manual/test_eco_email_ambiguous_smtp_safety.py` | 11 Tier-A checks **pass**; Tier B **skipped** (no DSN) |
| `diff` of weekly vs monthly driver mailers (period words normalised) | structurally identical send path; differences are templates, stats columns and env namespace |
| read-only aggregate SQL, platform DB | `runs`, `logs`, `dataset_registry`, `client_dataset_schedule`, `client_table_retention` |
| read-only aggregate SQL, 5 client business DBs | 4 send-log tables each: column presence, per-(period, scope, status) counts, `Message-ID` coverage, ambiguous count, archive status. **No PII selected** |
| static search for bounce/DSN/return-path/NOTIFY/ORCPT/NDR | no occurrences outside documentation that states their absence |
| static search for `add_attachment` in the mailers | none — no attachments are sent |
| CPython 3.12 `smtplib.sendmail` source | confirms the final `250` reply is discarded on success |

**Not performed, deliberately:** no SMTP connection of any kind, no test or customer
e-mail, no IMAP connection, no write to any database, no migration, no schedule or timer
change, no service restart, no credential access beyond reading configuration key
*names*, no commit, no push.
