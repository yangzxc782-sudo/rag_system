from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.knowledge_item import KnowledgeItem


class KnowledgeItemVersion(Base):
    __tablename__ = "knowledge_item_versions"
    __table_args__ = (
        Index("ix_knowledge_item_versions_knowledge_item_id", "knowledge_item_id"),
        Index("ix_knowledge_item_versions_version", "version"),
        UniqueConstraint("knowledge_item_id", "version", name="uq_knowledge_item_versions_item_version"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    knowledge_item_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_items.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    knowledge_item: Mapped[KnowledgeItem] = relationship("KnowledgeItem", back_populates="versions")
