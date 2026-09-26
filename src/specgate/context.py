"""Collect explicit local context before asking a decision provider."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Literal, TypedDict

from specgate.privacy import ensure_safe_content

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
_RULE_FILES = {"agents.md", "context.md", "readme.md"}
_BLOCKED_PARTS = {".git", ".codex", ".claude", ".grok", ".cursor"}
_BLOCKED_NAMES = {
    "credentials.json",
    "credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "session.jsonl",
    "session.txt",
    "transcript.jsonl",
    "transcript.txt",
}
_BLOCKED_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


@dataclass(frozen=True)
class Evidence:
    id: str
    source: str
    revision: str
    text: str


@dataclass(frozen=True)
class ContextPacket:
    objective: str
    rules: tuple[str, ...]
    artifact: str
    evidence: tuple[Evidence, ...]
    alternatives: tuple[str, ...]
    gaps: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    unexamined: tuple[str, ...] = ()

    @property
    def revision(self) -> str:
        """Bind a result to the complete context that was examined."""
        content = json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
        return sha256(content.encode()).hexdigest()


@dataclass(frozen=True)
class ContextResult:
    packet: ContextPacket
    rounds: int
    action: Literal["collect", "evaluate", "needs_human"]
    reason: str


class SkillCatalogEntry(TypedDict):
    id: str
    text: str


def _is_blocked(path: Path) -> bool:
    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return (
        bool(parts & _BLOCKED_PARTS)
        or name == ".env"
        or name.startswith(".env.")
        or name in _BLOCKED_NAMES
        or path.suffix.casefold() in _BLOCKED_SUFFIXES
    )


def _resolve_source(project: Path, source: str) -> tuple[Path, str]:
    candidate = Path(source)
    if _is_blocked(candidate):
        raise ValueError("Caminho bloqueado por conter dados sensíveis.")
    path = (
        (project / candidate).resolve()
        if not candidate.is_absolute()
        else candidate.resolve()
    )
    try:
        relative = path.relative_to(project)
    except ValueError as error:
        raise ValueError(f"Fonte fora do projeto: {source}") from error
    if _is_blocked(relative):
        raise ValueError("Caminho bloqueado por conter dados sensíveis.")
    return path, relative.as_posix()


def build_context(
    project: Path,
    objective: str,
    paths: list[str],
    required: list[str],
    artifact: str = "",
    alternatives: list[str] | None = None,
    *,
    gaps: list[str] | None = None,
    conflicts: list[str] | None = None,
) -> ContextPacket:
    """Read only explicit project files and report every source that was skipped.

    ``required`` contains public evidence IDs or source paths that must be present.
    For filesystem evidence both values are the normalized project-relative path.
    ``alternatives`` must name the options the later evaluation will compare.
    """
    root = project.resolve()
    evidence: list[Evidence] = []
    rules: list[str] = []
    gaps = list(gaps or ())
    unexamined: list[str] = []
    seen: set[str] = set()
    total = 0

    for source in paths:
        path, evidence_id = _resolve_source(root, source)
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        unexamined.append(evidence_id)
        try:
            size = path.stat().st_size
        except OSError as error:
            gaps.append(
                f"Conteúdo {source} não pôde ser lido: {error.strerror or type(error).__name__}."
            )
            continue
        if not path.is_file():
            gaps.append(f"Fonte {source} não pôde ser lida: não é um arquivo regular.")
            continue
        if size > MAX_FILE_BYTES:
            gaps.append(
                f"Fonte {source} excede o limite por arquivo de {MAX_FILE_BYTES} bytes."
            )
            continue
        if total + size > MAX_TOTAL_BYTES:
            gaps.append(
                f"Fonte {source} excede o limite total de {MAX_TOTAL_BYTES} bytes."
            )
            continue
        try:
            raw = path.read_bytes()
        except OSError as error:
            gaps.append(
                f"Conteúdo {source} não pôde ser lido: {error.strerror or type(error).__name__}."
            )
            continue
        if len(raw) != size:
            gaps.append(f"Fonte {source} mudou durante a leitura; colete novamente.")
            continue
        if b"\x00" in raw:
            gaps.append(f"Fonte {source} é um arquivo binário e não foi lida.")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            gaps.append(f"Fonte {source} não é texto UTF-8 e foi tratada como binário.")
            continue
        item = Evidence(evidence_id, evidence_id, sha256(raw).hexdigest(), text)
        evidence.append(item)
        unexamined.pop()
        total += size
        if path.name.casefold() in _RULE_FILES:
            rules.append(text)

    present = {value for item in evidence for value in (item.id, item.source)}
    for required_id in required:
        normalized = Path(required_id).as_posix()
        if required_id not in present and normalized not in present:
            gaps.append(f"Evidência obrigatória ausente: {required_id}")

    if not objective.strip():
        gaps.append("Objetivo ausente.")
    if not artifact.strip():
        gaps.append("Artefato ausente.")
    if not rules:
        gaps.append("Regras aplicáveis ausentes.")
    if not evidence:
        gaps.append("Nenhuma evidência legível foi coletada.")
    choices = tuple(choice.strip() for choice in alternatives or () if choice.strip())
    if not choices:
        gaps.append("Alternativas ausentes.")

    packet = ContextPacket(
        objective.strip(),
        tuple(rules),
        artifact,
        tuple(evidence),
        choices,
        tuple(gaps),
        tuple(conflicts or ()),
        tuple(unexamined),
    )
    ensure_safe_content(asdict(packet))
    return packet


def readiness_gaps(packet: ContextPacket | None) -> tuple[str, ...]:
    """Name each readiness check that still blocks a decision."""
    if packet is None:
        return (
            "objetivo ausente",
            "regras ausentes",
            "artefato ausente",
            "alternativas ausentes",
            "evidência ausente",
        )
    gaps: list[str] = []
    if not packet.objective.strip():
        gaps.append("objetivo ausente")
    if not any(rule.strip() for rule in packet.rules):
        gaps.append("regras ausentes")
    if not packet.artifact.strip():
        gaps.append("artefato ausente")
    if not any(choice.strip() for choice in packet.alternatives):
        gaps.append("alternativas ausentes")
    if not packet.evidence:
        gaps.append("evidência ausente")
    else:
        if any(
            not item.id.strip() or not item.source.strip() or not item.text.strip()
            for item in packet.evidence
        ):
            gaps.append("evidência sem identificador, origem ou texto")
        if any(
            item.revision != sha256(item.text.encode()).hexdigest()
            for item in packet.evidence
        ):
            gaps.append("revisão da evidência não é o sha256 do texto")
    if packet.gaps:
        gaps.append("lacuna presente")
    if packet.conflicts:
        gaps.append("conflito presente")
    if packet.unexamined:
        gaps.append("item não examinado")
    return tuple(gaps)


def _is_ready(packet: ContextPacket) -> bool:
    return not readiness_gaps(packet)


def assess_context(
    packet: ContextPacket, round_number: int, revisions: set[str]
) -> ContextResult:
    """Apply the same bounded collection policy to local and async harness flows."""
    if _is_ready(packet):
        return ContextResult(packet, round_number, "evaluate", "Contexto completo.")
    current = {sha256(item.text.encode()).hexdigest() for item in packet.evidence}
    if round_number > 1 and not current - revisions:
        return ContextResult(
            packet,
            round_number,
            "needs_human",
            "A rodada não trouxe nenhum conteúdo novo.",
        )
    revisions.update(current)
    if round_number >= 3:
        return ContextResult(
            packet,
            round_number,
            "needs_human",
            "O contexto permaneceu incompleto após três rodadas.",
        )
    return ContextResult(
        packet, round_number, "collect", "Reúna evidências para as lacunas pendentes."
    )


def collect_until_ready(collect: Callable[[int], ContextPacket]) -> ContextResult:
    """Collect at most three rounds, stopping on readiness or stalled evidence."""
    revisions: set[str] = set()
    for round_number in range(1, 4):
        result = assess_context(collect(round_number), round_number, revisions)
        if result.action != "collect":
            return result
    raise AssertionError("Context collection exceeded its round limit")


def _frontmatter_description(instructions: str) -> str:
    lines = instructions.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return ""
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if end is None:
        return ""
    frontmatter = lines[1:end]
    for index, line in enumerate(frontmatter):
        stripped = line.strip()
        key, separator, value = stripped.partition(":")
        if separator and key.strip() == "description":
            value = value.strip()
            if not (
                value
                and value[0] in ">|"
                and all(char in "+-0123456789" for char in value[1:])
            ):
                return value.strip("'\"")

            key_indent = len(line) - len(line.lstrip())
            block: list[str] = []
            for continuation in frontmatter[index + 1 :]:
                if continuation.strip():
                    indent = len(continuation) - len(continuation.lstrip())
                    if indent <= key_indent:
                        break
                block.append(continuation)
            content_indent = next(
                (len(item) - len(item.lstrip()) for item in block if item.strip()),
                key_indent + 1,
            )
            content = [
                item[content_indent:].rstrip("\r\n") if item.strip() else ""
                for item in block
            ]
            if value[0] == "|":
                text = "\n".join(content)
            else:
                text = "".join(
                    item
                    + (
                        " "
                        if item and next_item
                        else "\n"
                        if item or not next_item
                        else ""
                    )
                    for item, next_item in pairwise(content)
                )
                if content:
                    text += content[-1]
            if block and block[-1].endswith(("\n", "\r")):
                text += "\n"
            if "-" in value:
                return text.rstrip("\n")
            if "+" in value:
                return text
            return text.rstrip("\n") + ("\n" if text else "")
    return ""


@dataclass(frozen=True)
class SkillSource:
    id: str
    text: str
    source: str
    reference: str
    revision: str
    aliases: tuple[str, ...]
    issue: str | None = None


def read_skill(source: SkillSource, authorized_roots: list[Path]) -> str:
    """Recheck authorization, file identity and bytes before using instructions."""
    path = Path(source.reference).resolve()
    if not any(path.is_relative_to(root.resolve()) for root in authorized_roots):
        raise PermissionError("Skill fora das roots autorizadas.")
    if path != Path(source.source):
        raise ValueError("A origem da skill mudou; descubra o catálogo novamente.")
    with path.open("rb") as file:
        raw = file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES or b"\x00" in raw:
        raise ValueError("Skill excede o limite de leitura ou contém dados binários.")
    if source.revision and sha256(raw).hexdigest() != source.revision:
        raise ValueError("A revisão da skill mudou; avalie as novas instruções.")
    content = raw.decode("utf-8")
    ensure_safe_content(content)
    return content


def discover_skill_catalog(
    roots: list[Path], *, authorized_roots: list[Path]
) -> list[SkillSource]:
    """Catalog each authorized file once, retaining aliases and unreadable entries."""
    authorized = tuple(root.resolve() for root in authorized_roots)
    by_id: dict[str, Path] = {}
    files: dict[Path, list[tuple[str, Path]]] = {}
    for root in roots:
        if not any(root.resolve().is_relative_to(allowed) for allowed in authorized):
            raise PermissionError("Root de skills não autorizada.")
        children = sorted(root.iterdir(), key=lambda child: child.name)
        for child in children:
            skill_file = child / "SKILL.md"
            if not child.is_dir() or not skill_file.is_file():
                continue
            resolved = skill_file.resolve()
            if not any(resolved.is_relative_to(root) for root in authorized):
                raise ValueError(
                    f"Skill fora das roots autorizadas: {skill_file.as_posix()}"
                )
            skill_id = f"skills/{child.name}"
            existing = by_id.get(skill_id)
            if existing is not None and existing != resolved:
                raise ValueError(f"ID de skill duplicado: {skill_id}")
            by_id[skill_id] = resolved
            files.setdefault(resolved, []).append((skill_id, skill_file))
    catalog = []
    for path, aliases in files.items():
        entry = SkillSource(
            aliases[0][0],
            "",
            str(path),
            str(aliases[0][1].absolute()),
            "",
            tuple(
                dict.fromkeys(
                    [*(item[0] for item in aliases), f"skills/{path.parent.name}"]
                )
            ),
        )
        try:
            content = read_skill(entry, authorized_roots)
            entry = replace(
                entry,
                text=_frontmatter_description(content),
                revision=sha256(content.encode()).hexdigest(),
            )
        except (OSError, ValueError) as error:
            entry = replace(entry, issue=str(error))
        catalog.append(entry)
    return catalog


def discover_skills(roots: list[Path]) -> list[SkillCatalogEntry]:
    """Compatibility view containing only public IDs and descriptions."""
    return [
        {"id": item.id, "text": item.text}
        for item in discover_skill_catalog(roots, authorized_roots=roots)
    ]
