from __future__ import annotations

from sqlalchemy import inspect, select

import pytest

from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_item_review import KnowledgeItemReview
from app.models.knowledge_item_source import KnowledgeItemSource
from app.models.knowledge_item_version import KnowledgeItemVersion
from app.services.document_deletion import (
    finalize_postgresql_deletion,
)
from integration.phase10_fixtures import (
    assert_postgresql_target_absent,
    build_finalization_claim,
    persist_document_fixture,
    persist_shared_knowledge_fixture,
    request_owned_validation_document_deletion,
)
from integration.phase10_support import (
    Phase10AlembicTarget,
    assert_unrelated_resources_unchanged,
    capture_postgresql_snapshot,
    current_alembic_revision,
    require_clean_migration_database,
    run_phase10_alembic,
)


pytestmark = pytest.mark.integration


def test_real_0006_expand_enforce_and_enforce_only_downgrade(
    phase10_integration_settings,
    phase10_migration_engine,
) -> None:
    require_clean_migration_database(phase10_migration_engine)
    migration_started = False
    try:
        run_phase10_alembic(
            phase10_integration_settings,
            Phase10AlembicTarget.MIGRATION,
            "upgrade",
            "0006_add_document_parse",
        )
        migration_started = True
        assert current_alembic_revision(phase10_migration_engine) == "0006_add_document_parse"

        run_phase10_alembic(
            phase10_integration_settings,
            Phase10AlembicTarget.MIGRATION,
            "upgrade",
            "0007_phase10_expand",
        )
        assert current_alembic_revision(phase10_migration_engine) == "0007_phase10_expand"
        assert "knowledge_item_sources" in inspect(phase10_migration_engine).get_table_names()

        run_phase10_alembic(
            phase10_integration_settings,
            Phase10AlembicTarget.MIGRATION,
            "upgrade",
            "0008_phase10_enforce",
        )
        assert current_alembic_revision(phase10_migration_engine) == "0008_phase10_enforce"
        foreign_keys = inspect(phase10_migration_engine).get_foreign_keys("knowledge_item_chunks")
        assert any(
            foreign_key["name"] == "fk_knowledge_item_chunks_item_document_source"
            for foreign_key in foreign_keys
        )

        run_phase10_alembic(
            phase10_integration_settings,
            Phase10AlembicTarget.MIGRATION,
            "downgrade",
            "0007_phase10_expand",
        )
        assert current_alembic_revision(phase10_migration_engine) == "0007_phase10_expand"
        foreign_keys = inspect(phase10_migration_engine).get_foreign_keys("knowledge_item_chunks")
        assert all(
            foreign_key["name"] != "fk_knowledge_item_chunks_item_document_source"
            for foreign_key in foreign_keys
        )
    finally:
        if migration_started:
            run_phase10_alembic(
                phase10_integration_settings,
                Phase10AlembicTarget.MIGRATION,
                "upgrade",
                "0008_phase10_enforce",
            )


