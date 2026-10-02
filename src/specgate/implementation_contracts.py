"""Portable checked delivery and draft pull request contracts; no state or credentials."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=200)
    passed: bool


class DraftPullRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)
    url: str = Field(pattern=r"^https://github\.com/")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=65536)
    head: str = Field(min_length=1, max_length=200)
    base: str = Field(min_length=1, max_length=200)
    draft: Literal[True]
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")


class ImplementationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    branch: str = Field(pattern=r"^(feat|fix|hotfix|chore|docs|refactor|test|perf)/.+")
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    summary: str = Field(min_length=1, max_length=8000)
    checks: list[CheckResult] = Field(min_length=1, max_length=50)
    pull_request: DraftPullRequest | None = None
    publication_authorization: dict[str, Any] | None = None
