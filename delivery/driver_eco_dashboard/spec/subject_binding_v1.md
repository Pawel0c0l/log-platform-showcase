# `driver_eco_dashboard.subject_binding.v1`

Normative specification for the subject/object binding digest.

The publisher runs on the calculation host (Python); the delivery Worker runs on
Cloudflare (JavaScript). Both must compute **byte-identical** digests, so the
encoding is fixed here rather than left to either implementation.

Authoritative implementations:

| Side | File |
|---|---|
| Worker (JS) | `delivery/driver_eco_dashboard/worker/lib/capability.js` — `subjectBindingMaterial`, `subjectBindingDigest` |
| Host (Python) | `jobs/ecodriving_dashboard/subject_binding.py` — `subject_binding_material`, `subject_binding_digest` |

Fixed vectors: `subject_binding_v1_vectors.json` in this directory. Both sides
are asserted against that file by
`ops/tests_manual/test_driver_eco_dashboard_prepublisher.py`.

---

## 1. Purpose

The digest proves that a stored R2 object was published **for a specific
subject, under a specific object key**. The Worker recomputes it from the
authorization grant and compares it against the object's custom metadata before
parsing the body. A right-key/wrong-person mix-up, or an object copied to
another key, is therefore detected at the delivery boundary.

Neither input, nor the digest, ever reaches the browser.

## 2. Algorithm

| Property | Value |
|---|---|
| Domain label | `driver_eco_dashboard.subject_binding.v1` |
| Field separator | `U+001F` (UNIT SEPARATOR), encoded as the single byte `0x1F` |
| Length encoding | UTF-8 **byte** count, decimal, no padding, no sign |
| Text encoding | UTF-8, no BOM |
| Normalisation | **none** — see §4 |
| Digest (no pepper) | `SHA-256(material)` |
| Digest (with pepper) | `HMAC-SHA-256(key = pepper_utf8, message = material)` |
| Output | lowercase hexadecimal, 64 characters |

`CAPABILITY_PEPPER` is optional but recommended in production; when it is bound,
the HMAC form is used. The two forms are different values for the same inputs
and are never interchangeable.

## 3. Material

```
material :=
    DOMAIN                       US
    decimal(len_bytes(subject))  US
    subject                      US
    decimal(len_bytes(key))      US
    key
```

where `US` is `U+001F`, `subject` is the subject reference and `key` is the R2
object key, both UTF-8.

Worked example — `subject_ref = "subject-A"`, `object_key = "ab/AAAA.json"`:

```
driver_eco_dashboard.subject_binding.v1␟9␟subject-A␟12␟ab/AAAA.json
```

`9` is `len("subject-A")` in bytes and `12` is `len("ab/AAAA.json")` in bytes.

### Why length prefixes

Plain separator concatenation is ambiguous. Under `v1|subject|key`, the pairs

* `subject = "a"`, `key = "b|c"`
* `subject = "a|b"`, `key = "c"`

produce identical material and therefore the identical digest — one subject's
binding would validate another subject's object. Prefixing each field with its
byte length removes the ambiguity structurally: the length is part of the signed
material, so no arrangement of separator characters inside a value can imitate a
different pair. `subject_binding_v1_vectors.json` contains both of those inputs
(`separator_like_pipe`, `separator_like_pipe_alt`) with **different** expected
digests, and the test asserts that they differ.

`U+001F` is additionally rejected outright if it appears in either input
(`SUBJECT_BINDING_INVALID_INPUT`), so the separator can never be smuggled in.

## 4. Normalisation — deliberately absent

Inputs are hashed **verbatim**. There is no case folding, no trimming and no
Unicode normalisation.

Normalising here would be a defect, not a convenience: two distinct stored
subject references that differ only by composition (`é` as U+00E9 versus
`e` + U+0301) would produce the same binding, letting one subject's object
validate for another. The vectors include both forms
(`precomposed_sequence`, `combining_sequence`) with different digests.

**Publisher obligation:** supply the subject reference exactly as it is stored
in the authorization record. If the host ever normalises, it must do so *before*
the value is persisted, never at binding time.

## 5. Field contracts

| Field | Contract |
|---|---|
| `subject_ref` | opaque, publisher-chosen, never a driver identity, never sent to a browser. May be empty in the vectors; a real grant always has one. |
| `object_key` | the exact R2 key, `<hex-shard>/<32 base64url>.json`, as written and as stored on the grant. |

## 6. Vector file

`subject_binding_v1_vectors.json` carries, per vector:

| Field | Meaning |
|---|---|
| `name` | stable identifier |
| `subject_ref`, `object_key` | inputs |
| `material_utf8_bytes` | byte length of the assembled material |
| `material_hex` | the assembled material, hex-encoded, so an implementation can compare the *pre-digest* bytes and localise an encoding bug |
| `digest_sha256` | expected digest with no pepper |
| `digest_hmac_sha256` | expected digest under `test_pepper` |

`test_pepper` is a synthetic constant that exists only for these vectors. It is
not a credential and must never be used in any environment.

Coverage: ASCII, empty fields, single characters, separator-like characters
(`|`, `:`), an embedded newline, Polish diacritics, CJK, an emoji (4-byte
UTF-8), combining versus precomposed sequences, 255/256-byte boundary lengths,
and object-key variants including the full base64url alphabet. All 21 digests
are distinct.

## 7. Changing this specification

Any change to the domain label, separator, length encoding, field order or
normalisation rule is a **new version**. Bump the label to
`…subject_binding.v2`, add new vectors, and keep the v1 implementation until
every stored object has been re-published — an object written under v1 metadata
cannot validate under v2 rules, which is the intended fail-closed behaviour.
