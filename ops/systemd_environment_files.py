"""Parse effective systemd EnvironmentFile/Environment directives in unit order.

`effective_environment_files` answers *which files* a merged unit sources.
`unit_environment` answers the question that actually matters operationally:
**what environment will the process see?** — which is what decides whether a job
can resolve `SUSPECTED_BUG_ALERT_TO` at all. Testing `load_alert_config()` on
`os.environ` cannot answer that, because the gap lives in the unit definition,
not in the loader.

Deliberately bounded to the syntax this repository's units actually use: the
`NAME=value` forms, `#`/`;` comments, shell quoting, the `-` optional-file
prefix, and the empty-value reset. It is not a systemd parser, and it must never
be *more* capable than systemd — a helper that resolves something systemd would
not proves nothing about production.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Iterable, Mapping

CANONICAL_ENVIRONMENT_FILE = Path("/etc/log-platform/environment-identity.env")
RUNTIME_ENVIRONMENT_FILE = Path("/etc/log-platform/runtime.env")


def effective_environment_files(text: str) -> list[str]:
    """Apply EnvironmentFile= reset semantics to already ordered unit text."""
    effective: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("EnvironmentFile="):
            continue
        value = line.partition("=")[2].strip()
        if not value:
            effective.clear()
            continue
        try:
            fields = shlex.split(value, comments=False, posix=True)
        except ValueError:
            continue
        for field in fields:
            normalized = field[1:] if field.startswith("-") else field
            if normalized:
                effective.append(normalized)
    return effective


def merged_dropin_text(directory: Path) -> str:
    if directory.is_symlink() or not directory.is_dir():
        return ""
    blocks = []
    for path in sorted(directory.glob("*.conf"), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            continue
        blocks.append(f"# {path}\n{path.read_text(encoding='utf-8')}")
    return "\n".join(blocks)


def canonical_source_effective(text: str) -> bool:
    return str(CANONICAL_ENVIRONMENT_FILE) in effective_environment_files(text)


def inline_environment(text: str) -> list[tuple[str, str]]:
    """`Environment=` assignments in unit order.

    Returned as an ordered list rather than a mapping because systemd applies a
    later assignment over an earlier one, and callers that merge files and
    inline settings need that order preserved.
    """
    out: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("Environment="):
            continue
        value = line.partition("=")[2].strip()
        if not value:
            # `Environment=` with an empty value resets previous assignments,
            # exactly like the EnvironmentFile= reset above.
            out.clear()
            continue
        try:
            fields = shlex.split(value, comments=False, posix=True)
        except ValueError:
            continue
        for field in fields:
            name, sep, assigned = field.partition("=")
            if sep and name:
                out.append((name, assigned))
    return out


def parse_environment_file(text: str) -> list[tuple[str, str]]:
    """Parse systemd EnvironmentFile syntax into ordered assignments.

    Deliberately narrow: `NAME=value`, `#`/`;` comments, blank lines, and shell
    quoting on the value. It does not expand variables, because systemd does not
    expand them in EnvironmentFile either — reproducing that limitation is the
    point, since a test that is more capable than systemd proves nothing.
    """
    out: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        if name.startswith("export "):
            name = name[len("export "):].strip()
        try:
            fields = shlex.split(value.strip(), comments=False, posix=True)
        except ValueError:
            fields = [value.strip()]
        out.append((name, fields[0] if fields else ""))
    return out


def unit_environment(
    text: str,
    *,
    file_contents: Mapping[str, str],
    base: Mapping[str, str] | None = None,
    missing_files: Iterable[str] = (),
) -> dict[str, str]:
    """The environment a process started by this merged unit text would see.

    `file_contents` maps an EnvironmentFile path to its raw text. A path that is
    absent from it and not listed in `missing_files` raises: silently treating an
    unreadable authoritative file as empty is precisely the failure this helper
    exists to detect, so it must never be the default.

    **Precedence.** `EnvironmentFile=` wins over `Environment=`, and it does so
    regardless of the order the two appear in the unit text. From
    `man systemd.exec` (systemd 255):

        Settings from these files override settings made with Environment=.
        If the same variable is set twice from these files, the files will be
        read in the order they are specified and the later setting will
        override the earlier setting.

    Verified empirically on this host with a disposable transient user unit:
    with `Environment=X=inline` and a file setting `X=from_file`, the process
    saw `from_file` in both declaration orders.

    So inline assignments are the base layer and files are applied over them —
    not the textual order, which is what this helper originally reproduced.
    """
    env = dict(base or {})
    tolerated = set(missing_files)

    # Base layer: Environment=, later assignment overriding earlier.
    for name, value in inline_environment(text):
        env[name] = value

    # Override layer: EnvironmentFile= in effective order, later file winning.
    for path in effective_environment_files(text):
        if path in file_contents:
            for name, value in parse_environment_file(file_contents[path]):
                env[name] = value
        elif path in tolerated:
            continue
        else:
            raise KeyError(f"no contents supplied for EnvironmentFile={path}")
    return env
