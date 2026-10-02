"""Jira ticket content and pinned native links for one authorized project."""

import json
import re
from collections.abc import Callable
from copy import copy
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import urlencode

from specgate.jira_tracker import JiraSpecTracker
from specgate.spec_contracts import IssueReference
from specgate.tracker import IssueState, TrackerError


@dataclass(frozen=True)
class JiraLink:
    id: str
    name: str
    inward: str
    outward: str
    target_side: Literal["inward", "outward"]

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[0-9]+", self.id)
            or not all(
                isinstance(value, str) and value.strip()
                for value in (self.name, self.inward, self.outward)
            )
            or self.target_side not in {"inward", "outward"}
        ):
            raise ValueError("Fixe o tipo e a direção de cada relação Jira.")

    def matches(self, raw: dict[str, Any]) -> bool:
        return all(
            raw.get(key) == value
            for key, value in asdict(self).items()
            if key != "target_side"
        )


class JiraTicketTracker(JiraSpecTracker):
    artifact_label = "specgate-ticket"

    def __init__(
        self,
        *args: Any,
        blocker_link: JiraLink,
        spec_link: JiraLink,
        completed_resolution_ids: frozenset[str],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if blocker_link.id == spec_link.id:
            raise ValueError(
                "A spec e os bloqueadores exigem tipos de relação distintos."
            )
        if not completed_resolution_ids or any(
            not re.fullmatch(r"[0-9]+", item) for item in completed_resolution_ids
        ):
            raise ValueError(
                "Configure os IDs de resolução que significam trabalho concluído."
            )
        self.completed_resolution_ids = frozenset(completed_resolution_ids)
        self.blocker_link, self.spec_link = blocker_link, spec_link

    def with_authority(self, check: Callable[[], bool]) -> "JiraTicketTracker":
        result = copy(self)
        result.authorize_effect = lambda: (
            check() and (self.authorize_effect is None or self.authorize_effect())
        )
        return result

    @property
    def publication_scope(self) -> dict[str, str]:
        links = json.dumps(
            [
                asdict(self.blocker_link),
                asdict(self.spec_link),
                sorted(self.completed_resolution_ids),
            ],
            sort_keys=True,
        )
        return {
            "tracker": "jira",
            "project": self.project,
            "site": self.site,
            "issue_type": self.issue_type,
            "relations_revision": sha256(links.encode()).hexdigest(),
        }

    async def capabilities(self) -> dict[str, Any]:
        result = await super().capabilities()
        permission = await self._api(
            "GET",
            "mypermissions?"
            + urlencode({"projectKey": self.project, "permissions": "LINK_ISSUES"}),
        )
        if (
            permission.get("permissions", {})
            .get("LINK_ISSUES", {})
            .get("havePermission")
            is not True
        ):
            raise TrackerError("O projeto Jira não permite vincular tickets.")
        types = (await self._api("GET", "issueLinkType")).get("issueLinkTypes", [])
        if any(
            sum(link.matches(raw) for raw in types) != 1
            for link in (self.blocker_link, self.spec_link)
        ):
            raise TrackerError(
                "Os tipos de relação Jira mudaram ou estão indisponíveis."
            )
        resolutions = await self._api("GET", "resolution")
        if not isinstance(resolutions, list) or not self.completed_resolution_ids <= {
            row.get("id") for row in resolutions
        }:
            raise TrackerError(
                "O Jira não confirmou as resoluções de conclusão configuradas."
            )
        return {
            **result,
            "ticket_graph": True,
            "native_dependencies": True,
            "spec_relation": asdict(self.spec_link),
            "blocker_relation": asdict(self.blocker_link),
            "publication_scope": self.publication_scope,
        }

    def validate_parent(self, parent: IssueReference) -> None:
        if (
            parent.tracker != "jira"
            or parent.repository.casefold() != self.repository.casefold()
            or parent.project != self.project
            or parent.url != f"{self.site}/browse/{parent.external_id}"
        ):
            raise TrackerError("A spec pertence a outro site ou projeto Jira.")

    async def _links(self, issue: IssueReference, link: JiraLink) -> dict[str, str]:
        raw = await self._api("GET", f"issue/{issue.external_id}?fields=issuelinks")
        if raw.get("key") != issue.external_id:
            raise TrackerError("A consulta retornou relações de outro ticket.")
        rows = raw.get("fields", {}).get("issuelinks")
        if not isinstance(rows, list):
            raise TrackerError("O Jira não confirmou as relações do ticket.")
        matches: dict[str, str] = {}
        for row in rows:
            if row.get("type", {}).get("id") != link.id:
                continue
            if not link.matches(row["type"]):
                raise TrackerError("A semântica da relação Jira mudou.")
            target = row.get(link.target_side + "Issue")
            if target is None:
                continue
            key, link_id = target.get("key"), row.get("id")
            if (
                not isinstance(key, str)
                or not re.fullmatch(re.escape(self.project) + r"-[1-9][0-9]*", key)
                or not isinstance(link_id, str)
                or not re.fullmatch(r"[0-9]+", link_id)
                or key in matches
            ):
                raise TrackerError(
                    "O Jira retornou relações ambíguas ou de outro projeto."
                )
            matches[key] = link_id
        return matches

    async def _link(
        self, issue: IssueReference, target: IssueReference, link: JiraLink
    ) -> None:
        other = "outward" if link.target_side == "inward" else "inward"
        await self._api(
            "POST",
            "issueLink",
            {
                "type": {"id": link.id},
                link.target_side + "Issue": {"key": target.external_id},
                other + "Issue": {"key": issue.external_id},
            },
        )

    async def reconcile_relations(
        self,
        issue: IssueReference,
        parent: IssueReference,
        blockers: list[IssueReference],
        *,
        read_only: bool,
        previous_blockers: list[str],
    ) -> bool:
        self.validate_parent(parent)
        await self.capabilities()
        for item in [issue, *blockers]:
            self.validate_parent(item)
        parents = await self._links(issue, self.spec_link)
        if set(parents) - {parent.external_id} or (
            read_only and set(parents) != {parent.external_id}
        ):
            return False
        observed = await self._links(issue, self.blocker_link)
        expected = {blocker.external_id for blocker in blockers}
        extra = set(observed) - expected
        if (read_only and set(observed) != expected) or not extra <= set(
            previous_blockers
        ):
            return False
        if parent.external_id not in parents:
            await self._link(issue, parent, self.spec_link)
        for key in extra:
            await self._api("DELETE", "issueLink/" + observed[key])
        for blocker in blockers:
            if blocker.external_id not in observed:
                await self._link(issue, blocker, self.blocker_link)
        return (
            set(await self._links(issue, self.spec_link)) == {parent.external_id}
            and set(await self._links(issue, self.blocker_link)) == expected
        )

    async def issue_state(self, number: int | str) -> IssueState:
        await self.capabilities()
        issue = await self.get(number)
        raw = await self._api(
            "GET", f"issue/{issue.external_id}?fields=status,resolution"
        )
        if raw.get("key") != issue.external_id:
            raise TrackerError("O Jira retornou estado de outro ticket.")
        fields = raw["fields"]
        category = fields["status"]["statusCategory"]["key"]
        if category not in {"new", "indeterminate", "done"}:
            raise TrackerError("O estado Jira não foi reconhecido.")
        done = category == "done"
        completed = (
            done
            and (fields.get("resolution") or {}).get("id")
            in self.completed_resolution_ids
        )
        return IssueState(
            None,
            None,
            "closed" if done else "open",
            "completed" if completed else None,
            issue.external_id,
        )

    async def list_sub_issues(self, number: int) -> list[IssueReference]:
        raise TrackerError("Use a relação textual de spec fixada no projeto Jira.")

    async def add_sub_issue(self, number: int, issue_id: int) -> None:
        raise TrackerError("Use a relação textual de spec fixada no projeto Jira.")

    async def list_blockers(self, number: int) -> list[IssueReference]:
        raise TrackerError("Use as chaves textuais e a direção fixada do Jira.")

    async def add_blocker(self, number: int, issue_id: int) -> None:
        raise TrackerError("Use as chaves textuais e a direção fixada do Jira.")
