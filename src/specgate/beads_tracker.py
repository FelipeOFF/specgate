"""One existing Beads base; fixed cwd and textual identities, without a new store."""

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from specgate.privacy import ensure_safe_content
from specgate.spec_contracts import IssueReference
from specgate.tracker import TrackerError

BeadsCommand = Callable[[list[str], str | None], Awaitable[str]]


class BeadsSpecTracker:
    kind = "beads"
    issue_type = "epic"

    def __init__(
        self,
        repository: str,
        root: Path,
        prefix: str,
        *,
        command: BeadsCommand | None = None,
        authorize_effect: Callable[[], bool] | None = None,
    ) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
        ) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", prefix):
            raise ValueError("Fixe o repositório e o prefixo da base Beads autorizada.")
        self.repository = self.project = repository
        self.root, self.prefix = root.resolve(), prefix
        self.instance = sha256(str(self.root).encode()).hexdigest()
        self.command, self.authorize_effect = command, authorize_effect

    def with_authority(self, check: Callable[[], bool]) -> "BeadsSpecTracker":
        return BeadsSpecTracker(
            self.repository,
            self.root,
            self.prefix,
            command=self.command,
            authorize_effect=lambda: (
                check() and (self.authorize_effect is None or self.authorize_effect())
            ),
        )

    async def _execute(self, args: list[str], body: str | None) -> str:
        beads = self.root / ".beads"
        if not beads.is_dir() or beads.is_symlink() or (beads / "redirect").exists():
            raise TrackerError(
                "A base Beads precisa existir diretamente no projeto autorizado."
            )
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("BEADS_", "BD_"))
            and "SESSION" not in key
            and "TRANSCRIPT" not in key
        }
        process = await asyncio.create_subprocess_exec(
            "bd",
            "--sandbox",
            *args,
            cwd=self.root,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(body.encode() if body is not None else None),
                timeout=30,
            )
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise TrackerError(
                "A CLI não confirmou o efeito; preserve a intenção e reconcilie."
            ) from None
        if process.returncode:
            raise TrackerError(
                "A CLI Beads falhou; isso não confirma ausência do item."
            )
        return output.decode()

    async def _run(
        self, args: list[str], body: str | None = None, *, write: bool = False
    ) -> str:
        ensure_safe_content({"args": args, "body": body})
        if write and (self.authorize_effect is None or not self.authorize_effect()):
            raise TrackerError(
                "A escrita no Beads exige autorização atual para esta entrega."
            )
        try:
            return await (self.command or self._execute)(args, body)
        except (OSError, UnicodeError):
            raise TrackerError(
                "Não foi possível confirmar a resposta da CLI Beads."
            ) from None

    async def _json(
        self, args: list[str], body: str | None = None, *, write: bool = False
    ) -> Any:
        raw = await self._run([*args, "--json"], body, write=write)
        try:
            return json.loads(raw)
        except ValueError:
            raise TrackerError("A CLI Beads não retornou JSON válido.") from None

    async def capabilities(self) -> dict[str, Any]:
        create, update = (
            await self._run(["create", "--help"]),
            await self._run(["update", "--help"]),
        )
        if not all(
            flag in create for flag in ("--id", "--spec-id", "--body-file")
        ) or not all(
            flag in update for flag in ("--spec-id", "--body-file", "--title")
        ):
            raise TrackerError(
                "A CLI instalada não oferece o contrato necessário para specs."
            )
        return {
            "destination": "beads",
            "reference_contract": 2,
            "project": self.project,
            "publish": True,
            "reconcile": True,
            "spec_versions": True,
            "spec_version_mode": "replace_with_readback",
            "ticket_graph": False,
            "native_sub_issues": False,
            "native_dependencies": False,
        }

    @staticmethod
    def origin(body: str) -> str:
        matches: list[str] = re.findall(r"<!-- dev-decision-spec:([a-f0-9]{32}) -->", body)
        if len(matches) != 1:
            raise TrackerError("A spec precisa de um único marcador de origem.")
        return matches[0]

    def spec_id(self, body: str) -> str:
        return self.origin(body)

    def planned_id(self, body: str) -> str:
        return f"{self.prefix}-{sha256(self.origin(body).encode()).hexdigest()[:20]}"

    def _issue(self, raw: Any) -> IssueReference:
        try:
            identifier, title, body = raw["id"], raw["title"], raw["description"]
            if not re.fullmatch(
                re.escape(self.prefix) + r"-[A-Za-z0-9_.-]+", identifier
            ) or raw.get("spec_id") != self.spec_id(body):
                raise ValueError
            revision = raw["updated_at"]
            if not isinstance(revision, str) or not revision:
                raise ValueError
            digest = sha256((title + "\0" + body).encode()).hexdigest()
            issue = IssueReference(
                tracker="beads",
                project=self.project,
                repository=self.repository,
                external_id=identifier,
                remote_revision=revision + ":" + digest,
                url=None,
                title=title,
                body=body,
                labels=raw.get("labels", []),
            )
            ensure_safe_content(issue.model_dump())
            return issue
        except (KeyError, TypeError, ValueError):
            raise TrackerError(
                "O Beads não confirmou origem, revisão e conteúdo da spec."
            ) from None

    async def get(self, number: int | str) -> IssueReference:
        if not isinstance(number, str) or not re.fullmatch(
            re.escape(self.prefix) + r"-[A-Za-z0-9_.-]+", number
        ):
            raise TrackerError("O ID não pertence à base Beads autorizada.")
        rows = await self._json(["show", number])
        if not isinstance(rows, list) or len(rows) != 1:
            raise TrackerError("A leitura Beads não retornou um único item.")
        result = self._issue(rows[0])
        if result.external_id != number:
            raise TrackerError("A CLI retornou outro item Beads.")
        return result

    async def find(self, marker: str) -> list[IssueReference]:
        rows = await self._json(["list", "--all", "--limit", "0"])
        if not isinstance(rows, list):
            raise TrackerError("A listagem Beads não foi concluída.")
        matches = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(
                row.get("description", ""), str
            ):
                raise TrackerError("A listagem Beads retornou conteúdo inválido.")
            if marker in row.get("description", ""):
                matches.append(self._issue(row))
        return matches

    async def create(self, title: str, body: str) -> IssueReference:
        await self.capabilities()
        identifier = self.planned_id(body)
        rows = await self._json(["list", "--all", "--limit", "0"])
        if not isinstance(rows, list):
            raise TrackerError("A listagem Beads não foi concluída.")
        if any(row["id"] == identifier for row in rows):
            current = await self.get(identifier)
            if current.title == title and current.body == body:
                return current
            raise TrackerError(
                "O ID reservado já tem outra origem ou conteúdo; revise a colisão."
            )
        await self._json(
            [
                "create",
                "--id",
                identifier,
                "--title",
                title,
                "--type",
                self.issue_type,
                "--spec-id",
                self.spec_id(body),
                "--labels",
                "ready-for-agent",
                "--body-file",
                "-",
            ],
            body,
            write=True,
        )
        return await self.get(identifier)

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
                "A spec mudou no Beads; preserve a edição remota e revise."
            )
        if self.planned_id(body) != current.external_id:
            raise TrackerError("A atualização pertence a outra origem de spec.")
        await self._json(
            [
                "update",
                current.external_id,
                "--title",
                title,
                "--spec-id",
                self.spec_id(body),
                "--body-file",
                "-",
            ],
            body,
            write=True,
        )
        return await self.get(current.lookup_id)

    async def ensure_label(self, number: int | str) -> IssueReference:
        current = await self.get(number)
        if "ready-for-agent" not in current.labels:
            raise TrackerError("A label mudou no Beads; revise o item existente.")
        return current

    async def find_versions(self, number: int, marker: str) -> list[IssueReference]:
        raise TrackerError("Beads atualiza o item; versões não são comentários GitHub.")

    async def get_version(self, number: int, comment_id: int) -> IssueReference:
        raise TrackerError("Beads não usa IDs de comentário como revisão.")

    async def append_version(
        self, number: int, title: str, body: str
    ) -> IssueReference:
        raise TrackerError(
            "Use a atualização com confronto da revisão remota do Beads."
        )
