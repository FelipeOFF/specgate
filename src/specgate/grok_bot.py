"""Cooperative Grok Bot bundle and decision flow."""

from __future__ import annotations

import ipaddress
import plistlib
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from specgate.codex_setup import (
    _files,
    _manifest,
    _owned_and_unchanged,
    _skill_dir,
    _validate_url,
    _write_marker,
)
from specgate.gate_policy import recommendation_basis
from specgate.mcp_catalog import merge_json_mcp, remove_json_mcp
from specgate.transport import MCPTransportError, list_tools

_NAME = "specgate"
_SKILL: Literal["specgate"] = "specgate"
_MARKER = ".specgate-install.json"
_REQUIRED_TOOLS = {"jev_decide", "jev_find", "jev_screen", "jev_verify"}

DecisionFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class GrokBotSetupReport(TypedDict):
    bundle: Literal["created", "unchanged", "removed", "preserved", "absent"]
    capability: Literal["cooperative", "unavailable"]
    cloud_installation: Literal["manual_required"]
    skill_entry: Literal["private_skill_manual"]
    mcp_entry: Literal["custom_connector_manual"]


class GrokBotUninstallReport(TypedDict):
    bundle: Literal["removed", "preserved", "absent"]
    cloud_installation: Literal["manual_required"]


@dataclass(frozen=True)
class GrokBotQuestion:
    id: str
    prompt: str
    options: tuple[tuple[str, str], ...]
    question_type: Literal["single_choice", "open", "multi_select"] = "single_choice"
    requires_authorization: bool = False
    missing_personal_fact: bool = False

    def __post_init__(self) -> None:
        option_ids = [option_id for option_id, _ in self.options]
        if (
            not self.id.strip()
            or not self.prompt.strip()
            or not 2 <= len(self.options) <= 48
            or any(
                not option_id.strip() or not text.strip()
                for option_id, text in self.options
            )
            or len(set(option_ids)) != len(option_ids)
        ):
            raise ValueError("Grok Bot questions require unique, non-empty options.")


@dataclass(frozen=True)
class GrokBotTurn:
    status: Literal["answered", "needs_human"]
    skill: Literal["specgate"] = _SKILL
    question: GrokBotQuestion | None = None
    selected_option: str | None = None
    answer_origin: Literal["automated", "human"] | None = None
    mcp_consulted: bool = False
    error: str | None = None
    warning: str | None = None


class GrokBotCooperativeFlow:
    """Apply the private-skill policy around one structured Bot question."""

    offline_warning = (
        "Specgate MCP is unavailable; continue in Grok Bot without automated review."
    )

    def __init__(self, decide: DecisionFn) -> None:
        self._decide = decide
        self._pending: GrokBotQuestion | None = None
        self._offline = False
        self._offline_warning_emitted = False

    async def run(self, prompt: str, question: GrokBotQuestion) -> GrokBotTurn:
        self._pending = question
        request = {
            "objective": prompt,
            "tool": "jev_decide",
            "arguments": {
                "question": question.prompt,
                "options": [
                    {"id": option_id, "text": text}
                    for option_id, text in question.options
                ],
                "question_type": question.question_type,
                "requires_authorization": question.requires_authorization,
                "missing_personal_fact": question.missing_personal_fact,
            },
            "sources": [],
            "required": [],
            "artifact": question.prompt,
        }
        if self._offline:
            return GrokBotTurn(
                "needs_human",
                question=question,
                mcp_consulted=False,
                error="mcp_unavailable",
            )
        for attempt in range(2):
            try:
                result = await self._decide(request)
                break
            except (
                ExceptionGroup,
                MCPTransportError,
                OSError,
                TimeoutError,
                ValueError,
            ):
                if attempt == 0:
                    continue
                self._offline = True
                warning = (
                    None if self._offline_warning_emitted else self.offline_warning
                )
                self._offline_warning_emitted = True
                return GrokBotTurn(
                    "needs_human",
                    question=question,
                    mcp_consulted=True,
                    error="mcp_unavailable",
                    warning=warning,
                )
        decision = result.get("decision")
        selected = (
            decision.get("selected_option") if isinstance(decision, dict) else None
        )
        automatable = (
            question.question_type == "single_choice"
            and not question.requires_authorization
            and not question.missing_personal_fact
        )
        if (
            automatable
            and result.get("action") == "auto"
            and result.get("origin") == "automated"
            and result.get("execution_authorized") is False
            and isinstance(decision, dict)
            and decision.get("mode") == "real"
            and recommendation_basis(decision) is not None
            and decision.get("auto_advance") is True
            and selected in {option_id for option_id, _ in question.options}
        ):
            self._pending = None
            return GrokBotTurn(
                "answered",
                selected_option=selected,
                answer_origin="automated",
                mcp_consulted=True,
            )
        return GrokBotTurn(
            "needs_human",
            question=question,
            mcp_consulted=True,
        )

    def answer(
        self, question: GrokBotQuestion | None, selected_option: str
    ) -> GrokBotTurn:
        if question is None or question is not self._pending:
            raise ValueError("Question is no longer pending.")
        if selected_option not in {option_id for option_id, _ in question.options}:
            raise ValueError("Answer must use one current question option.")
        self._pending = None
        return GrokBotTurn(
            "answered",
            selected_option=selected_option,
            answer_origin="human",
            mcp_consulted=True,
        )


def _cloud_reachable(url: str) -> bool:
    endpoint = urlsplit(url)
    try:
        loopback = ipaddress.ip_address(endpoint.hostname or "").is_loopback
    except ValueError:
        loopback = endpoint.hostname == "localhost"
    return endpoint.scheme == "https" and not loopback


