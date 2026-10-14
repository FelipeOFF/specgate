"""The section of a draft pull request that shows each delegated rewrite next to its original.

The reviewer of a delivery sees, for every decision the agent rewrote under the delegation, the
decision as the approved spec held it (id and text), the text that replaced it or the parts it was
divided into, what the equivalence check found, and the revision of the grant the rewrite used.
A delivery whose spec was never rewritten under the delegation gets no section, so its body is
the one it always had.

The body of a pull request has a size limit on the forge and in the adapters, so the section
never takes more than the room the rest of the body leaves. A text that is too long is cut, the
parts of a decision that do not fit are left out, and only when even the original of every
decision does not fit are the last decisions left out, each with an explicit warning. It names no
credential, no local path and no session: the record keeps the project and the reference of the
grant, and neither is shown here. A text shows no markup: its HTML is escaped, so nothing a text
says can close a list of the section, open a block of its own or pass for the marker of the forge.
"""

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from specgate.decision_ids import claim_id
from specgate.privacy import ensure_safe_content

# The body of a pull request holds 65,536 characters on GitHub and in `DraftPullRequest`; the
# budget counts bytes, which is never less, and leaves a margin.
BODY_LIMIT = 65_000
# What the section takes at most, however much room is left.
SECTION_LIMIT = 20_000
# What a single text of a decision may take before it is cut.
TEXT_LIMIT = 1_500
TITLE = "## Reescritas delegadas\n\n"
INTRODUCTION = (
    "Cada reescrita abaixo foi feita sob delegação, e só vale com a equivalência aceita pelo "
    "gate. O score filtra incerteza e não mede acurácia.\n\n"
)
PATH_OMITTED = "[caminho omitido]"
WITHHELD = "[texto omitido: contém credencial ou referência de sessão]"
KINDS = {"split": "Divisão", "reformulation": "Reformulação"}
_HASH = re.compile(r"[a-f0-9]{64}")
# The end of a path: it runs up to a blank, a quote, a backtick or the edge of a tag.
_REST = r"[^\s\"'<>`]*"
# Where a home keeps its own things: a dot directory or a folder every system creates.
_HOME_ENTRY = (
    r"(?:\.[\w.-]+|(?i:documents|downloads|desktop|projects?|library|pictures|music|videos|src|code"
    r"|dev|work|repos?|git|go|workspaces?|dropbox)(?![\w-]))"
)
# A path of this machine, and nothing else: the home of a person (with the name), a file URL, a
# drive of Windows and the directories only a Mac makes. A route that looks like a directory
# (`/home/painel`, `/var/queue/eventos`, `/opt/out/{id}`, `/tmp/export.csv`) is a decision of the
# spec, which is on its issue already, and stays. A root is never read after a word character, so
# the path of a URL or of a relative path is not one.
_LOCAL_PATH = re.compile(
    rf"\bfile://{_REST}"
    rf"|(?<![\w.-])(?:"
    rf"[A-Za-z]:[\\/](?:Users|Documents and Settings)(?:[\\/]{_REST})?"
    rf"|~/{_REST}"
    rf"|/Users/[A-Za-z0-9._-]+(?:/{_REST})?"
    rf"|/home/[A-Za-z0-9._-]+/{_HOME_ENTRY}(?:/{_REST})?"
    rf"|/root/{_HOME_ENTRY}(?:/{_REST})?"
    rf"|/private/(?:var|tmp|etc)(?:/{_REST})?"
    rf"|/var/folders/{_REST}"
    rf"|/tmp/claude-{_REST}"
    rf"|/Volumes/[A-Za-z0-9._-]+(?:/{_REST})?"
    rf")"
)
# What `ensure_safe_content` does not know: the address of a session without its scheme, and the
# trailer with any id.
_SESSION = re.compile(r"claude\.ai/code/session_[A-Za-z0-9_-]+|Claude-Session:\s*\S+", re.IGNORECASE)


def _text(value: Any) -> str:
    """One text of the spec as a single line, cut when long and shown only when it is safe.

    Whitespace collapses so a decision cannot open a heading or a list of its own. A credential or
    a session withholds the text; a local path is cut out of it and the rest stays. The HTML is
    escaped last, after the cut, so no entity is split.
    """
    if not isinstance(value, str) or not value.strip():
        return "(ausente)"
    line = " ".join(value.split())
    try:
        ensure_safe_content(line)
    except ValueError:
        return WITHHELD
    if _SESSION.search(line):
        return WITHHELD
    line = _LOCAL_PATH.sub(PATH_OMITTED, line)
    if len(line) > TEXT_LIMIT:
        line = f"{line[:TEXT_LIMIT].rstrip()} [truncado: {TEXT_LIMIT} de {len(line)} caracteres]"
    return html.escape(line, quote=False)


def _revision(value: Any, length: int) -> str:
    """A revision hash as the first digits of it, or `ausente` for anything that is not one."""
    return f"`{value[:length]}`" if isinstance(value, str) and _HASH.fullmatch(value) else "`ausente`"


def _probabilities(value: Any) -> str:
    rows = value if isinstance(value, Mapping) else {}
    return ", ".join(
        f"{name} {number}"
        for name, number in rows.items()
        if isinstance(name, str)
        and isinstance(number, (int, float))
        and not isinstance(number, bool)
    )


def _part(part: Mapping[str, Any]) -> str:
    identifier = part.get("id")
    return claim_id(identifier) if isinstance(identifier, str) and identifier else "ausente"


_NO_ROOM = "por falta de espaço; o registro completo está em `rewrite_audit` da spec da run"


