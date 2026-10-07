"""Business anchor v2, matching the package's start/end annotations.

Source/version/build identity belongs to the enclosing record, never this DTO.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class AnchorBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    anchor_id: str = Field(min_length=1, max_length=512)
    graph_id: str = Field(min_length=1, max_length=128)
    heading_ref: str | None = Field(default=None, min_length=1, max_length=512)
    heading: str | None = Field(default=None, min_length=1, max_length=2048)

    @field_validator("*")
    @classmethod
    def safe_field(cls, value):
        if isinstance(value, str) and (not value.strip() or value != value.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or "<!--" in value or "-->" in value):
            raise ValueError("Invalid anchor field")
        return value

    @model_validator(mode="after")
    def identity(self):
        local = self.table_ref if isinstance(self, TableAnchor) else self.clause_ref
        prefix = "T-" if isinstance(self, TableAnchor) else "C-"
        if not local.startswith(prefix) or self.anchor_id != f"{self.graph_id}::{local}":
            raise ValueError("Anchor identity conflict")
        return self

    def business_metadata(self) -> dict:
        return self.model_dump(exclude_none=True)


class TableAnchor(AnchorBase):
    anchor_type: Literal["table"]
    table_ref: str = Field(min_length=3, max_length=380)
    table_no: str | None = Field(default=None, min_length=1, max_length=100)


class ClauseAnchor(AnchorBase):
    anchor_type: Literal["clause"]
    clause_ref: str = Field(min_length=3, max_length=380)


Anchor = Annotated[TableAnchor | ClauseAnchor, Field(discriminator="anchor_type")]
ANCHOR_ADAPTER = TypeAdapter(Anchor)


class AnchorEnd(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    anchor_id: str = Field(min_length=1, max_length=512)


def validate_anchors(values: list[dict]) -> tuple[TableAnchor | ClauseAnchor, ...]:
    seen = {}
    for value in values:
        ref = ANCHOR_ADAPTER.validate_python(value)
        key = (ref.graph_id, ref.anchor_id)
        if key in seen and seen[key] != ref:
            raise ValueError("Conflicting duplicate anchor")
        seen[key] = ref
    return tuple(seen.values())


def format_anchor_start(anchor: TableAnchor | ClauseAnchor) -> str:
    data = anchor.business_metadata()
    order = ("anchor_id", "graph_id", "anchor_type", "table_ref", "clause_ref",
             "table_no", "heading_ref", "heading")
    return "<!-- kg-anchor-start\n" + "\n".join(f"{k}: {data[k]}" for k in order if k in data) + "\n-->"


def format_anchor_end(anchor: TableAnchor | ClauseAnchor) -> str:
    return f"<!-- kg-anchor-end\nanchor_id: {anchor.anchor_id}\n-->"
