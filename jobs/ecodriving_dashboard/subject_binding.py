"""Host-side subject/object binding digest — `driver_eco_dashboard.subject_binding.v1`.

Independent implementation of the specification in
`delivery/driver_eco_dashboard/spec/subject_binding_v1.md`. It is written from
the specification rather than ported from the Worker, so the cross-language test
compares two genuinely separate implementations against one fixed vector file.

The digest proves that a published R2 object belongs to a specific subject under
a specific object key. The Worker recomputes it from the authorization grant and
refuses the object when they disagree.

Nothing here reaches a browser: neither input, nor the material, nor the digest.
"""

from __future__ import annotations

import hashlib
import hmac

SUBJECT_BINDING_DOMAIN = "driver_eco_dashboard.subject_binding.v1"
UNIT_SEPARATOR = "\u001F"


class SubjectBindingError(ValueError):
    """Raised when an input cannot be encoded unambiguously."""


def subject_binding_material(subject_ref: str, object_key: str) -> str:
    """Assemble the canonical, length-prefixed binding material.

    Length prefixes are what make the encoding unambiguous: a separator inside
    a value cannot imitate a different (subject, key) pair, because each field's
    UTF-8 byte length is itself part of the material.

    Inputs are used verbatim — no case folding, no trimming, no Unicode
    normalisation. Normalising here would let two distinct stored references
    collide; see the specification §4.
    """
    subject = str(subject_ref)
    key = str(object_key)
    if UNIT_SEPARATOR in subject or UNIT_SEPARATOR in key:
        raise SubjectBindingError("SUBJECT_BINDING_INVALID_INPUT")

    subject_bytes = len(subject.encode("utf-8"))
    key_bytes = len(key.encode("utf-8"))
    return (
        SUBJECT_BINDING_DOMAIN + UNIT_SEPARATOR
        + str(subject_bytes) + UNIT_SEPARATOR + subject + UNIT_SEPARATOR
        + str(key_bytes) + UNIT_SEPARATOR + key
    )


def subject_binding_digest(subject_ref: str, object_key: str, pepper: str | None = None) -> str:
    """Return the lowercase hex binding digest.

    With a pepper the digest is `HMAC-SHA-256(pepper, material)`; without one it
    is `SHA-256(material)`. The two forms are different values for the same
    inputs and are never interchangeable.
    """
    material = subject_binding_material(subject_ref, object_key).encode("utf-8")
    if pepper:
        return hmac.new(pepper.encode("utf-8"), material, hashlib.sha256).hexdigest()
    return hashlib.sha256(material).hexdigest()
