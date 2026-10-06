from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CastingDesignFile(Base):
    __tablename__ = "casting_design_files"
    __table_args__ = (
        UniqueConstraint("session_id", "id", name="uq_casting_files_session_id"),
        UniqueConstraint("session_id", "upload_request_id", name="uq_casting_files_upload"),
        UniqueConstraint("run_id", "execution_no", "artifact_name", name="uq_casting_files_artifact"),
        UniqueConstraint("bucket", "object_key", name="uq_casting_files_object"),
        ForeignKeyConstraint(["session_id", "run_id"], ["casting_design_runs.session_id", "casting_design_runs.id"],
                             name="fk_casting_files_run", use_alter=True),
        CheckConstraint("size_bytes >= 0 AND size_bytes <= 33554432", name="ck_casting_files_size"),
        CheckConstraint("storage_state IN ('pending', 'ready')", name="ck_casting_files_state"),
        CheckConstraint("(kind = 'input' AND upload_request_id IS NOT NULL AND run_id IS NULL AND execution_no IS NULL AND artifact_name IS NULL) OR "
                        "(kind IN ('rules', 'engine', 'audit', 'recommendation') AND upload_request_id IS NULL AND run_id IS NOT NULL AND execution_no IS NOT NULL AND execution_no > 0 AND artifact_name IS NOT NULL)", name="ck_casting_files_kind"),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("qa_sessions.id"), nullable=False, index=True)
    upload_request_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    run_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    execution_no: Mapped[int | None] = mapped_column(Integer)
    artifact_name: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    admission_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
