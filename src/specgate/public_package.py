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

PUBLIC_PACKAGE = "specgate-client"
PUBLIC_REPOSITORY = "https://github.com/FelipeOFF/specgate.git"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_READ_ERROR = "Could not read the installed commit or the public master."
_REINSTALL_ERROR = "Public package reinstall failed."
_REAPPLY_ERROR = "The installed package could not apply the harnesses."
# Both scripts run with `-P`: the update runs from the person's project, and a module there
# (a `secrets.py`, say) would take the place of the standard library one.
_QUERY = (
    "import importlib.metadata, sys\n"
    "dist = importlib.metadata.distribution(sys.argv[1])\n"
    "print(dist.read_text('direct_url.json') or '', end='')\n"
)
# Applies the saved harnesses with the code and the skills of the package it runs in.
# Every client already installed runs its own copy against a newer package, so
# `specgate.product.packaged_skills()` and
# `specgate.public_setup.update_public_harnesses(source, home=, codex_command=)`
# are a contract with those clients: they keep these names and this call.
_REAPPLY = (
    "import json, pathlib, sys\n"
    "from specgate.product import packaged_skills\n"
    "from specgate.public_setup import update_public_harnesses\n"
    "home = pathlib.Path(sys.argv[1]) if sys.argv[1] else None\n"
    "command = tuple(json.loads(sys.argv[2]))\n"
    "report = update_public_harnesses(packaged_skills(), home=home, codex_command=command)\n"
    "print(json.dumps(report, ensure_ascii=False))\n"
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
    # Applies the harnesses with the installed package, in a process of its own.
    reapply: Callable[[Path | None, tuple[str, ...]], dict[str, Any]]


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


def _query(python: Path, package: str, spawn: Spawn) -> str:
    completed = spawn([str(python), "-P", "-c", _QUERY, package], capture=True)
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
    return _commit_from_direct_url(_query(python, package, spawn))


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


def reapply_public_package(
    home: Path | None,
    codex_command: tuple[str, ...],
    package: str = PUBLIC_PACKAGE,
    *,
    spawn: Spawn = _spawn,
) -> dict[str, Any]:
    """Apply the saved harnesses with the installed package, in its own interpreter.

    This process runs the code it started with, which a reinstall does not replace: applied
    from here, a skill or a hook a new package brings would only arrive on the next update.
    """
    python = _pipx_python(package)
    if python is None:
        raise ValueError(_REAPPLY_ERROR)
    completed = spawn(
        [str(python), "-P", "-c", _REAPPLY, str(home or ""), json.dumps(list(codex_command))],
        capture=True,
    )
    report = _report(completed)
    if report is None:
        reason = (completed.stderr or "").strip().splitlines()[-1:]
        raise ValueError(" ".join([_REAPPLY_ERROR, *reason]))
    return report


def _report(completed: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    """The JSON report the last line of a successful run printed, if any."""
    lines = (completed.stdout or "").strip().splitlines()
    if completed.returncode != 0 or not lines:
        return None
    try:
        report = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return report if isinstance(report, dict) else None


def live_probes() -> PackageProbes:
    """Probes that read pipx, git, and the package just installed."""
    return PackageProbes(
        read_installed=installed_package_commit,
        read_remote=remote_master_commit,
        reinstall=reinstall_public_package,
        reapply=reapply_public_package,
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


def refresh_public_package(
    *,
    home: Path | None = None,
    codex_command: tuple[str, ...] = ("codex",),
    probes: PackageProbes | None = None,
) -> dict[str, Any]:
    """Reapply saved harnesses, reinstalling only when public master differs.

    The installed package applies them itself, so the skills and hooks of a new one land in
    this same update.
    """
    selected = live_probes() if probes is None else probes
    installed, remote = _read_commits(selected)
    replaced = installed != remote
    if replaced:
        _reinstall(selected)
    report = dict(selected.reapply(home, codex_command))
    report["package_replaced"] = replaced
    report["package"] = {"from": installed, "to": remote}
    return report
