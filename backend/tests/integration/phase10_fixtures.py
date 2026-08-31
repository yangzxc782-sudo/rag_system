from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_asset import DocumentAsset
from app.models.document_block import DocumentBlock
from app.models.document_chunk import DocumentChunk
from app.models.document_chunk_block import DocumentChunkBlock
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.document_parse_run import DocumentParseRun
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_chunk import KnowledgeItemChunk
from app.models.knowledge_item_review import KnowledgeItemReview
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.document_deletion import ClaimedDocumentDeletion
from app.services.document_deletion_manifest import (
    DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION,
    DocumentDeletionManifest,
)
from integration.phase10_support import Phase10TestDocument


@dataclass(frozen=True, slots=True)
class Phase10KnowledgeFixture:
    target_document_id: UUID
    shared_item_id: UUID
    target_only_item_id: UUID
    all_item_ids: tuple[UUID, ...]
    target_shared_source_id: UUID
    target_only_source_id: UUID
    source_ids_by_document: tuple[tuple[UUID, tuple[UUID, ...]], ...]
    chunk_relation_ids: tuple[UUID, ...]
    chunk_relation_ids_by_document: tuple[tuple[UUID, tuple[UUID, ...]], ...]
    version_ids: tuple[UUID, ...]
    version_ids_by_item: tuple[tuple[UUID, UUID], ...]
    review_ids: tuple[UUID, ...]
    review_ids_by_item: tuple[tuple[UUID, UUID], ...]
    control_document_id: UUID | None

    def source_ids_for(self, document_id: UUID) -> tuple[UUID, ...]:
        return next(
            (identifiers for current, identifiers in self.source_ids_by_document if current == document_id),
            (),
        )

    @property
    def target_delete_orphan_item_ids(self) -> tuple[UUID, ...]:
        if self.control_document_id is None:
            return self.all_item_ids
        return (self.target_only_item_id,)

    @property
    def target_delete_chunk_relation_ids(self) -> tuple[UUID, ...]:
        return next(
            (
                identifiers
                for document_id, identifiers in self.chunk_relation_ids_by_document
                if document_id == self.target_document_id
            ),
            (),
        )

    @property
    def target_delete_version_ids(self) -> tuple[UUID, ...]:
        orphan_ids = set(self.target_delete_orphan_item_ids)
        return tuple(
            version_id
            for item_id, version_id in self.version_ids_by_item
            if item_id in orphan_ids
        )

    @property
    def target_delete_review_ids(self) -> tuple[UUID, ...]:
        orphan_ids = set(self.target_delete_orphan_item_ids)
        return tuple(
            review_id
            for item_id, review_id in self.review_ids_by_item
            if item_id in orphan_ids
        )


@dataclass(frozen=True, slots=True)
class Phase10RevisionBoundaryFixture:
    orphan_parent_id: UUID
    surviving_child_id: UUID
    self_cycle_id: UUID


