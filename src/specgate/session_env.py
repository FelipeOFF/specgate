"""Publish the saved MCP credential to new shells and the graphical session."""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from specgate.product import LEGACY_TOKEN_ENV, TOKEN_ENV

START = "# specgate:env:start"
END = "# specgate:env:end"
_AGENT_LABEL = "com.specgate.session-env"
_CLEAN_NAMES = (
    ".zshenv",
    ".zprofile",
    ".zshrc",
    ".bashrc",
    ".bash_profile",
    ".profile",
)


class SessionEnvReport(TypedDict):
    env_sh: str
    shells: list[str]
    session_script: str
    launch_agent: str | None
    activated: bool


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


def _home_ref(home: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return shlex.quote(str(path))
    if any(character in relative for character in "\"\\$`"):
        return shlex.quote(str(path))
    return f'"$HOME/{relative}"'


def _env_sh(credential_ref: str) -> str:
    return (
        "# Managed by Specgate. The key stays in the credential file.\n"
        f"credential={credential_ref}\n"
        'if [ -r "$credential" ]; then\n'
        f"  {TOKEN_ENV}=$(tr -d '\\r\\n' < \"$credential\")\n"
        f"  export {TOKEN_ENV}\n"
        f'  export {LEGACY_TOKEN_ENV}="${{{TOKEN_ENV}}}"\n'
        "fi\n"
    )


def _posix_block() -> str:
    return (
        f"{START}\n"
        '[ -r "$HOME/.config/specgate/env.sh" ] && . "$HOME/.config/specgate/env.sh"\n'
        f"{END}"
    )


def _fish_block(credential_ref: str) -> str:
    return (
        f"{START}\n"
        f"if test -r {credential_ref}\n"
        f"  set -gx {TOKEN_ENV} (string trim (cat {credential_ref}))\n"
        f"  set -gx {LEGACY_TOKEN_ENV} ${TOKEN_ENV}\n"
        "end\n"
        f"{END}"
    )


def _session_script(credential_ref: str) -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"credential={credential_ref}\n"
        'if [ ! -r "$credential" ]; then\n'
        "  exit 0\n"
        "fi\n"
        "value=$(tr -d '\\r\\n' < \"$credential\")\n"
        'if [ -z "$value" ]; then\n'
        "  exit 0\n"
        "fi\n"
        'case "$(uname -s)" in\n'
        "  Darwin)\n"
        f'    launchctl setenv {TOKEN_ENV} "$value"\n'
        f'    launchctl setenv {LEGACY_TOKEN_ENV} "$value"\n'
        "    ;;\n"
        "  Linux)\n"
        "    if command -v systemctl >/dev/null 2>&1; then\n"
        "      systemctl --user set-environment "
        f'{TOKEN_ENV}="$value" {LEGACY_TOKEN_ENV}="$value"\n'
        "    fi\n"
        "    ;;\n"
        "esac\n"
    )


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _plist(script: Path) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{_xml(_AGENT_LABEL)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{_xml(str(script))}</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _exec_path(script: Path) -> str:
    text = str(script)
    if any(character.isspace() for character in text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _systemd_unit(script: Path) -> str:
    return (
        "[Unit]\n"
        "Description=Publish the Specgate MCP credential to the user session\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={_exec_path(script)}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _login_bash(home: Path) -> Path:
    bash_profile = home / ".bash_profile"
    profile = home / ".profile"
    if bash_profile.is_file() or not profile.is_file():
        return bash_profile
    return profile


def _install_shells(home: Path) -> list[Path]:
    return [
        home / ".zshenv",
        home / ".zprofile",
        home / ".zshrc",
        home / ".bashrc",
        _login_bash(home),
        home / ".config/fish/conf.d/specgate-env.fish",
    ]


def _clean_shells(home: Path) -> list[Path]:
    return [home / name for name in _CLEAN_NAMES] + [
        home / ".config/fish/conf.d/specgate-env.fish"
    ]


def _upsert_block(path: Path, block: str) -> None:
    existing = path.read_text() if path.is_file() else ""
    if START in existing and END in existing:
        pre, _, rest = existing.partition(START)
        _, _, post = rest.partition(END)
        post = post.removeprefix("\n")
        body = pre.rstrip()
        prefix = f"{body}\n\n" if body else ""
        text = f"{prefix}{block}\n{post}"
    else:
        body = existing.rstrip()
        prefix = f"{body}\n\n" if body else ""
        text = f"{prefix}{block}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else f"{text}\n")


def _remove_block(path: Path) -> None:
    if not path.is_file():
        return
    text = path.read_text()
    if START not in text or END not in text:
        return
    pre, _, rest = text.partition(START)
    _, _, post = rest.partition(END)
    post = post.removeprefix("\n")
    body = pre.rstrip()
    merged = f"{body}\n{post.lstrip(chr(10))}" if post.strip() else body
    if not merged.strip():
        path.unlink()
        return
    path.write_text(merged if merged.endswith("\n") else f"{merged}\n")


def _write_script(path: Path, content: str) -> None:
    _write_private(path, content.encode())
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _gui_files(home: Path, script: Path) -> Path | None:
    if sys.platform == "darwin":
        path = home / "Library/LaunchAgents" / f"{_AGENT_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_plist(script))
        return path
    if sys.platform == "linux":
        path = home / ".config/systemd/user" / f"{_AGENT_LABEL}.service"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_systemd_unit(script))
        return path
    return None


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True)


def _publish_darwin(value: str, agent: Path | None) -> bool:
    # launchctl takes the value as an argument. Shell rc files never store it.
    published = True
    for name in (TOKEN_ENV, LEGACY_TOKEN_ENV):
        result = _run(["launchctl", "setenv", name, value])
        published = published and result.returncode == 0
    if agent is None:
        return published
    domain = f"gui/{os.getuid()}"
    _run(["launchctl", "enable", f"{domain}/{_AGENT_LABEL}"])
    _run(["launchctl", "bootout", f"{domain}/{_AGENT_LABEL}"])
    _run(["launchctl", "bootstrap", domain, str(agent)])
    return published


def _publish_linux(value: str) -> bool:
    result = _run(
        [
            "systemctl",
            "--user",
            "set-environment",
            f"{TOKEN_ENV}={value}",
            f"{LEGACY_TOKEN_ENV}={value}",
        ]
    )
    return result.returncode == 0


def _activate(credential_path: Path, agent: Path | None) -> bool:
    value = credential_path.read_text().strip()
    if not value:
        return False
    if sys.platform == "darwin":
        return _publish_darwin(value, agent)
    if sys.platform == "linux":
        return _publish_linux(value)
    return False


def _deactivate() -> None:
    if sys.platform == "darwin":
        for name in (TOKEN_ENV, LEGACY_TOKEN_ENV):
            _run(["launchctl", "unsetenv", name])
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{_AGENT_LABEL}"])
        return
    if sys.platform == "linux":
        _run(
            [
                "systemctl",
                "--user",
                "unset-environment",
                TOKEN_ENV,
                LEGACY_TOKEN_ENV,
            ]
        )


def install_session_env(
    home: Path,
    credential_path: Path,
    *,
    activate: bool | None = None,
) -> SessionEnvReport:
    """Export the saved MCP key for every new shell and the graphical session."""
    if activate is None:
        activate = home.resolve() == Path.home().resolve()
    credential_ref = _home_ref(home, credential_path)
    root = home / ".config/specgate"
    env_sh = root / "env.sh"
    script = root / "session-env.sh"
    _write_private(env_sh, _env_sh(credential_ref).encode())
    _write_script(script, _session_script(credential_ref))
    fish = home / ".config/fish/conf.d/specgate-env.fish"
    shells: list[str] = []
    for path in _install_shells(home):
        block = _fish_block(credential_ref) if path == fish else _posix_block()
        _upsert_block(path, block)
        shells.append(str(path))
    agent = _gui_files(home, script)
    activated = _activate(credential_path, agent) if activate else False
    return {
        "env_sh": str(env_sh),
        "shells": shells,
        "session_script": str(script),
        "launch_agent": str(agent) if agent is not None else None,
        "activated": activated,
    }


def uninstall_session_env(home: Path, *, activate: bool | None = None) -> None:
    """Remove the shell blocks and the graphical session publisher."""
    if activate is None:
        activate = home.resolve() == Path.home().resolve()
    for path in _clean_shells(home):
        _remove_block(path)
    root = home / ".config/specgate"
    for name in ("env.sh", "session-env.sh"):
        (root / name).unlink(missing_ok=True)
    if sys.platform == "darwin":
        agent = home / "Library/LaunchAgents" / f"{_AGENT_LABEL}.plist"
    elif sys.platform == "linux":
        agent = home / ".config/systemd/user" / f"{_AGENT_LABEL}.service"
    else:
        agent = None
    if agent is not None:
        agent.unlink(missing_ok=True)
    if activate:
        _deactivate()
