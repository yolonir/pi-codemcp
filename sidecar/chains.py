from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal, NamedTuple
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .json_types import JSON_OBJECT_ADAPTER, JsonObject

CHAIN_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
CHAIN_STORE_VERSION: Literal[1] = 1
type ChainScope = Literal["global", "project"]


class ChainDependency(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["mcp_tool", "saved_chain"]
    name: str
    call: str
    server: str
    schema_fingerprint: str


class SavedChainManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = CHAIN_STORE_VERSION
    id: str
    name: str = Field(pattern=CHAIN_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=1000)
    code: str = Field(min_length=1)
    input_schema: JsonObject
    output_schema: JsonObject
    enabled: bool = True
    dependencies: list[ChainDependency] = Field(default_factory=list)
    schema_fingerprint: str
    created_at: float
    updated_at: float
    validated_at: float

    @field_validator("code")
    @classmethod
    def code_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code must not be blank")
        return value

    @field_validator("input_schema")
    @classmethod
    def input_schema_must_be_object(cls, value: JsonObject) -> JsonObject:
        if value.get("type") != "object":
            raise ValueError("input_schema.type must be object")
        return value

    @property
    def public_name(self) -> str:
        return f"chain_{self.name}"

    @property
    def call(self) -> str:
        return f"chains.{self.name}"

    @property
    def native_tool(self) -> str:
        return f"mcp_chain_{self.name}"


class ChainStatusView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chain: SavedChainManifest
    scope: ChainScope
    status: Literal["ready", "disabled", "stale", "shadowed"]
    stale_dependencies: list[str] = Field(default_factory=list)
    called_by: list[str] = Field(default_factory=list)


class ChainListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chains: list[ChainStatusView]


class SaveChainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chain: ChainStatusView
    created: bool


class ChainStore:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def load_all(self) -> list[SavedChainManifest]:
        if not self.directory.exists():
            return []
        manifests: list[SavedChainManifest] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                manifests.append(
                    SavedChainManifest.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as error:
                raise ValueError(f"Invalid saved chain manifest {path}: {error}") from error
        return manifests

    def enabled(self) -> list[SavedChainManifest]:
        return [chain for chain in self.load_all() if chain.enabled]

    def contains(self, name: str) -> bool:
        self._validate_name(name)
        return self._path(name).is_file()

    def get(self, name: str) -> SavedChainManifest:
        self._validate_name(name)
        path = self._path(name)
        try:
            return SavedChainManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(f"Unknown saved chain: {name}") from error
        except (OSError, ValueError) as error:
            raise ValueError(f"Invalid saved chain manifest {path}: {error}") from error

    @staticmethod
    def build(
        *,
        name: str,
        description: str,
        code: str,
        input_schema: JsonObject,
        output_schema: JsonObject,
        dependencies: list[ChainDependency],
        previous: SavedChainManifest | None = None,
    ) -> SavedChainManifest:
        now = time.time()
        validated_input_schema = JSON_OBJECT_ADAPTER.validate_python(input_schema)
        validated_output_schema = JSON_OBJECT_ADAPTER.validate_python(output_schema)
        schema_fingerprint = _fingerprint({
            "input_schema": validated_input_schema,
            "output_schema": validated_output_schema,
        })
        return SavedChainManifest(
            id=previous.id if previous is not None else uuid4().hex,
            name=name,
            description=description,
            code=code,
            input_schema=validated_input_schema,
            output_schema=validated_output_schema,
            enabled=previous.enabled if previous is not None else True,
            dependencies=dependencies,
            schema_fingerprint=schema_fingerprint,
            created_at=previous.created_at if previous is not None else now,
            updated_at=now,
            validated_at=now,
        )

    def save(self, chain: SavedChainManifest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            self.directory.chmod(0o700)
        path = self._path(chain.name)
        temporary = self.directory / f".{chain.name}.{os.getpid()}.{time.time_ns()}.tmp"
        temporary.write_text(
            f"{chain.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)

    def set_enabled(self, name: str, enabled: bool) -> SavedChainManifest:
        current = self.get(name)
        updated = current.model_copy(update={"enabled": enabled, "updated_at": time.time()})
        self.save(updated)
        return updated

    def delete(self, name: str) -> None:
        self._validate_name(name)
        try:
            self._path(name).unlink()
        except FileNotFoundError as error:
            raise ValueError(f"Unknown saved chain: {name}") from error

    def _path(self, name: str) -> Path:
        self._validate_name(name)
        return self.directory / f"{name}.json"

    @staticmethod
    def _validate_name(name: str) -> None:
        if re.fullmatch(CHAIN_NAME_PATTERN, name) is None:
            raise ValueError(
                "Saved chain name must start with a lowercase letter and contain only "
                "lowercase letters, digits, and underscores (maximum 64 characters)"
            )


class ScopedChain(NamedTuple):
    scope: ChainScope
    chain: SavedChainManifest


class ScopedChainStore:
    def __init__(self, global_directory: Path, project_directory: Path | None) -> None:
        self.global_store = ChainStore(global_directory)
        self.project_store = (
            ChainStore(project_directory) if project_directory is not None else None
        )

    def load_all(self) -> list[ScopedChain]:
        chains = [
            ScopedChain(scope="global", chain=chain) for chain in self.global_store.load_all()
        ]
        if self.project_store is not None:
            chains.extend(
                ScopedChain(scope="project", chain=chain) for chain in self.project_store.load_all()
            )
        return sorted(
            chains,
            key=lambda item: (item.chain.name, item.scope != "project"),
        )

    def effective(self) -> list[ScopedChain]:
        effective: dict[str, ScopedChain] = {}
        for item in self.load_all():
            current = effective.get(item.chain.name)
            if current is None or item.scope == "project":
                effective[item.chain.name] = item
        return [effective[name] for name in sorted(effective)]

    def enabled(self) -> list[SavedChainManifest]:
        return [item.chain for item in self.effective() if item.chain.enabled]

    def get(self, name: str, scope: ChainScope | None = None) -> ScopedChain:
        if scope is not None:
            return ScopedChain(scope=scope, chain=self._store(scope).get(name))
        if self.project_store is not None and self.project_store.contains(name):
            return ScopedChain(scope="project", chain=self.project_store.get(name))
        return ScopedChain(scope="global", chain=self.global_store.get(name))

    def contains(self, scope: ChainScope, name: str) -> bool:
        return self._store(scope).contains(name)

    def save(self, scope: ChainScope, chain: SavedChainManifest) -> None:
        self._store(scope).save(chain)

    def set_enabled(
        self,
        scope: ChainScope,
        name: str,
        enabled: bool,
    ) -> ScopedChain:
        return ScopedChain(
            scope=scope,
            chain=self._store(scope).set_enabled(name, enabled),
        )

    def delete(self, scope: ChainScope, name: str) -> None:
        self._store(scope).delete(name)

    def is_shadowed(self, item: ScopedChain) -> bool:
        return (
            item.scope == "global"
            and self.project_store is not None
            and self.project_store.contains(item.chain.name)
        )

    def _store(self, scope: ChainScope) -> ChainStore:
        if scope == "global":
            return self.global_store
        if self.project_store is None:
            raise ValueError("Project saved-chain scope is unavailable for this session")
        return self.project_store


def schema_fingerprint(input_schema: JsonObject, output_schema: JsonObject) -> str:
    return _fingerprint({"input_schema": input_schema, "output_schema": output_schema})


def _fingerprint(value: JsonObject) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