def persist_document_fixture(
    session: Session,
    identity: Phase10TestDocument,
    *,
    bucket_name: str,
) -> Document:
    document = Document(
        id=identity.document_id,
        original_filename=identity.filename,
        bucket_name=bucket_name,
        object_key=identity.raw_object_key,
        file_type="pdf",
        mime_type="application/pdf",
        file_size=128,
        file_hash=sha256(identity.namespace.encode()).hexdigest(),
        process_status="parsed",
        deletion_status="normal",
    )
    parse_run = DocumentParseRun(
        id=identity.parse_run_id,
        document_id=identity.document_id,
        parser_provider="mineru_api",
        parser_version="phase10-integration",
        status="succeeded",
        is_active=True,
        input_file_key=identity.raw_object_key,
        output_prefix=f"{identity.derived_prefix}{identity.parse_run_id}",
        output_markdown_key=identity.explicit_derived_keys[0],
        output_json_key=identity.explicit_derived_keys[1],
        page_count=1,
        block_count=len(identity.block_ids),
        asset_count=len(identity.asset_ids),
    )
    block = DocumentBlock(
        id=identity.block_ids[0],
        document_id=identity.document_id,
        parse_run_id=identity.parse_run_id,
        block_index=0,
        block_key="phase10-block-0",
        block_type="text",
        page_start=1,
        page_end=1,
        text=f"fixture {identity.namespace}",
    )
    asset = DocumentAsset(
        id=identity.asset_ids[0],
        document_id=identity.document_id,
        parse_run_id=identity.parse_run_id,
        asset_type="image",
        page_number=1,
        asset_key=identity.explicit_derived_keys[2],
        filename="page-1.png",
        mime_type="image/png",
        size_bytes=32,
    )
    chunks = [
        DocumentChunk(
            id=chunk_id,
            document_id=identity.document_id,
            parse_run_id=identity.parse_run_id,
            chunk_index=index,
            content=f"{identity.namespace} chunk {index}",
            token_count=8,
            page_start=1,
            page_end=1,
            chunk_type="text",
            chunk_method="block_aware",
            content_format="text",
            embedding=[0.0] * 1024,
            embedding_model="Qwen3-Embedding-0.6B",
            embedding_dim=1024,
            embedding_status="embedded",
        )
        for index, chunk_id in enumerate(identity.chunk_ids)
    ]
    mappings = [
        DocumentChunkBlock(
            id=identity.chunk_block_ids[index],
            chunk_id=chunk.id,
            block_id=block.id,
            block_order=0,
        )
        for index, chunk in enumerate(chunks)
    ]
    session.add_all([document, parse_run, block, asset, *chunks, *mappings])
    session.flush()
    return document


def persist_shared_knowledge_fixture(
    session: Session,
    *,
    target: Phase10TestDocument,
    control: Phase10TestDocument | None,
) -> Phase10KnowledgeFixture:
    now = datetime.now(UTC)
    shared = _knowledge_item(target, label="shared")
    target_only = _knowledge_item(target, label="target-only")
    session.add_all([shared, target_only])
    session.flush()

    target_shared_source = _source(shared.id, target, created_at=now)
    target_only_source = _source(target_only.id, target, created_at=now)
    sources = [target_shared_source, target_only_source]
    sources_by_document: list[tuple[UUID, tuple[UUID, ...]]] = [
        (
            target.document_id,
            (target_shared_source.id, target_only_source.id),
        )
    ]
    control_source = None
    if control is not None:
        control_source = _source(shared.id, control, created_at=now)
        sources.append(control_source)
        sources_by_document.append((control.document_id, (control_source.id,)))
    session.add_all(sources)
    session.flush()

    chunk_relations = [
        _knowledge_chunk(shared.id, target, target.chunk_ids[0], 0),
        _knowledge_chunk(target_only.id, target, target.chunk_ids[-1], 1),
    ]
    if control is not None:
        chunk_relations.append(
            _knowledge_chunk(shared.id, control, control.chunk_ids[0], 0)
        )
    versions = [
        _version(shared, target),
        _version(target_only, target),
    ]
    reviews = [_review(shared), _review(target_only)]
    session.add_all([*chunk_relations, *versions, *reviews])
    session.flush()
    chunk_relations_by_document = [
        (
            target.document_id,
            tuple(
                relation.id
                for relation in chunk_relations
                if relation.document_id == target.document_id
            ),
        )
    ]
    if control is not None:
        chunk_relations_by_document.append(
            (
                control.document_id,
                tuple(
                    relation.id
                    for relation in chunk_relations
                    if relation.document_id == control.document_id
                ),
            )
        )
    return Phase10KnowledgeFixture(
        target_document_id=target.document_id,
        shared_item_id=shared.id,
        target_only_item_id=target_only.id,
        all_item_ids=(shared.id, target_only.id),
        target_shared_source_id=target_shared_source.id,
        target_only_source_id=target_only_source.id,
        source_ids_by_document=tuple(sources_by_document),
        chunk_relation_ids=tuple(relation.id for relation in chunk_relations),
        chunk_relation_ids_by_document=tuple(chunk_relations_by_document),
        version_ids=tuple(version.id for version in versions),
        version_ids_by_item=tuple(
            (version.knowledge_item_id, version.id) for version in versions
        ),
        review_ids=tuple(review.id for review in reviews),
        review_ids_by_item=tuple(
            (review.knowledge_item_id, review.id) for review in reviews
        ),
        control_document_id=control.document_id if control is not None else None,
    )


