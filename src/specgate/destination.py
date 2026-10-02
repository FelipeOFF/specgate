"""Project tracker destination. Credentials stay in the harness, never here."""

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from specgate.privacy import ensure_safe_content
from specgate.spec_contracts import IssueReference
from specgate.tracker import IssueState, TrackerError
from specgate.workflow_policy import WorkflowPolicy

DESTINATIONS = ("github", "gitlab", "jira", "beads", "local", "freeform")
_LOCAL = frozenset({"local", "freeform"})
_CREDENTIAL = re.compile(r"(token|password|api_key|secret)=", re.IGNORECASE)


class DestinationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def destination_capabilities(destination: str) -> dict[str, Any]:
    # GitLab retains the existing harness-supplied adapter contract.
    if destination not in {"github", "gitlab", "jira", "beads", "local", "freeform"}:
        raise DestinationError("destination_adapter_missing")
    return {
        "destination": destination,
        "reference_contract": 2,
        "publish": True,
        "reconcile": True,
        "spec_versions": destination in {"github", "jira", "beads"},
        "spec_version_mode": "replace_with_readback"
        if destination in {"jira", "beads"}
        else "comment"
        if destination == "github"
        else None,
        "ticket_graph": True,
        "native_sub_issues": destination == "github",
        "native_dependencies": destination in {"github", "jira", "beads"},
    }


def read_project_destination(project: os.PathLike[str]) -> str:
    """Return the project destination. A missing file keeps the GitHub default."""
    root = os.fspath(project)
    config = os.path.join(root, ".specgate")
    path = os.path.join(config, "tracker.json")
    if os.path.islink(config) or os.path.islink(path):
        raise DestinationError("destination_invalid")
    if not os.path.isfile(path):
        return "github"
    if os.path.getsize(path) > 4096:
        raise DestinationError("destination_invalid")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, json.JSONDecodeError):
        raise DestinationError("destination_invalid") from None
    if not isinstance(data, dict) or set(data) != {"destination"}:
        raise DestinationError("destination_invalid")
    destination = data["destination"]
    if not isinstance(destination, str) or destination not in DESTINATIONS:
        raise DestinationError("destination_invalid")
    return destination


def resolve_destination(
    project: os.PathLike[str] | None,
    destination: str | None,
    *,
    policy: dict[str, Any] | None = None,
) -> str:
    if destination == "issue":
        destination = "github"
    if policy is not None:
        selected = WorkflowPolicy.model_validate(policy).tracker
        if destination is not None and destination != selected:
            raise DestinationError("workflow_policy_change_required")
        return selected
    if destination is not None:
        if destination not in DESTINATIONS:
            raise DestinationError("destination_invalid")
        return destination
    if project is not None:
        return read_project_destination(project)
    return "github"


def valid_project_reference(issue: IssueReference, destination: str) -> bool:
    if destination not in DESTINATIONS or destination == "github":
        return False
    if issue.tracker != destination:
        return False
    if issue.url is None:
        return bool(issue.project.strip() and issue.external_id.strip())
    url = urlsplit(issue.url)
    if (
        url.username
        or url.password
        or url.query
        or url.fragment
        or _CREDENTIAL.search(issue.url)
    ):
        return False
    if destination in _LOCAL:
        return (
            url.scheme == "specgate"
            and url.netloc == destination
            and url.path.casefold()
            == f"/{issue.repository}/items/{issue.number}".casefold()
        )
    return (
        url.scheme == "https"
        and bool(url.netloc)
        and url.netloc.casefold() != "github.com"
        and bool(url.path)
    )


