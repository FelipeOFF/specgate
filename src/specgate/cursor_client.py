"""Controlled Cursor ACP client for explicit skills and structured questions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from specgate.codex_client import DecisionFn, mcp_question_decider
from specgate.routing import HumanReview, confirm_skill_loaded
from specgate.transport import MCPTransportError

MAX_FRAME_BYTES = 4 * 1024 * 1024


class CursorProtocolError(RuntimeError):
    """The Cursor ACP process disconnected or violated its documented protocol."""


@dataclass(frozen=True)
class CursorSkill:
    id: str
    revision: str
    instructions: str


@dataclass(frozen=True)
class CursorOption:
    id: str
    label: str


@dataclass(frozen=True)
class CursorQuestion:
    id: str
    prompt: str
    options: tuple[CursorOption, ...]
    allow_multiple: bool


@dataclass(frozen=True)
class CursorQuestionRequest:
    request_id: int | str
    tool_call_id: str
    title: str | None
    questions: tuple[CursorQuestion, ...]

    @property
    def automatable(self) -> bool:
        return bool(
            len(self.questions) == 1
            and not self.questions[0].allow_multiple
            and 2 <= len(self.questions[0].options) <= 48
        )


@dataclass(frozen=True)
class ControlledCursorTurn:
    status: Literal["completed", "needs_human", "failed"]
    question: CursorQuestionRequest | None = None
    answer_origin: Literal["automated", "human"] | None = None
    pending_method: str | None = None
    warning: str | None = None


def cursor_skill_from_indication(
    indication: dict[str, Any],
    skill_id: str,
    revision: str,
    *,
    authorized_roots: list[Path],
    disabled_ids: Sequence[str] = (),
    review: HumanReview | None = None,
) -> CursorSkill:
    """Recheck and load the exact skill revision reviewed by routing.

    Cursor has no native hook: outside a validated manifest the skill loads only with the
    `review` of the person who chose this exact revision.
    """
    confirmed = confirm_skill_loaded(
        indication,
        skill_id,
        revision,
        authorized_roots=authorized_roots,
        disabled_ids=disabled_ids,
        review=review,
    )
    candidate = confirmed.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("loaded") is not True:
        if isinstance(confirmed.get("loading"), dict):
            raise ValueError(
                "Loading this indication needs a native hook or a human review of "
                "this exact revision, and Cursor has no hook; pass the review of the "
                "person who chose it."
            )
        raise ValueError("The selected skill does not match the reviewed revision.")
    instructions = candidate.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("The selected skill has no reviewed instructions.")
    return CursorSkill(skill_id, revision, instructions)


class CursorControlledClient:
    """Own one ephemeral Cursor ACP process and its structured-question channel."""

    offline_warning: ClassVar[str] = (
        "Specgate MCP is unavailable; continuing with human review."
    )
    question_method: ClassVar[str] = "cursor/ask_question"
    auth_method: ClassVar[str] = "cursor_login"
    client_mode: ClassVar[str] = "controlled_cursor_acp"
    question_evidence: ClassVar[str] = "protocol_double_only"
    review_methods: ClassVar[set[str]] = {
        "cursor/create_plan",
        "session/request_permission",
    }

    def __init__(
        self,
        *,
        decide: DecisionFn,
        command: Sequence[str] | None = None,
        timeout_seconds: float = 30,
        authenticate: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Timeout must be greater than zero.")
        self._command = tuple(command) if command is not None else (
            str(Path.home() / ".local/bin/cursor-agent"),
            "acp",
        )
        self._decide = decide
        self._authenticate = authenticate
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._prompt_task: asyncio.Task[dict[str, Any]] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._question: CursorQuestionRequest | None = None
        self._answer_origin: Literal["automated", "human"] | None = None
        self._resolved_requests: set[int | str] = set()
        self._decision_offline = False
        self._offline_warning_emitted = False
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
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "specgate", "version": "0.1.0"},
            },
        )
        if initialized.get("protocolVersion") != 1:
            raise CursorProtocolError("Cursor did not negotiate ACP protocol version 1.")
        auth = initialized.get("authMethods")
        if not isinstance(auth, list) or self.auth_method not in {
            item.get("id") for item in auth if isinstance(item, dict)
        }:
            raise CursorProtocolError("The ACP agent did not advertise the required login.")
        if self._authenticate:
            await self._call("authenticate", {"methodId": self.auth_method})
        capabilities = initialized.get("agentCapabilities")
        if not isinstance(capabilities, dict):
            raise CursorProtocolError("Cursor returned invalid ACP capabilities.")
        mcp = capabilities.get("mcpCapabilities")
        self.diagnostic = {
            "client_mode": self.client_mode,
            "protocol_version": 1,
            "authentication": self.auth_method if self._authenticate else "not_attempted",
            "mcp": dict(mcp) if isinstance(mcp, dict) else {},
            "structured_questions": self.question_evidence,
        }
        metadata = initialized.get("_meta")
        if isinstance(metadata, dict) and isinstance(metadata.get("agentVersion"), str):
            self.diagnostic["agent_version"] = metadata["agentVersion"]

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
        tasks = (self._prompt_task, self._reader_task, self._stderr_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task), return_exceptions=True)

    async def run(
        self,
        prompt: str,
        project: Path,
        *,
        skill: CursorSkill,
        delegate_questions: bool = False,
    ) -> ControlledCursorTurn:
        if self._prompt_task is not None and not self._prompt_task.done():
            raise ValueError("A Cursor turn is already active.")
        if not prompt.strip() or not skill.instructions.strip():
            raise ValueError("Prompt and selected skill instructions are required.")
        created = await self._call(
            "session/new",
            {"cwd": str(project.resolve()), "mcpServers": []},
        )
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise CursorProtocolError("Cursor returned an invalid session ID.")
        text = (
            f"Selected skill: {skill.id}\n"
            f"Skill revision: {skill.revision}\n\n"
            f"{skill.instructions}\n\n"
            f"User request:\n{prompt}"
        )
        self._answer_origin = None
        self._question = None
        self._prompt_task = asyncio.create_task(
            self._call(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            )
        )
        return await self._wait_for_turn(delegate_questions)

    async def answer(
        self,
        request: CursorQuestionRequest,
        answers: dict[str, list[str]],
    ) -> ControlledCursorTurn:
        if request is not self._question:
            raise ValueError("Question is no longer pending.")
        if set(answers) != {question.id for question in request.questions}:
            raise ValueError("Answers must cover the current questions exactly.")
        for question in request.questions:
            selected = answers[question.id]
            valid = {option.id for option in question.options}
            if (
                not selected
                or len(selected) != len(set(selected))
                or (not question.allow_multiple and len(selected) != 1)
                or not set(selected) <= valid
            ):
                raise ValueError("Answer must use the current question options.")
        await self._respond(request, answers, "human")
        return await self._wait_for_turn(False)

    async def _wait_for_turn(self, delegate_questions: bool) -> ControlledCursorTurn:
        if self._prompt_task is None:
            raise CursorProtocolError("No Cursor turn is active.")
        while True:
            event_task = asyncio.create_task(self._events.get())
            done, _ = await asyncio.wait(
                {event_task, self._prompt_task},
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                raise TimeoutError("Cursor ACP turn timed out.")
            if self._prompt_task in done:
                event_task.cancel()
                await asyncio.gather(event_task, return_exceptions=True)
                result = self._prompt_task.result()
                self._prompt_task = None
                return ControlledCursorTurn(
                    "completed" if result.get("stopReason") == "end_turn" else "failed",
                    answer_origin=self._answer_origin,
                )
            message = event_task.result()
            method = message.get("method")
            if method == self.question_method:
                question = self._parse_question(message)
                self._question = question
                if delegate_questions and question.automatable:
                    decision, warning = await self._delegate_question(question)
                    if decision is not None:
                        selected = decision.get("decision", {}).get("selected_option")
                        option_ids = {
                            option.id for option in question.questions[0].options
                        }
                        if (
                            decision.get("action") == "auto"
                            and decision.get("origin") == "automated"
                            and selected in option_ids
                            and question.request_id not in self._resolved_requests
                        ):
                            await self._respond(
                                question,
                                {question.questions[0].id: [selected]},
                                "automated",
                            )
                            continue
                    return ControlledCursorTurn(
                        "needs_human",
                        question,
                        pending_method=method,
                        warning=warning,
                    )
                return ControlledCursorTurn("needs_human", question, pending_method=method)
            if method in self.review_methods:
                return ControlledCursorTurn("needs_human", pending_method=method)

    async def _delegate_question(
        self, question: CursorQuestionRequest
    ) -> tuple[dict[str, Any] | None, str | None]:
        if self._decision_offline:
            return None, None
        for attempt in range(2):
            try:
                return await self._decide(self._decision_request(question)), None
            except (
                ExceptionGroup,
                MCPTransportError,
                OSError,
                TimeoutError,
                ValueError,
            ):
                if attempt == 0:
                    continue
        self._decision_offline = True
        if self._offline_warning_emitted:
            return None, None
        self._offline_warning_emitted = True
        return None, self.offline_warning

    def _parse_question(self, message: dict[str, Any]) -> CursorQuestionRequest:
        params = message.get("params")
        if not isinstance(params, dict):
            raise CursorProtocolError("Invalid cursor/ask_question parameters.")
        tool_call_id = params.get("toolCallId")
        raw_questions = params.get("questions")
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            raise CursorProtocolError("Invalid Cursor tool call ID.")
        if not isinstance(raw_questions, list) or not raw_questions:
            raise CursorProtocolError("Cursor question list is empty.")
        questions: list[CursorQuestion] = []
        question_ids: set[str] = set()
        for raw in raw_questions:
            if not isinstance(raw, dict):
                raise CursorProtocolError("Invalid Cursor question.")
            question_id, prompt, options = raw.get("id"), raw.get("prompt"), raw.get("options")
            if (
                not isinstance(question_id, str)
                or not question_id.strip()
                or question_id in question_ids
                or not isinstance(prompt, str)
                or not prompt.strip()
                or not isinstance(options, list)
            ):
                raise CursorProtocolError("Invalid or duplicate Cursor question ID.")
            parsed: list[CursorOption] = []
            option_ids: set[str] = set()
            for option in options:
                if not isinstance(option, dict):
                    raise CursorProtocolError("Invalid Cursor question option.")
                option_id, label = option.get("id"), option.get("label")
                if (
                    not isinstance(option_id, str)
                    or not option_id.strip()
                    or option_id in option_ids
                    or not isinstance(label, str)
                    or not label.strip()
                ):
                    raise CursorProtocolError("Invalid or duplicate Cursor option ID.")
                option_ids.add(option_id)
                parsed.append(CursorOption(option_id, label))
            question_ids.add(question_id)
            questions.append(
                CursorQuestion(
                    question_id,
                    prompt,
                    tuple(parsed),
                    raw.get("allowMultiple") is True,
                )
            )
        title = params.get("title")
        if title is not None and not isinstance(title, str):
            raise CursorProtocolError("Invalid Cursor question title.")
        return CursorQuestionRequest(
            message["id"],
            tool_call_id,
            title,
            tuple(questions),
        )

    def _decision_request(self, request: CursorQuestionRequest) -> dict[str, Any]:
        question = request.questions[0]
        return {
            "objective": question.prompt,
            "tool": "jev_decide",
            "arguments": {
                "question": question.prompt,
                "options": [
                    {"id": option.id, "text": option.label}
                    for option in question.options
                ],
                "question_type": "single_choice",
            },
            "sources": [],
            "required": [],
            "artifact": question.prompt,
        }

    async def _respond(
        self,
        request: CursorQuestionRequest,
        answers: dict[str, list[str]],
        origin: Literal["automated", "human"],
    ) -> None:
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {
                    "outcome": {
                        "outcome": "answered",
                        "answers": [
                            {
                                "questionId": question.id,
                                "selectedOptionIds": answers[question.id],
                            }
                            for question in request.questions
                        ],
                    }
                },
            }
        )
        self._resolved_requests.add(request.request_id)
        self._question = None
        self._answer_origin = origin

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        try:
            message = await asyncio.wait_for(future, self._timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in message:
            raise CursorProtocolError(str(message["error"]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise CursorProtocolError(f"Invalid response for {method}.")
        return result

    async def _write(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise CursorProtocolError("Cursor ACP is not connected.")
        data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure: CursorProtocolError
        try:
            while line := await self._process.stdout.readline():
                if len(line) > MAX_FRAME_BYTES:
                    raise CursorProtocolError("Cursor ACP message exceeds the frame limit.")
                message = json.loads(line)
                if message.get("jsonrpc") != "2.0":
                    raise CursorProtocolError("Cursor sent an invalid JSON-RPC envelope.")
                if "method" in message:
                    await self._events.put(message)
                elif (future := self._pending.get(message.get("id"))) is not None:
                    future.set_result(message)
            raise CursorProtocolError("Cursor ACP closed the connection.")
        except asyncio.CancelledError:
            return
        except CursorProtocolError as error:
            failure = error
        except (json.JSONDecodeError, ValueError):
            failure = CursorProtocolError("Cursor ACP sent invalid JSON.")
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


__all__ = [
    "ControlledCursorTurn",
    "CursorControlledClient",
    "CursorProtocolError",
    "CursorQuestionRequest",
    "CursorSkill",
    "cursor_skill_from_indication",
    "mcp_question_decider",
]
