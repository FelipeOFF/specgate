"""Skills Claude Code keeps from the model, read from its settings files."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from specgate.context import SkillSource

_VISIBLE = frozenset({"on", "name-only"})
_SETTINGS = ("settings.json", "settings.local.json")


def _settings(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"{path.name} is not a JSON object.")
    return value


def _hidden_patterns(settings: dict[str, Any]) -> Iterable[str]:
    """Skill names or patterns that this settings file hides from the model."""
    overrides = settings.get("skillOverrides", {})
    if not isinstance(overrides, dict):
        raise TypeError("skillOverrides is not an object.")
    # Anything but on or name-only hides the skill, a value Claude Code may add later.
    yield from (name for name, mode in overrides.items() if mode not in _VISIBLE)
    permissions = settings.get("permissions", {})
    rules = permissions.get("deny", []) if isinstance(permissions, dict) else None
    if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
        raise TypeError("permissions.deny is not a list of rules.")
    for rule in rules:
        rule = rule.strip()
        if rule == "Skill":
            yield "*"
        elif rule.startswith("Skill(") and rule.endswith(")"):
            # `Skill(name)` is exact and `Skill(name *)` takes any arguments.
            yield rule[len("Skill(") : -1].strip().removesuffix(" *").strip()


def disabled_skill_ids(
    catalog: Sequence[SkillSource], config_dir: Path | None, project: Path
) -> set[str]:
    """Aliases of the skills the Claude Code settings hide from the model.

    Reads `skillOverrides` and the `Skill` deny rules of the user settings (when the
    hook knows where they are) and of the project settings, each with its local file.
    Settings that cannot be read hide every skill: this must fail closed.
    """
    folders = [project / ".claude"]
    if config_dir is not None:
        folders.insert(0, config_dir)
    try:
        patterns = {
            pattern
            for folder in folders
            for name in _SETTINGS
            for pattern in _hidden_patterns(_settings(folder / name))
        }
    except (OSError, ValueError, TypeError):
        patterns = {"*"}
    hidden: set[str] = set()
    for skill in catalog:
        if any(
            fnmatchcase(Path(alias).name, pattern)
            for alias in skill.aliases
            for pattern in patterns
        ):
            hidden.update(skill.aliases)
    return hidden
