"""Opaque, confidentiality-preserving row references for Database Explorer.

The Database Explorer addresses a row by its configured *technical* row
identifier — for the current target datasets, the physical ``record_id``
column. That identifier is deliberately **not** a user-visible business column
(`docs/29`), so it must never reach the browser: not in HTML, not in a data
attribute, and not in the URL.

This module turns a technical row identity into a token that the user may hold,
copy and bookmark without ever learning the underlying value, and that the
server can turn back into that value.

Construction — a direct use of established primitives only, no custom
cryptography:

* **AES-256-GCM** (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`)
  provides confidentiality *and* integrity in one primitive. The raw identifier
  is the plaintext, so the token is not a reversible encoding of it.
* **HKDF-SHA256** (`cryptography.hazmat.primitives.kdf.hkdf.HKDF`) derives the
  256-bit content key from the application's existing stable session secret with
  a fixed, purpose-separating ``info`` label, so a row token can never be
  confused with a session cookie even though both descend from one secret.
* The **binding context** — token version, dataset id, client code and the
  identifier column name — is passed as GCM *associated data*. It is
  authenticated but not stored, which is what makes a token minted for one
  dataset fail to open against another instead of merely being rejected after
  the fact.

The token is **not** authorization. It names a row; it never grants access to
one. Every caller must re-run the ordinary user/client/dataset checks before
resolving it (see `resolve_row_reference` callers in ``api/main.py``).
"""
from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# One byte of version prefix so the wire format can change without every
# existing bookmark becoming an ambiguous blob. An unknown version is rejected
# before any cryptographic work happens.
ROW_REFERENCE_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32

# Purpose separation for the derived key. Changing this string invalidates every
# issued row reference by design.
_HKDF_INFO = b"log-platform/portal/database-explorer/row-reference/v1"

# A token carries one identifier value; a generous ceiling keeps a hostile input
# from reaching the AEAD at all.
MAX_ROW_REFERENCE_CHARS = 512
MAX_ROW_IDENTITY_CHARS = 256


class RowReferenceError(Exception):
    """A row reference could not be produced or resolved.

    Deliberately carries no detail about *why*: the caller renders one generic
    state for every failure so that a tampered token, a token minted for another
    dataset and a token for a row that never existed are indistinguishable from
    outside.
    """


def _derive_key(secret: str) -> bytes:
    if not secret:
        raise RowReferenceError("row reference secret is unavailable")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_HKDF_INFO,
    ).derive(secret.encode("utf-8"))


def _binding(*, dataset_id: str, client_code: str, identifier_column: str) -> bytes:
    """The authenticated context a token is cryptographically tied to.

    NUL-separated because none of these values may contain a NUL byte, so the
    encoding is unambiguous: a dataset id ending in ``a`` with client ``b`` can
    never produce the same binding as one ending in ``a\\x00b``.
    """
    parts = (
        str(ROW_REFERENCE_VERSION),
        str(dataset_id or ""),
        str(client_code or ""),
        str(identifier_column or ""),
    )
    return "\x00".join(parts).encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def build_row_reference(
    *,
    secret: str,
    dataset_id: str,
    client_code: str,
    identifier_column: str,
    identity_value: str,
) -> str:
    """Encrypt one technical row identity into a URL-safe opaque reference.

    A fresh random nonce per call means the same row yields a different token
    every time it is rendered. That is intentional: it denies a viewer the
    ability to correlate two pages by comparing tokens, and it is why a token
    must never be used as a cache key or an equality test for row sameness.
    """
    value = "" if identity_value is None else str(identity_value)
    if not value or len(value) > MAX_ROW_IDENTITY_CHARS:
        raise RowReferenceError("row identity is not representable")
    key = _derive_key(secret)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        value.encode("utf-8"),
        _binding(
            dataset_id=dataset_id,
            client_code=client_code,
            identifier_column=identifier_column,
        ),
    )
    return _b64url_encode(bytes([ROW_REFERENCE_VERSION]) + nonce + ciphertext)


def resolve_row_reference(
    *,
    secret: str,
    token: str,
    dataset_id: str,
    client_code: str,
    identifier_column: str,
) -> str:
    """Recover the technical row identity from a reference, or raise.

    Raises `RowReferenceError` for every failure mode — malformed, truncated,
    unsupported version, tampered, or minted for a different dataset/client/
    identifier column — with no way for the caller to tell them apart.
    """
    text = "" if token is None else str(token).strip()
    if not text or len(text) > MAX_ROW_REFERENCE_CHARS:
        raise RowReferenceError("row reference is not usable")
    try:
        raw = _b64url_decode(text)
    except Exception as exc:  # noqa: BLE001 - normalized to one opaque failure
        raise RowReferenceError("row reference is not usable") from exc
    # 1 version byte + nonce + at least the GCM tag.
    if len(raw) < 1 + _NONCE_BYTES + 16:
        raise RowReferenceError("row reference is not usable")
    if raw[0] != ROW_REFERENCE_VERSION:
        raise RowReferenceError("row reference is not usable")
    key = _derive_key(secret)
    nonce = raw[1:1 + _NONCE_BYTES]
    ciphertext = raw[1 + _NONCE_BYTES:]
    try:
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            _binding(
                dataset_id=dataset_id,
                client_code=client_code,
                identifier_column=identifier_column,
            ),
        )
    except InvalidTag as exc:
        raise RowReferenceError("row reference is not usable") from exc
    except Exception as exc:  # noqa: BLE001
        raise RowReferenceError("row reference is not usable") from exc
    try:
        value = plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RowReferenceError("row reference is not usable") from exc
    if not value or len(value) > MAX_ROW_IDENTITY_CHARS:
        raise RowReferenceError("row reference is not usable")
    return value