class ProjectTracker:
    """Local documents or a free-form workflow. No tracker credential is used."""

    def __init__(self, project: os.PathLike[str], kind: str, repository: str) -> None:
        if kind not in _LOCAL:
            raise DestinationError("destination_invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise DestinationError("destination_invalid")
        self.project, self.kind, self.repository = project, kind, repository

    def _path(self) -> str:
        safe = self.repository.replace("/", "__")
        return os.path.join(
            os.fspath(self.project),
            ".specgate",
            "trackers",
            self.kind,
            safe,
            "issues.json",
        )

    def _read(self) -> list[dict[str, Any]]:
        path = self._path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.loads(handle.read())
        except (OSError, json.JSONDecodeError):
            raise TrackerError(
                "The local destination did not return a valid list."
            ) from None
        if not isinstance(data, list):
            raise TrackerError("The local destination did not return a valid list.")
        return data

    def _write(self, issues: list[dict[str, Any]]) -> None:
        path = self._path()
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        blob = json.dumps(issues, ensure_ascii=False, indent=2)
        ensure_safe_content(blob)
        temporary = f"{path}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(blob)
        except OSError:
            os.unlink(temporary)
            raise
        os.replace(temporary, path)

    def _reference(self, raw: Any) -> IssueReference:
        try:
            issue = IssueReference.model_validate(raw)
        except ValidationError:
            raise TrackerError(
                "The local destination did not return a valid reference."
            ) from None
        if (
            issue.repository.casefold() != self.repository.casefold()
            or not valid_project_reference(issue, self.kind)
        ):
            raise TrackerError(
                "The local destination did not return a valid reference."
            )
        return issue

    def _url(self, number: int) -> str:
        return f"specgate://{self.kind}/{self.repository}/items/{number}"

    async def find(self, marker: str) -> list[IssueReference]:
        return [
            self._reference(raw)
            for raw in self._read()
            if isinstance(raw, dict)
            and isinstance(raw.get("body"), str)
            and marker in raw["body"]
        ]

    async def get(self, number: int | str) -> IssueReference:
        number = int(number)
        issues = self._read()
        if number < 1 or number > len(issues):
            raise TrackerError("The local destination has no such reference.")
        issue = self._reference(issues[number - 1])
        if issue.number != number:
            raise TrackerError("The local destination returned another reference.")
        return issue

    async def create(self, title: str, body: str) -> IssueReference:
        issues = self._read()
        number = len(issues) + 1
        issue = IssueReference(
            repository=self.repository,
            id=number,
            number=number,
            url=self._url(number),
            title=title,
            body=body,
            labels=[],
        )
        ensure_safe_content(issue.model_dump())
        issues.append(issue.model_dump())
        self._write(issues)
        return issue

    async def ensure_label(self, number: int | str) -> IssueReference:
        number = int(number)
        issues = self._read()
        current = self._reference(issues[number - 1])
        updated = current.model_copy(
            update={"labels": list(dict.fromkeys([*current.labels, "ready-for-agent"]))}
        )
        issues[number - 1] = updated.model_dump()
        self._write(issues)
        return updated

    async def find_versions(self, number: int, marker: str) -> list[IssueReference]:
        issue = await self.get(number)
        if marker not in issue.body:
            return []
        raise TrackerError("This destination does not version a spec by comment.")

    async def get_version(self, number: int, comment_id: int) -> IssueReference:
        await self.get(number)
        if comment_id <= 0:
            raise TrackerError("The local destination has no such reference.")
        raise TrackerError("This destination does not version a spec by comment.")

    async def append_version(
        self, number: int, title: str, body: str
    ) -> IssueReference:
        await self.get(number)
        ensure_safe_content({"title": title, "body": body})
        raise TrackerError("This destination does not version a spec by comment.")

    async def list_sub_issues(self, number: int) -> list[IssueReference]:
        await self.get(number)
        return []

    async def add_sub_issue(self, number: int, issue_id: int) -> None:
        await self.get(number)
        if issue_id <= 0:
            raise TrackerError("The local destination has no such reference.")

    async def list_blockers(self, number: int) -> list[IssueReference]:
        await self.get(number)
        return []

    async def add_blocker(self, number: int, issue_id: int) -> None:
        await self.get(number)
        if issue_id <= 0:
            raise TrackerError("The local destination has no such reference.")

    async def issue_state(self, number: int | str) -> IssueState:
        issue = await self.get(number)
        return IssueState(
            id=issue.legacy_id,
            number=issue.legacy_number,
            state="open",
            state_reason=None,
            external_id=issue.external_id,
        )