def install_grok_bot(
    source: Path, bundle_root: Path, url: str, *, token: str | None = None
) -> GrokBotSetupReport:
    """Prepare the owned skill bundle for the documented manual Bot entries."""
    _validate_url(url)
    source = source.resolve()
    bundle_root = bundle_root.expanduser().resolve()
    existing = _skill_dir(bundle_root)
    target = bundle_root / _SKILL
    if not (source / "SKILL.md").is_file():
        raise ValueError("The Specgate skill must contain SKILL.md.")
    previous = _manifest(existing) if existing.exists() else None
    if existing.exists() and (
        previous is None or not _owned_and_unchanged(existing, previous)
    ):
        raise ValueError("The destination bundle exists and is not plugin-managed.")

    mcp_path = bundle_root / "mcp.json"
    mcp_status = merge_json_mcp(mcp_path, url, include_type=False, token=token)
    previous_mcp = (previous or {}).get("mcp")
    source_files = _files(source)
    manifest = {
        "version": 1,
        "owner": _NAME,
        "files": source_files,
        "grok_bot": {
            "mcp_url": url,
            "skill_entry": "private_skill_manual",
            "mcp_entry": "custom_connector_manual",
        },
        "mcp": {
            "name": _NAME,
            "url": url,
            "path": str(mcp_path),
            "created": mcp_status == "created"
            or (isinstance(previous_mcp, dict) and previous_mcp.get("created")),
        },
    }
    if (
        previous is not None
        and previous.get("files") == source_files
        and existing == target
    ):
        _write_marker(target, manifest)
        status: Literal["created", "unchanged"] = "unchanged"
    else:
        bundle_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{_SKILL}.", dir=bundle_root))
        staging.rmdir()
        shutil.copytree(source, staging)
        _write_marker(staging, manifest)
        if target.exists():
            shutil.rmtree(target)
        staging.rename(target)
        if existing != target and existing.exists():
            shutil.rmtree(existing)
        status = "created"
    return {
        "bundle": status,
        "capability": "cooperative" if _cloud_reachable(url) else "unavailable",
        "cloud_installation": "manual_required",
        "skill_entry": "private_skill_manual",
        "mcp_entry": "custom_connector_manual",
    }


async def diagnose_grok_bot(
    bundle_root: Path,
    url: str,
    token: str,
    *,
    app_bundle: Path = Path("/Applications/Grok Bot.app"),
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Inspect the local app and MCP without sending a Bot prompt."""
    _validate_url(url)
    target = _skill_dir(bundle_root)
    manifest = _manifest(target) if target.exists() else None
    if manifest is None or not _owned_and_unchanged(target, manifest):
        raise ValueError("The managed Grok Bot bundle is missing or modified.")
    config = manifest.get("grok_bot")
    if not isinstance(config, dict) or config.get("mcp_url") != url:
        raise ValueError("The managed Grok Bot endpoint does not match the diagnosis.")

    info_path = app_bundle.expanduser() / "Contents/Info.plist"
    runtime: dict[str, Any] = {"installed": False, "product": "Grok Bot"}
    if info_path.is_file():
        with info_path.open("rb") as file:
            info = plistlib.load(file)
        runtime = {
            "installed": True,
            "product": info.get("CFBundleName", "Grok Bot"),
            "version": info.get("CFBundleShortVersionString", "unknown"),
            "bundle_id": info.get("CFBundleIdentifier", "unknown"),
        }

    tools = await list_tools(url, token, timeout_seconds=timeout_seconds)
    if not _REQUIRED_TOOLS <= set(tools):
        raise ValueError("The MCP server does not expose all required decision tools.")
    cooperative = bool(runtime["installed"] and _cloud_reachable(url))
    return {
        "capability": "cooperative" if cooperative else "unavailable",
        "compatibility_tested": False,
        "runtime": runtime,
        "bundle": {
            "path": str(target),
            "skill_entry": "private_skill_manual",
            "prepared": True,
        },
        "mcp": {
            "url": url,
            "connector_entry": "custom_connector_manual",
            "endpoint_scope": (
                "public_https_candidate" if _cloud_reachable(url) else "local_only"
            ),
            "bot_reachability": "unverified",
            "tools": sorted(tools),
        },
        "harness_access": "unavailable",
        "structured_questions": "unverified",
        "mandatory_interception": "no_public_contract",
        "limitations": [
            "The public Bot interface requires manual private-skill and connector setup.",
            "The controlled flow is verified with a double, not the native Bot runtime.",
            "Free-form and native questions are not intercepted by a public contract.",
        ],
    }


def uninstall_grok_bot(bundle_root: Path) -> GrokBotUninstallReport:
    """Remove only an unchanged bundle; cloud entries remain user-managed."""
    target = _skill_dir(bundle_root)
    manifest = _manifest(target) if target.exists() else None
    status: Literal["removed", "preserved", "absent"]
    mcp = manifest.get("mcp") if isinstance(manifest, dict) else None
    if isinstance(mcp, dict):
        remove_json_mcp(
            Path(str(mcp.get("path") or bundle_root / "mcp.json")),
            str(mcp.get("url", "")),
            created=mcp.get("created") is True,
            include_type=False,
        )
    if manifest is None:
        status = "preserved" if target.exists() else "absent"
    elif _owned_and_unchanged(target, manifest):
        shutil.rmtree(target)
        status = "removed"
    else:
        status = "preserved"
    return {"bundle": status, "cloud_installation": "manual_required"}