def persist_revision_cycle_fixture(
    session: Session,
    *,
    document_a: Phase10TestDocument,
    document_b: Phase10TestDocument,
) -> Phase10KnowledgeFixture:
    items = [_knowledge_item(document_a, label=f"revision-{index}") for index in range(3)]
    session.add_all(items)
    session.flush()
    items[0].revises_item_id = items[2].id
    items[1].revises_item_id = items[0].id
    items[2].revises_item_id = items[1].id

    sources = [
        _source(items[0].id, document_a),
        _source(items[1].id, document_b),
        _source(items[2].id, document_a),
        _source(items[2].id, document_b),
    ]
    session.add_all(sources)
    session.flush()
    chunk_relations = [
        _knowledge_chunk(items[0].id, document_a, document_a.chunk_ids[0], 0),
        _knowledge_chunk(items[1].id, document_b, document_b.chunk_ids[0], 0),
        _knowledge_chunk(items[2].id, document_a, document_a.chunk_ids[-1], 1),
        _knowledge_chunk(items[2].id, document_b, document_b.chunk_ids[-1], 1),
    ]
    versions = [_version(item, document_a if index != 1 else document_b) for index, item in enumerate(items)]
    reviews = [_review(item) for item in items]
    session.add_all([*chunk_relations, *versions, *reviews])
    session.flush()
    return Phase10KnowledgeFixture(
        target_document_id=document_a.document_id,
        shared_item_id=items[2].id,
        target_only_item_id=items[0].id,
        all_item_ids=tuple(item.id for item in items),
        target_shared_source_id=sources[2].id,
        target_only_source_id=sources[0].id,
        source_ids_by_document=(
            (document_a.document_id, (sources[0].id, sources[2].id)),
            (document_b.document_id, (sources[1].id, sources[3].id)),
        ),
        chunk_relation_ids=tuple(relation.id for relation in chunk_relations),
        chunk_relation_ids_by_document=(
            (
                document_a.document_id,
                tuple(
                    relation.id
                    for relation in chunk_relations
                    if relation.document_id == document_a.document_id
                ),
            ),
            (
                document_b.document_id,
                tuple(
                    relation.id
                    for relation in chunk_relations
                    if relation.document_id == document_b.document_id
                ),
            ),
        ),
        version_ids=tuple(version.id for version in versions),
        version_ids_by_item=tuple(
            (version.knowledge_item_id, version.id) for version in versions
        ),
        review_ids=tuple(review.id for review in reviews),
        review_ids_by_item=tuple(
            (review.knowledge_item_id, review.id) for review in reviews
        ),
        control_document_id=document_b.document_id,
    )


def persist_revision_boundary_fixture(
    session: Session,
    *,
    document_a: Phase10TestDocument,
    document_b: Phase10TestDocument,
) -> Phase10RevisionBoundaryFixture:
    parent = _knowledge_item(document_a, label="orphan-parent")
    child = _knowledge_item(document_b, label="surviving-child")
    self_cycle = _knowledge_item(document_a, label="self-cycle")
    session.add_all([parent, child, self_cycle])
    session.flush()
    child.revises_item_id = parent.id
    self_cycle.revises_item_id = self_cycle.id
    sources = [
        _source(parent.id, document_a),
        _source(child.id, document_b),
        _source(self_cycle.id, document_a),
    ]
    session.add_all(sources)
    session.flush()
    chunks = [
        _knowledge_chunk(parent.id, document_a, document_a.chunk_ids[0], 0),
        _knowledge_chunk(child.id, document_b, document_b.chunk_ids[0], 0),
        _knowledge_chunk(self_cycle.id, document_a, document_a.chunk_ids[-1], 1),
    ]
    session.add_all(
        [
            *chunks,
            _version(parent, document_a),
            _version(child, document_b),
            _version(self_cycle, document_a),
            _review(parent),
            _review(child),
            _review(self_cycle),
        ]
    )
    session.flush()
    return Phase10RevisionBoundaryFixture(
        orphan_parent_id=parent.id,
        surviving_child_id=child.id,
        self_cycle_id=self_cycle.id,
    )


