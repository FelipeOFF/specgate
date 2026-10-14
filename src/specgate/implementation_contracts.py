"""Portable checked delivery and draft pull request contracts; no state or credentials."""

from typing import Any, Literal, Protocol

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


class MergedPullRequest(BaseModel):
    """How the forge says a PR entered its base; a PR that merged is no draft to look at."""

    model_config = ConfigDict(extra="forbid", strict=True)
    # The head the PR had when it merged: a push after the merge does not change it.
    merged_head: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    base: str = Field(min_length=1, max_length=200)
    # Only a merge commit keeps the commits a dependent was built on in the history.
    method: Literal["merge", "squash", "rebase"]


class PullRequestPublisher(Protocol):
    repository: str

    async def find(self, marker: str) -> list[DraftPullRequest]: ...
    async def create(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        commit_sha: str,
    ) -> DraftPullRequest: ...
    async def merged(self, number: int) -> bool: ...
    # None when the forge cannot say how it merged. A forge that is down raises OSError.
    async def merged_pull_request(self, number: int) -> MergedPullRequest | None: ...
    async def retarget(self, number: int, base: str) -> DraftPullRequest: ...


class ImplementationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    branch: str = Field(pattern=r"^(feat|fix|hotfix|chore|docs|refactor|test|perf)/.+")
    commit_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    summary: str = Field(min_length=1, max_length=8000)
    checks: list[CheckResult] = Field(min_length=1, max_length=50)
    pull_request: DraftPullRequest | None = None
    publication_authorization: dict[str, Any] | None = None
