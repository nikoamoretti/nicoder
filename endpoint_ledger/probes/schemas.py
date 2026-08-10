"""Pydantic models for probe definitions (version-controlled YAML files)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ProbeCategory = Literal["structured_output", "tool_calling", "instruction_adherence"]
Importance = Literal["critical", "high", "medium", "low"]


class ProbeMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None


class ProbeDefinition(BaseModel):
    """A single version-controlled probe definition."""

    stable_key: str = Field(pattern=r"^[a-z0-9_]+$", min_length=3, max_length=120)
    version: int = Field(ge=1)
    category: ProbeCategory
    title: str = Field(min_length=1)
    description: str = Field(default="")
    messages: list[ProbeMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None
    request_parameters: dict[str, Any] = Field(default_factory=dict)
    expected_behavior: dict[str, Any] = Field(default_factory=dict)
    scorers: list[str] = Field(min_length=1)
    importance: Importance = "medium"
    tags: list[str] = Field(default_factory=list)

    @field_validator("scorers")
    @classmethod
    def scorers_must_be_unique(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for name in v:
            if name in seen:
                raise ValueError(f"duplicate scorer in list: {name!r}")
            seen.add(name)
        return v


class ProbeBundle(BaseModel):
    """All probes loaded from a directory, keyed by stable_key."""

    probes: list[ProbeDefinition]

    def by_key(self) -> dict[str, ProbeDefinition]:
        return {p.stable_key: p for p in self.probes}
