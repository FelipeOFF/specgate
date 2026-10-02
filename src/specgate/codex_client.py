"""Controlled Codex app-server client for explicit skills and structured questions."""

import asyncio
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from mcp.shared.dispatcher import ProgressFnT

from specgate.client import (
    ContextUpdate,
    ReviewRequest,
    review_request,
)
from specgate.context import ContextResult, discover_skill_catalog
from specgate.gate_policy import BASIS_MANIFEST
from specgate.routing import route_skills
from specgate.transport import MCPTransportError

MAX_FRAME_BYTES = 4 * 1024 * 1024

DecisionFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SkillSelector = Callable[
    [str, tuple["CodexSkill", ...]], str | None | Awaitable[str | None]
]


class CodexProtocolError(RuntimeError):
    """The app-server disconnected or violated the documented protocol."""


@dataclass(frozen=True)
class CodexSkill:
    name: str
    path: str
    description: str


@dataclass(frozen=True)
class CodexQuestion:
    request_id: int | str
    question_id: str
    prompt: str
    options: tuple[str, ...]
    option_ids: tuple[str, ...]
    option_texts: tuple[str, ...]
    automatable: bool


@dataclass(frozen=True)
class ControlledTurn:
    status: Literal["completed", "needs_human", "failed"]
    question: CodexQuestion | None = None
    answer_origin: Literal["automated", "human"] | None = None
    warning: str | None = None


