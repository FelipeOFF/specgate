"""Native prompt routing over the installed, public Specgate skill bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - installed command
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from specgate.client import ReviewRequest
from specgate.codex_client import (
    CodexControlledClient,
    CodexSkill,
)
from specgate.codex_setup import _validate_url
from specgate.context import discover_skill_catalog, read_skill
from specgate.delegation import (
    handle_permission,
    mcp_permission_decider,
    permission_output,
)
from specgate.product import packaged_skills
from specgate.routing import NativeHook, confirm_skill_loaded, route_skills
from specgate.workflow_policy import resolve_workflow_policy


async def enabled_skills(project: Path) -> tuple[CodexSkill, ...]:
    # Catalog lookup only: never start a model turn (or recursively invoke hooks).
    async def unused_decision(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Catalog lookup must not delegate questions.")

    async with CodexControlledClient(decide=unused_decision) as client:
        return await client.list_skills(project)


def hook_installed(profile: dict[str, Any]) -> bool:
    """Whether the managed `UserPromptSubmit` entry is still in the Codex hooks file.

    Installed is not trusted or executed: Codex asks for its own trust approval.
    """
    try:
        record = profile["codex_hook"]
        settings = json.loads(Path(record["path"]).read_text())
        return bool(record["entry"] in settings["hooks"]["UserPromptSubmit"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def context_output(text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


async def handle_prompt(
    payload: dict[str, Any],
    profile_path: Path,
    source_root: Path,
    *,
    # The public skills to compare with; None means the bundle this package ships,
    # never the source root, which the installer may have read from anywhere.
    public_roots: Sequence[Path] | None = None,
) -> dict[str, Any]:
    event = payload.get("hook_event_name")
    if event not in {"UserPromptSubmit", "PermissionRequest"}:
        return {}
    if event == "PermissionRequest":
        from specgate.delegation import load_delegation

        if load_delegation().mode == "manual":
            return {}
    prompt, cwd = payload.get("prompt"), payload.get("cwd")
    if not isinstance(cwd, str) or (
        event == "UserPromptSubmit"
        and (not isinstance(prompt, str) or not prompt.strip())
    ):
        raise ValueError("Invalid prompt event.")
    profile = json.loads(profile_path.read_text())
    credential = Path(profile["credential_file"])
    raw = credential.read_bytes()
    if credential.stat().st_mode & 0o077 or hashlib.sha256(
        raw
    ).hexdigest() != profile.get("credential_sha256"):
        raise ValueError("Invalid managed credential.")
    _validate_url(profile["url"])
    if event == "PermissionRequest":
        policy = resolve_workflow_policy(project=Path(cwd))
        return await handle_permission(
            payload,
            host=profile["url"],
            confidence=policy["effective_confidence"],
            decide=mcp_permission_decider(profile["url"], raw.decode().strip()),
        )
    assert isinstance(prompt, str)
    root = source_root.resolve()
    catalog = discover_skill_catalog([root], authorized_roots=[root])
    enabled = {
        skill.name: Path(skill.path) for skill in await enabled_skills(Path(cwd))
    }
    disabled: set[str] = set()
    for skill in catalog:
        installed = enabled.get(Path(skill.reference).parent.name)
        if (
            installed is None
            or not installed.is_file()
            or hashlib.sha256(installed.read_bytes()).hexdigest() != skill.revision
        ):
            disabled.update(skill.aliases)
    # No project files, personal catalogs or native session metadata go to the MCP.
    request = ReviewRequest(
        objective=prompt,
        tool="jev_find",
        arguments={"query": prompt, "candidates": []},
        sources=["specgate/references/README.md"],
        skill_roots=[str(root)],
    )
    result = await route_skills(
        request,
        root,
        profile["url"],
        raw.decode().strip(),
        authorized_roots=[root],
        disabled_ids=disabled,
        public_roots=[packaged_skills()]
        if public_roots is None
        else list(public_roots),
        timeout_seconds=15,
    )
    candidate = result.get("candidate")
    if (
        result.get("action") == "auto"
        and result.get("auto_advance") is True
        and result.get("origin") == "automated"
        and isinstance(candidate, dict)
    ):
        selected = next((s for s in catalog if s.id == candidate.get("id")), None)
        if selected and not set(selected.aliases) & disabled:
            instructions = read_skill(selected, [root])
            if candidate.get("revision") == selected.revision:
                # This handler is the hook: it runs now and hands the instructions to Codex.
                hook = NativeHook(
                    harness="codex",
                    event="UserPromptSubmit",
                    installed=hook_installed(profile),
                    executed=True,
                    skill_id=selected.id,
                    revision=selected.revision,
                )
                confirmed = confirm_skill_loaded(
                    result,
                    selected.id,
                    selected.revision,
                    authorized_roots=[root],
                    disabled_ids=disabled,
                    hook=hook,
                )
                loaded = confirmed.get("candidate")
                if isinstance(loaded, dict) and loaded.get("loaded") is True:
                    return context_output(
                        f"Specgate selected {selected.id}. Instructions supplied to the harness; "
                        "this does not authorize execution, publication or merge.\n\n"
                        + instructions
                    )
                result = confirmed
    gates = sorted(
        {
            str(row.get("result", {}).get("decision", {}).get("gate", {}).get("reason"))
            for row in result.get("evaluations", [])
            if row.get("result", {}).get("decision", {}).get("gate", {}).get("reason")
        }
    )
    loading = result.get("loading")
    unconfirmed = (
        f"Skill loading was not confirmed by the native hook ({loading['hook']}). "
        if isinstance(loading, dict)
        else ""
    )
    return context_output(
        "Specgate native routing ran without authorizing automatic selection. "
        f"Status: {result.get('status', 'incomplete')}. Gates: {', '.join(gates) or 'review'}. "
        f"{unconfirmed}"
        "Catalog: enabled public Specgate skills in this installation. "
        "Personal skills and project files require a separately authorized scope."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()

    event = "UserPromptSubmit"

    async def run() -> dict[str, Any]:
        nonlocal event
        async with asyncio.timeout(45):
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Hook input too large.")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("Invalid hook input.")
            event = payload.get("hook_event_name", "UserPromptSubmit")
            return await handle_prompt(payload, args.profile, args.source_root)

    try:
        output = asyncio.run(run())
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, ExceptionGroup):
        output = (
            permission_output(False, "hook indisponível; preserve a ação pendente")
            if event == "PermissionRequest"
            else context_output(
                "Specgate native routing failed; no skill was selected. "
                "Run specgate doctor to inspect the installation."
            )
        )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
