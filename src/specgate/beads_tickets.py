"""Stable Beads tickets and read-back dependencies within one existing base."""

import re
from collections.abc import Callable
from copy import copy
from typing import Any

from specgate.beads_tracker import BeadsSpecTracker
from specgate.spec_contracts import IssueReference
from specgate.tracker import IssueState, TrackerError


class BeadsTicketTracker(BeadsSpecTracker):
    issue_type = "task"

    def __init__(self, *args: Any, spec: IssueReference, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.validate_parent(spec)
        self.spec = spec

    def validate_parent(self, spec: IssueReference) -> None:
        if (
            spec.tracker != "beads"
            or spec.repository.casefold() != self.repository.casefold()
            or spec.project != self.project
            or spec.url is not None
            or not re.fullmatch(
                re.escape(self.prefix) + r"-[A-Za-z0-9_.-]+", spec.external_id
            )
        ):
            raise TrackerError("A spec não pertence à base Beads configurada.")

    def with_authority(self, check: Callable[[], bool]) -> "BeadsTicketTracker":
        result = copy(self)
        result.authorize_effect = lambda: (
            check() and (self.authorize_effect is None or self.authorize_effect())
        )
        return result

    @staticmethod
    def origin(body: str) -> str:
        matches = re.findall(
            r"<!-- dev-decision-ticket:([a-f0-9]{32}):[a-f0-9]{64}:([A-Za-z0-9_-]{1,80})(?::[1-9][0-9]*)? -->",
            body,
        )
        if len(matches) != 1:
            raise TrackerError("O ticket precisa de um único marcador de origem.")
        return ":".join(matches[0])

    def spec_id(self, body: str) -> str:
        self.origin(body)
        return self.spec.external_id

    @property
    def base_scope(self) -> dict[str, str]:
        return {
            "tracker": "beads",
            "project": self.project,
            "instance": self.instance,
            "prefix": self.prefix,
            "spec_id": self.spec.external_id,
        }

    def publication_scope(self, body: str) -> dict[str, str]:
        return {**self.base_scope, "external_id": self.planned_id(body)}

    async def capabilities(self) -> dict[str, Any]:
        result = await super().capabilities()
        add = await self._run(["dep", "add", "--help"])
        listing = await self._run(["dep", "list", "--help"])
        remove = await self._run(["dep", "remove", "--help"])
        if (
            not all(word in add for word in ("--type", "blocks"))
            or not all(flag in listing for flag in ("--direction", "--type"))
            or "depends-on-id" not in remove
        ):
            raise TrackerError(
                "A CLI Beads não oferece o contrato de dependências necessário."
            )
        return {
            **result,
            "ticket_graph": True,
            "native_dependencies": True,
            "spec_relation": "spec_id",
        }

    async def _blockers(self, issue: IssueReference) -> set[str]:
        rows = await self._json(
            [
                "dep",
                "list",
                issue.external_id,
                "--direction",
                "down",
                "--type",
                "blocks",
            ]
        )
        if not isinstance(rows, list):
            raise TrackerError("O Beads não confirmou a listagem de dependências.")
        found = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("dependency_type") != "blocks"
                or not isinstance(row.get("id"), str)
                or not re.fullmatch(
                    re.escape(self.prefix) + r"-[A-Za-z0-9_.-]+", row["id"]
                )
                or row["id"] in found
            ):
                raise TrackerError(
                    "O Beads retornou dependências ambíguas ou de outra base."
                )
            found.add(row["id"])
        return found

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
        if parent.identity_key != self.spec.identity_key:
            raise TrackerError("A spec mudou durante a publicação dos tickets.")
        await self.capabilities()
        await self.get(issue.external_id)
        for blocker in blockers:
            await self.get(blocker.external_id)
        observed = await self._blockers(issue)
        expected = {blocker.external_id for blocker in blockers}
        extra = observed - expected
        if (read_only and observed != expected) or not extra <= set(previous_blockers):
            return False
        for identifier in extra:
            await self._json(
                ["dep", "remove", issue.external_id, identifier], write=True
            )
        for identifier in sorted(expected - observed):
            await self._json(
                ["dep", "add", issue.external_id, identifier, "--type", "blocks"],
                write=True,
            )
        return await self._blockers(issue) == expected

    async def issue_state(self, number: int | str) -> IssueState:
        issue = await self.get(number)
        rows = await self._json(["show", issue.external_id])
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("id") != issue.external_id
        ):
            raise TrackerError("O Beads retornou estado de outro ticket.")
        status = rows[0].get("status")
        if status not in {
            "open",
            "in_progress",
            "blocked",
            "deferred",
            "hooked",
            "closed",
        }:
            raise TrackerError("O estado Beads não foi reconhecido.")
        closed = status == "closed"
        return IssueState(
            None,
            None,
            "closed" if closed else "open",
            "completed" if closed else None,
            issue.external_id,
            actionable=status == "open",
        )

    async def list_sub_issues(self, number: int) -> list[IssueReference]:
        raise TrackerError("Use o vínculo spec_id da CLI Beads.")

    async def add_sub_issue(self, number: int, issue_id: int) -> None:
        raise TrackerError("Use o vínculo spec_id da CLI Beads.")

    async def list_blockers(self, number: int) -> list[IssueReference]:
        raise TrackerError("Use os IDs textuais da CLI Beads.")

    async def add_blocker(self, number: int, issue_id: int) -> None:
        raise TrackerError("Use os IDs textuais da CLI Beads.")