def build_storage_manifest(
    identity: Phase10TestDocument,
    settings: Any,
    *,
    knowledge: Phase10KnowledgeFixture | None = None,
) -> DocumentDeletionManifest:
    source_ids = knowledge.source_ids_for(identity.document_id) if knowledge else ()
    item_ids = knowledge.all_item_ids if knowledge else ()
    return DocumentDeletionManifest(
        schema_version=DOCUMENT_DELETION_MANIFEST_SCHEMA_VERSION,
        document_id=identity.document_id,
        bucket_name=str(_setting(settings, "minio_bucket")),
        raw_object_key=identity.raw_object_key,
        derived_object_keys=identity.explicit_derived_keys,
        derived_prefixes=(f"{identity.derived_prefix}{identity.parse_run_id}/",),
        parse_run_ids=(identity.parse_run_id,),
        block_ids=identity.block_ids,
        asset_ids=identity.asset_ids,
        chunk_ids=identity.chunk_ids,
        knowledge_source_relation_ids=source_ids,
        knowledge_item_ids=item_ids,
        search_index_name=str(_setting(settings, "search_index_name", "opensearch_index")),
        search_index_alias=str(_setting(settings, "search_index_alias", "opensearch_alias")),
    )


def schedule_pending_job(
    session: Session,
    identity: Phase10TestDocument,
    settings: Any,
    *,
    current_step: str,
    knowledge: Phase10KnowledgeFixture | None = None,
) -> DocumentDeletionJob:
    document = session.get(Document, identity.document_id)
    if document is None:
        raise AssertionError("Phase 10 fixture Document was not persisted.")
    document.deletion_status = "deleting"
    job = DocumentDeletionJob(
        document_id=identity.document_id,
        status="pending",
        current_step=current_step,
        step_attempts=0,
        max_attempts=int(getattr(settings, "document_deletion_max_step_attempts", 5)),
        manifest=build_storage_manifest(identity, settings, knowledge=knowledge).to_payload(),
    )
    session.add(job)
    session.flush()
    return job


def build_finalization_claim(
    session: Session,
    identity: Phase10TestDocument,
    *,
    knowledge: Phase10KnowledgeFixture | None,
    settings: Any,
) -> tuple[ClaimedDocumentDeletion, DocumentDeletionManifest]:
    manifest = build_storage_manifest(identity, settings, knowledge=knowledge)
    document = session.get(Document, identity.document_id)
    if document is None:
        raise AssertionError("Phase 10 fixture Document was not persisted.")
    document.deletion_status = "deleting"
    token = uuid4()
    job = DocumentDeletionJob(
        document_id=identity.document_id,
        status="processing",
        current_step="finalize_postgresql",
        step_attempts=1,
        max_attempts=int(getattr(settings, "document_deletion_max_step_attempts", 5)),
        manifest=manifest.to_payload(),
        locked_by=f"phase10-integration-{identity.document_id}",
        lease_token=token,
        locked_at=func.now(),
        lease_expires_at=func.now() + text("INTERVAL '10 minutes'"),
    )
    session.add(job)
    session.flush()
    session.refresh(job)
    return ClaimedDocumentDeletion.from_job(job), manifest


def assert_postgresql_target_absent(
    session: Session,
    identity: Phase10TestDocument,
    *,
    knowledge: Phase10KnowledgeFixture,
) -> None:
    assert session.get(Document, identity.document_id) is None
    assert session.scalar(
        select(DocumentDeletionJob).where(
            DocumentDeletionJob.document_id == identity.document_id
        )
    ) is None
    assert not session.scalars(
        select(DocumentChunk.id).where(DocumentChunk.document_id == identity.document_id)
    ).all()
    assert not session.scalars(
        select(DocumentParseRun.id).where(DocumentParseRun.document_id == identity.document_id)
    ).all()
    assert not session.scalars(
        select(DocumentAsset.id).where(DocumentAsset.document_id == identity.document_id)
    ).all()
    assert not session.scalars(
        select(DocumentBlock.id).where(DocumentBlock.document_id == identity.document_id)
    ).all()
    assert not session.scalars(
        select(KnowledgeItemSource.id).where(
            KnowledgeItemSource.document_id == identity.document_id
        )
    ).all()
    assert not session.scalars(
        select(KnowledgeItemChunk.id).where(
            KnowledgeItemChunk.document_id == identity.document_id
        )
    ).all()
    for item_id in knowledge.target_delete_orphan_item_ids:
        assert session.get(KnowledgeItem, item_id) is None


