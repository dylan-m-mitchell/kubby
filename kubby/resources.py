"""Resource-shaped kubectl calls, with no I/O of their own.

Following the same rule as :mod:`kubby.installer.minikube`: this module only
builds argv and does the one piece of string surgery that an editor needs. The
service runs the commands, so everything here is directly unit-testable and
nothing here knows about a cluster.

The YAML that comes back from ``kubectl get`` is machine-generated and stable
(keys sorted, two-space indent, list items written ``- ``), which is what makes
:func:`sanitize` safe as line-based surgery. It is deliberately not a general
YAML parser: ``pyproject.toml`` has exactly one runtime dependency and this is
not worth being the second.
"""

from __future__ import annotations

from typing import Final

from kubby.cluster import ResourceRef

#: ``kubectl diff`` exits 1 when it found a difference. That is the happy
#: path, not a failure — treating it as an error is the single most likely bug
#: in an edit flow. Anything above 1 (or a negative return code from a signal)
#: is the error case.
DIFF_UNCHANGED: Final = "unchanged"
DIFF_CHANGED: Final = "changed"
DIFF_ERROR: Final = "error"


def get_yaml_args(ref: ResourceRef) -> list[str]:
    """Read *one* object as YAML.

    Never the ``-A`` form: that wraps everything in a ``kind: List``, and the
    editor would then be handed a List to apply back. The editor's whole
    contract is one bare object.

    ``--show-managed-fields=false`` is explicit even though modern kubectl
    already hides the field. An older kubectl putting ``managedFields`` back
    would make every save fight a wall of server-owned bookkeeping.
    """
    args = [
        "kubectl",
        "get",
        ref.kind.lower(),
        ref.name,
        "-o",
        "yaml",
        "--show-managed-fields=false",
    ]
    if ref.namespace:
        args[4:4] = ["-n", ref.namespace]
    return args


def diff_args() -> list[str]:
    """Server-side dry run plus unified diff, in one call, from stdin."""
    return ["kubectl", "diff", "-f", "-"]


def apply_args() -> list[str]:
    """Apply from stdin, so no YAML — and no Secret — ever reaches argv."""
    return ["kubectl", "apply", "-f", "-"]


def diff_outcome(returncode: int) -> str:
    """Classify ``kubectl diff``'s exit code.

    ``0`` means the live object already matches; ``1`` means differences were
    found (and the diff text is the useful output); everything else is a real
    failure. A negative code is a signal, which is also a failure.
    """
    if returncode == 0:
        return DIFF_UNCHANGED
    if returncode == 1:
        return DIFF_CHANGED
    return DIFF_ERROR


#: Metadata fields owned by the API server. Left in place, they make the
#: editor show bookkeeping and make ``apply`` fight a stale resourceVersion.
_SERVER_METADATA: Final = frozenset(
    {"creationTimestamp", "generation", "resourceVersion", "uid"}
)


def _at_column_zero(line: str) -> bool:
    """True for a YAML top-level key (not blank, not indented)."""
    return bool(line) and not line[0].isspace()


def _is_owned_metadata_field(line: str) -> bool:
    """True for a two-space-indented server-owned field inside ``metadata``.

    Exactly two spaces, not "at least two": nested maps and lists live at four
    or more, and a field called ``generation`` inside ``spec`` is a real
    field belonging to the user — it must survive.
    """
    if not line.startswith("  ") or len(line) <= 2 or line[2].isspace():
        return False
    key, separator, _rest = line[2:].partition(":")
    return bool(separator) and key.strip() in _SERVER_METADATA


def sanitize(yaml_text: str) -> str:
    """Drop what the server owns, so the editor shows what is editable.

    Two removals, both line-based:

    * the whole top-level ``status:`` block (last key in kubectl's output, but
      the scan stops at the next top-level key rather than assuming that);
    * ``creationTimestamp``, ``generation``, ``resourceVersion`` and ``uid``
      **only inside the top-level metadata block**.

    The metadata removals are scoped to that block on purpose. An unanchored
    ``^  generation:`` would eventually eat a field inside ``spec``, and a
    user's Deployment is exactly where that would hurt.
    """
    out: list[str] = []
    in_metadata = False
    skipping_status = False

    for line in yaml_text.splitlines(keepends=True):
        content = line.rstrip("\r\n")

        if skipping_status:
            if _at_column_zero(content):
                skipping_status = False
            else:
                continue

        if content.startswith("status:") and _at_column_zero(content):
            skipping_status = True
            continue

        if in_metadata and _at_column_zero(content):
            in_metadata = False

        if content.startswith("metadata:") and _at_column_zero(content):
            in_metadata = True
            out.append(line)
            continue

        if in_metadata and _is_owned_metadata_field(content):
            continue

        out.append(line)

    return "".join(out)
