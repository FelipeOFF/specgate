"""Native prompt routing over the installed, public Specgate skill bundle."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:  # pragma: no cover - installed command
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from specgate.client import ReviewRequest
from specgate.codex_client import CodexControlledClient, CodexSkill
from specgate.codex_setup import _validate_url
from specgate.context import discover_skill_catalog, read_skill
from specgate.routing import route_skills


async def enabled_skills(project: Path) -> tuple[CodexSkill, ...]:
    # Catalog lookup only: never start a model turn (or recursively invoke hooks).
    async def unused_decision(_: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("Catalog lookup must not delegate questions.")

    async with CodexControlledClient(decide=unused_decision) as client:
        return await client.list_skills(project)


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
) -> dict[str, Any]:
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return {}
    prompt, cwd = payload.get("prompt"), payload.get("cwd")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(cwd, str):
        raise ValueError("Invalid prompt event.")
    profile = json.loads(profile_path.read_text())
    credential = Path(profile["credential_file"])
    raw = credential.read_bytes()
    if credential.stat().st_mode & 0o077 or hashlib.sha256(
        raw
    ).hexdigest() != profile.get("credential_sha256"):
        raise ValueError("Invalid managed credential.")
    _validate_url(profile["url"])
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
                return context_output(
                    f"Specgate selected {selected.id}. Instructions supplied to the harness; "
                    "this does not authorize execution, publication or merge.\n\n"
                    + instructions
                )
    gates = sorted(
        {
            str(row.get("result", {}).get("decision", {}).get("gate", {}).get("reason"))
            for row in result.get("evaluations", [])
            if row.get("result", {}).get("decision", {}).get("gate", {}).get("reason")
        }
    )
    return context_output(
        "Specgate native routing ran without authorizing automatic selection. "
        f"Status: {result.get('status', 'incomplete')}. Gates: {', '.join(gates) or 'review'}. "
        "Catalog: enabled public Specgate skills in this installation. "
        "Personal skills and project files require a separately authorized scope."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()

    async def run() -> dict[str, Any]:
        async with asyncio.timeout(45):
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("Hook input too large.")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise TypeError("Invalid hook input.")
            return await handle_prompt(payload, args.profile, args.source_root)

    try:
        output = asyncio.run(run())
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, ExceptionGroup):
        output = context_output(
            "Specgate native routing failed; no skill was selected. "
            "Run specgate doctor to inspect the installation."
        )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
