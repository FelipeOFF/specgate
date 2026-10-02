"""Read back the sources a stored verification names, since it keeps no research text."""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from specgate.context import ContextPacket, _resolve_source
from specgate.research_ledger import admitted_texts


def _project_text(project: Path, ref: dict[str, str]) -> str | None:
    """The text of the project file a ref names, while it keeps that revision."""
    try:
        path, _ = _resolve_source(project.resolve(), ref["source"])
        raw = path.read_bytes()
        return raw.decode() if sha256(raw).hexdigest() == ref["revision"] else None
    except (OSError, ValueError):
        return None


def restore_research(
    state: dict[str, Any],
    project: Path,
    refs: list[dict[str, str]],
    vocabulary_revision: str | None,
    domain: Callable[[list[str]], ContextPacket],
) -> tuple[ContextPacket, list[dict[str, str]]] | None:
    """Read back the stored source refs: project files, then admitted research.

    The stored verification keeps revisions only, so the texts come from where
    they live. The refs list the project sources first and the admitted ones
    after, and the vocabulary revision names the project part: the first split
    that reproduces it is the one the synthesis used, however an admitted
    source declares its origin. `domain` builds the vocabulary packet from the
    project paths. None means a source changed or is gone, and the verdicts that
    the refs name no longer describe what Jev would read.
    """
    admitted = admitted_texts(state)
    for split in range(len(refs) + 1):
        if split and _project_text(project, refs[split - 1]) is None:
            return None
        local, rest = refs[:split], refs[split:]
        if any(ref["revision"] not in admitted for ref in rest):
            continue
        packet = domain([ref["source"] for ref in local])
        if not packet.gaps and packet.revision == vocabulary_revision:
            return packet, [{**ref, "text": admitted[ref["revision"]]} for ref in rest]
    return None


def restore_refs(
    state: dict[str, Any], project: Path, refs: list[dict[str, str]]
) -> list[dict[str, str]] | None:
    """Read back each stored ref on its own: admitted research, else a project file.

    For a verification that does not read the vocabulary, so no vocabulary revision
    tells the project part from the admitted one. A ref the journal admitted takes
    its text from there, however it declares its origin; any other must be a project
    file that still has the revision the ref names. None means a source changed or is
    gone, and the verdicts that the refs name no longer describe what Jev would read.
    """
    admitted = admitted_texts(state)
    restored = []
    for ref in refs:
        text = admitted.get(ref["revision"])
        if text is None:
            text = _project_text(project, ref)
        if text is None:
            return None
        restored.append({**ref, "text": text})
    return restored
