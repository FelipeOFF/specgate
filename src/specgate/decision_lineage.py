"""The lineage of a decision or an acceptance criterion the agent split or reformulated.

A spec lists the lineage of its implementation decisions and a ticket graph that of its
acceptance criteria, both written by the agent: for each part, the id of the original it
replaces in the previous revision and whether it is a division or a reformulation. The Jev
never writes a part. What the lineage owes is an equivalence check against the original, so
this module resolves each entry to the group of parts and the original text it is judged
against, and the verification stores and reads one record per group.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from specgate.slices.equivalence import MAX_PARTS
from specgate.wording import INVALID_LINEAGE, AgentRefusal

LineageKind = Literal["split", "reformulation"]
MESSAGE = (
    "A linhagem não se sustenta: cada parte aponta uma decisão da revisão anterior, uma "
    "divisão tem duas ou mais partes e uma reformulação tem uma."
)


class Lineage(BaseModel):
    """What one part says about the text it replaces."""

    model_config = ConfigDict(extra="forbid", strict=True)
    original: str = Field(min_length=1, max_length=200)
    kind: LineageKind


@dataclass(frozen=True)
class Revision:
    """One stored revision as the lineage reads it: its texts by id, and its own lineage."""

    texts: Mapping[str, str]
    lineage: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Group:
    """An original and the parts that replace it, judged together by one equivalence check."""

    original: str
    kind: str
    original_text: str
    parts: tuple[tuple[str, str], ...]
    # The ids the original itself descends from, nearest first and ending at the root of the
    # lineage, as far as the stored revisions say; the original is its first entry.
    ancestry: tuple[str, ...] = ()


def _entry(value: Any) -> dict[str, Any]:
    return {key: value.get(key) for key in ("original", "kind")}


def _baseline(part: str, entry: dict[str, Any], earlier: Sequence[Revision]) -> int | None:
    """The place in `earlier` of the newest revision before the part declared this lineage.

    That revision is where its original lives. One that carries the same entry for the same
    part is a later copy of the declaration, not the original. Walking past those, a part that
    is edited, carried over or checked again is always judged against the text it first replaced.
    """
    for index, revision in enumerate(earlier):
        if _entry(revision.lineage.get(part, {})) != entry:
            return index
    return None


def _ancestry(original: str, index: int, earlier: Sequence[Revision]) -> tuple[str, ...]:
    """The ids an original descends from: the chain of originals, as far as the revisions go.

    `index` is the revision where the original lives. When it was itself a part there, its own
    original lives in the revision before the declaration, and so on to the root. A
    reformulation that keeps its id adds nothing to the chain.
    """
    chain = [original]
    while True:
        declared = earlier[index].lineage.get(chain[-1])
        if not declared:
            return tuple(chain)
        entry = _entry(declared)
        found = _baseline(chain[-1], entry, earlier[index + 1 :])
        if found is None or entry["original"] not in earlier[index + 1 + found].texts:
            return tuple(chain)
        index += 1 + found
        if entry["original"] != chain[-1]:
            chain.append(entry["original"])


def groups_of(
    lineage: Mapping[str, Any], texts: Mapping[str, str], earlier: Sequence[Revision]
) -> list[Group]:
    """Resolve every entry of a lineage, or raise the typed refusal that lists what fails.

    `texts` are the decisions of the revision being verified, by id and in order; `earlier`
    are the stored revisions before it, newest first. A part must be one of the texts, its
    original must be in the revision it replaces, the parts of an original must agree on the
    kind and on the text, a reformulation has one part and a division has two or more.
    """
    problems: list[dict[str, Any]] = [
        {"part": part, "problem": "part_unknown"} for part in lineage if part not in texts
    ]
    built: dict[str, dict[str, Any]] = {}
    for part, text in texts.items():
        if part not in lineage:
            continue
        entry = _entry(lineage[part])
        original = entry["original"]
        found = _baseline(part, entry, earlier)
        if found is None or original not in earlier[found].texts:
            problems.append({"part": part, "original": original, "problem": "original_unknown"})
            continue
        base = earlier[found]
        group = built.setdefault(
            original,
            {
                "kind": entry["kind"],
                "text": base.texts[original],
                "parts": [],
                "ancestry": _ancestry(original, found, earlier),
            },
        )
        if group["kind"] != entry["kind"]:
            problems.append({"part": part, "original": original, "problem": "kind_mismatch"})
        elif group["text"] != base.texts[original]:
            problems.append(
                {"part": part, "original": original, "problem": "original_ambiguous"}
            )
        else:
            group["parts"].append((part, text))
    for original, group in built.items():
        count = len(group["parts"])
        wrong = (
            "reformulation_has_one_part"
            if group["kind"] == "reformulation" and count != 1
            else "split_has_two_or_more_parts"
            if group["kind"] == "split" and count < 2
            else "too_many_parts"
            if count > MAX_PARTS
            else None
        )
        if wrong:
            problems.append({"original": original, "problem": wrong})
    if problems:
        raise AgentRefusal(INVALID_LINEAGE, MESSAGE, problems)
    return [
        Group(
            original,
            group["kind"],
            group["text"],
            tuple(group["parts"]),
            group["ancestry"],
        )
        for original, group in built.items()
    ]
