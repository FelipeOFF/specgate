"""One authorized Jira Cloud project and issue type; credentials stay in the harness."""

import re
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Any
from urllib.parse import urlencode, urlsplit

from specgate.privacy import ensure_safe_content
from specgate.spec_contracts import IssueReference
from specgate.tracker import TrackerError

JiraRequest = Callable[[str, str, dict[str, Any] | None], Awaitable[Any]]


def adf(body: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "codeBlock", "content": [{"type": "text", "text": body}]}],
    }


def description(document: Any) -> str:
    try:
        block = document["content"][0]
        if (
            document["type"] != "doc"
            or document["version"] != 1
            or len(document["content"]) != 1
            or block["type"] != "codeBlock"
        ):
            raise ValueError
        text = "".join(
            item["text"] for item in block["content"] if item["type"] == "text"
        )
        if adf(text) != document:
            raise ValueError
        return text
    except (KeyError, TypeError, ValueError, IndexError):
        raise TrackerError(
            "A descrição remota mudou de formato; preserve a edição do Jira."
        ) from None


def searchable_text(node: Any) -> str:
    if not isinstance(node, dict):
        raise TrackerError("A busca no Jira retornou uma descrição inválida.")
    if node.get("type") == "text":
        text = node.get("text")
        if not isinstance(text, str):
            raise TrackerError("A busca no Jira retornou texto inválido.")
        return text
    children = node.get("content", [])
    if not isinstance(children, list):
        raise TrackerError("A busca no Jira retornou conteúdo inválido.")
    return "".join(searchable_text(child) for child in children)


