"""Portable spec content, references and publication markers; no state or credentials."""

import re
from hashlib import sha256
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from specgate.grill_contracts import GrillError, _json
from specgate.shared.domain.inputs import checked_text


class SpecDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    problem_statement: str = Field(min_length=1)
    solution: str = Field(min_length=1)
    user_stories: list[str] = Field(min_length=1)
    implementation_decisions: list[str] = Field(min_length=1)
    testing_decisions: list[str] = Field(min_length=1)
    out_of_scope: list[str] = Field(min_length=1)
    further_notes: str = Field(min_length=1)
    decision_ids: list[str] = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)

    def render(self) -> str:
        sections = [
            ("Problem Statement", self.problem_statement),
            ("Solution", self.solution),
            (
                "User Stories",
                "\n".join(f"{i}. {s}" for i, s in enumerate(self.user_stories, 1)),
            ),
            (
                "Implementation Decisions",
                "\n".join(f"- {s}" for s in self.implementation_decisions),
            ),
            ("Testing Decisions", "\n".join(f"- {s}" for s in self.testing_decisions)),
            ("Out of Scope", "\n".join(f"- {s}" for s in self.out_of_scope)),
            ("Further Notes", self.further_notes),
        ]
        for _, content in sections:
            checked_text(content)
        if any(
            not item.strip()
            for items in (
                self.user_stories,
                self.implementation_decisions,
                self.testing_decisions,
                self.out_of_scope,
            )
            for item in items
        ):
            raise GrillError(
                "invalid_spec", "Preencha todas as decisões e histórias da spec."
            )
        if any(
            re.search(r"(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]+\b|```", item)
            for item in self.implementation_decisions
        ):
            raise GrillError(
                "implementation_paths",
                "Descreva decisões sem caminhos de implementação ou código.",
            )
        return checked_text(
            "\n\n".join(f"## {title}\n\n{content}" for title, content in sections)
        )


class SpecAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    seams_approved: bool = False
    origin: Literal["human", "automated"] = "human"
    delegation_revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    project: str | None = None
    repositories: list[str] = Field(default_factory=list)
    publication_authorized: bool = False
    reference: str = Field(default="", max_length=2000)


class IssueReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tracker: str = Field(default="github", min_length=1, max_length=80)
    project: str = Field(default="", max_length=500)
    external_id: str = Field(default="", max_length=500)
    remote_revision: str | None = Field(default=None, min_length=1, max_length=500)
    # Additive compatibility: these fields belong to legacy adapters, not identity.
    repository: str = ""
    id: int | None = Field(default=None, gt=0)
    number: int | None = Field(default=None, gt=0)
    url: str | None = None
    title: str
    body: str
    labels: list[str]
    comment_id: int | None = Field(default=None, gt=0)
    comment_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def legacy_reference(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if "tracker" not in value:
            url = value.get("url") or ""
            if not isinstance(url, str):
                return value
            if url.startswith("specgate://"):
                value["tracker"] = urlsplit(url).netloc
            elif url.startswith("https://") and urlsplit(url).netloc != "github.com":
                value["tracker"] = "gitlab"
        value.setdefault("project", value.get("repository", ""))
        value.setdefault("repository", value.get("project", ""))
        if "external_id" not in value and value.get("number") is not None:
            value["external_id"] = str(value["number"])
        return value

    @model_validator(mode="after")
    def identity(self) -> Self:
        if (
            not self.project.strip()
            or not self.external_id.strip()
            or (
                self.tracker != "jira"
                and self.project.casefold() != self.repository.casefold()
            )
        ):
            raise ValueError("Informe projeto e ID externo consistentes.")
        if self.tracker == "github" and (
            self.id is None
            or self.number is None
            or self.external_id != str(self.number)
        ):
            raise ValueError("A referência GitHub exige seus IDs originais.")
        return self

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return self.tracker, self.project.casefold(), self.external_id

    @property
    def lookup_id(self) -> int | str:
        return self.legacy_number if self.tracker == "github" else self.external_id

    @property
    def legacy_number(self) -> int:
        if self.number is None:
            raise ValueError("O adapter exige uma referência numérica legada.")
        return self.number

    @property
    def legacy_id(self) -> int:
        if self.id is None:
            raise ValueError("O adapter exige um ID numérico legado.")
        return self.id


def valid_reference(issue: IssueReference) -> bool:
    url = urlsplit(issue.url or "")
    return (
        issue.tracker == "github"
        and issue.id is not None
        and issue.number is not None
        and issue.external_id == str(issue.number)
        and issue.project.casefold() == issue.repository.casefold()
        and url.scheme == "https"
        and url.netloc == "github.com"
        and url.path.casefold()
        == f"/{issue.repository}/issues/{issue.number}".casefold()
        and not url.query
        and not url.fragment
        and (
            issue.comment_url == f"{issue.url}#issuecomment-{issue.comment_id}"
            if issue.comment_id is not None
            else issue.comment_url is None
        )
    )


def _fingerprint(value: Any) -> str:
    return sha256(checked_text(_json(value)).encode()).hexdigest()


def source_revision(state: dict[str, Any]) -> str:
    # References and answers define material source changes; metadata writes do not.
    return sha256(
        _json({key: state[key] for key in ("objective", "context", "turns")}).encode()
    ).hexdigest()


def publication_body(state: dict[str, Any]) -> str:
    body: str = state["spec"]["body"]
    return (
        body
        + f"\n\n<!-- dev-decision-spec:{state['id']} -->\n{publication_marker(state)}\n"
    )


def publication_marker(state: dict[str, Any]) -> str:
    return (
        f"<!-- dev-decision-spec-version:{state['id']}:{state['spec']['revision']} -->"
    )
