"""Idempotent Claude Code installation owned by Specgate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, TypedDict

from specgate.codex_setup import (
    _files,
    _manifest,
    _owned_and_unchanged,
    _skill_dir,
    _validate_url,
    _write_marker,
)
from specgate.mcp_catalog import (
    entry_matches,
    json_http_server,
    merge_json_mcp,
    remove_json_mcp,
    specgate_http_owned,
)
from specgate.product import LEGACY_MCP_SERVER, MCP_SERVER, SKILL, TOKEN_ENV
from specgate.transport import list_tools

_NAME = MCP_SERVER
_SKILL = SKILL
_REQUIRED_TOOLS = {"jev_decide", "jev_find", "jev_screen", "jev_verify"}


class ClaudeSetupReport(TypedDict):
    skill: Literal["created", "unchanged", "removed", "preserved", "absent"]
    hooks: Literal["created", "unchanged", "removed", "preserved", "absent"]
    mcp: Literal["created", "unchanged", "updated", "removed", "preserved", "absent"]


def _run(command: Sequence[str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*command, *args], check=False, capture_output=True, text=True
    )


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Claude Code settings.json is invalid.") from error
    if not isinstance(value, dict):
        raise TypeError("Claude Code settings.json is invalid.")
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict) or any(
        not isinstance(entries, list) for entries in hooks.values()
    ):
        raise ValueError("Claude Code settings.json contains invalid hooks.")
    return value


def _write_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(settings, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _hook_entries(
    config_dir: Path,
    url: str,
    python_command: Sequence[str],
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    if not python_command:
        raise ValueError("A Python command is required to install hooks.")
    script = Path(__file__).with_name("claude_hook.py").resolve()
    hook = {
        "type": "command",
        "command": python_command[0],
        "args": [
            *python_command[1:],
            "-B",
            str(script),
            "--url",
            url,
            "--skill-root",
            str((config_dir / "skills").resolve()),
            "--timeout",
            str(timeout_seconds),
            "--token-env",
            TOKEN_ENV,
            "--state-root",
            str((config_dir / ".specgate").resolve()),
            "--config-dir",
            str(config_dir.resolve()),
        ],
        "timeout": timeout_seconds,
    }
    return {
        "UserPromptSubmit": {"hooks": [hook]},
        "PreToolUse": {"matcher": "AskUserQuestion", "hooks": [hook]},
        "PermissionRequest": {"hooks": [hook]},
    }


def _user_catalog(config_dir: Path) -> Path:
    return config_dir.parent / ".claude.json"


def _drop_settings_mcp(
    settings: dict[str, Any], url: str, entry: dict[str, Any]
) -> None:
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        return
    for name in (_NAME, LEGACY_MCP_SERVER):
        existing = servers.get(name)
        if existing is None:
            continue
        if entry_matches(existing, entry) or specgate_http_owned(existing, url):
            servers.pop(name, None)
    if not servers:
        settings.pop("mcpServers", None)


def _require_user_mcp(config_dir: Path, url: str) -> None:
    message = (
        "Claude Code has no specgate server in the user MCP registry (~/.claude.json)."
    )
    path = _user_catalog(config_dir)
    if not path.is_file():
        raise ValueError(message)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Claude Code .claude.json is invalid.") from error
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    entry = servers.get(_NAME) if isinstance(servers, dict) else None
    if not specgate_http_owned(entry, url):
        raise ValueError(message)


def _restore_catalog(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(previous)


def _remove_entries(
    settings: dict[str, Any], entries: dict[str, dict[str, Any]]
) -> tuple[int, int]:
    hooks = settings.setdefault("hooks", {})
    removed = 0
    for event, entry in entries.items():
        values = hooks.get(event, [])
        if entry in values:
            values.remove(entry)
            removed += 1
        if not values:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return removed, len(entries)


def install_claude(
    source: Path,
    config_dir: Path,
    url: str,
    *,
    python_command: Sequence[str] = (sys.executable,),
    timeout_seconds: float = 30,
    token: str | None = None,
) -> ClaudeSetupReport:
    """Install the skill, native hooks, and the user-scoped Claude MCP server."""
    _validate_url(url)
    if timeout_seconds <= 0:
        raise ValueError("Timeout must be greater than zero.")
    source = source.resolve()
    config_dir = config_dir.expanduser().resolve()
    existing = _skill_dir(config_dir / "skills")
    target = (config_dir / "skills" / _SKILL).resolve()
    settings_path = config_dir / "settings.json"
    if not (source / "SKILL.md").is_file():
        raise ValueError("The Specgate skill must contain SKILL.md.")
    previous = _manifest(existing) if existing.exists() else None
    if existing.exists() and (
        previous is None or not _owned_and_unchanged(existing, previous)
    ):
        raise ValueError("The destination skill exists and is not plugin-managed.")

    settings = _read_settings(settings_path)
    old_settings = json.loads(json.dumps(settings))
    previous_hooks = (previous or {}).get("hooks", {})
    if isinstance(previous_hooks, dict):
        configured = settings.get("hooks", {})
        if any(
            entry not in configured.get(event, [])
            for event, entry in previous_hooks.items()
        ):
            raise ValueError("A managed Claude Code hook was modified.")
        _remove_entries(settings, previous_hooks)
    entries = _hook_entries(config_dir, url, python_command, timeout_seconds)
    hooks = settings.setdefault("hooks", {})
    owned: dict[str, dict[str, Any]] = {}
    for event, entry in entries.items():
        values = hooks.setdefault(event, [])
        was_owned = (
            isinstance(previous_hooks, dict) and previous_hooks.get(event) == entry
        )
        if entry not in values:
            values.append(entry)
            owned[event] = entry
        elif was_owned:
            owned[event] = entry

    user_catalog = _user_catalog(config_dir)
    catalog_bytes = user_catalog.read_bytes() if user_catalog.is_file() else None
    mcp_entry = json_http_server(url, include_type=True, token=token)
    _drop_settings_mcp(settings, url, mcp_entry)
    previous_mcp = (previous or {}).get("mcp")

    source_files = _files(source)
    mcp_record: dict[str, object] = {
        "name": _NAME,
        "url": url,
        "catalog": str(user_catalog),
        "created": False,
    }
    manifest = {
        "version": 1,
        "owner": _NAME,
        "files": source_files,
        "hooks": owned,
        "settings": str(settings_path),
        "url": url,
        "mcp": mcp_record,
    }
    skill_status: Literal["created", "unchanged"] = (
        "unchanged"
        if previous is not None
        and previous.get("files") == source_files
        and existing == target
        else "created"
    )
    wrote_settings = False
    try:
        mcp_status = merge_json_mcp(user_catalog, url, include_type=True, token=token)
        mcp_record["created"] = mcp_status == "created" or (
            isinstance(previous_mcp, dict) and previous_mcp.get("created") is True
        )
        _write_settings(settings_path, settings)
        wrote_settings = True
        if skill_status == "unchanged":
            _write_marker(target, manifest)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{_SKILL}.", dir=target.parent))
            staging.rmdir()
            shutil.copytree(source, staging)
            _write_marker(staging, manifest)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            if existing != target and existing.exists():
                shutil.rmtree(existing)
    except Exception:
        if wrote_settings:
            _write_settings(settings_path, old_settings)
        _restore_catalog(user_catalog, catalog_bytes)
        raise
    return {
        "skill": skill_status,
        "hooks": "created" if settings != old_settings else "unchanged",
        "mcp": mcp_status,
    }


def _require_managed_hooks(config_dir: Path) -> None:
    """Raise unless the managed skill and every hook it recorded are in place."""
    target = _skill_dir(config_dir / "skills")
    manifest = _manifest(target) if target.exists() else None
    if manifest is None or not _owned_and_unchanged(target, manifest):
        raise ValueError("The managed Specgate skill is missing or modified.")
    settings = _read_settings(config_dir / "settings.json")
    entries = manifest.get("hooks")
    if not isinstance(entries, dict) or set(entries) != {
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
    }:
        raise ValueError("The managed Claude Code hooks are missing.")
    hooks = settings.get("hooks", {})
    if any(entry not in hooks.get(event, []) for event, entry in entries.items()):
        raise ValueError("The managed Claude Code hooks are missing.")


def hooks_installed(config_dir: Path) -> bool:
    """Whether the managed Claude Code hooks are registered, as install left them.

    Registered is not executed: this never shows that a hook ran.
    """
    try:
        _require_managed_hooks(config_dir.expanduser().resolve())
    except (OSError, TypeError, ValueError):
        return False
    return True


async def diagnose_claude(
    project: Path,
    config_dir: Path,
    url: str,
    token: str,
    *,
    claude_command: Sequence[str] = ("claude",),
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Verify the installed runtime, managed hooks and authenticated MCP catalog."""
    project = project.resolve()
    if not project.is_dir():
        raise ValueError("The project directory is unavailable.")
    config_dir = config_dir.expanduser().resolve()
    target = _skill_dir(config_dir / "skills")
    _require_managed_hooks(config_dir)
    _require_user_mcp(config_dir, url)
    version = _run(claude_command, "--version")
    if version.returncode or not version.stdout.strip():
        raise ValueError("The Claude Code runtime is unavailable.")
    tools = await list_tools(url, token, timeout_seconds=timeout_seconds)
    if not _REQUIRED_TOOLS <= set(tools):
        raise ValueError("The MCP server does not expose all decision tools.")
    return {
        "usable": True,
        "runtime": version.stdout.strip(),
        "skill": {
            "name": _SKILL,
            "loaded": True,
            "path": str((target / "SKILL.md").resolve()),
        },
        "hooks": {
            "UserPromptSubmit": "installed",
            "PreToolUse/AskUserQuestion": "installed",
            "PermissionRequest": "installed",
        },
        "mcp": {
            "url": url,
            "registry": "user",
            "authentication": f"bearer_env:{TOKEN_ENV}",
            "transport": "streamable_http_sse",
            "tools": sorted(tools),
        },
        "scope": "native_claude_code_hooks",
        "limitations": [
            "Perguntas abertas, múltiplas ou sem escolha autorizada continuam humanas.",
            "A instalação não habilita gates nem armazena credenciais de modelo.",
        ],
    }


