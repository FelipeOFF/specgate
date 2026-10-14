"""A stable id for each implementation decision of a spec, independent of its position.

The id of a decision used to be its index, so splitting, inserting or removing a decision
moved every id after it and dropped the strong verdicts those decisions had earned. The id
now follows the decision itself: the label the spec gives it by convention
(`**noul_per_defect:** ...`), or, with no label, a digest of its normalized text.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from specgate.wording import DECISION_ID_CHANGED, DUPLICATE_DECISION_ID, AgentRefusal

# "**<slug>:**" opens the text of a labelled decision, as the specs of this project write it.
LABEL = re.compile(r"\s*\*\*([A-Za-z0-9][A-Za-z0-9_-]{0,63}):\*\*")
CLAIM_PREFIX = "decision-"
HASHED = "d-"
HASH_LENGTH = 12
MESSAGE = (
    "Duas implementation decisions têm o mesmo id; dê um rótulo diferente a cada uma "
    "ou reescreva uma delas."
)
CHANGED_MESSAGE = (
    "A reparação trocou o id da decisão pesquisada, e os ciclos de pesquisa da claim seguem o id: "
    "mantenha o rótulo `**slug:**` dela, ou divida-a ou reformule-a declarando a linhagem. "
    "Uma decisão sem rótulo muda de id a cada reescrita; dê-lhe um rótulo numa revisão da spec "
    "antes de pesquisá-la."
)


def normalized(text: str) -> str:
    """The text the id digests: Unicode composed and whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def decision_id(text: str) -> str:
    """The label of the decision, or `d-` and the first 12 hex digits of its digest."""
    found = LABEL.match(text)
    if found:
        return found.group(1)
    return HASHED + sha256(normalized(text).encode()).hexdigest()[:HASH_LENGTH]


def decision_ids(texts: Sequence[str]) -> list[str]:
    """One id per decision, in order. Two decisions with the same id are refused.

    The refusal is typed and the agent's to revise: it names each id and the positions
    that share it, and says whether the id came from a label or from identical texts.
    """
    ids = [decision_id(text) for text in texts]
    findings = []
    for identifier in dict.fromkeys(ids):
        positions = [index for index, other in enumerate(ids, 1) if other == identifier]
        if len(positions) > 1:
            labelled = LABEL.match(texts[positions[0] - 1]) is not None
            findings.append(
                {
                    "id": identifier,
                    "positions": positions,
                    "source": "label" if labelled else "text",
                }
            )
    if findings:
        raise AgentRefusal(DUPLICATE_DECISION_ID, MESSAGE, findings)
    return ids


def claim_id(identifier: str) -> str:
    """The id of the fidelity claim of a decision: what the verification and the journal name."""
    return CLAIM_PREFIX + identifier


def keeps_id(
    identifier: str, before: Sequence[str], after: Sequence[str], lineage: Mapping[str, Any]
) -> None:
    """Refuse a repair of a researched decision that took its id from the claim.

    The cycles of a claim are counted under its id, so a repair that rewrites the decision
    without its label starts the count again. It may keep the id, or divide or reformulate
    the decision and say so in the lineage, where the equivalence check guards it. The
    refusal is typed and the agent's to revise, and says whether the decision had a label.
    """
    texts = dict(zip(decision_ids(before), before, strict=True))
    if identifier in decision_ids(after) or any(
        entry.get("original") == identifier for entry in lineage.values()
    ):
        return
    raise AgentRefusal(
        DECISION_ID_CHANGED,
        CHANGED_MESSAGE,
        [
            {
                "target": claim_id(identifier),
                "labelled": LABEL.match(texts.get(identifier, "")) is not None,
            }
        ],
    )