class JiraSpecTracker:
    kind = "jira"
    artifact_label = "specgate-spec"

    def __init__(
        self,
        repository: str,
        project: str,
        issue_type: str,
        site: str,
        *,
        request: JiraRequest,
        authorize_effect: Callable[[], bool] | None = None,
    ) -> None:
        parsed = urlsplit(site)
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", project)
            or not re.fullmatch(r"[0-9]+", issue_type)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Fixe repositório, projeto, tipo de issue e site HTTPS autorizados."
            )
        self.repository, self.project, self.issue_type = repository, project, issue_type
        self.site, self.request, self.authorize_effect = (
            site.rstrip("/"),
            request,
            authorize_effect,
        )

    def with_authority(self, check: Callable[[], bool]) -> "JiraSpecTracker":
        return JiraSpecTracker(
            self.repository,
            self.project,
            self.issue_type,
            self.site,
            request=self.request,
            authorize_effect=lambda: (
                check() and (self.authorize_effect is None or self.authorize_effect())
            ),
        )

    async def _api(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        ensure_safe_content(payload)
        if method != "GET" and (
            self.authorize_effect is None or not self.authorize_effect()
        ):
            raise TrackerError(
                "A escrita no Jira exige autorização atual para esta entrega."
            )
        try:
            return await self.request(method, "/rest/api/3/" + path, payload)
        except (TimeoutError, OSError):
            raise TrackerError(
                "O Jira não confirmou a operação; reconcilie a intenção antes de repetir."
            ) from None

    async def capabilities(self) -> dict[str, Any]:
        permission_query = urlencode(
            {
                "projectKey": self.project,
                "permissions": "BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES",
            }
        )
        permissions = await self._api("GET", "mypermissions?" + permission_query)
        if not all(
            permissions.get("permissions", {}).get(key, {}).get("havePermission")
            is True
            for key in ("BROWSE_PROJECTS", "CREATE_ISSUES", "EDIT_ISSUES")
        ):
            raise TrackerError(
                "O projeto Jira não permite consultar, criar e editar a spec."
            )
        fields: dict[str, Any] = {}
        start = 0
        while True:
            page = await self._api(
                "GET",
                f"issue/createmeta/{self.project}/issuetypes/{self.issue_type}?startAt={start}&maxResults=100",
            )
            rows = page.get("fields", page.get("results"))
            total = page.get("total")
            if (
                not isinstance(rows, list)
                or type(total) is not int
                or total < 0
                or page.get("startAt") != start
            ):
                raise TrackerError("O Jira não confirmou os campos do tipo de issue.")
            fields.update({row["fieldId"]: row for row in rows})
            if start + len(rows) >= total:
                break
            if not rows:
                raise TrackerError("A consulta de capabilities não terminou.")
            start += len(rows)
        supported = {"project", "issuetype", "summary", "description", "labels"}
        if not {"summary", "description", "labels"} <= fields.keys() or any(
            row.get("required")
            and name not in supported
            and not row.get("hasDefaultValue")
            for name, row in fields.items()
        ):
            raise TrackerError(
                "O tipo de issue requer campos sem mapeamento autorizado."
            )
        return {
            "destination": "jira",
            "reference_contract": 2,
            "project": self.project,
            "issue_type": self.issue_type,
            "publish": True,
            "reconcile": True,
            "spec_versions": True,
            "spec_version_mode": "replace_with_readback",
            "ticket_graph": False,
            "native_sub_issues": False,
            "native_dependencies": False,
        }

    def _issue(self, raw: Any) -> IssueReference:
        try:
            fields = raw["fields"]
            key = raw["key"]
            if (
                fields["project"]["key"] != self.project
                or fields["issuetype"]["id"] != self.issue_type
                or not re.fullmatch(re.escape(self.project) + r"-[1-9][0-9]*", key)
            ):
                raise ValueError
            body = description(fields["description"])
            title, updated = fields["summary"], fields["updated"]
            if not isinstance(updated, str) or not updated:
                raise ValueError
            digest = sha256((title + "\0" + body).encode()).hexdigest()
            result = IssueReference(
                tracker="jira",
                repository=self.repository,
                project=self.project,
                external_id=key,
                remote_revision=updated + ":" + digest,
                url=f"{self.site}/browse/{key}",
                title=title,
                body=body,
                labels=fields["labels"],
            )
            ensure_safe_content(result.model_dump())
            return result
        except (KeyError, TypeError, ValueError):
            raise TrackerError(
                "O Jira não retornou a referência e o conteúdo esperados."
            ) from None

    async def get(self, number: int | str) -> IssueReference:
        if not isinstance(number, str) or not re.fullmatch(
            re.escape(self.project) + r"-[1-9][0-9]*", number
        ):
            raise TrackerError("A chave pertence a outro projeto Jira.")
        issue = self._issue(
            await self._api(
                "GET",
                f"issue/{number}?fields=summary,description,project,issuetype,updated,labels",
            )
        )
        if issue.external_id != number:
            raise TrackerError("A consulta retornou outra issue Jira.")
        return issue

    async def find(self, marker: str) -> list[IssueReference]:
        matches = []
        token = None
        seen = set()
        while True:
            query = {
                "jql": f'project = "{self.project}" AND issuetype = "{self.issue_type}" AND labels = "{self.artifact_label}"',
                "maxResults": "100",
                "fields": "summary,description,project,issuetype,updated,labels",
            }
            if token:
                query["nextPageToken"] = token
            page = await self._api("GET", "search/jql?" + urlencode(query))
            if not isinstance(page.get("issues"), list) or not isinstance(
                page.get("isLast"), bool
            ):
                raise TrackerError("A busca no Jira não foi concluída.")
            for raw in page["issues"]:
                document = raw.get("fields", {}).get("description")
                if document is not None and marker in searchable_text(document):
                    matches.append(self._issue(raw))
            if page["isLast"]:
                return matches
            token = page.get("nextPageToken")
            if not isinstance(token, str) or not token or token in seen:
                raise TrackerError("A paginação do Jira não foi concluída.")
            seen.add(token)

    async def create(self, title: str, body: str) -> IssueReference:
        await self.capabilities()
        result = await self._api(
            "POST",
            "issue",
            {
                "fields": {
                    "project": {"key": self.project},
                    "issuetype": {"id": self.issue_type},
                    "summary": title,
                    "description": adf(body),
                    "labels": [self.artifact_label, "ready-for-agent"],
                }
            },
        )
        return await self.get(result["key"])

    async def update(
        self, previous: IssueReference, title: str, body: str
    ) -> IssueReference:
        current = await self.get(previous.lookup_id)
        if (
            current.identity_key != previous.identity_key
            or current.remote_revision != previous.remote_revision
            or current.title != previous.title
            or current.body != previous.body
        ):
            raise TrackerError(
                "A spec mudou no Jira desde a revisão; preserve a versão remota."
            )
        metadata = await self._api("GET", f"issue/{current.external_id}/editmeta")
        if not {"summary", "description"} <= metadata.get("fields", {}).keys():
            raise TrackerError("O Jira não permite atualizar os campos da spec.")
        await self._api(
            "PUT",
            f"issue/{current.external_id}",
            {"fields": {"summary": title, "description": adf(body)}},
        )
        return await self.get(current.lookup_id)

    async def ensure_label(self, number: int | str) -> IssueReference:
        current = await self.get(number)
        if "ready-for-agent" not in current.labels:
            raise TrackerError(
                "A label da spec foi alterada no Jira; revise a edição remota."
            )
        return current

    async def find_versions(self, number: int, marker: str) -> list[IssueReference]:
        raise TrackerError(
            "Jira atualiza a descrição; não cria versões como comentários GitHub."
        )

    async def get_version(self, number: int, comment_id: int) -> IssueReference:
        raise TrackerError("Jira não usa IDs de comentários como versões da spec.")

    async def append_version(
        self, number: int, title: str, body: str
    ) -> IssueReference:
        raise TrackerError("Use a atualização com confronto da revisão remota do Jira.")
