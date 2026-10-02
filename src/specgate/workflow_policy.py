"""Portable workflow policy; setup writes, adapters only resolve and snapshot."""

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from specgate.context import _resolve_source
from specgate.privacy import ensure_safe_content
from specgate.product import config_root

TRACKER_CAPABILITIES = {
    "github": ("issues", "versions", "labels", "sub_issues", "dependencies"),
    "local": ("issues", "labels"),
    "freeform": ("issues", "labels"),
    "gitlab": (),
    "jira": (),
    "beads": (),
}


class AdapterScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    catalog: Literal["public_enabled"] = "public_enabled"
    personal_skills: list[str] = Field(default_factory=list, max_length=100)
    project_files: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("personal_skills", "project_files")
    @classmethod
    def safe_names(cls, values: list[str]) -> list[str]:
        for value in values:
            ensure_safe_content(value)
            if (
                not value.strip()
                or not Path(value).parts
                or len(value) > 500
                or Path(value).is_absolute()
                or ".." in Path(value).parts
                or "\\" in value
            ):
                raise ValueError(
                    "Informe nomes ou caminhos relativos dentro do escopo autorizado."
                )
        return sorted({Path(value).as_posix() for value in values})


class WorkflowPolicy(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )
    confidence: float = Field(default=0.8, ge=0, le=1)
    research_budget: int = Field(default=12, ge=0, le=100)
    tracker: Literal["github", "gitlab", "jira", "beads", "local", "freeform"] = (
        "github"
    )
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    adapter_scope: AdapterScope = Field(default_factory=AdapterScope)

    @field_validator("tracker", mode="before")
    @classmethod
    def issue_alias(cls, value: Any) -> Any:
        return "github" if value == "issue" else value

    @model_validator(mode="after")
    def declared_capabilities(self) -> "WorkflowPolicy":
        if not set(self.required_capabilities) <= set(
            TRACKER_CAPABILITIES[self.tracker]
        ):
            raise ValueError("O destino não declara as capacidades exigidas.")
        return self

    def snapshot(self, project: Path) -> "WorkflowPolicy":
        """Bind local permissions to evidence identities without reading content."""
        root = project.resolve()
        sources = sorted(
            {
                _resolve_source(root, source)[1]
                for source in self.adapter_scope.project_files
            }
        )
        return self.model_copy(
            update={
                "adapter_scope": self.adapter_scope.model_copy(
                    update={"project_files": sources}
                )
            }
        )

    @property
    def revision(self) -> str:
        return sha256(
            json.dumps(
                self.model_dump(), sort_keys=True, ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()

    def threshold(self, host_floor: float = 0.8) -> float:
        return max(0.8, host_floor, self.confidence)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("A configuração de política não pode ser um symlink.")
    if not path.exists():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("A configuração de política excede 64 KB.")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Configuração de política inválida.")  # noqa: TRY004
    return data


def _layer(data: dict[str, Any], skill: str | None = None) -> dict[str, Any]:
    skills = data.get("skills", {})
    if not isinstance(skills, dict):
        raise ValueError("Configuração por skill inválida.")  # noqa: TRY004
    value = skills.get(skill, {}) if skill else data.get("policy", {})
    if not isinstance(value, dict):
        raise ValueError("Política inválida.")  # noqa: TRY004
    # Validate each supplied layer, even if another layer would mask it.
    WorkflowPolicy.model_validate(value)
    return value


def resolve_workflow_policy(
    *,
    home: Path | None = None,
    project: Path | None = None,
    skill: str | None = None,
) -> dict[str, Any]:
    global_config = _read(
        config_root(home or Path.home(), write=True) / "workflow.json"
    )
    local = _read(project / ".specgate/workflow.json") if project else {}
    return _resolve(global_config, local, skill)


def _resolve(
    global_config: dict[str, Any], local: dict[str, Any], skill: str | None
) -> dict[str, Any]:
    values = WorkflowPolicy().model_dump()
    sources = dict.fromkeys(values, "default")
    layers = [
        ("global", _layer(global_config)),
        ("global_skill", _layer(global_config, skill) if skill else {}),
        ("project", _layer(local)),
        ("project_skill", _layer(local, skill) if skill else {}),
    ]
    for name, layer in layers:
        values.update(layer)
        sources.update(dict.fromkeys(layer, name))
    policy = WorkflowPolicy.model_validate(values)
    capabilities = TRACKER_CAPABILITIES[policy.tracker]
    return {
        "policy": policy.model_dump(),
        "policy_hash": policy.revision,
        "sources": sources,
        "effective_confidence": policy.threshold(),
        "comparison": "strictly_greater",
        "tracker": {
            "destination": policy.tracker,
            "available": bool(capabilities),
            "capabilities": list(capabilities),
            "reason": "adapter_available_auth_unverified"
            if capabilities
            else "adapter_unavailable",
        },
    }


def configure_workflow_policy(
    changes: dict[str, Any],
    *,
    home: Path | None = None,
    project: Path | None = None,
    skill: str | None = None,
    scope: str = "global",
    delegation_changes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if scope not in {"global", "project"} or (scope == "project" and project is None):
        raise ValueError("Informe um projeto para configurar o escopo local.")
    if skill is not None and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,99}", skill):
        raise ValueError("Nome de skill inválido.")
    if scope == "project":
        assert project is not None
        root = project / ".specgate"
    else:
        root = config_root(home or Path.home(), write=True)
    path = root / "workflow.json"
    data = _read(path)
    if delegation_changes is not None:
        from specgate.delegation import checked_delegation
        if scope != "global" or skill:
            raise ValueError("Configure a delegação somente no escopo global do usuário.")
        data["delegation"] = checked_delegation(delegation_changes, data.get("delegation", {})).model_dump()
    current = _layer(data, skill)
    values = {**current, **changes}
    checked = WorkflowPolicy.model_validate(values).model_dump()
    # Store only explicit fields; inherited defaults remain inheritable.
    updated = {name: checked[name] for name in values}
    if skill:
        data["skills"] = {**data.get("skills", {}), skill: updated}
    else:
        data["policy"] = updated
    global_data = (
        data
        if scope == "global"
        else _read(config_root(home or Path.home(), write=True) / "workflow.json")
    )
    local_data = (
        data
        if scope == "project"
        else _read(project / ".specgate/workflow.json")
        if project
        else {}
    )
    report = _resolve(global_data, local_data, skill)
    encoded = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("A configuração de política excede 64 KB.")
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".workflow.", dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return report
