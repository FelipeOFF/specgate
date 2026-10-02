"""GitHub effects stay in the authorized client, using the installed gh login."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from specgate.privacy import ensure_safe_content
from specgate.spec_contracts import IssueReference, valid_reference


class TrackerError(ValueError):
    pass


@dataclass(frozen=True)
class IssueState:
    id: int | None
    number: int | None
    state: Literal["open", "closed"]
    state_reason: Literal["completed", "not_planned", "reopened", "duplicate"] | None
    external_id: str | None = None
    actionable: bool = True


class Tracker(Protocol):
    repository: str

    async def find(self, marker: str) -> list[IssueReference]: ...
    async def get(self, number: int | str) -> IssueReference: ...
    async def create(self, title: str, body: str) -> IssueReference: ...
    async def find_versions(self, number: int, marker: str) -> list[IssueReference]: ...
    async def get_version(self, number: int, comment_id: int) -> IssueReference: ...
    async def append_version(
        self, number: int, title: str, body: str
    ) -> IssueReference: ...
    async def ensure_label(self, number: int | str) -> IssueReference: ...


class TicketTracker(Tracker, Protocol):
    async def list_sub_issues(self, number: int) -> list[IssueReference]: ...
    async def add_sub_issue(self, number: int, issue_id: int) -> None: ...
    async def list_blockers(self, number: int) -> list[IssueReference]: ...
    async def add_blocker(self, number: int, issue_id: int) -> None: ...
    async def issue_state(self, number: int | str) -> IssueState: ...


async def _gh(args: list[str], payload: dict[str, Any] | None) -> Any:
    process = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(
                json.dumps(payload, ensure_ascii=False).encode()
                if payload is not None
                else None
            ),
            timeout=30,
        )
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise TrackerError(
            "O tracker não confirmou a operação; reconcilie antes de repetir."
        ) from None
    if process.returncode:
        raise TrackerError(
            "O gh não confirmou a operação; confira autenticação e permissões."
        )
    if not stdout.strip() and args[args.index("--method") + 1] == "DELETE":
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        raise TrackerError("O tracker retornou uma resposta inválida.") from None


class GitHubTracker:
    kind = "github"

    def __init__(
        self,
        repository: str,
        *,
        command: Callable[[list[str], dict[str, Any] | None], Awaitable[Any]] = _gh,
        authorize_effect: Callable[[], bool] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Informe o repositório como owner/repo.")
        self.repository, self.command = repository, command
        self.authorize_effect = authorize_effect

    def with_authority(self, check: Callable[[], bool]) -> "GitHubTracker":
        def allowed() -> bool:
            return check() and (self.authorize_effect is None or self.authorize_effect())

        return GitHubTracker(
            self.repository, command=self.command, authorize_effect=allowed
        )

    async def _api(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        paginate: bool = False,
    ) -> Any:
        ensure_safe_content(payload)
        args = [
            f"repos/{self.repository}/{path}",
            "--method",
            method,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2026-03-10",
        ]
        if payload is not None:
            args.extend(["--input", "-"])
        if paginate:
            args.extend(["--paginate", "--slurp"])
        if method != "GET" and self.authorize_effect is not None and not self.authorize_effect():
            raise TrackerError("A autorização mudou antes da escrita no tracker.")
        return await self.command(args, payload)

    def _issue(self, raw: Any, *, related: bool = False) -> IssueReference:
        try:
            if not isinstance(raw, dict) or "pull_request" in raw:
                raise ValueError
            repository = self.repository
            if related:
                match = re.fullmatch(
                    r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/[1-9][0-9]*",
                    raw["html_url"],
                )
                if match is None:
                    raise ValueError
                repository = match[1]
            result = IssueReference(
                repository=repository,
                remote_revision=raw.get("updated_at"),
                id=raw["id"],
                number=raw["number"],
                url=raw["html_url"],
                title=raw["title"],
                body=raw["body"],
                labels=[label["name"] for label in raw["labels"]],
            )
            ensure_safe_content(result.model_dump())
            if not valid_reference(result):
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise TrackerError("O tracker não retornou uma issue válida.") from None

    async def find(self, marker: str) -> list[IssueReference]:
        pages = await self._api(
            "GET",
            "issues?state=all&per_page=100&sort=created&direction=asc",
            paginate=True,
        )
        if not isinstance(pages, list) or any(
            not isinstance(page, list) for page in pages
        ):
            raise TrackerError("A varredura do tracker não foi concluída.")
        matches = []
        for page in pages:
            for raw in page:
                if not isinstance(raw, dict):
                    raise TrackerError(
                        "A varredura do tracker retornou um item inválido."
                    )
                body = raw.get("body") or ""
                if not isinstance(body, str):
                    raise TrackerError(
                        "A varredura do tracker retornou conteúdo inválido."
                    )
                if "pull_request" not in raw and marker in body:
                    matches.append(self._issue(raw))
        return matches

    async def get(self, number: int | str) -> IssueReference:
        number = int(number)
        issue = self._issue(await self._api("GET", f"issues/{number}"))
        if issue.number != number:
            raise TrackerError("O tracker retornou outra issue.")
        return issue

    async def create(self, title: str, body: str) -> IssueReference:
        return self._issue(
            await self._api(
                "POST",
                "issues",
                {"title": title, "body": body, "labels": ["ready-for-agent"]},
            )
        )

    async def update(
        self, previous: IssueReference, title: str, body: str
    ) -> IssueReference:
        current = await self.get(previous.lookup_id)
        if (
            current.identity_key != previous.identity_key
            or current.id != previous.id
            or current.title != previous.title
            or current.body != previous.body
        ):
            raise TrackerError("A issue mudou desde a revisão; preserve a versão remota.")
        await self._api(
            "PATCH", f"issues/{previous.legacy_number}", {"title": title, "body": body}
        )
        return await self.get(previous.lookup_id)

    async def remove_blocker(self, number: int, issue_id: int) -> None:
        await self._api(
            "DELETE", f"issues/{number}/dependencies/blocked_by/{issue_id}"
        )

    def _comment(self, issue: IssueReference, raw: Any) -> IssueReference:
        try:
            if (
                raw["issue_url"].casefold()
                != f"https://api.github.com/repos/{self.repository}/issues/{issue.number}".casefold()
            ):
                raise ValueError
            heading, separator, body = raw["body"].partition("\n\n")
            if not separator or not heading.startswith("# ") or "\n" in heading:
                raise ValueError
            result = IssueReference.model_validate(
                {
                    **issue.model_dump(),
                    "comment_id": raw["id"],
                    "remote_revision": raw.get("updated_at"),
                    "comment_url": raw["html_url"],
                    "title": heading[2:],
                    "body": body,
                }
            )
            if not valid_reference(result):
                raise ValueError
            ensure_safe_content(result.model_dump())
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TrackerError(
                "O tracker não retornou uma versão válida da spec."
            ) from None

    async def find_versions(self, number: int, marker: str) -> list[IssueReference]:
        issue = await self.get(number)
        pages = await self._api(
            "GET", f"issues/{number}/comments?per_page=100", paginate=True
        )
        if not isinstance(pages, list) or any(
            not isinstance(page, list) for page in pages
        ):
            raise TrackerError("A varredura das versões não foi concluída.")
        matches = []
        for page in pages:
            for raw in page:
                if not isinstance(raw, dict) or not isinstance(raw.get("body"), str):
                    raise TrackerError(
                        "A varredura das versões retornou conteúdo inválido."
                    )
                if marker in raw["body"]:
                    matches.append(self._comment(issue, raw))
        return matches

    async def get_version(self, number: int, comment_id: int) -> IssueReference:
        issue = await self.get(number)
        version = self._comment(
            issue, await self._api("GET", f"issues/comments/{comment_id}")
        )
        if version.comment_id != comment_id:
            raise TrackerError("O tracker retornou outra versão.")
        return version

    async def append_version(
        self, number: int, title: str, body: str
    ) -> IssueReference:
        issue = await self.get(number)
        raw = await self._api(
            "POST", f"issues/{number}/comments", {"body": f"# {title}\n\n{body}"}
        )
        return self._comment(issue, raw)

    async def ensure_label(self, number: int | str) -> IssueReference:
        number = int(number)
        await self._api(
            "POST", f"issues/{number}/labels", {"labels": ["ready-for-agent"]}
        )
        return await self.get(number)

    def _issue_pages(self, pages: Any, operation: str) -> list[IssueReference]:
        if not isinstance(pages, list) or any(
            not isinstance(page, list) for page in pages
        ):
            raise TrackerError(f"A consulta de {operation} não foi concluída.")
        return [self._issue(raw, related=True) for page in pages for raw in page]

    async def list_sub_issues(self, number: int) -> list[IssueReference]:
        pages = await self._api(
            "GET", f"issues/{number}/sub_issues?per_page=100", paginate=True
        )
        return self._issue_pages(pages, "sub-issues")

    async def add_sub_issue(self, number: int, issue_id: int) -> None:
        await self._api(
            "POST",
            f"issues/{number}/sub_issues",
            {"sub_issue_id": issue_id, "replace_parent": False},
        )

    async def list_blockers(self, number: int) -> list[IssueReference]:
        pages = await self._api(
            "GET",
            f"issues/{number}/dependencies/blocked_by?per_page=100",
            paginate=True,
        )
        return self._issue_pages(pages, "dependências")

    async def add_blocker(self, number: int, issue_id: int) -> None:
        await self._api(
            "POST",
            f"issues/{number}/dependencies/blocked_by",
            {"issue_id": issue_id},
        )

    async def issue_state(self, number: int | str) -> IssueState:
        raw = await self._api("GET", f"issues/{number}")
        issue = self._issue(raw)
        try:
            return IssueState(
                id=issue.legacy_id,
                number=issue.legacy_number,
                state=raw["state"],
                state_reason=raw.get("state_reason"),
                external_id=issue.external_id,
            )
        except (KeyError, TypeError, ValueError):
            raise TrackerError("O tracker não retornou o estado da issue.") from None
