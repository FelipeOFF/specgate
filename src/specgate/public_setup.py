"""Global public-client setup built on the harness-specific installers."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from specgate.claude_setup import (
    _read_settings,
    _write_settings,
    diagnose_claude,
    install_claude,
    uninstall_claude,
)
from specgate.codex_setup import (
    SetupReport,
    diagnose_codex,
    install_codex,
    uninstall_codex,
)
from specgate.cursor_setup import diagnose_cursor, install_cursor, uninstall_cursor
from specgate.grok_bot import (
    diagnose_grok_bot,
    install_grok_bot,
    uninstall_grok_bot,
)
from specgate.grok_build_setup import (
    diagnose_grok_build,
    install_grok_build,
    uninstall_grok_build,
)
from specgate.product import (
    LEGACY_PUBLIC_MARKER,
    LEGACY_TOKEN_ENV,
    OWNERS,
    PUBLIC_MARKER,
    PUBLIC_MARKERS,
    SKILL,
    SKILL_NAMES,
    TOKEN_ENV,
    config_root,
    grok_bot_root,
    wrapper_path,
)
from specgate.session_env import install_session_env, uninstall_session_env

PROFILE = "default"
_OWNER = "specgate"
_WORKFLOW_SKILLS = (
    "triage-jev",
    "grill-with-jev",
    "research-with-jev",
    "research-filter-jev",
    "to-spec-jev",
    "to-tickets-jev",
    "implement-spec-jev",
    "verify-spec-jev",
    "iterate-with-jev",
)
_WORKFLOW_MARKER = PUBLIC_MARKER
_TOKEN_ENV = TOKEN_ENV

SkillInstallStatus = Literal["created", "unchanged", "removed", "preserved", "absent"]
HarnessName = Literal["codex", "claude-code", "cursor", "grok-build", "grok-bot"]
_HARNESSES: tuple[HarnessName, ...] = (
    "codex",
    "claude-code",
    "cursor",
    "grok-build",
    "grok-bot",
)


@dataclass(frozen=True)
class _PathBackup:
    target: Path
    copy: Path | None
    is_directory: bool
    mode: int | None


class _PathTransaction:
    """Snapshot exact managed destinations and restore them as one unit."""

    def __init__(self, paths: Sequence[Path]) -> None:
        self._root = Path(tempfile.mkdtemp(prefix="dev-decision-transaction-"))
        self._backups: list[_PathBackup] = []
        for index, target in enumerate(dict.fromkeys(paths)):
            if target.is_symlink():
                self.close()
                raise ValueError(f"Managed destination cannot be a symlink: {target}")
            if not target.exists():
                self._backups.append(_PathBackup(target, None, False, None))
                continue
            copy = self._root / str(index)
            mode = target.stat().st_mode & 0o777
            if target.is_dir():
                shutil.copytree(target, copy, symlinks=True)
                self._backups.append(_PathBackup(target, copy, True, mode))
            else:
                shutil.copy2(target, copy, follow_symlinks=False)
                self._backups.append(_PathBackup(target, copy, False, mode))

    def rollback(self) -> None:
        for backup in reversed(self._backups):
            if backup.target.is_symlink() or backup.target.is_file():
                backup.target.unlink(missing_ok=True)
            elif backup.target.is_dir():
                shutil.rmtree(backup.target)
            if backup.copy is None:
                continue
            backup.target.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_directory:
                shutil.copytree(backup.copy, backup.target, symlinks=True)
            else:
                shutil.copy2(backup.copy, backup.target, follow_symlinks=False)
            if backup.mode is not None:
                os.chmod(backup.target, backup.mode)

    def close(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)


class PublicSetupReport(TypedDict):
    profile: str
    credential: str
    wrapper: str
    codex: SetupReport
    workflows: dict[str, SkillInstallStatus]
    hooks: str


def public_harness_capabilities() -> dict[str, dict[str, Any]]:
    """Return the supported control surface and its validated boundary."""
    return {
        "codex": {
            "mode": "controlled_app_server",
            "prompt_routing": "native_hook_requires_trust",
            "structured_questions": "protocol_double",
            "mandatory_interception": False,
        },
        "claude-code": {
            "mode": "native_hooks",
            "structured_questions": "native_hook",
            "mandatory_interception": False,
        },
        "cursor": {
            "mode": "controlled_acp",
            "structured_questions": "protocol_double",
            "mandatory_interception": False,
        },
        "grok-build": {
            "mode": "controlled_acp",
            "structured_questions": "protocol_double",
            "mandatory_interception": False,
        },
        "grok-bot": {
            "mode": "cooperative_manual",
            "structured_questions": "unverified",
            "mandatory_interception": False,
        },
    }


def detect_public_harnesses(*, home: Path | None = None) -> tuple[HarnessName, ...]:
    """Detect supported runtimes without reading credentials or user configuration."""
    home = home or Path.home()
    detected: list[HarnessName] = []
    if shutil.which("codex"):
        detected.append("codex")
    if shutil.which("claude"):
        detected.append("claude-code")
    if Path(_cursor_executable(home)).is_file():
        detected.append("cursor")
    if shutil.which("grok"):
        detected.append("grok-build")
    if (
        Path("/Applications/Grok Bot.app").is_dir()
        or (home / "Applications/Grok Bot.app").is_dir()
    ):
        detected.append("grok-bot")
    return tuple(detected)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _tree_digest(path: Path) -> dict[str, str]:
    return {
        file.relative_to(path).as_posix(): _digest(file.read_bytes())
        for file in sorted(path.rglob("*"))
        if file.is_file() and file.name not in PUBLIC_MARKERS
    }


def _workflow_manifest(target: Path) -> dict[str, Any] | None:
    for name in PUBLIC_MARKERS:
        marker = target / name
        if not marker.is_file():
            continue
        try:
            value = json.loads(marker.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _install_workflow_skills(
    source_root: Path, skill_root: Path
) -> dict[str, SkillInstallStatus]:
    """Install the public workflow skills without touching foreign skills."""
    reports: dict[str, SkillInstallStatus] = {}
    backups: dict[Path, Path | None] = {}
    backup_root = Path(tempfile.mkdtemp(prefix="dev-decision-skills-"))
    try:
        for name in _WORKFLOW_SKILLS:
            source = source_root / name
            target = skill_root / name
            if not (source / "SKILL.md").is_file():
                raise ValueError(f"Public skill {name} is unavailable.")
            files = _tree_digest(source)
            previous = _workflow_manifest(target) if target.exists() else None
            if target.exists() and (
                previous is None
                or previous.get("owner") not in OWNERS
                or previous.get("files") != _tree_digest(target)
            ):
                raise ValueError(f"Destination skill {name} is not plugin-managed.")
            if previous is not None and previous.get("files") == files:
                reports[name] = "unchanged"
                continue
            backup = backup_root / name
            if target.exists():
                shutil.copytree(target, backup)
                backups[target] = backup
            else:
                backups[target] = None
            skill_root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=skill_root))
            staging.rmdir()
            shutil.copytree(source, staging)
            (staging / _WORKFLOW_MARKER).write_text(
                json.dumps(
                    {"version": 1, "owner": _OWNER, "files": files},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            (staging / LEGACY_PUBLIC_MARKER).unlink(missing_ok=True)
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            reports[name] = "created"
    except BaseException:
        for target, snapshot in backups.items():
            shutil.rmtree(target, ignore_errors=True)
            if snapshot is not None:
                shutil.copytree(snapshot, target)
        raise
    finally:
        shutil.rmtree(backup_root)
    return reports


def _uninstall_workflow_skills(skill_root: Path) -> dict[str, SkillInstallStatus]:
    reports: dict[str, SkillInstallStatus] = {}
    for name in _WORKFLOW_SKILLS:
        target = skill_root / name
        manifest = _workflow_manifest(target) if target.exists() else None
        if manifest is None:
            reports[name] = "preserved" if target.exists() else "absent"
        elif manifest.get("owner") in OWNERS and manifest.get("files") == _tree_digest(
            target
        ):
            shutil.rmtree(target)
            reports[name] = "removed"
        else:
            reports[name] = "preserved"
    return reports


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_profile(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _owned_profile(profile: dict[str, Any] | None) -> bool:
    return bool(
        profile and profile.get("owner") in OWNERS and profile.get("profile") == PROFILE
    )


def _profile_paths(home: Path, *, write: bool = False) -> tuple[Path, Path, Path]:
    root = config_root(home, write=write)
    return (
        root / "profiles/default.json",
        root / "credentials/default.key",
        wrapper_path(home, "codex"),
    )


def _cursor_executable(home: Path) -> str:
    local = home / ".local/bin/cursor-agent"
    if local.is_file():
        return str(local)
    found = shutil.which("cursor-agent")
    return found if found else str(local)


def _wrapper_path(home: Path, harness: HarnessName) -> Path:
    suffix = {
        "codex": "codex",
        "claude-code": "claude",
        "cursor": "cursor",
        "grok-build": "grok-build",
        "grok-bot": "grok-bot",
    }[harness]
    return wrapper_path(home, suffix)


def _wrapper_command(
    home: Path,
    harness: HarnessName,
    codex_command: Sequence[str],
) -> tuple[str, ...] | None:
    if harness == "codex":
        return tuple(codex_command)
    if harness == "claude-code":
        return ("claude",)
    if harness == "cursor":
        return (_cursor_executable(home),)
    if harness == "grok-build":
        return ("grok",)
    return None


def _wrapper_content(credential_path: Path, command: Sequence[str]) -> bytes:
    if not command:
        raise ValueError("A harness wrapper requires a command.")
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"credential={shlex.quote(str(credential_path))}\n"
        'if [ ! -r "$credential" ]; then\n'
        '  echo "Specgate MCP unavailable: credential is missing." >&2\n'
        f'  exec {shlex.join(command)} "$@"\n'
        "fi\n"
        f'export {_TOKEN_ENV}="$(cat "$credential")"\n'
        f': "${{{LEGACY_TOKEN_ENV}:=${{{_TOKEN_ENV}}}}}"\n'
        f"export {LEGACY_TOKEN_ENV}\n"
        f'exec {shlex.join(command)} "$@"\n'
    ).encode()


def _profile_wrapper_digest(
    profile: dict[str, Any] | None, harness: HarnessName
) -> str | None:
    if profile is None:
        return None
    wrappers = profile.get("wrappers")
    if isinstance(wrappers, dict):
        entry = wrappers.get(harness)
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if isinstance(digest, str):
            return digest
    wrapper_digest = profile.get("wrapper_sha256")
    if harness == "codex" and isinstance(wrapper_digest, str):
        return wrapper_digest
    return None


def _validate_profile_files(
    profile_path: Path,
    credential_path: Path,
    profile: dict[str, Any] | None,
) -> None:
    if profile_path.exists() and not _owned_profile(profile):
        raise ValueError("The existing default profile is not plugin-managed.")
    if credential_path.is_symlink():
        raise ValueError("The managed credential cannot be a symlink.")
    if credential_path.exists() and (
        profile is None
        or profile.get("credential_sha256") != _digest(credential_path.read_bytes())
    ):
        raise ValueError("The managed credential was modified.")


def _write_wrappers(
    home: Path,
    credential_path: Path,
    harnesses: Sequence[HarnessName],
    previous_profile: dict[str, Any] | None,
    codex_command: Sequence[str],
) -> dict[str, dict[str, str]]:
    raw_wrappers = (
        previous_profile.get("wrappers") if previous_profile is not None else {}
    )
    previous_entries = raw_wrappers if isinstance(raw_wrappers, dict) else {}
    wrappers: dict[str, dict[str, str]] = {
        name: dict(value)
        for name, value in previous_entries.items()
        if isinstance(name, str) and isinstance(value, dict)
    }
    for harness in harnesses:
        command = _wrapper_command(home, harness, codex_command)
        if command is None:
            continue
        path = _wrapper_path(home, harness)
        expected = _profile_wrapper_digest(previous_profile, harness)
        if path.is_symlink() or (
            path.exists()
            and (expected is None or expected != _digest(path.read_bytes()))
        ):
            raise ValueError(f"The {harness} wrapper is not plugin-managed.")
        content = _wrapper_content(credential_path, command)
        _write_private(path, content)
        os.chmod(path, 0o700)
        wrappers[harness] = {"path": str(path), "sha256": _digest(content)}
    return wrappers


def _profile_content(
    previous: dict[str, Any] | None,
    *,
    url: str,
    credential_path: Path,
    credential: bytes,
    harnesses: Sequence[HarnessName],
    wrappers: dict[str, dict[str, str]],
) -> dict[str, Any]:
    profile = dict(previous or {})
    enabled = profile.get("harnesses")
    selections = {
        name: True
        for name in _HARNESSES
        if isinstance(enabled, dict) and enabled.get(name) is True
    }
    selections.update({name: True for name in harnesses})
    profile.update(
        {
            "version": 1,
            "owner": _OWNER,
            "profile": PROFILE,
            "url": url,
            "credential_file": str(credential_path),
            "credential_sha256": _digest(credential),
            "credential_env": _TOKEN_ENV,
            "harnesses": selections,
            "skills": list(_WORKFLOW_SKILLS),
            "wrappers": wrappers,
        }
    )
    codex_wrapper = wrappers.get("codex")
    if codex_wrapper is not None:
        profile["wrapper"] = codex_wrapper["path"]
        profile["wrapper_sha256"] = codex_wrapper["sha256"]
    return profile


def _skill_targets(root: Path) -> list[Path]:
    return [
        *(root / name for name in SKILL_NAMES),
        *(root / name for name in _WORKFLOW_SKILLS),
    ]


def _codex_home(home: Path) -> Path:
    override = os.environ.get("CODEX_HOME") if home == Path.home() else None
    return Path(override).expanduser().resolve() if override else home / ".codex"


def _codex_hook_settings(
    path: Path,
    profile: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    settings = _read_settings(path)
    previous = (profile or {}).get("codex_hook")
    if isinstance(previous, dict):
        if previous.get("path") != str(path):
            raise ValueError(
                "O diretório do hook Codex mudou; preserve a instalação anterior."
            )
        entry = previous.get("entry")
        if entry not in settings.get("hooks", {}).get("UserPromptSubmit", []):
            raise ValueError("O hook Codex gerenciado foi modificado ou removido.")
        approval = previous.get("permission_entry")
        if approval is not None and approval not in settings.get("hooks", {}).get(
            "PermissionRequest", []
        ):
            raise ValueError(
                "O hook de aprovação Codex gerenciado foi modificado ou removido."
            )
        return settings, entry
    return settings, None


def _remove_codex_hook(profile: dict[str, Any]) -> str:
    previous = profile.get("codex_hook")
    if not isinstance(previous, dict):
        return "absent"
    path = Path(previous["path"])
    if path.is_symlink():
        return "preserved"
    try:
        settings, entry = _codex_hook_settings(path, profile)
    except (ValueError, TypeError, OSError):
        return "preserved"
    entries = settings["hooks"]["UserPromptSubmit"]
    entries.remove(entry)
    if not entries:
        del settings["hooks"]["UserPromptSubmit"]
    approval = previous.get("permission_entry")
    if approval is not None:
        approvals = settings["hooks"]["PermissionRequest"]
        approvals.remove(approval)
        if not approvals:
            del settings["hooks"]["PermissionRequest"]
    _write_settings(path, settings)
    return "removed"


def _managed_targets(home: Path, harnesses: Sequence[HarnessName]) -> list[Path]:
    profile_path, credential_path, _ = _profile_paths(home, write=True)
    targets = [profile_path, credential_path]
    for harness in harnesses:
        command = _wrapper_command(home, harness, ())
        if command is not None:
            targets.append(_wrapper_path(home, harness))
        if harness == "codex":
            targets.append(_codex_home(home) / "hooks.json")
            targets.extend(_skill_targets(home / ".agents/skills"))
        elif harness == "claude-code":
            targets.append(home / ".claude/settings.json")
            targets.append(home / ".claude.json")
            targets.extend(_skill_targets(home / ".claude/skills"))
        elif harness == "cursor":
            targets.append(home / ".cursor/mcp.json")
            targets.extend(_skill_targets(home / ".cursor/skills"))
        elif harness == "grok-build":
            targets.append(home / ".grok/config.toml")
            targets.append(home / ".grok/mcp.json")
            targets.extend(_skill_targets(home / ".grok/skills"))
        else:
            bundle = grok_bot_root(home, write=True)
            targets.append(bundle / "mcp.json")
            targets.extend(_skill_targets(bundle))
    return targets


def install_public_codex(
    source: Path,
    url: str,
    api_key: str,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> PublicSetupReport:
    """Install profile, private credential, wrapper and the managed Codex skill."""
    home = home or Path.home()
    if not api_key or any(not 33 <= ord(character) <= 126 for character in api_key):
        raise ValueError("A API key do MCP é inválida.")
    profile_path, credential_path, wrapper_path = _profile_paths(home)
    skill_root = home / ".agents/skills"
    previous_profile = _load_profile(profile_path)
    _validate_profile_files(profile_path, credential_path, previous_profile)
    expected_wrapper = _profile_wrapper_digest(previous_profile, "codex")
    if wrapper_path.is_symlink() or (
        wrapper_path.exists()
        and (
            expected_wrapper is None
            or expected_wrapper != _digest(wrapper_path.read_bytes())
        )
    ):
        raise ValueError("Um arquivo de destino não pertence à instalação.")

    credential = f"{api_key}\n".encode()
    wrapper = _wrapper_content(credential_path, codex_command)
    raw_wrappers = previous_profile.get("wrappers") if previous_profile else {}
    wrappers = dict(raw_wrappers) if isinstance(raw_wrappers, dict) else {}
    wrappers["codex"] = {"path": str(wrapper_path), "sha256": _digest(wrapper)}
    profile = _profile_content(
        previous_profile,
        url=url,
        credential_path=credential_path,
        credential=credential,
        harnesses=("codex",),
        wrappers=wrappers,
    )
    hooks_path = _codex_home(home) / "hooks.json"
    settings, previous_hook = _codex_hook_settings(hooks_path, previous_profile)
    entry = {
        "hooks": [
            {
                "type": "command",
                "command": shlex.join(
                    [
                        sys.executable,
                        "-B",
                        str(Path(__file__).with_name("codex_hook.py")),
                        "--profile",
                        str(profile_path),
                        "--source-root",
                        str(source.parent.resolve()),
                    ]
                ),
                "timeout": 50,
                "statusMessage": "Specgate skill routing",
                "additionalContextLimit": 20000,
            }
        ]
    }
    entries = settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if previous_hook is not None:
        entries[entries.index(previous_hook)] = entry
    else:
        entries.append(entry)
    permission_entry = {
        "hooks": [{**entry["hooks"][0], "statusMessage": "Specgate action review"}]
    }
    permission_entries = settings["hooks"].setdefault("PermissionRequest", [])
    previous_permission = ((previous_profile or {}).get("codex_hook") or {}).get(
        "permission_entry"
    )
    if previous_permission is not None:
        permission_entries[permission_entries.index(previous_permission)] = (
            permission_entry
        )
    else:
        permission_entries.append(permission_entry)
    profile["codex_hook"] = {
        "path": str(hooks_path),
        "entry": entry,
        "permission_entry": permission_entry,
    }
    transaction = _PathTransaction(
        [
            profile_path,
            credential_path,
            wrapper_path,
            hooks_path,
            *_skill_targets(skill_root),
        ]
    )
    try:
        _write_private(credential_path, credential)
        _write_private(wrapper_path, wrapper)
        os.chmod(wrapper_path, 0o700)
        _write_private(
            profile_path,
            (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        workflows = _install_workflow_skills(source.parent, skill_root)
        codex = install_codex(source, skill_root, url, codex_command=codex_command)
        _write_settings(hooks_path, settings)
    except BaseException:
        transaction.rollback()
        raise
    finally:
        transaction.close()
    install_session_env(home, credential_path)
    return {
        "profile": PROFILE,
        "credential": str(credential_path),
        "wrapper": str(wrapper_path),
        "codex": codex,
        "workflows": workflows,
        "hooks": "unchanged" if previous_hook == entry else "created",
    }


def _install_selected_harness(
    source_root: Path,
    source: Path,
    url: str,
    home: Path,
    harness: HarnessName,
    api_key: str,
    codex_command: tuple[str, ...],
) -> dict[str, Any]:
    if harness == "codex":
        return dict(
            install_public_codex(
                source, url, api_key, home=home, codex_command=codex_command
            )
        )
    if harness == "claude-code":
        return {
            "adapter": install_claude(
                source,
                home / ".claude",
                url,
                python_command=(sys.executable,),
                token=api_key,
            ),
            "workflows": _install_workflow_skills(source_root, home / ".claude/skills"),
        }
    if harness == "cursor":
        return {
            "adapter": install_cursor(
                source, home / ".cursor/skills", url, token=api_key
            ),
            "workflows": _install_workflow_skills(source_root, home / ".cursor/skills"),
        }
    if harness == "grok-build":
        return {
            "adapter": install_grok_build(
                source, home / ".grok/skills", url, token=api_key
            ),
            "workflows": _install_workflow_skills(source_root, home / ".grok/skills"),
        }
    bundle_root = grok_bot_root(home, write=True)
    return {
        "adapter": install_grok_bot(source, bundle_root, url, token=api_key),
        "workflows": _install_workflow_skills(source_root, bundle_root),
    }


def install_public_harnesses(
    source_root: Path,
    url: str,
    api_key: str,
    harnesses: tuple[HarnessName, ...],
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> dict[str, Any]:
    """Install each explicitly selected global integration."""
    if not harnesses:
        raise ValueError("Select at least one supported harness.")
    if len(set(harnesses)) != len(harnesses):
        raise ValueError("Harness selections must be unique.")
    if any(name not in _HARNESSES for name in harnesses):
        raise ValueError("Harness selections must be supported.")
    if not api_key or any(not 33 <= ord(character) <= 126 for character in api_key):
        raise ValueError("The MCP API key is invalid.")
    home = home or Path.home()
    os.environ[TOKEN_ENV] = api_key
    os.environ.setdefault(LEGACY_TOKEN_ENV, api_key)
    source = source_root / SKILL
    if not (source / "SKILL.md").is_file():
        raise ValueError("The Specgate skill must contain SKILL.md.")
    existing_profile_path, existing_credential_path, _ = _profile_paths(home)
    profile_path, credential_path, _ = _profile_paths(home, write=True)
    previous = _load_profile(existing_profile_path) or _load_profile(profile_path)
    _validate_profile_files(existing_profile_path, existing_credential_path, previous)
    credential = f"{api_key}\n".encode()
    wrappers: dict[str, dict[str, str]] = {}
    mcp_created = False
    transaction = _PathTransaction(_managed_targets(home, harnesses))
    try:
        reports: dict[str, Any] = {}
        if "codex" in harnesses:
            reports["codex"] = _install_selected_harness(
                source_root, source, url, home, "codex", api_key, codex_command
            )
            mcp_created = reports["codex"].get("codex", {}).get("mcp") == "created"
        else:
            _write_private(credential_path, credential)
        current = _load_profile(profile_path)
        wrappers = _write_wrappers(
            home,
            credential_path,
            harnesses,
            current or previous,
            codex_command,
        )
        for harness in harnesses:
            if harness == "codex":
                continue
            reports[harness] = _install_selected_harness(
                source_root, source, url, home, harness, api_key, codex_command
            )
        profile = _profile_content(
            _load_profile(profile_path) or previous,
            url=url,
            credential_path=credential_path,
            credential=credential,
            harnesses=harnesses,
            wrappers=wrappers,
        )
        _write_private(
            profile_path,
            (json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode(),
        )
    except BaseException:
        if mcp_created:
            try:
                uninstall_codex(
                    home / ".agents/skills",
                    codex_command=codex_command,
                )
            except (ValueError, OSError, ExceptionGroup):
                pass
        transaction.rollback()
        raise
    finally:
        transaction.close()
    return {
        "profile": PROFILE,
        "credential": str(credential_path),
        "harnesses": reports,
        "wrappers": {
            name: wrappers[name]["path"] for name in harnesses if name in wrappers
        },
        "session_env": install_session_env(home, credential_path),
    }


def update_public_codex(
    source: Path,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> PublicSetupReport:
    """Update managed Codex files and restore the installed skill on failure."""
    home = home or Path.home()
    profile_path, credential_path, _ = _profile_paths(home)
    profile = _load_profile(profile_path)
    if profile is None or not _owned_profile(profile) or not credential_path.is_file():
        raise ValueError("Managed installation is unavailable for update.")
    target = home / ".agents/skills" / SKILL
    backup_root = Path(tempfile.mkdtemp(prefix="dev-decision-update-"))
    backup = backup_root / "skill"
    if target.exists():
        shutil.copytree(target, backup)
    try:
        return install_public_codex(
            source,
            str(profile["url"]),
            credential_path.read_text().strip(),
            home=home,
            codex_command=codex_command,
        )
    except BaseException:
        if target.exists():
            shutil.rmtree(target)
        if backup.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(backup, target)
        raise
    finally:
        shutil.rmtree(backup_root)


def update_public_harnesses(
    source_root: Path,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> dict[str, Any]:
    """Update the harnesses recorded in the managed default profile."""
    home = home or Path.home()
    profile_path, credential_path, _ = _profile_paths(home)
    profile = _load_profile(profile_path)
    if profile is None or not _owned_profile(profile) or not credential_path.is_file():
        raise ValueError("Managed installation is unavailable for update.")
    enabled = profile.get("harnesses")
    if not isinstance(enabled, dict):
        raise TypeError("Managed harness selection is unavailable.")
    harnesses = tuple(name for name in _HARNESSES if enabled.get(name) is True)
    return install_public_harnesses(
        source_root,
        str(profile["url"]),
        credential_path.read_text().strip(),
        harnesses,
        home=home,
        codex_command=codex_command,
    )


async def doctor_public_codex(
    project: Path,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Validate the default profile without starting a model turn."""
    home = home or Path.home()
    profile_path = _profile_paths(home)[0]
    profile = _load_profile(profile_path)
    if profile is None or not _owned_profile(profile):
        raise ValueError("O profile default gerenciado não foi encontrado.")
    _, credential_path, _ = _profile_paths(home)
    if (
        profile.get("credential_file") != str(credential_path)
        or not credential_path.is_file()
        or credential_path.stat().st_mode & 0o077
        or _digest(credential_path.read_bytes()) != profile.get("credential_sha256")
    ):
        raise ValueError("A credencial gerenciada está ausente, exposta ou modificada.")
    report = await diagnose_codex(
        project,
        home / ".agents/skills",
        str(profile["url"]),
        credential_path.read_text().strip(),
        codex_command=codex_command,
        timeout_seconds=timeout_seconds,
    )
    try:
        _, entry = _codex_hook_settings(_codex_home(home) / "hooks.json", profile)
    except (ValueError, TypeError, OSError):
        entry = None
    report["native_hook"] = {
        "installed": entry is not None,
        "event": "UserPromptSubmit",
        "permission_request": bool(
            (profile.get("codex_hook") or {}).get("permission_entry")
        ),
        "trust": "requires_harness_confirmation",
        "runtime_verified": False,
        "catalog": "enabled_public_specgate_bundle",
    }
    report["limitations"].append(
        "Native prompt routing requires trusted hooks and a remote gate that approves "
        "the selection, by confidence policy or validated manifest; "
        "doctor does not submit a prompt or prove automatic selection."
    )
    if entry is None:
        report["usable"] = False
    return report


