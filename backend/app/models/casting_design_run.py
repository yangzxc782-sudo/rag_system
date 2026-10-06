from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CastingDesignRun(Base):
    __tablename__ = "casting_design_runs"
    __table_args__ = (
        UniqueConstraint("session_id", "id", name="uq_casting_runs_session_id"),
        UniqueConstraint("turn_id", name="uq_casting_runs_turn"),
        UniqueConstraint("session_id", "call_key", name="uq_casting_runs_call"),
        ForeignKeyConstraint(["session_id", "turn_id"], ["qa_turns.session_id", "qa_turns.id"], name="fk_casting_runs_turn"),
        ForeignKeyConstraint(["session_id", "input_file_id"], ["casting_design_files.session_id", "casting_design_files.id"], name="fk_casting_runs_input"),
        ForeignKeyConstraint(["session_id", "result_file_id"], ["casting_design_files.session_id", "casting_design_files.id"], name="fk_casting_runs_result", use_alter=True),
        CheckConstraint("execution_no > 0", name="ck_casting_runs_execution"),
        CheckConstraint("status IN ('pending', 'running', 'persisting', 'succeeded', 'no_feasible_candidate', 'admission_failed', 'engine_failed', 'timed_out', 'interrupted')", name="ck_casting_runs_status"),
        CheckConstraint("(status IN ('succeeded', 'no_feasible_candidate') AND result_file_id IS NOT NULL AND result_sha256 IS NOT NULL AND finished_at IS NOT NULL AND candidate_count IS NOT NULL) OR "
                        "(status NOT IN ('succeeded', 'no_feasible_candidate') AND result_file_id IS NULL AND result_sha256 IS NULL)", name="ck_casting_runs_result"),
        CheckConstraint("candidate_count IS NULL OR candidate_count >= 0", name="ck_casting_runs_count"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    turn_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    call_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_file_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_input_sha256: Mapped[str | None] = mapped_column(String(64))
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    project_key: Mapped[str] = mapped_column(String(128), nullable=False)
    engine_id: Mapped[str] = mapped_column(String(128), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(128), nullable=False)
    engine_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    engine_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dependency_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    execution_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    result_file_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    result_sha256: Mapped[str | None] = mapped_column(String(64))
    recommended_candidate_id: Mapped[str | None] = mapped_column(String(128))
    candidate_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