@dataclass(frozen=True)
class _Unit:
    """One decision of the audit, or the header of a rewrite that has none, ready to be cut.

    `lead` is what is never left out: the header of the rewrite (on its first decision), the id
    and the original. `parts` are the lines that can be, from the last one back. `tail` is the
    equivalence, which also stays. `nested` says whether the parts are a list under their own
    bullet (a division) or the one new text of a reformulation.
    """

    lead: str
    parts: tuple[str, ...] = ()
    tail: str = ""
    nested: bool = True

    def render(self, shown: int) -> str:
        """The block with its first `shown` parts, and a line that counts the ones left out."""
        if not self.tail:
            return self.lead
        lines = [self.lead]
        omitted = len(self.parts) - shown
        if self.nested:
            lines.append("- Partes:")
            lines += self.parts[:shown]
            if omitted:
                lines.append(f"  - {omitted} de {len(self.parts)} parte(s) omitida(s) {_NO_ROOM}")
        elif shown:
            lines.append(self.parts[0])
        else:
            lines.append(f"- Novo texto: omitido {_NO_ROOM}")
        lines.append(self.tail)
        return "\n".join(lines) + "\n\n"


def _unit(decision: Mapping[str, Any], header: str) -> _Unit:
    """The block of one decision: the original, the new text or the parts, and the equivalence."""
    kind = decision.get("kind")
    original = decision.get("original")
    identifier = claim_id(original) if isinstance(original, str) and original else "ausente"
    parts = [part for part in decision.get("parts") or [] if isinstance(part, Mapping)]
    single = kind == "reformulation" and len(parts) == 1
    lines: tuple[str, ...]
    if single:
        lines = (f"- Novo texto (`{_part(parts[0])}`): {_text(parts[0].get('text'))}",)
    else:
        lines = tuple(f"  - `{_part(part)}`: {_text(part.get('text'))}" for part in parts)
    equivalence = decision.get("equivalence")
    found = equivalence if isinstance(equivalence, Mapping) else {}
    state = "aceita" if found.get("state") == "accepted" else "não aceita"
    return _Unit(
        lead=(
            f"{header}#### {KINDS.get(str(kind), 'Reescrita')} da decisão `{identifier}`\n\n"
            f"- Original: {_text(decision.get('original_text'))}"
        ),
        parts=lines,
        tail=f"- Equivalência: {state} ({_probabilities(found.get('probabilities'))})",
        nested=not single,
    )


def _header(entry: Mapping[str, Any], index: int, total: int) -> str:
    delegation = entry.get("delegation")
    grant = delegation if isinstance(delegation, Mapping) else {}
    return (
        f"### Reescrita {index} de {total}\n\n"
        f"- Revisão da spec que a gravou: {_revision(entry.get('revision'), 12)}\n"
        f"- Revisão da spec que ela substitui: {_revision(entry.get('replaces'), 12)}\n"
        f"- Revisão da delegação: {_revision(grant.get('revision'), 64)}\n\n"
    )


def _omitted(count: int) -> str:
    return (
        f"Aviso: {count} decisão(ões) reescrita(s) não cabem no limite do corpo da PR e foram "
        "omitidas; o registro completo está em `rewrite_audit` da spec da run.\n\n"
    )


def _units(audit: Sequence[Mapping[str, Any]]) -> list[_Unit]:
    units: list[_Unit] = []
    for index, entry in enumerate(audit, 1):
        decisions = [item for item in entry.get("decisions") or [] if isinstance(item, Mapping)]
        header = _header(entry, index, len(audit))
        if not decisions:
            units.append(_Unit(lead=header))
        # The header goes with the first decision of its rewrite, so it is never left alone.
        units += [
            _unit(decision, header if position == 0 else "")
            for position, decision in enumerate(decisions)
        ]
    return units


def _bytes(text: str) -> int:
    return len(text.encode())


def _oldest(opening: str, blocks: Sequence[str], limit: int) -> str:
    """The oldest blocks that fit with the warning that counts the others."""
    spare = limit - _bytes(opening + _omitted(len(blocks)))
    kept = 0
    for block in blocks:
        if _bytes(block) > spare:
            break
        spare -= _bytes(block)
        kept += 1
    return opening + "".join(blocks[:kept]) + _omitted(len(blocks) - kept)


def rewrite_note(audit: Sequence[Mapping[str, Any]], room: int) -> str:
    """The section for the rewrites of the audit, in at most `room` bytes; empty with no rewrite.

    `room` is what the rest of the body leaves. What the section gives up, it gives up in this
    order, and says so: the parts of a decision, from the last one back (every decision keeps its
    header, its id, its original and its equivalence, and a later rewrite keeps these before an
    earlier one takes more room); and only when not even those fit, the last decisions, with a
    warning that counts them.
    """
    if not audit:
        return ""
    limit = min(SECTION_LIMIT, room)
    units = _units(audit)
    opening = TITLE + INTRODUCTION
    whole = [unit.render(len(unit.parts)) for unit in units]
    if _bytes(opening + "".join(whole)) <= limit:
        return opening + "".join(whole)
    blocks = [unit.render(0) for unit in units]
    spare = limit - _bytes(opening + "".join(blocks))
    if spare < 0:
        return _oldest(opening, blocks, limit)
    # The room that is left goes to the oldest rewrite first, as many parts as it holds.
    for position, unit in enumerate(units):
        base = _bytes(blocks[position])
        for shown in range(len(unit.parts), 0, -1):
            block = unit.render(shown)
            if _bytes(block) - base <= spare:
                spare -= _bytes(block) - base
                blocks[position] = block
                break
    return opening + "".join(blocks)