def uninstall_claude(config_dir: Path) -> ClaudeSetupReport:
    """Remove only unchanged skill and hook entries owned by this installer."""
    config_dir = config_dir.expanduser().resolve()
    target = _skill_dir(config_dir / "skills")
    manifest = _manifest(target) if target.exists() else None
    if manifest is None:
        return {
            "skill": "preserved" if target.exists() else "absent",
            "hooks": "absent",
            "mcp": "absent",
        }
    settings_path = config_dir / "settings.json"
    settings = _read_settings(settings_path)
    entries = manifest.get("hooks")
    if isinstance(entries, dict):
        removed, expected = _remove_entries(settings, entries)
        if removed:
            _write_settings(settings_path, settings)
        hooks_status: Literal["removed", "preserved", "absent"] = (
            "removed" if removed == expected else "preserved"
        )
    else:
        hooks_status = "absent"
    mcp = manifest.get("mcp")
    mcp_url = str(mcp.get("url", "")) if isinstance(mcp, dict) else ""
    mcp_created = isinstance(mcp, dict) and mcp.get("created") is True
    mcp_status = remove_json_mcp(
        _user_catalog(config_dir),
        mcp_url,
        created=mcp_created,
        include_type=True,
    )
    legacy_status = remove_json_mcp(
        settings_path,
        mcp_url,
        created=mcp_created,
        include_type=True,
    )
    if mcp_status == "absent" and legacy_status == "removed":
        mcp_status = "removed"
    if _owned_and_unchanged(target, manifest):
        shutil.rmtree(target)
        skill_status: Literal["removed", "preserved"] = "removed"
    else:
        skill_status = "preserved"
    return {"skill": skill_status, "hooks": hooks_status, "mcp": mcp_status}