@pytest.mark.phase10_knowledge_deferred(
    reason="Deferred by project owner pending Knowledge Item Library refactor."
)
def test_postgresql_finalization_removes_target_and_preserves_shared_and_unrelated_rows(
    phase10_validation_session_factory,
    phase10_validation_document_factory,
    phase10_validation_runtime_settings,
) -> None:
    target = phase10_validation_document_factory.create()
    control = phase10_validation_document_factory.create()
    with phase10_validation_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_validation_runtime_settings.minio_bucket,
            document_factory=phase10_validation_document_factory,
        )
        persist_document_fixture(
            session,
            control,
            bucket_name=phase10_validation_runtime_settings.minio_bucket,
            document_factory=phase10_validation_document_factory,
        )
        knowledge = persist_shared_knowledge_fixture(session, target=target, control=control)
        claimed, manifest = build_finalization_claim(
            session,
            target,
            document_factory=phase10_validation_document_factory,
            knowledge=knowledge,
            settings=phase10_validation_runtime_settings,
        )
        session.commit()
        before = capture_postgresql_snapshot(session)

        finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
        session.commit()

        assert_postgresql_target_absent(session, target, knowledge=knowledge)
        assert session.get(KnowledgeItem, knowledge.shared_item_id) is not None
        assert session.scalar(
            select(KnowledgeItemSource).where(
                KnowledgeItemSource.knowledge_item_id == knowledge.shared_item_id,
                KnowledgeItemSource.document_id == control.document_id,
            )
        ) is not None
        assert session.scalar(
            select(KnowledgeItemVersion).where(
                KnowledgeItemVersion.knowledge_item_id == knowledge.shared_item_id
            )
        ) is not None
        assert session.scalar(
            select(KnowledgeItemReview).where(
                KnowledgeItemReview.knowledge_item_id == knowledge.shared_item_id
            )
        ) is not None

        after = capture_postgresql_snapshot(session)
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
            target_knowledge_chunk_ids=set(
                knowledge.target_delete_chunk_relation_ids
            ),
            target_knowledge_version_ids=set(knowledge.target_delete_version_ids),
            target_knowledge_review_ids=set(knowledge.target_delete_review_ids),
            target_deletion_job_ids={claimed.job_id},
        )


def test_core_postgresql_finalization_removes_target_and_preserves_unrelated_rows(
    phase10_validation_session_factory,
    phase10_validation_document_factory,
    phase10_validation_runtime_settings,
) -> None:
    """Knowledge is intentionally excluded pending the separate library refactor."""

    target = phase10_validation_document_factory.create()
    control = phase10_validation_document_factory.create()
    with phase10_validation_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_validation_runtime_settings.minio_bucket,
            document_factory=phase10_validation_document_factory,
        )
        persist_document_fixture(
            session,
            control,
            bucket_name=phase10_validation_runtime_settings.minio_bucket,
            document_factory=phase10_validation_document_factory,
        )
        claimed, manifest = build_finalization_claim(
            session,
            target,
            document_factory=phase10_validation_document_factory,
            knowledge=None,
            settings=phase10_validation_runtime_settings,
        )
        session.commit()
        before = capture_postgresql_snapshot(session)

        finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
        session.commit()

        assert_postgresql_target_absent(session, target, knowledge=None)
        after = capture_postgresql_snapshot(session)
        assert_unrelated_resources_unchanged(
            before,
            after,
            target_document_ids={target.document_id},
            target_chunk_ids=set(target.chunk_ids),
            target_parse_run_ids={target.parse_run_id},
            target_asset_ids=set(target.asset_ids),
            target_block_ids=set(target.block_ids),
            target_chunk_block_ids=set(target.chunk_block_ids),
            target_deletion_job_ids={claimed.job_id},
        )


def test_finalization_rollback_then_retry_and_ambiguous_response_duplicate_delete_is_204_semantics(
    phase10_validation_session_factory,
    phase10_validation_document_factory,
    phase10_validation_runtime_settings,
) -> None:
    target = phase10_validation_document_factory.create()
    with phase10_validation_session_factory() as session:
        persist_document_fixture(
            session,
            target,
            bucket_name=phase10_validation_runtime_settings.minio_bucket,
            document_factory=phase10_validation_document_factory,
        )
        claimed, manifest = build_finalization_claim(
            session,
            target,
            document_factory=phase10_validation_document_factory,
            knowledge=None,
            settings=phase10_validation_runtime_settings,
        )
        session.commit()

        finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
        session.rollback()  # injected failure before the final COMMIT

        assert session.get(Document, target.document_id) is not None
        assert session.get(DocumentDeletionJob, claimed.job_id) is not None

        finalize_postgresql_deletion(session, claimed=claimed, manifest=manifest)
        session.commit()
        assert_postgresql_target_absent(session, target, knowledge=None)

        assert (
            request_owned_validation_document_deletion(
                session,
                target,
                document_factory=phase10_validation_document_factory,
                settings=phase10_validation_runtime_settings,
            )
            is None
        )