class AutomaticSkillRouter:
    """Route only enabled Codex skills and fall back safely when MCP is offline."""

    offline_warning = (
        "Specgate MCP is unavailable; continuing with Codex skill routing."
    )

    def __init__(
        self,
        project: Path,
        url: str,
        token: str,
        *,
        skill_roots: Sequence[Path],
        authorized_roots: Sequence[Path],
        sources: Sequence[str],
        required: Sequence[str],
        disabled_ids: Sequence[str],
        transport: str,
        ca_file: Path | None,
        timeout_seconds: float,
        project_id: str | None = None,
    ) -> None:
        self.project = project
        self.url = url
        self.token = token
        self.skill_roots = tuple(skill_roots)
        self.authorized_roots = tuple(authorized_roots)
        self.sources = tuple(sources)
        self.required = tuple(required)
        self.disabled_ids = tuple(disabled_ids)
        self.transport = transport
        self.ca_file = ca_file
        self.timeout_seconds = timeout_seconds
        self.project_id = project_id
        self._cache: dict[str, str | None] = {}
        self._offline = False
        self._warning_pending = False
        self._warning_emitted = False

    def consume_warning(self) -> str | None:
        if not self._warning_pending or self._warning_emitted:
            return None
        self._warning_pending = False
        self._warning_emitted = True
        return self.offline_warning

    @property
    def offline(self) -> bool:
        return self._offline

    async def __call__(self, prompt: str, skills: tuple[CodexSkill, ...]) -> str | None:
        if self._offline:
            return None
        paths: dict[Path, CodexSkill] = {}
        names: set[str] = set()
        for skill in skills:
            path = Path(skill.path).resolve()
            if skill.name in names or path in paths:
                raise ValueError("Enabled skill IDs or paths are ambiguous.")
            names.add(skill.name)
            paths[path] = skill
        cache_key = self._cache_key(prompt, paths)
        if cache_key in self._cache:
            return self._cache[cache_key]

        roots = [self.project / Path(root).expanduser() for root in self.skill_roots]
        catalog = discover_skill_catalog(
            roots, authorized_roots=list(self.authorized_roots)
        )
        aliases = set(self.disabled_ids)
        catalog_paths = {Path(item.source).resolve() for item in catalog}
        if not paths.keys() <= catalog_paths:
            raise ValueError(
                "Enabled Codex skills do not match the authorized catalog."
            )
        for item in catalog:
            if Path(item.source).resolve() not in paths:
                aliases.update(item.aliases)

        request = ReviewRequest(
            objective=prompt,
            tool="jev_find",
            arguments={"query": prompt, "candidates": []},
            sources=list(self.sources),
            required=list(self.required),
            skill_roots=[str(root) for root in self.skill_roots],
        )
        for attempt in range(2):
            try:
                result = await route_skills(
                    request,
                    self.project,
                    self.url,
                    self.token,
                    authorized_roots=list(self.authorized_roots),
                    disabled_ids=aliases,
                    transport=self.transport,
                    ca_file=self.ca_file,
                    timeout_seconds=self.timeout_seconds,
                    project_id=self.project_id,
                )
            except (MCPTransportError, OSError, TimeoutError):
                result = {"status": "incomplete", "evaluations": []}
                offline = True
            else:
                offline = self._is_offline(result)
            if offline:
                if attempt == 0:
                    continue
                self._offline = True
                self._warning_pending = True
                return None
            candidate = result.get("candidate")
            selected: str | None = None
            # Without a native hook to confirm the load, only a validated manifest
            # may select here; a selection under the confidence policy stays advice.
            if (
                result.get("action") == "auto"
                and result.get("gate_basis") == BASIS_MANIFEST
                and candidate
            ):
                source = Path(candidate["source"]).resolve()
                selected = paths[source].name if source in paths else None
            self._cache[cache_key] = selected
            return selected
        raise AssertionError("The bounded routing loop must return.")

    @staticmethod
    def _is_offline(result: dict[str, Any]) -> bool:
        for evaluation in result.get("evaluations", []):
            failure = evaluation.get("result", {}).get("error", {})
            if failure.get("fallback") == "harness" or failure.get("code") in {
                "connection",
                "timeout",
                "transport",
            }:
                return True
        return False

    def _cache_key(self, prompt: str, skills: dict[Path, CodexSkill]) -> str:
        digest = hashlib.sha256()
        digest.update(prompt.encode())
        digest.update(b"\0sources\0")
        for source in self.sources:
            digest.update(source.encode())
            path = self.project / source
            if path.is_file():
                digest.update(path.read_bytes())
        digest.update(b"\0required\0")
        for item in self.required:
            digest.update(item.encode())
        digest.update(b"\0disabled\0")
        for item in self.disabled_ids:
            digest.update(item.encode())
        digest.update(b"\0catalog\0")
        for path, skill in sorted(skills.items(), key=lambda item: str(item[0])):
            digest.update(skill.name.encode())
            digest.update(str(path).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


def mcp_question_decider(
    project: Path,
    url: str,
    token: str,
    *,
    sources: Sequence[str],
    required: Sequence[str] = (),
    request_context: Callable[[ContextResult], Awaitable[ContextUpdate]] | None = None,
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    progress_callback: ProgressFnT | None = None,
) -> DecisionFn:
    """Bind structured Codex questions to the public MCP review flow."""

    async def decide(payload: dict[str, Any]) -> dict[str, Any]:
        request = ReviewRequest.model_validate(
            {
                **payload,
                "sources": list(sources),
                "required": list(required),
            }
        )
        return await review_request(
            request,
            project,
            url,
            token,
            request_context=request_context,
            transport=transport,
            ca_file=ca_file,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
        )

    return decide


def mcp_skill_selector(
    project: Path,
    url: str,
    token: str,
    *,
    skill_roots: Sequence[Path],
    authorized_roots: Sequence[Path],
    sources: Sequence[str],
    required: Sequence[str] = (),
    disabled_ids: Sequence[str] = (),
    transport: str = "streamable",
    ca_file: Path | None = None,
    timeout_seconds: float = 30,
    project_id: str | None = None,
) -> AutomaticSkillRouter:
    """Route a prompt, then bind the approved file to the current Codex catalog."""

    return AutomaticSkillRouter(
        project,
        url,
        token,
        skill_roots=skill_roots,
        authorized_roots=authorized_roots,
        sources=sources,
        required=required,
        disabled_ids=disabled_ids,
        transport=transport,
        ca_file=ca_file,
        timeout_seconds=timeout_seconds,
        project_id=project_id,
    )


class CodexControlledClient:
    """Own one ephemeral Codex app-server thread and its question channel."""

    def __init__(
        self,
        *,
        decide: DecisionFn,
        command: Sequence[str] = (
            "codex",
            "-c",
            "features.default_mode_request_user_input=true",
            "app-server",
            "--stdio",
        ),
        timeout_seconds: float = 30,
        approve: DecisionFn | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Timeout must be greater than zero.")
        self._command = tuple(command)
        self._decide = decide
        self._approve = approve
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._question: CodexQuestion | None = None
        self._answer_origin: Literal["automated", "human"] | None = None
        self._resolved_requests: set[int | str] = set()
        self.diagnostic: dict[str, Any] = {}

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME_BYTES,
        )
        self._reader_task = asyncio.create_task(self._read_messages())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        initialized = await self._call(
            "initialize",
            {
                "clientInfo": {
                    "name": "specgate",
                    "title": "Specgate",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._write({"method": "initialized", "params": {}})
        server = initialized.get("serverInfo")
        if not isinstance(server, dict):
            server = {
                key: initialized[key]
                for key in ("userAgent", "platformFamily", "platformOs")
                if key in initialized
            }
        self.diagnostic = {
            "client_mode": "controlled_app_server",
            "server": server,
            "experimental_api": True,
            "structured_questions": "experimental_default_mode",
        }

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task),
            return_exceptions=True,
        )

    async def list_skills(self, cwd: Path) -> tuple[CodexSkill, ...]:
        result = await self._call(
            "skills/list", {"cwds": [str(cwd.resolve())], "forceReload": True}
        )
        groups = result.get("data")
        if not isinstance(groups, list):
            raise CodexProtocolError("Invalid skills catalog.")
        skills: list[CodexSkill] = []
        for group in groups:
            if group.get("errors"):
                raise CodexProtocolError("The app-server did not load all skills.")
            for item in group.get("skills", []):
                if item.get("enabled") is True:
                    skills.append(
                        CodexSkill(
                            name=item["name"],
                            path=item["path"],
                            description=item.get("description", ""),
                        )
                    )
        return tuple(skills)

    async def run(
        self,
        prompt: str,
        project: Path,
        *,
        select_skill: SkillSelector | None = None,
        requested_skill: str | None = None,
        delegate_questions: bool = False,
    ) -> ControlledTurn:
        skills = await self.list_skills(project)
        name: str | None
        if requested_skill is not None:
            name = requested_skill
        elif select_skill is not None:
            selected = select_skill(prompt, skills)
            name = await selected if inspect.isawaitable(selected) else selected
        else:
            raise ValueError("A skill selector or requested skill is required.")
        if name is None:
            warning = None
            consume_warning = getattr(select_skill, "consume_warning", None)
            if callable(consume_warning):
                warning = consume_warning()
            if getattr(select_skill, "offline", False):
                started = await self._call(
                    "thread/start", {"cwd": str(project.resolve()), "ephemeral": True}
                )
                await self._call(
                    "turn/start",
                    {
                        "threadId": started["thread"]["id"],
                        "input": [{"type": "text", "text": prompt}],
                    },
                )
                return await self._wait_for_turn(delegate_questions, warning)
            return ControlledTurn("needs_human", warning=warning)
        matches = [skill for skill in skills if skill.name == name]
        if len(matches) != 1:
            raise ValueError("Selected skill is not enabled in the app-server.")
        skill = matches[0]
        started = await self._call(
            "thread/start", {"cwd": str(project.resolve()), "ephemeral": True}
        )
        thread_id = started["thread"]["id"]
        await self._call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {"type": "text", "text": f"${skill.name} {prompt}"},
                    {"type": "skill", "name": skill.name, "path": skill.path},
                ],
            },
        )
        return await self._wait_for_turn(delegate_questions)

    async def answer(
        self, question: CodexQuestion, answers: list[str]
    ) -> ControlledTurn:
        if question is not self._question:
            raise ValueError("Question is no longer pending.")
        if (
            len(answers) != 1
            or not answers[0].strip()
            or (question.options and answers[0] not in question.options)
        ):
            raise ValueError("Answer must use one current question option.")
        await self._respond(question, answers[0], "human")
        return await self._wait_for_turn(False)

    async def _wait_for_turn(
        self, delegate_questions: bool, warning: str | None = None
    ) -> ControlledTurn:
        while True:
            message = await asyncio.wait_for(self._events.get(), self._timeout)
            method = message.get("method")
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                if self._approve is None:
                    return ControlledTurn(
                        "needs_human",
                        warning="Configure a delegação de aprovações do runner.",
                    )
                response = await self._approve(message)
                if message["id"] not in self._resolved_requests:
                    await self._write({"id": message["id"], "result": response})
                continue
            if method == "item/tool/requestUserInput":
                question = self._parse_question(message)
                self._question = question
                if delegate_questions and question.automatable:
                    try:
                        decision = await self._decide(self._decision_request(question))
                    except (
                        ExceptionGroup,
                        MCPTransportError,
                        OSError,
                        TimeoutError,
                        ValueError,
                    ):
                        return ControlledTurn("needs_human", question, warning=warning)
                    selected = decision.get("decision", {}).get("selected_option")
                    if (
                        decision.get("action") == "auto"
                        and decision.get("origin") == "automated"
                        and selected in question.option_ids
                        and question.request_id not in self._resolved_requests
                    ):
                        label = question.options[question.option_ids.index(selected)]
                        await self._respond(question, label, "automated")
                        continue
                return ControlledTurn("needs_human", question, warning=warning)
            if method == "serverRequest/resolved":
                if (
                    self._question is not None
                    and message.get("params", {}).get("requestId")
                    == self._question.request_id
                ):
                    self._question = None
                continue
            if method == "turn/completed":
                status = message.get("params", {}).get("turn", {}).get("status")
                return ControlledTurn(
                    "completed" if status == "completed" else "failed",
                    answer_origin=self._answer_origin,
                    warning=warning,
                )

    def _parse_question(self, message: dict[str, Any]) -> CodexQuestion:
        params = message.get("params", {})
        questions = params.get("questions")
        if params.get("isBlocking") is not True or not isinstance(questions, list):
            raise CodexProtocolError("Invalid structured question.")
        if len(questions) != 1:
            raise CodexProtocolError("Question requires human review.")
        raw = questions[0]
        options = raw.get("options") or []
        if not isinstance(options, list):
            raise CodexProtocolError("Invalid question options.")
        labels = tuple(option.get("label") for option in options)
        if any(not isinstance(label, str) or not label.strip() for label in labels):
            raise CodexProtocolError("Invalid question option.")
        if len(set(labels)) != len(labels):
            raise CodexProtocolError("Duplicate options require human review.")
        return CodexQuestion(
            request_id=message["id"],
            question_id=raw["id"],
            prompt=raw["question"],
            options=labels,
            option_ids=tuple(f"option_{index}" for index in range(len(labels))),
            option_texts=tuple(
                (
                    f"{option['label']} — {option['description']}"
                    if option.get("description")
                    else option["label"]
                )
                for option in options
            ),
            automatable=bool(options)
            and not raw.get("isSecret")
            and not raw.get("isOther"),
        )

    def _decision_request(self, question: CodexQuestion) -> dict[str, Any]:
        return {
            "objective": question.prompt,
            "tool": "jev_decide",
            "arguments": {
                "question": question.prompt,
                "options": [
                    {"id": option_id, "text": text}
                    for option_id, text in zip(
                        question.option_ids, question.option_texts, strict=True
                    )
                ],
                "question_type": "single_choice",
            },
            "sources": [],
            "required": [],
            "artifact": question.prompt,
        }

    async def _respond(
        self,
        question: CodexQuestion,
        label: str,
        origin: Literal["automated", "human"],
    ) -> None:
        await self._write(
            {
                "id": question.request_id,
                "result": {
                    "answers": {
                        question.question_id: {
                            "answers": [label],
                        }
                    }
                },
            }
        )
        self._answer_origin = origin

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"id": request_id, "method": method, "params": params})
        try:
            message = await asyncio.wait_for(future, self._timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in message:
            raise CodexProtocolError(str(message["error"]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexProtocolError(f"Invalid response for {method}.")
        return result

    async def _write(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise CodexProtocolError("The app-server is not connected.")
        data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure: CodexProtocolError
        try:
            while line := await self._process.stdout.readline():
                if len(line) > MAX_FRAME_BYTES:
                    raise CodexProtocolError(
                        "App-server message exceeds the frame limit."
                    )
                message = json.loads(line)
                if message.get("method") == "serverRequest/resolved":
                    self._resolved_requests.add(
                        message.get("params", {}).get("requestId")
                    )
                if "method" in message:
                    await self._events.put(message)
                elif (future := self._pending.get(message.get("id"))) is not None:
                    future.set_result(message)
            raise CodexProtocolError("The app-server closed the connection.")
        except asyncio.CancelledError:
            return
        except CodexProtocolError as exc:
            failure = exc
        except (json.JSONDecodeError, ValueError):
            failure = CodexProtocolError("The app-server sent invalid JSON.")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while await self._process.stderr.read(65536):
                pass
        except asyncio.CancelledError:
            return