async def doctor_public_harnesses(
    project: Path,
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    """Validate the managed profile and each enabled harness without inference."""
    home = home or Path.home()
    profile_path, credential_path, _ = _profile_paths(home)
    profile = _load_profile(profile_path)
    if profile is None or not _owned_profile(profile):
        raise ValueError("O profile default gerenciado não foi encontrado.")
    if (
        profile.get("credential_file") != str(credential_path)
        or not credential_path.is_file()
        or credential_path.stat().st_mode & 0o077
        or _digest(credential_path.read_bytes()) != profile.get("credential_sha256")
    ):
        raise ValueError("A credencial gerenciada está ausente, exposta ou modificada.")
    url = str(profile["url"])
    token = credential_path.read_text().strip()
    os.environ[TOKEN_ENV] = token
    os.environ.setdefault(LEGACY_TOKEN_ENV, token)

    async def report(coro: Any) -> dict[str, Any]:
        try:
            result = await coro
        except (ValueError, OSError, ExceptionGroup, RuntimeError) as error:
            return {"usable": False, "error": str(error)}
        return result if isinstance(result, dict) else {"usable": False}

    project_id = await _sync_project_id(
        url,
        token,
        project,
        profile_path,
        profile,
        timeout_seconds,
    )
    enabled = profile.get("harnesses")
    enabled = enabled if isinstance(enabled, dict) else {}
    reports: dict[str, Any] = {}
    if enabled.get("codex") is True:
        reports["codex"] = await report(
            doctor_public_codex(
                project,
                home=home,
                codex_command=codex_command,
                timeout_seconds=timeout_seconds,
            )
        )
    if enabled.get("claude-code") is True:
        reports["claude-code"] = await report(
            diagnose_claude(
                project,
                home / ".claude",
                url,
                token,
                timeout_seconds=timeout_seconds,
            )
        )
    if enabled.get("cursor") is True:
        reports["cursor"] = await report(
            diagnose_cursor(
                home / ".cursor/skills",
                url,
                token,
                timeout_seconds=timeout_seconds,
            )
        )
    if enabled.get("grok-build") is True:
        reports["grok-build"] = await report(
            diagnose_grok_build(
                home / ".grok/skills",
                url,
                token,
                timeout_seconds=timeout_seconds,
            )
        )
    if enabled.get("grok-bot") is True:
        reports["grok-bot"] = await report(
            diagnose_grok_bot(
                grok_bot_root(home),
                url,
                token,
                timeout_seconds=timeout_seconds,
            )
        )
    return {
        "profile": PROFILE,
        "project_id": project_id,
        "harnesses": reports,
        "capabilities": public_harness_capabilities(),
    }


def managed_project_id(*, home: Path | None = None) -> str | None:
    profile = _load_profile(_profile_paths(home or Path.home())[0])
    value = profile.get("project_id") if profile else None
    return value if isinstance(value, str) and value else None


async def _sync_project_id(
    url: str,
    token: str,
    project: Path,
    profile_path: Path,
    profile: dict[str, Any],
    timeout_seconds: float,
) -> str | None:
    from specgate.identity_client import ProjectClient

    existing = profile.get("project_id")
    existing_id = existing if isinstance(existing, str) else None
    try:
        setup = await ProjectClient(url, token, timeout_seconds=timeout_seconds).setup(
            path_metadata=str(project.resolve()),
            project_id=existing_id,
        )
    except ValueError:
        return existing_id
    project_id = setup.get("project_id")
    if setup.get("action") != "ready" or not isinstance(project_id, str):
        return existing_id
    if existing_id != project_id:
        updated = dict(profile)
        updated["project_id"] = project_id
        _write_private(
            profile_path,
            (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode(),
        )
    return project_id


def uninstall_public_codex(
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> dict[str, Any]:
    """Remove only unchanged files owned by the default public profile."""
    home = home or Path.home()
    profile_path, _, _ = _profile_paths(home)
    profile = _load_profile(profile_path)
    skill_root = home / ".agents/skills"
    codex = uninstall_codex(skill_root, codex_command=codex_command)
    workflows = _uninstall_workflow_skills(skill_root)
    removed: list[str] = []
    preserved: list[str] = []
    if profile is None or not _owned_profile(profile):
        return {
            "codex": codex,
            "workflows": workflows,
            "removed": removed,
            "preserved": preserved,
        }
    hooks = _remove_codex_hook(profile)
    if hooks == "preserved":
        preserved.append(str(profile["codex_hook"]["path"]))
    _, credential_path, wrapper_path = _profile_paths(home)
    candidates = (
        (credential_path, "credential_sha256"),
        (wrapper_path, "wrapper_sha256"),
    )
    for path, digest_key in candidates:
        if path.is_file() and _digest(path.read_bytes()) == profile.get(digest_key):
            path.unlink()
            removed.append(str(path))
        elif path.exists():
            preserved.append(str(path))
    if not preserved:
        profile_path.unlink(missing_ok=True)
        removed.append(str(profile_path))
    else:
        preserved.append(str(profile_path))
    return {
        "codex": codex,
        "workflows": workflows,
        "removed": removed,
        "preserved": preserved,
        "hooks": hooks,
    }


def uninstall_public_harnesses(
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
) -> dict[str, Any]:
    """Remove only unchanged artifacts for harnesses in the managed profile."""
    home = home or Path.home()
    profile_path, credential_path, wrapper_path = _profile_paths(home)
    profile = _load_profile(profile_path)
    if profile is None or not _owned_profile(profile):
        return {"removed": [], "preserved": [], "harnesses": {}}
    enabled = profile.get("harnesses")
    enabled = enabled if isinstance(enabled, dict) else {}
    reports: dict[str, Any] = {}
    if enabled.get("claude-code") is True:
        reports["claude-code"] = {
            "adapter": uninstall_claude(home / ".claude"),
            "workflows": _uninstall_workflow_skills(home / ".claude/skills"),
        }
    if enabled.get("cursor") is True:
        reports["cursor"] = {
            "adapter": uninstall_cursor(home / ".cursor/skills"),
            "workflows": _uninstall_workflow_skills(home / ".cursor/skills"),
        }
    if enabled.get("grok-build") is True:
        reports["grok-build"] = {
            "adapter": uninstall_grok_build(home / ".grok/skills"),
            "workflows": _uninstall_workflow_skills(home / ".grok/skills"),
        }
    if enabled.get("grok-bot") is True:
        reports["grok-bot"] = {
            "adapter": uninstall_grok_bot(grok_bot_root(home)),
            "workflows": _uninstall_workflow_skills(grok_bot_root(home)),
        }
    if enabled.get("codex") is True:
        reports["codex"] = uninstall_public_codex(
            home=home,
            codex_command=codex_command,
        )

    removed: list[str] = []
    preserved: list[str] = []
    stored_wrappers = profile.get("wrappers")
    entries = stored_wrappers if isinstance(stored_wrappers, dict) else {}
    candidates: list[tuple[Path, str | None]] = [
        (
            Path(str(entry.get("path", ""))),
            entry.get("sha256") if isinstance(entry.get("sha256"), str) else None,
        )
        for entry in entries.values()
        if isinstance(entry, dict)
    ]
    if not candidates:
        candidates = [
            (credential_path, str(profile.get("credential_sha256") or "")),
            (wrapper_path, str(profile.get("wrapper_sha256") or "")),
        ]
    else:
        candidates.append(
            (credential_path, str(profile.get("credential_sha256") or ""))
        )
    seen: set[Path] = set()
    for path, digest_key in candidates:
        if path in seen or not str(path):
            continue
        seen.add(path)
        expected = digest_key
        if path.is_file() and expected and _digest(path.read_bytes()) == expected:
            path.unlink()
            removed.append(str(path))
        elif path.exists():
            preserved.append(str(path))
    if enabled.get("codex") is True:
        codex = reports.get("codex", {})
        removed = list(dict.fromkeys([*codex.get("removed", []), *removed]))
        preserved = list(dict.fromkeys([*codex.get("preserved", []), *preserved]))
    if not preserved:
        profile_path.unlink(missing_ok=True)
        removed.append(str(profile_path))
    else:
        preserved.append(str(profile_path))
    uninstall_session_env(home)
    return {"harnesses": reports, "removed": removed, "preserved": preserved}
