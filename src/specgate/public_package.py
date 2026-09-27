"""Follow the public client repository when specgate update runs."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from specgate.public_setup import update_public_harnesses

PUBLIC_PACKAGE = "specgate-client"
PUBLIC_REPOSITORY = "https://github.com/FelipeOFF/specgate.git"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_READ_ERROR = "Could not read the installed commit or the public master."
_REINSTALL_ERROR = "Public package reinstall failed."
_SKILLS_ERROR = "Installed package skills are unavailable."
_QUERY = (
    "import importlib.metadata, pathlib, sys\n"
    "package, mode = sys.argv[1], sys.argv[2]\n"
    "dist = importlib.metadata.distribution(package)\n"
    "if mode == 'commit':\n"
    "    print(dist.read_text('direct_url.json') or '', end='')\n"
    "else:\n"
    "    print(pathlib.Path(dist.locate_file('specgate/skills')))\n"
)


class Spawn(Protocol):
    """Run one command and return its completed process."""

    def __call__(
        self, command: Sequence[str], *, capture: bool
    ) -> subprocess.CompletedProcess[str]:
        """Run command, capturing stdout only when capture is true."""


@dataclass(frozen=True)
class PackageProbes:
    """Reads and the installer used by one update."""

    read_installed: Callable[[], str]
    read_remote: Callable[[], str]
    reinstall: Callable[[], None]
    skills_root: Callable[[], Path]


def _spawn(
    command: Sequence[str], *, capture: bool
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=capture,
        text=True,
    )


def _venv_roots() -> list[Path]:
    override = os.environ.get("PIPX_VENV_DIR")
    if override:
        return [Path(override)]
    home = os.environ.get("PIPX_HOME")
    if home:
        return [Path(home) / "venvs"]
    return [
        Path.home() / ".local/pipx/venvs",
        Path.home() / ".local/share/pipx/venvs",
    ]


def _pipx_python(package: str) -> Path | None:
    for root in _venv_roots():
        python = root / package / "bin" / "python"
        if python.is_file():
            return python
    return None


def _query(python: Path, package: str, mode: str, spawn: Spawn) -> str:
    completed = spawn([str(python), "-c", _QUERY, package, mode], capture=True)
    if completed.returncode != 0 or not isinstance(completed.stdout, str):
        raise ValueError("Installed package metadata is unavailable.")
    return completed.stdout


def _commit_from_direct_url(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("Installed package commit is unavailable.") from error
    vcs = payload.get("vcs_info") if isinstance(payload, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("Installed package commit is unavailable.")
    return commit


def _commit_from_ls_remote(stdout: str) -> str:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("Public master commit is unavailable.")
    commit, separator, ref = lines[0].partition("\t")
    if not separator:
        commit, separator, ref = lines[0].partition(" ")
    if separator and ref == "refs/heads/master" and _COMMIT.fullmatch(commit):
        return commit
    raise ValueError("Public master commit is unavailable.")


def installed_package_commit(
    package: str = PUBLIC_PACKAGE, *, spawn: Spawn = _spawn
) -> str:
    """Return the VCS commit pipx recorded for the installed public client."""
    python = _pipx_python(package)
    if python is None:
        raise ValueError("Installed package commit is unavailable.")
    return _commit_from_direct_url(_query(python, package, "commit", spawn))


def remote_master_commit(
    repository: str = PUBLIC_REPOSITORY, *, spawn: Spawn = _spawn
) -> str:
    """Return the commit currently at the public repository master branch."""
    completed = spawn(
        ["git", "ls-remote", repository, "refs/heads/master"],
        capture=True,
    )
    if completed.returncode != 0:
        raise ValueError("Public master commit is unavailable.")
    return _commit_from_ls_remote(completed.stdout or "")


def reinstall_public_package(
    repository: str = PUBLIC_REPOSITORY, *, spawn: Spawn = _spawn
) -> None:
    """Replace the installed public client with the repository tip."""
    completed = spawn(
        ["pipx", "install", "--force", f"git+{repository}"],
        capture=False,
    )
    if completed.returncode != 0:
        raise ValueError("Public package reinstall failed.")


def installed_skill_root(
    package: str = PUBLIC_PACKAGE, *, spawn: Spawn = _spawn
) -> Path:
    """Return the on-disk skill root of the installed public client."""
    python = _pipx_python(package)
    if python is None:
        raise ValueError("Installed skills are unavailable.")
    skills = Path(_query(python, package, "skills", spawn).strip())
    if not (skills / "specgate" / "SKILL.md").is_file():
        raise ValueError("Installed skills are unavailable.")
    return skills


def live_probes() -> PackageProbes:
    """Probes that read pipx, git, and the package just installed."""
    return PackageProbes(
        read_installed=installed_package_commit,
        read_remote=remote_master_commit,
        reinstall=reinstall_public_package,
        skills_root=installed_skill_root,
    )


def _read_commits(probes: PackageProbes) -> tuple[str, str]:
    try:
        installed = probes.read_installed()
        remote = probes.read_remote()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(_READ_ERROR) from error
    if _COMMIT.fullmatch(installed) is None or _COMMIT.fullmatch(remote) is None:
        raise ValueError(_READ_ERROR)
    return installed, remote


def _reinstall(probes: PackageProbes) -> None:
    try:
        probes.reinstall()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ValueError(_REINSTALL_ERROR) from error


def _skills(probes: PackageProbes) -> Path:
    try:
        source = probes.skills_root()
    except (OSError, ValueError) as error:
        raise ValueError(_SKILLS_ERROR) from error
    if not (source / "specgate" / "SKILL.md").is_file():
        raise ValueError(_SKILLS_ERROR)
    return source


def refresh_public_package(
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
    probes: PackageProbes | None = None,
) -> dict[str, Any]:
    """Reapply saved harnesses, reinstalling only when public master differs."""
    selected = live_probes() if probes is None else probes
    installed, remote = _read_commits(selected)
    replaced = installed != remote
    if replaced:
        _reinstall(selected)
    report = dict(
        update_public_harnesses(
            _skills(selected),
            home=home,
            codex_command=codex_command,
        )
    )
    report["package_replaced"] = replaced
    return report
