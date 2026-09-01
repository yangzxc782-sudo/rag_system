from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob
from app.services.hybrid_search import hybrid_search_chunks
from app.services.rag import answer_question
from app.tasks.document_deletion_executor import DocumentDeletionExecutor
from integration.phase10_fixtures import (
    index_opensearch_fixture,
    persist_document_fixture,
    persist_shared_knowledge_fixture,
    request_owned_document_deletion,
    upload_minio_fixture_versions,
)
from integration.phase10_support import (
    Phase10ResourceSnapshot,
    assert_unrelated_resources_unchanged,
    capture_minio_snapshot,
    capture_opensearch_snapshot,
    capture_postgresql_snapshot,
    merge_resource_snapshots,
)


pytestmark = pytest.mark.integration


class FixedEmbeddingProvider:
    def encode_query(self, query: str):
        del query
        return SimpleNamespace(
            embeddings=[[0.0] * 1024],
            embedding_dim=1024,
            embedding_model="Qwen3-Embedding-0.6B",
            device="cpu",
        )


@pytest.mark.phase10_knowledge_deferred(
    reason="Deferred by project owner pending Knowledge Item Library refactor."
)
def test_full_saga_converges_to_zero_residual_and_preserves_unrelated_snapshot(
    monkeypatch,
    phase10_rollout_pristine,
    phase10_rollout_settings,
    phase10_rollout_session_factory,
    phase10_rollout_minio_client,
    phase10_rollout_opensearch_client,
    phase10_rollout_document_factory,
    phase10_rollout_runtime_settings,
) -> None:
    target = phase10_rollout_document_factory.create()
    control = phase10_rollout_document_factory.create()
    with phase10_rollout_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_rollout_settings.minio_bucket,
            document_factory=phase10_rollout_document_factory,
        )
        persist_document_fixture(
            session,
            control,
            bucket_name=phase10_rollout_settings.minio_bucket,
            document_factory=phase10_rollout_document_factory,
        )
        knowledge = persist_shared_knowledge_fixture(session, target=target, control=control)
        session.commit()
        before_pg = capture_postgresql_snapshot(session)

    upload_minio_fixture_versions(
        phase10_rollout_minio_client,
        target,
        phase10_rollout_settings.minio_bucket,
        document_factory=phase10_rollout_document_factory,
    )
    upload_minio_fixture_versions(
        phase10_rollout_minio_client,
        control,
        phase10_rollout_settings.minio_bucket,
        document_factory=phase10_rollout_document_factory,
    )
    index_opensearch_fixture(
        phase10_rollout_opensearch_client,
        target,
        phase10_rollout_settings.opensearch_index,
        document_factory=phase10_rollout_document_factory,
    )
    index_opensearch_fixture(
        phase10_rollout_opensearch_client,
        control,
        phase10_rollout_settings.opensearch_index,
        document_factory=phase10_rollout_document_factory,
    )
    before_minio = capture_minio_snapshot(phase10_rollout_minio_client, phase10_rollout_settings.minio_bucket)
    before_search = capture_opensearch_snapshot(
        phase10_rollout_opensearch_client,
        index_name=phase10_rollout_settings.opensearch_index,
        alias_name=phase10_rollout_settings.opensearch_alias,
    )

    with phase10_rollout_session_factory() as session:
        scheduled = request_owned_document_deletion(
            session,
            target,
            document_factory=phase10_rollout_document_factory,
            settings=phase10_rollout_runtime_settings,
        )
    assert scheduled is not None and scheduled.status == "deleting"

    executor = DocumentDeletionExecutor(
        session_factory=phase10_rollout_session_factory,
        settings=phase10_rollout_runtime_settings,
    )
    for _ in range(20):
        executor.run_once()
        with phase10_rollout_session_factory() as session:
            job = session.scalar(
                select(DocumentDeletionJob.id).where(
                    DocumentDeletionJob.document_id == target.document_id
                )
            )
            if job is None and session.get(Document, target.document_id) is None:
                break
    else:
        pytest.fail("Phase 10 executor did not converge within the bounded test drain.")

    with phase10_rollout_session_factory() as session:
        after_pg = capture_postgresql_snapshot(session)
        assert session.get(Document, target.document_id) is None
        assert (
            request_owned_document_deletion(
                session,
                target,
                document_factory=phase10_rollout_document_factory,
                settings=phase10_rollout_runtime_settings,
            )
            is None
        )
        assert not session.execute(
            Document.__table__.select().where(Document.id == target.document_id)
        ).all()

    after_minio = capture_minio_snapshot(phase10_rollout_minio_client, phase10_rollout_settings.minio_bucket)
    after_search = capture_opensearch_snapshot(
        phase10_rollout_opensearch_client,
        index_name=phase10_rollout_settings.opensearch_index,
        alias_name=phase10_rollout_settings.opensearch_alias,
    )
    assert phase10_rollout_opensearch_client.count(
        index=phase10_rollout_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(target.document_id)}}},
    )["count"] == 0
    for chunk_id in target.chunk_ids:
        assert phase10_rollout_opensearch_client.count(
            index=phase10_rollout_settings.opensearch_index,
            body={
                "query": {
                    "bool": {
                        "should": [
                            {"ids": {"values": [str(chunk_id)]}},
                            {"term": {"chunk_id": str(chunk_id)}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            },
        )["count"] == 0
    before = merge_resource_snapshots(before_pg, before_minio, before_search)
    after = merge_resource_snapshots(after_pg, after_minio, after_search)
    assert_unrelated_resources_unchanged(
        before,
        after,
        target_document_ids={target.document_id},
        target_chunk_ids=set(target.chunk_ids),
        target_parse_run_ids={target.parse_run_id},
        target_asset_ids=set(target.asset_ids),
        target_block_ids=set(target.block_ids),
        target_chunk_block_ids=set(target.chunk_block_ids),
        target_knowledge_item_ids={knowledge.target_only_item_id},
        target_knowledge_source_ids={
            knowledge.target_shared_source_id,
            knowledge.target_only_source_id,
        },
        target_knowledge_chunk_ids=set(knowledge.target_delete_chunk_relation_ids),
        target_knowledge_version_ids=set(knowledge.target_delete_version_ids),
        target_knowledge_review_ids=set(knowledge.target_delete_review_ids),
        target_minio_keys=set(target.all_minio_keys),
        target_opensearch_ids={str(chunk_id) for chunk_id in target.chunk_ids},
    )

    search_settings = phase10_rollout_runtime_settings.model_copy(
        update={
            "search_index_name": phase10_rollout_settings.opensearch_index,
            "search_index_alias": phase10_rollout_settings.opensearch_alias,
        }
    )
    with phase10_rollout_session_factory() as session:
        retrieval = hybrid_search_chunks(
            session,
            query=target.namespace,
            limit=8,
            client=phase10_rollout_opensearch_client,
            settings=search_settings,
            embedding_provider=FixedEmbeddingProvider(),
            deletion_filter_session_factory=phase10_rollout_session_factory,
        )
    assert all(item.document_id != str(target.document_id) for item in retrieval.items)

    monkeypatch.setattr("app.services.rag.retrieve_chunks", lambda *args, **kwargs: retrieval)
    with phase10_rollout_session_factory() as session:
        rag_result = answer_question(
            session,
            target.namespace,
            settings=search_settings,
        )
    assert all(str(citation.document_id) != str(target.document_id) for citation in rag_result.citations)


def test_core_full_saga_converges_to_zero_residual_and_preserves_unrelated_snapshot(
    monkeypatch,
    phase10_rollout_pristine,
    phase10_rollout_settings,
    phase10_rollout_session_factory,
    phase10_rollout_minio_client,
    phase10_rollout_opensearch_client,
    phase10_rollout_document_factory,
    phase10_rollout_runtime_settings,
) -> None:
    """Knowledge is intentionally excluded pending the separate library refactor."""

    target = phase10_rollout_document_factory.create()
    control = phase10_rollout_document_factory.create()
    with phase10_rollout_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_rollout_settings.minio_bucket,
            document_factory=phase10_rollout_document_factory,
        )
        persist_document_fixture(
            session,
            control,
            bucket_name=phase10_rollout_settings.minio_bucket,
            document_factory=phase10_rollout_document_factory,
        )
        session.commit()
        before_pg = capture_postgresql_snapshot(session)

    upload_minio_fixture_versions(
        phase10_rollout_minio_client,
        target,
        phase10_rollout_settings.minio_bucket,
        document_factory=phase10_rollout_document_factory,
    )
    upload_minio_fixture_versions(
        phase10_rollout_minio_client,
        control,
        phase10_rollout_settings.minio_bucket,
        document_factory=phase10_rollout_document_factory,
    )
    index_opensearch_fixture(
        phase10_rollout_opensearch_client,
        target,
        phase10_rollout_settings.opensearch_index,
        document_factory=phase10_rollout_document_factory,
    )
    index_opensearch_fixture(
        phase10_rollout_opensearch_client,
        control,
        phase10_rollout_settings.opensearch_index,
        document_factory=phase10_rollout_document_factory,
    )
    before_minio = capture_minio_snapshot(phase10_rollout_minio_client, phase10_rollout_settings.minio_bucket)
    before_search = capture_opensearch_snapshot(
        phase10_rollout_opensearch_client,
        index_name=phase10_rollout_settings.opensearch_index,
        alias_name=phase10_rollout_settings.opensearch_alias,
    )

    with phase10_rollout_session_factory() as session:
        scheduled = request_owned_document_deletion(
            session,
            target,
            document_factory=phase10_rollout_document_factory,
            settings=phase10_rollout_runtime_settings,
        )
        target_job_id = session.scalar(
            select(DocumentDeletionJob.id).where(
                DocumentDeletionJob.document_id == target.document_id
            )
        )
    assert scheduled is not None and scheduled.status == "deleting"
    assert target_job_id is not None

    executor = DocumentDeletionExecutor(
        session_factory=phase10_rollout_session_factory,
        settings=phase10_rollout_runtime_settings,
    )
    for _ in range(20):
        executor.run_once()
        with phase10_rollout_session_factory() as session:
            job = session.scalar(
                select(DocumentDeletionJob.id).where(
                    DocumentDeletionJob.document_id == target.document_id
                )
            )
            if job is None and session.get(Document, target.document_id) is None:
                break
    else:
        pytest.fail("Phase 10 core executor did not converge within the bounded test drain.")

    with phase10_rollout_session_factory() as session:
        after_pg = capture_postgresql_snapshot(session)
        assert session.get(Document, target.document_id) is None
        assert (
            request_owned_document_deletion(
                session,
                target,
                document_factory=phase10_rollout_document_factory,
                settings=phase10_rollout_runtime_settings,
            )
            is None
        )

    after_minio = capture_minio_snapshot(phase10_rollout_minio_client, phase10_rollout_settings.minio_bucket)
    after_search = capture_opensearch_snapshot(
        phase10_rollout_opensearch_client,
        index_name=phase10_rollout_settings.opensearch_index,
        alias_name=phase10_rollout_settings.opensearch_alias,
    )
    assert phase10_rollout_opensearch_client.count(
        index=phase10_rollout_settings.opensearch_index,
        body={"query": {"term": {"document_id": str(target.document_id)}}},
    )["count"] == 0
    for chunk_id in target.chunk_ids:
        assert phase10_rollout_opensearch_client.count(
            index=phase10_rollout_settings.opensearch_index,
            body={
                "query": {
                    "bool": {
                        "should": [
                            {"ids": {"values": [str(chunk_id)]}},
                            {"term": {"chunk_id": str(chunk_id)}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            },
        )["count"] == 0
    before = merge_resource_snapshots(before_pg, before_minio, before_search)
    after = merge_resource_snapshots(after_pg, after_minio, after_search)
    assert_unrelated_resources_unchanged(
        before,
        after,
        target_document_ids={target.document_id},
        target_chunk_ids=set(target.chunk_ids),
        target_parse_run_ids={target.parse_run_id},
        target_asset_ids=set(target.asset_ids),
        target_block_ids=set(target.block_ids),
        target_chunk_block_ids=set(target.chunk_block_ids),
        target_deletion_job_ids={target_job_id},
        target_minio_keys=set(target.all_minio_keys),
        target_opensearch_ids={str(chunk_id) for chunk_id in target.chunk_ids},
    )

    search_settings = phase10_rollout_runtime_settings.model_copy(
        update={
            "search_index_name": phase10_rollout_settings.opensearch_index,
            "search_index_alias": phase10_rollout_settings.opensearch_alias,
        }
    )
    with phase10_rollout_session_factory() as session:
        retrieval = hybrid_search_chunks(
            session,
            query=target.namespace,
            limit=8,
            client=phase10_rollout_opensearch_client,
            settings=search_settings,
            embedding_provider=FixedEmbeddingProvider(),
            deletion_filter_session_factory=phase10_rollout_session_factory,
        )
    assert all(item.document_id != str(target.document_id) for item in retrieval.items)

    monkeypatch.setattr("app.services.rag.retrieve_chunks", lambda *args, **kwargs: retrieval)
    with phase10_rollout_session_factory() as session:
        rag_result = answer_question(
            session,
            target.namespace,
            settings=search_settings,
        )
    assert all(str(citation.document_id) != str(target.document_id) for citation in rag_result.citations)
