"""A recusa tipada de um texto que o agente escreveu, distinta da escalada humana.

O agente reescreve ou divide o texto que escreveu; o Jev só julgou. Três recusas têm essa
forma: a formulação defeituosa, a divisão ou reformulação que acrescenta ou perde conteúdo
e a identidade ou a linhagem de decisão que não se sustenta, inclusive a reparação que troca
o id da decisão pesquisada. A reescrita delegada de uma spec aprovada ganha a que passa do
que a operação cobre. A ação fora do conjunto permitido, a ação que o agente não registrou e o
reenvio de um conteúdo já rejeitado têm a mesma forma.
"""

from typing import Any

from specgate.grill_contracts import GrillError

WORDING_REJECTED = "wording_rejected"
EQUIVALENCE_REJECTED = "equivalence_rejected"
DUPLICATE_DECISION_ID = "duplicate_decision_id"
INVALID_LINEAGE = "invalid_decision_lineage"
DECISION_ID_CHANGED = "decision_id_changed"
ACTION_NOT_ALLOWED = "action_not_allowed"
ACTION_NOT_REGISTERED = "action_not_registered"
IDENTICAL_RESUBMISSION = "identical_resubmission"
REWRITE_OUT_OF_SCOPE = "rewrite_out_of_scope"
# Every code whose result is the agent's to revise, never a person's to decide.
AGENT_CODES = frozenset(
    {
        WORDING_REJECTED,
        EQUIVALENCE_REJECTED,
        DUPLICATE_DECISION_ID,
        INVALID_LINEAGE,
        DECISION_ID_CHANGED,
        ACTION_NOT_ALLOWED,
        ACTION_NOT_REGISTERED,
        IDENTICAL_RESUBMISSION,
        REWRITE_OUT_OF_SCOPE,
    }
)
AGENT_REVISION = "agent_revision"
MESSAGE = (
    "A checagem de formulação recusou o texto; siga next_actions de cada achado: "
    "reformule, divida ou abra uma nova pergunta de grill."
)


class AgentRefusal(GrillError):
    """A refusal the agent answers by revising its own text, with a finding per text."""

    def __init__(self, code: str, message: str, findings: list[dict[str, Any]]) -> None:
        super().__init__(code, message)
        self.findings = findings

    def refusal(self) -> dict[str, Any]:
        """What the agent receives: not needs_human, and no research opens."""
        return typed_refusal(self.code, str(self), self.findings)


class WordingRejected(AgentRefusal):
    """Uma ou mais formulações recusadas pela checagem, com as ações permitidas."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        super().__init__(WORDING_REJECTED, MESSAGE, findings)


EQUIVALENCE_MESSAGE = (
    "A checagem de equivalência recusou as partes; siga next_actions de cada achado: "
    "revise as partes para que nenhuma afirme o que o original não afirma e todas "
    "cubram o que ele afirma."
)


class EquivalenceRejected(AgentRefusal):
    """One or more divisions or reformulations that add or lose content, with the defects."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        super().__init__(EQUIVALENCE_REJECTED, EQUIVALENCE_MESSAGE, findings)


REWRITE_MESSAGE = (
    "A reescrita delegada troca só as implementation decisions da spec aprovada: cada "
    "decisão que muda declara a linhagem, e as outras seções e a aprovação ficam como "
    "estavam. Corrija o achado e envie de novo."
)


class RewriteOutOfScope(AgentRefusal):
    """A rewrite of an approved spec that changes more than the operation covers."""

    def __init__(self, findings: list[dict[str, Any]]) -> None:
        super().__init__(REWRITE_OUT_OF_SCOPE, REWRITE_MESSAGE, findings)


def typed_refusal(
    code: str, message: str, findings: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "action": AGENT_REVISION,
        "auto_advance": False,
        "error": {"code": code, "message": message, "findings": findings},
    }


def refusal(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """O que o agente recebe: não é needs_human e não abre pesquisa."""
    return typed_refusal(WORDING_REJECTED, MESSAGE, findings)
