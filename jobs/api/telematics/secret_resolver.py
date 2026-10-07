import os
from pathlib import Path


class SecretResolutionError(RuntimeError):
    pass


def resolve_secret(secret_ref: str) -> str:
    """
    Resolve an operator-managed secret reference.

    Supported forms (v1 minimal, explicit):
      - plain ENV var name: secret_ref="TELEMATICS_PASSWORD"  -> uses $TELEMATICS_PASSWORD
      - file reference: secret_ref="file:/path/to/secret.txt" -> reads file contents (trimmed)

    Anything else is treated as an ENV var name.
    """
    if not secret_ref:
        raise SecretResolutionError("Empty secret_ref")

    if secret_ref.startswith("file:"):
        p = Path(secret_ref[len("file:"):])
        if not p.exists() or not p.is_file():
            raise SecretResolutionError(f"Secret file not found: {p}")
        return p.read_text(encoding="utf-8").strip()

    v = os.getenv(secret_ref)
    if v is None:
        raise SecretResolutionError(
            f"Missing secret for ref={secret_ref!r}. "
            f"Provide env var named exactly {secret_ref!r} or use secret_ref starting with 'file:'."
        )
    return v.strip()
