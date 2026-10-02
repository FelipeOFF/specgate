"""Interactive entry point for the public pipx client."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from specgate import flow, research, research_filter
from specgate.client import ReviewRequest
from specgate.product import (
    LEGACY_SKILL_SOURCE_ENV,
    SKILL_SOURCE_ENV,
    mcp_api_key,
    packaged_skills,
)
from specgate.public_package import PackageProbes, refresh_public_package
from specgate.public_setup import (
    HarnessName,
    detect_public_harnesses,
    doctor_public_harnesses,
    install_public_harnesses,
    managed_project_id,
    public_harness_capabilities,
    uninstall_public_harnesses,
)
from specgate.routing import route_skills
from specgate.transport import call_tool, negotiate_protocol
from specgate.workflow_policy import configure_workflow_policy, resolve_workflow_policy

_HARNESS_SETUP_HINTS = {
    "codex": "Codex — hook nativo de prompts e app-server controlado",
    "claude-code": "Claude Code — hooks nativos (UserPromptSubmit, PreToolUse)",
    "cursor": "Cursor — ACP controlado",
    "grok-build": "Grok Build — ACP controlado",
    "grok-bot": (
        "Grok Bot — cooperativo: copie GROK_BOT.md e anexe o Custom MCP; "
        "sem interceptação obrigatória"
    ),
}
_HARNESS_LABELS = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "cursor": "Cursor",
    "grok-build": "Grok Build",
    "grok-bot": "Grok Bot",
}
_ACTION_LABELS = {
    "created": "instalado",
    "unchanged": "já estava",
    "updated": "atualizado",
    "removed": "removido",
    "preserved": "preservado",
    "absent": "ausente",
}
_MANUAL_CONNECTOR = "conector manual"


def public_harness_setup_hints() -> dict[str, str]:
    """Return the installer line shown for each supported harness."""
    return dict(_HARNESS_SETUP_HINTS)


def _harness_label(name: str) -> str:
    return _HARNESS_LABELS.get(name, name)


def _status_row(name: str, status: str) -> str:
    return f"  {_harness_label(name):<13} {status}"


def _doctor_row(name: str, report: object) -> tuple[bool, str]:
    if not isinstance(report, dict):
        return False, _status_row(name, "falhou")
    if report.get("usable") is False:
        line = _status_row(name, "falhou")
        error = str(report.get("error") or "").strip()
        if error:
            return False, f"{line}\n    {error}"
        return False, line
    if name == "grok-bot":
        return True, _status_row(name, _MANUAL_CONNECTOR)
    if name == "codex" and isinstance(report.get("native_hook"), dict):
        return True, _status_row(name, "instalado; confira confiança do hook em /hooks")
    return True, _status_row(name, "ok")


def _ordered_harnesses(reports: Mapping[str, Any]) -> list[str]:
    names = [name for name in _HARNESS_LABELS if name in reports]
    names.extend(name for name in reports if name not in _HARNESS_LABELS)
    return names


def _format_doctor(result: Mapping[str, Any]) -> str:
    harnesses = result.get("harnesses")
    harnesses = harnesses if isinstance(harnesses, dict) else {}
    rows: list[str] = []
    ok = True
    for name in _ordered_harnesses(harnesses):
        passed, row = _doctor_row(name, harnesses[name])
        ok = ok and passed
        rows.append(row)
    title = "Specgate ok." if ok else "Specgate com falha."
    return "\n".join([title, "", *rows])


def _harness_line(name: str, report: object) -> str:
    if name == "grok-bot":
        return _MANUAL_CONNECTOR
    return _install_action(report)


def _install_action(report: object) -> str:
    if not isinstance(report, dict):
        return "ok"
    nested = report.get("adapter") or report.get("codex")
    source = nested if isinstance(nested, dict) else report
    if isinstance(source, dict):
        for key in ("mcp", "skill", "bundle"):
            value = source.get(key)
            if value in _ACTION_LABELS:
                return _ACTION_LABELS[value]
    return "ok"


def _append_session_env(text: str, result: Mapping[str, Any]) -> str:
    if not isinstance(result.get("session_env"), dict):
        return text
    return (
        text.rstrip() + "\n\nA key do MCP fica disponível em todo terminal novo. "
        "Feche o harness e abra de novo."
    )


def _format_install(result: Mapping[str, Any]) -> str:
    if result.get("installed") is False:
        return "Nenhum harness selecionado."
    doctor = result.get("doctor")
    if isinstance(doctor, dict) and isinstance(doctor.get("harnesses"), dict):
        text = _format_doctor(doctor)
        if doctor.get("status") == "configured_unverified":
            error = str(doctor.get("error") or "").strip()
            title = "Specgate instalado, verificação incompleta."
            extra = f"\n    {error}" if error else ""
            body = "\n".join(text.splitlines()[1:])
            return _append_session_env(f"{title}{extra}{body}", result)
        return _append_session_env(
            text.replace("Specgate ok.", "Specgate instalado.", 1).replace(
                "Specgate com falha.", "Specgate instalado, com falha.", 1
            ),
            result,
        )
    harnesses = result.get("harnesses")
    harnesses = harnesses if isinstance(harnesses, dict) else {}
    rows = [
        _status_row(name, _harness_line(name, harnesses[name]))
        for name in _ordered_harnesses(harnesses)
    ]
    return _append_session_env("\n".join(["Specgate instalado.", "", *rows]), result)


def _format_uninstall(result: Mapping[str, Any]) -> str:
    harnesses = result.get("harnesses")
    harnesses = harnesses if isinstance(harnesses, dict) else {}
    rows = [
        _status_row(name, _install_action(harnesses[name]))
        for name in _ordered_harnesses(harnesses)
    ]
    if not rows:
        return "Specgate removido."
    return "\n".join(["Specgate removido.", "", *rows])


def _format_update(result: Mapping[str, Any]) -> str:
    text = _format_install(result)
    return text.replace("instalado", "atualizado", 1)


def _format_smoke(result: Mapping[str, Any]) -> str:
    attached = result.get("attached_operation")
    if isinstance(attached, dict) and attached.get("status") == "completed":
        return "Smoke ok."
    return "Smoke falhou."


def format_public_report(command: str, result: Mapping[str, Any]) -> str:
    """Render a short human status; JSON callers use emit_public_report."""
    if command == "doctor":
        return _format_doctor(result)
    if command == "install":
        return _format_install(result)
    if command == "uninstall":
        return _format_uninstall(result)
    if command == "update":
        return _format_update(result)
    if command == "smoke":
        return _format_smoke(result)
    if command == "setup":
        return json.dumps(dict(result), ensure_ascii=False, indent=2)
    return ""


def emit_public_report(
    command: str,
    result: Mapping[str, Any],
    *,
    as_json: bool,
    stdout: TextIO,
) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=stdout)
        return
    print(format_public_report(command, result).rstrip(), file=stdout)


def _doctor_failed(result: Mapping[str, Any]) -> bool:
    harnesses = result.get("harnesses")
    if not isinstance(harnesses, dict):
        return False
    return any(
        isinstance(report, dict) and report.get("usable") is False
        for report in harnesses.values()
    )


def _ask(prompt: str, stdin: TextIO, stderr: TextIO) -> str:
    print(prompt, end="", file=stderr, flush=True)
    try:
        return stdin.readline()
    except KeyboardInterrupt:
        print("\nCancelado.", file=stderr)
        raise SystemExit(130) from None


def prompt_harness_selection(
    available: Sequence[HarnessName],
    *,
    stdin: TextIO,
    stderr: TextIO,
) -> tuple[HarnessName, ...]:
    """Toggle detected harnesses; empty input confirms the current selection."""
    if not available:
        print(
            "Nenhum harness suportado foi detectado nesta máquina.",
            file=stderr,
        )
        return ()
    selected = set(available)
    names = tuple(available)
    while True:
        print(
            "Harnesses detectados. a instala em todos; números alternam a seleção; Enter confirma.",
            file=stderr,
        )
        print(file=stderr)
        all_mark = "x" if selected == set(names) else " "
        print(
            f"  [{all_mark}] a  Todos — instalar em todos os harnesses detectados",
            file=stderr,
        )
        for index, name in enumerate(names, start=1):
            mark = "x" if name in selected else " "
            print(
                f"  [{mark}] {index}  {_HARNESS_SETUP_HINTS[name]}",
                file=stderr,
            )
        print(file=stderr)
        line = _ask(
            "Toggle (números ou a=todos) ou Enter para continuar: ",
            stdin,
            stderr,
        )
        if line == "" or not line.strip():
            break
        for token in line.replace(",", " ").split():
            key = token.lower()
            if key == "a":
                if selected == set(names):
                    selected.clear()
                else:
                    selected.update(names)
                continue
            if not token.isdigit() or not 1 <= int(token) <= len(names):
                print(f"Valor inválido: {token}", file=stderr)
                continue
            name = names[int(token) - 1]
            if name in selected:
                selected.remove(name)
            else:
                selected.add(name)
    if not selected:
        print("Nenhum harness selecionado.", file=stderr)
        return ()
    return tuple(name for name in names if name in selected)


def select_harnesses(
    *,
    available: Sequence[HarnessName],
    requested: Sequence[str] | None,
    accept_all: bool,
    stdin: TextIO,
    stderr: TextIO,
) -> tuple[HarnessName, ...]:
    """Resolve harnesses from flags or the interactive toggle prompt."""
    names = tuple(available)
    if requested:
        missing = sorted(set(requested) - set(names))
        if missing:
            raise ValueError("Harnesses not found: " + ", ".join(missing))
        wanted = set(requested)
        return tuple(name for name in names if name in wanted)
    if accept_all:
        return names
    return prompt_harness_selection(names, stdin=stdin, stderr=stderr)


def prompt_host(
    raw: str | None,
    *,
    stdin: TextIO,
    stderr: TextIO,
) -> str:
    """Read the MCP origin if it was not passed as --host."""
    if raw is None:
        raw = _ask("Host HTTPS do MCP: ", stdin, stderr).strip()
    return _endpoint(raw)


def prompt_api_key(
    *,
    stdin: TextIO,
    stderr: TextIO,
    environ: Mapping[str, str] | None = None,
    get_pass: Any = getpass.getpass,
) -> str:
    """Prefer SPECGATE_MCP_API_KEY, then DEV_DECISION_MCP_API_KEY; else prompt."""
    env = mcp_api_key(environ)
    if env:
        return env
    try:
        return str(get_pass("API key do MCP: "))
    except (EOFError, KeyboardInterrupt):
        print("\nCancelado.", file=stderr)
        raise SystemExit(130) from None


def _bundled_skills() -> Path:
    configured = os.environ.get(SKILL_SOURCE_ENV) or os.environ.get(
        LEGACY_SKILL_SOURCE_ENV
    )
    return Path(configured) if configured else packaged_skills()


def _endpoint(value: str) -> str:
    value = value.rstrip("/")
    return value if value.endswith(("/mcp", "/sse")) else f"{value}/mcp"


async def _smoke(project: Path, host: str, token: str) -> dict[str, Any]:
    project = project.resolve()
    skills = _bundled_skills().resolve()
    query = (
        "Resolve specification questions from authorized project evidence through "
        "the Specgate MCP, escalating only business choices, authorizations, "
        "and persistent gaps."
    )
    request = ReviewRequest(
        objective="Route the requirements clarification skill with authorized evidence.",
        tool="jev_find",
        arguments={"query": query},
        sources=["CONTEXT.md"],
        required=["CONTEXT.md"],
        artifact=query,
        skill_roots=[str(skills)],
    )
    project_id = managed_project_id()
    protocol = await negotiate_protocol(host, token, project_id=project_id)
    route = await route_skills(
        request,
        project,
        host,
        token,
        authorized_roots=[skills],
        public_roots=[skills],
        project_id=project_id,
    )
    operation = await call_tool(
        host,
        token,
        "jev_screen",
        {"text": "Public release smoke fixture.", "purpose": "Verify attached MCP."},
        project_id=project_id,
    )
    content = operation.structured_content
    if operation.is_error or not isinstance(content, dict):
        raise ValueError("Smoke requires a completed attached MCP operation.")
    mode = content.get("mode")
    if content.get("auto_advance") is True or content.get("calibrated") is True:
        raise ValueError("Smoke must not enable a gate or calibration.")
    if mode not in {"mock", "real"}:
        raise ValueError("Smoke requires a completed attached MCP operation.")
    candidate = route.get("candidate")
    calls = content.get("provider_calls")
    paid_calls = 0 if mode == "mock" else len(calls) if isinstance(calls, list) else 1
    return {
        "paid_calls": paid_calls,
        "calibration_calls": 0,
        "protocol": {
            "client_version": protocol.client_version,
            "server_version": protocol.server_version,
            "capabilities": sorted(protocol.capabilities),
        },
        "route": {
            "status": route["status"],
            "candidate": candidate.get("id") if isinstance(candidate, dict) else None,
            "action": route["action"],
        },
        "attached_operation": {
            "status": "completed",
            "action": content.get("action"),
            "mode": mode,
            "auto_advance": False,
            "calibrated": False,
        },
        "harnesses": public_harness_capabilities(),
    }


def _add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime o relatório em JSON",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
    probes: PackageProbes | None = None,
) -> None:
    parser = argparse.ArgumentParser(description="Instalador público Specgate")
    commands = parser.add_subparsers(dest="command", required=True)
    flow.configure(commands.add_parser("flow"))
    research.configure(commands.add_parser("research-with-jev"))
    research_filter.configure(commands.add_parser("research-filter-jev"))
    setup = commands.add_parser("setup")
    setup.add_argument("--project", type=Path, default=Path.cwd())
    setup.add_argument("--scope", choices=("global", "project"), default="global")
    setup.add_argument("--skill")
    setup.add_argument("--confidence", type=float)
    setup.add_argument("--research-budget", type=int)
    setup.add_argument("--tracker")
    setup.add_argument("--autonomy", choices=("manual", "until_draft"))
    setup.add_argument("--allow-project", action="append")
    setup.add_argument("--allow-command", action="append")
    setup.add_argument("--allow-host", action="append")
    setup.add_argument("--allow-repository", action="append")
    setup.add_argument(
        "--allow-publication", action="append", choices=("spec", "tickets", "draft_pr")
    )
    setup.add_argument("--allow-file-edits", action=argparse.BooleanOptionalAction)
    setup.add_argument("--authorization-reference")
    setup.add_argument("--expires-at")
    setup.add_argument("--require-capability", action="append")
    setup.add_argument("--personal-skill", action="append")
    setup.add_argument("--project-file", action="append")
    setup.add_argument("--clear-adapter-scope", action="store_true")
    _add_json_flag(setup)
    install = commands.add_parser("install")
    install.add_argument("--host")
    install.add_argument(
        "--harness",
        action="append",
        choices=tuple(public_harness_capabilities()),
    )
    install.add_argument("--yes", action="store_true")
    _add_json_flag(install)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--project", type=Path, default=Path.cwd())
    doctor.add_argument("--timeout", type=float, default=30)
    _add_json_flag(doctor)
    update = commands.add_parser("update")
    _add_json_flag(update)
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--host", required=True)
    smoke.add_argument("--project", type=Path, default=Path.cwd())
    _add_json_flag(smoke)
    uninstall = commands.add_parser("uninstall")
    _add_json_flag(uninstall)
    args = parser.parse_args(argv)
    as_json = bool(getattr(args, "json", False))

    try:
        if args.command == "flow":
            result = flow.execute(
                args, home=home, project_id=managed_project_id(home=home)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.command == "research-with-jev":
            print(json.dumps(research.execute(args), ensure_ascii=False, indent=2))
            return
        if args.command == "research-filter-jev":
            print(
                json.dumps(research_filter.execute(args), ensure_ascii=False, indent=2)
            )
            return
        if args.command == "setup":
            from specgate.delegation import load_delegation

            delegation = {
                key: value
                for key, value in {
                    "mode": args.autonomy,
                    "projects": args.allow_project,
                    "commands": args.allow_command,
                    "hosts": args.allow_host,
                    "repositories": args.allow_repository,
                    "publications": args.allow_publication,
                    "file_edits": args.allow_file_edits,
                    "reference": args.authorization_reference,
                    "expires_at": args.expires_at,
                }.items()
                if value is not None
            }
            if delegation and (args.scope != "global" or args.skill):
                raise ValueError(
                    "A delegação pertence ao usuário; configure-a no escopo global."
                )
            changes = {
                key: value
                for key, value in {
                    "confidence": args.confidence,
                    "research_budget": args.research_budget,
                    "tracker": args.tracker,
                    "required_capabilities": args.require_capability,
                }.items()
                if value is not None
            }
            if (
                args.personal_skill is not None
                or args.project_file is not None
                or args.clear_adapter_scope
            ):
                changes["adapter_scope"] = {
                    "personal_skills": args.personal_skill or [],
                    "project_files": args.project_file or [],
                }
            if changes or delegation:
                result = configure_workflow_policy(
                    changes,
                    home=home,
                    project=args.project,
                    skill=args.skill,
                    scope=args.scope,
                    delegation_changes=delegation if delegation else None,
                )
            else:
                result = resolve_workflow_policy(
                    home=home, project=args.project, skill=args.skill
                )
            grant = load_delegation(home)
            result["delegation"] = {**grant.model_dump(), "revision": grant.revision}
        elif args.command == "install":
            selected = select_harnesses(
                available=detect_public_harnesses(),
                requested=args.harness,
                accept_all=args.yes,
                stdin=sys.stdin,
                stderr=sys.stderr,
            )
            if not selected:
                emit_public_report(
                    "install",
                    {"installed": False, "harnesses": []},
                    as_json=as_json,
                    stdout=sys.stdout,
                )
                return
            host = prompt_host(args.host, stdin=sys.stdin, stderr=sys.stderr)
            api_key = prompt_api_key(stdin=sys.stdin, stderr=sys.stderr)
            result = install_public_harnesses(
                _bundled_skills(),
                host,
                api_key,
                selected,
            )
            try:
                result["doctor"] = asyncio.run(doctor_public_harnesses(Path.cwd()))
            except (ValueError, OSError, ExceptionGroup, RuntimeError) as error:
                result["doctor"] = {
                    "status": "configured_unverified",
                    "error": str(error),
                }
        elif args.command == "doctor":
            try:
                result = asyncio.run(
                    doctor_public_harnesses(args.project, timeout_seconds=args.timeout)
                )
            except (ValueError, OSError, ExceptionGroup, RuntimeError) as error:
                if as_json:
                    print(
                        json.dumps(
                            {"ok": False, "error": str(error)},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    print(str(error))
                raise SystemExit(1) from None
            emit_public_report("doctor", result, as_json=as_json, stdout=sys.stdout)
            if _doctor_failed(result):
                raise SystemExit(1)
            return
        elif args.command == "update":
            result = refresh_public_package(
                home=home,
                codex_command=codex_command,
                probes=probes,
            )
        elif args.command == "smoke":
            api_key = mcp_api_key()
            result = asyncio.run(_smoke(args.project, _endpoint(args.host), api_key))
        else:
            result = uninstall_public_harnesses()
        emit_public_report(args.command, result, as_json=as_json, stdout=sys.stdout)
        if args.command == "install":
            doctor_report = result.get("doctor")
            if isinstance(doctor_report, dict) and _doctor_failed(doctor_report):
                raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nCancelado.", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, OSError, ExceptionGroup) as error:
        if as_json:
            print(
                json.dumps(
                    {"ok": False, "error": str(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(1) from None
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
