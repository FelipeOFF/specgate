"""Public product identity, client paths and environment names."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PRODUCT = "Specgate"
PRODUCT_MCP = "Specgate MCP"
MCP_SERVER = "specgate"
SKILL = "specgate"
OWNER = "specgate"
LEGACY_OWNER = "dev_decision"
LEGACY_MCP_SERVER = "dev_decision"
LEGACY_SKILL = "dev-decision"
OWNERS = frozenset({OWNER, LEGACY_OWNER})
CURSOR_OWNER = "specgate_cursor"
LEGACY_CURSOR_OWNER = "dev_decision_cursor"
CURSOR_OWNERS = frozenset({CURSOR_OWNER, LEGACY_CURSOR_OWNER})
GROK_BUILD_OWNER = "specgate_grok_build"
LEGACY_GROK_BUILD_OWNER = "dev_decision_grok_build"
GROK_BUILD_OWNERS = frozenset({GROK_BUILD_OWNER, LEGACY_GROK_BUILD_OWNER})

TOKEN_ENV = "SPECGATE_MCP_API_KEY"
LEGACY_TOKEN_ENV = "DEV_DECISION_MCP_API_KEY"
BOOTSTRAP_PYTHON_ENV = "SPECGATE_BOOTSTRAP_PYTHON"
LEGACY_BOOTSTRAP_PYTHON_ENV = "DEV_DECISION_BOOTSTRAP_PYTHON"
PYTHON_PACKAGE_ENV = "SPECGATE_PYTHON_PACKAGE"
LEGACY_PYTHON_PACKAGE_ENV = "DEV_DECISION_PYTHON_PACKAGE"
SKILL_SOURCE_ENV = "SPECGATE_SKILL_SOURCE"
LEGACY_SKILL_SOURCE_ENV = "DEV_DECISION_SKILL_SOURCE"

INSTALL_MARKER = ".specgate-install.json"
LEGACY_INSTALL_MARKER = ".dev-decision-install.json"
PUBLIC_MARKER = ".specgate-public.json"
LEGACY_PUBLIC_MARKER = ".dev-decision-public.json"
CURSOR_MARKER = ".specgate-cursor.json"
LEGACY_CURSOR_MARKER = ".dev-decision-cursor.json"
GROK_BUILD_MARKER = ".specgate-grok-build.json"
LEGACY_GROK_BUILD_MARKER = ".dev-decision-grok-build.json"
INSTALL_MARKERS = (INSTALL_MARKER, LEGACY_INSTALL_MARKER)
PUBLIC_MARKERS = (PUBLIC_MARKER, LEGACY_PUBLIC_MARKER)
CURSOR_MARKERS = (CURSOR_MARKER, LEGACY_CURSOR_MARKER)
GROK_BUILD_MARKERS = (GROK_BUILD_MARKER, LEGACY_GROK_BUILD_MARKER)
SKILL_NAMES = (SKILL, LEGACY_SKILL)


def packaged_skills() -> Path:
    """The public skills this package ships; no environment can redirect it."""
    packaged = Path(__file__).with_name("skills")
    return (
        packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "skill"
    )


def env_first(*names: str, environ: Mapping[str, str] | None = None) -> str:
    env = environ or os.environ
    for name in names:
        value = env.get(name)
        if value:
            return value
    return ""


def mcp_api_key(environ: Mapping[str, str] | None = None) -> str:
    """Prefer SPECGATE_MCP_API_KEY, then the legacy client name."""
    return env_first(TOKEN_ENV, LEGACY_TOKEN_ENV, environ=environ)


def config_root(home: Path, *, write: bool = False) -> Path:
    current = home / ".config/specgate"
    if write or (current / "profiles/default.json").is_file():
        return current
    legacy = home / ".config/dev-decision"
    if (legacy / "profiles/default.json").is_file():
        return legacy
    return current


def share_root(home: Path) -> Path:
    return home / ".local/share/specgate"


def wrapper_path(home: Path, suffix: str) -> Path:
    return home / ".local/bin" / f"specgate-{suffix}"


def grok_bot_root(home: Path, *, write: bool = False) -> Path:
    current = home / ".specgate/grok-bot"
    if write or current.exists():
        return current
    legacy = home / ".dev-decision/grok-bot"
    if legacy.exists():
        return legacy
    return current