def upload_minio_fixture_versions(
    client: Any,
    identity: Phase10TestDocument,
    bucket_name: str,
) -> None:
    for key in sorted(identity.all_minio_keys):
        for revision in (b"phase10-v1", b"phase10-v2"):
            client.put_object(
                bucket_name,
                key,
                BytesIO(revision),
                len(revision),
                content_type="application/octet-stream",
            )
    marker_key = identity.explicit_derived_keys[-1]
    client.remove_object(bucket_name, marker_key)
    replacement = b"phase10-after-marker"
    client.put_object(
        bucket_name,
        marker_key,
        BytesIO(replacement),
        len(replacement),
        content_type="application/json",
    )


def index_opensearch_fixture(
    client: Any,
    identity: Phase10TestDocument,
    index_name: str,
) -> None:
    body: list[dict[str, Any]] = []
    for index, chunk_id in enumerate(identity.chunk_ids):
        body.append({"index": {"_index": index_name, "_id": str(chunk_id)}})
        body.append(
            {
                "chunk_id": str(chunk_id),
                "document_id": str(identity.document_id),
                "original_filename": identity.filename,
                "chunk_index": index,
                "content": f"{identity.namespace} chunk {index}",
                "content_max": f"{identity.namespace} chunk {index}",
                "content_smart": f"{identity.namespace} chunk {index}",
                "source_metadata": {},
                "exact_terms": [],
                "embedding": [0.0] * 1024,
                "embedding_model": "Qwen3-Embedding-0.6B",
                "embedding_dim": 1024,
                "embedding_status": "embedded",
                "document_process_status": "parsed",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    response = client.bulk(body=body, refresh=True)
    if bool(response.get("errors")):
        raise AssertionError("Dedicated Phase 10 OpenSearch fixture indexing failed.")


def _knowledge_item(identity: Phase10TestDocument, *, label: str) -> KnowledgeItem:
    body = f"{identity.namespace} {label} knowledge"
    return KnowledgeItem(
        id=uuid4(),
        item_type="process_rule",
        title=f"Phase 10 {label}",
        content=body,
        content_hash=sha256(body.encode()).hexdigest(),
        status="approved",
        source_document_id=identity.document_id,
        source_filename=identity.filename,
        version=1,
    )


def _source(
    item_id: UUID,
    identity: Phase10TestDocument,
    *,
    created_at: datetime | None = None,
) -> KnowledgeItemSource:
    return KnowledgeItemSource(
        id=uuid4(),
        knowledge_item_id=item_id,
        document_id=identity.document_id,
        source_filename=identity.filename,
        created_at=created_at or datetime.now(UTC),
    )


def _knowledge_chunk(
    item_id: UUID,
    identity: Phase10TestDocument,
    chunk_id: UUID,
    chunk_index: int,
) -> KnowledgeItemChunk:
    return KnowledgeItemChunk(
        id=uuid4(),
        knowledge_item_id=item_id,
        chunk_id=chunk_id,
        document_id=identity.document_id,
        chunk_index=chunk_index,
        source_text=f"{identity.namespace} provenance",
    )


def _version(item: KnowledgeItem, identity: Phase10TestDocument) -> KnowledgeItemVersion:
    return KnowledgeItemVersion(
        id=uuid4(),
        knowledge_item_id=item.id,
        version=1,
        snapshot={
            "title": item.title,
            "content": item.content,
            "source_document_id": str(identity.document_id),
            "source_filename": identity.filename,
            "source_chunk_ids": [str(identity.chunk_ids[0])],
        },
        change_reason="phase10 integration fixture",
    )


def _review(item: KnowledgeItem) -> KnowledgeItemReview:
    return KnowledgeItemReview(
        id=uuid4(),
        knowledge_item_id=item.id,
        review_action="approve",
        from_status="draft",
        to_status="approved",
        reviewer="phase10-integration",
    )


def _setting(settings: Any, primary: str, secondary: str | None = None) -> Any:
    value = getattr(settings, primary, None)
    if value is None and secondary is not None:
        value = getattr(settings, secondary, None)
    if value is None:
        raise AssertionError(f"Phase 10 integration setting {primary} is missing.")
    return value
