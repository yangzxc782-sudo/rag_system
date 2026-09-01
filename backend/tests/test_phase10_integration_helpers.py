from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from minio.deleteobjects import DeleteObject

from integration import phase10_fixtures, phase10_support
from integration.phase10_run_context import (
    Phase10IntegrationRunContext,
    Phase10ResourceDomain,
)
from app.models.document import Document
from app.models.document_deletion_job import DocumentDeletionJob
from app.models.knowledge_item import KnowledgeItem
from integration.phase10_fixtures import (
    Phase10KnowledgeFixture,
    assert_postgresql_target_absent,
    build_storage_manifest,
    index_opensearch_fixture,
    persist_document_fixture,
)
from integration.phase10_support import (
    Phase10IntegrationGateError,
    Phase10TestDocumentFactory,
)
from integration.test_document_deletion_storage import SlowFakeMinio


class _Rows:
    def __init__(self, values: list[object] | None = None) -> None:
        self._values = values or []

    def all(self) -> list[object]:
        return list(self._values)


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.get_calls: list[tuple[type[object], UUID]] = []
        self.get_results: dict[tuple[type[object], UUID], object] = {}

    def get(self, model: type[object], identifier: UUID) -> object | None:
        self.get_calls.append((model, identifier))
        return self.get_results.get((model, identifier))

    def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement))
        return None

    def scalars(self, statement: object) -> _Rows:
        self.statements.append(str(statement))
        return _Rows()


class _BulkClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.body: list[dict[str, object]] | None = None

    def bulk(self, *, body: list[dict[str, object]], refresh: bool) -> dict[str, object]:
        assert refresh is True
        self.body = body
        return self.response


class _RollbackSession:
    def __init__(self) -> None:
        self.rollback_calls = 0

    def rollback(self) -> None:
        self.rollback_calls += 1


class _ForbiddenSession:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"session must not be touched before ownership validation: {name}")


class _PreprovisionedLockedBucketClient:
    def __init__(self) -> None:
        self.bucket_exists_calls: list[str] = []
        self.versioning_calls: list[str] = []
        self.object_lock_calls: list[str] = []

    def bucket_exists(self, bucket_name: str) -> bool:
        self.bucket_exists_calls.append(bucket_name)
        return True

    def get_bucket_versioning(self, bucket_name: str) -> object:
        self.versioning_calls.append(bucket_name)
        return SimpleNamespace(status="Enabled")

    def get_object_lock_config(self, bucket_name: str) -> object:
        self.object_lock_calls.append(bucket_name)
        return SimpleNamespace(mode=None, duration=None, duration_unit=None)

    def make_bucket(self, *_args, **_kwargs) -> None:
        raise AssertionError("retention consumer must not create the locked bucket")

    def remove_bucket(self, *_args, **_kwargs) -> None:
        raise AssertionError("retention consumer must not remove the locked bucket")


def _document_factory(
    run_token: str = "abcdef12",
    *,
    resource_domain: Phase10ResourceDomain = Phase10ResourceDomain.VALIDATION,
) -> Phase10TestDocumentFactory:
    return Phase10TestDocumentFactory(
        run_context=Phase10IntegrationRunContext(run_token=run_token),
        resource_domain=resource_domain,
    )


def _knowledge_expectation(target_document_id: UUID) -> Phase10KnowledgeFixture:
    shared_item_id = uuid4()
    orphan_item_id = uuid4()
    shared_source_id = uuid4()
    orphan_source_id = uuid4()
    shared_chunk_id = uuid4()
    orphan_chunk_id = uuid4()
    shared_version_id = uuid4()
    orphan_version_id = uuid4()
    shared_review_id = uuid4()
    orphan_review_id = uuid4()
    return Phase10KnowledgeFixture(
        target_document_id=target_document_id,
        shared_item_id=shared_item_id,
        target_only_item_id=orphan_item_id,
        all_item_ids=(shared_item_id, orphan_item_id),
        target_shared_source_id=shared_source_id,
        target_only_source_id=orphan_source_id,
        source_ids_by_document=(
            (target_document_id, (shared_source_id, orphan_source_id)),
        ),
        chunk_relation_ids=(shared_chunk_id, orphan_chunk_id),
        chunk_relation_ids_by_document=(
            (target_document_id, (shared_chunk_id, orphan_chunk_id)),
        ),
        version_ids=(shared_version_id, orphan_version_id),
        version_ids_by_item=(
            (shared_item_id, shared_version_id),
            (orphan_item_id, orphan_version_id),
        ),
        review_ids=(shared_review_id, orphan_review_id),
        review_ids_by_item=(
            (shared_item_id, shared_review_id),
            (orphan_item_id, orphan_review_id),
        ),
        control_document_id=uuid4(),
    )


def test_core_absence_helper_skips_all_knowledge_specific_assertions() -> None:
    identity = _document_factory().create()
    session = _RecordingSession()

    assert_postgresql_target_absent(session, identity, knowledge=None)  # type: ignore[arg-type]

    statements = "\n".join(session.statements)
    assert "document_chunk_blocks" in statements
    assert "knowledge_item_sources" not in statements
    assert "knowledge_item_chunks" not in statements
    assert all(model is not KnowledgeItem for model, _identifier in session.get_calls)


def test_knowledge_aware_absence_helper_preserves_existing_assertions() -> None:
    identity = _document_factory().create()
    knowledge = _knowledge_expectation(identity.document_id)
    session = _RecordingSession()

    assert_postgresql_target_absent(session, identity, knowledge=knowledge)  # type: ignore[arg-type]

    statements = "\n".join(session.statements)
    assert "knowledge_item_sources" in statements
    assert "knowledge_item_chunks" in statements
    assert (KnowledgeItem, knowledge.target_only_item_id) in session.get_calls


def test_core_absence_helper_still_fails_when_document_remains() -> None:
    identity = _document_factory().create()
    session = _RecordingSession()
    session.get_results[(Document, identity.document_id)] = object()

    with pytest.raises(AssertionError):
        assert_postgresql_target_absent(session, identity, knowledge=None)  # type: ignore[arg-type]


def test_opensearch_fixture_uses_deterministic_non_zero_1024_dimension_vectors() -> None:
    factory = _document_factory()
    identity = factory.create()
    client = _BulkClient({"errors": False})

    index_opensearch_fixture(
        client,
        identity,
        factory.run_context.opensearch_index_for(Phase10ResourceDomain.VALIDATION),
        document_factory=factory,
    )

    assert client.body is not None
    sources = client.body[1::2]
    assert len(sources) == len(identity.chunk_ids)
    for source in sources:
        vector = source["embedding"]
        assert isinstance(vector, list)
        assert len(vector) == 1024
        assert any(value != 0.0 for value in vector)
        assert sum(value * value for value in vector) == 1.0


def test_opensearch_fixture_bulk_failure_reports_safe_item_detail() -> None:
    factory = _document_factory()
    identity = factory.create()
    client = _BulkClient(
        {
            "errors": True,
            "items": [
                {
                    "index": {
                        "_id": str(identity.chunk_ids[0]),
                        "status": 400,
                        "error": {
                            "type": "mapper_parsing_exception",
                            "reason": "controlled vector rejection",
                        },
                    }
                }
            ],
        }
    )

    with pytest.raises(AssertionError) as captured:
        index_opensearch_fixture(
            client,
            identity,
            factory.run_context.opensearch_index_for(Phase10ResourceDomain.VALIDATION),
            document_factory=factory,
        )

    message = str(captured.value)
    assert "index status=400" in message
    assert "type=mapper_parsing_exception" in message
    assert "reason=controlled vector rejection" in message
    assert str(identity.chunk_ids[0]) in message
    assert "embedding" not in message
    assert "_source" not in message


def test_contender_drain_skips_target_drains_unrelated_and_rolls_back() -> None:
    target_job_id = uuid4()
    other_job_ids = (uuid4(), uuid4())
    claims = iter(
        [
            SimpleNamespace(job_id=other_job_ids[0]),
            SimpleNamespace(job_id=other_job_ids[1]),
            None,
        ]
    )
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    claimed_ids = drain(
        session,
        target_job_id=target_job_id,
        max_claims=4,
        claim_function=lambda *_args, **_kwargs: next(claims),
    )

    assert claimed_ids == other_job_ids
    assert target_job_id not in claimed_ids
    assert session.rollback_calls == 1


def test_contender_drain_fails_and_rolls_back_if_target_is_returned() -> None:
    target_job_id = uuid4()
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    with pytest.raises(AssertionError, match="heartbeat target"):
        drain(
            session,
            target_job_id=target_job_id,
            max_claims=2,
            claim_function=lambda *_args, **_kwargs: SimpleNamespace(
                job_id=target_job_id
            ),
        )

    assert session.rollback_calls == 1


def test_contender_drain_has_a_finite_safety_ceiling_and_rolls_back() -> None:
    session = _RollbackSession()
    drain = getattr(
        phase10_fixtures,
        "drain_claimable_jobs_excluding_target",
        None,
    )
    assert callable(drain)

    with pytest.raises(AssertionError, match="safety limit"):
        drain(
            session,
            target_job_id=uuid4(),
            max_claims=2,
            claim_function=lambda *_args, **_kwargs: SimpleNamespace(job_id=uuid4()),
        )

    assert session.rollback_calls == 1


def test_slow_fake_minio_consumes_real_delete_object_name_and_version_id() -> None:
    object_name = "parsed-assets/11111111-1111-1111-1111-111111111111/output.md"
    client = SlowFakeMinio([object_name], delay_seconds=0)

    errors = list(
        client.remove_objects(
            "phase10-test-bucket",
            [DeleteObject(object_name, version_id="v1")],
        )
    )

    assert errors == []
    assert client.objects == {}


def test_locked_bucket_capability_gate_consumes_preprovisioned_bucket_without_lifecycle_calls() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    client = _PreprovisionedLockedBucketClient()
    capability_gate = getattr(
        phase10_support,
        "require_phase10_validation_locked_bucket_ready",
        None,
    )
    assert callable(capability_gate)

    bucket_name = capability_gate(client, run_context=context)

    assert bucket_name == context.validation_minio_locked_bucket
    assert client.bucket_exists_calls == [bucket_name]
    assert client.versioning_calls == [bucket_name]
    assert client.object_lock_calls == [bucket_name]


def test_missing_bucket_gate_requires_provisioning_to_leave_bucket_absent() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    client = SimpleNamespace(bucket_exists=lambda _bucket_name: False)

    bucket_name = phase10_support.require_phase10_validation_missing_bucket_absent(
        client,
        run_context=context,
    )

    assert bucket_name == context.validation_minio_missing_bucket


def test_locked_bucket_capability_gate_fails_when_object_lock_cannot_be_read() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    client = _PreprovisionedLockedBucketClient()

    def unavailable(_bucket_name: str) -> object:
        raise RuntimeError("object lock is not enabled")

    client.get_object_lock_config = unavailable  # type: ignore[method-assign]

    with pytest.raises(Phase10IntegrationGateError, match="object-lock"):
        phase10_support.require_phase10_validation_locked_bucket_ready(
            client,
            run_context=context,
        )


def test_postgresql_fixture_rejects_cross_context_identity_before_session_use() -> None:
    identity = _document_factory("aaaabbbb").create()
    wrong_factory = _document_factory("ccccdddd")

    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        persist_document_fixture(
            _ForbiddenSession(),  # type: ignore[arg-type]
            identity,
            bucket_name="phase10-m7b-ccccdddd",
            document_factory=wrong_factory,
        )


def test_storage_manifest_rejects_cross_context_identity() -> None:
    identity = _document_factory("aaaabbbb").create()
    wrong_factory = _document_factory("ccccdddd")
    settings = SimpleNamespace(
        minio_bucket="phase10-m7b-ccccdddd",
        search_index_name="phase10-m7b-ccccdddd-v1",
        search_index_alias="phase10-m7b-ccccdddd-current",
    )

    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        build_storage_manifest(
            identity,
            settings,
            document_factory=wrong_factory,
        )


def test_owned_deletion_wrapper_rejects_external_id_before_service_call() -> None:
    identity = _document_factory(
        "aaaabbbb",
        resource_domain=Phase10ResourceDomain.ROLLOUT,
    ).create()
    wrong_factory = _document_factory(
        "ccccdddd",
        resource_domain=Phase10ResourceDomain.ROLLOUT,
    )
    request_owned = getattr(
        phase10_fixtures,
        "request_owned_document_deletion",
        None,
    )

    assert callable(request_owned)
    with pytest.raises(Phase10IntegrationGateError, match="not created"):
        request_owned(
            _ForbiddenSession(),
            identity,
            document_factory=wrong_factory,
            settings=object(),
        )


def test_rollout_deletion_wrapper_rejects_validation_factory_before_service_call() -> None:
    factory = _document_factory(resource_domain=Phase10ResourceDomain.VALIDATION)
    identity = factory.create()
    request_owned = getattr(
        phase10_fixtures,
        "request_owned_document_deletion",
        None,
    )

    assert callable(request_owned)
    with pytest.raises(Phase10IntegrationGateError, match="resource domain"):
        request_owned(
            _ForbiddenSession(),
            identity,
            document_factory=factory,
            settings=object(),
        )


def test_storage_manifest_rejects_rollout_factory_before_storage_use() -> None:
    context = Phase10IntegrationRunContext(run_token="abcdef12")
    factory = _document_factory(resource_domain=Phase10ResourceDomain.ROLLOUT)
    identity = factory.create()
    settings = SimpleNamespace(
        minio_bucket=context.minio_bucket_for(Phase10ResourceDomain.ROLLOUT),
        opensearch_index=context.opensearch_index_for(Phase10ResourceDomain.ROLLOUT),
        opensearch_alias=context.opensearch_alias_for(Phase10ResourceDomain.ROLLOUT),
    )

    with pytest.raises(Phase10IntegrationGateError, match="resource domain"):
        build_storage_manifest(
            identity,
            settings,
            document_factory=factory,
        )


def test_retry_wait_assertion_reads_only_the_test_owned_job_and_document() -> None:
    target_job_id = uuid4()
    target_document_id = uuid4()
    unrelated_job_ids = (uuid4(), uuid4(), uuid4())
    session = _RecordingSession()
    session.get_results[(DocumentDeletionJob, target_job_id)] = SimpleNamespace(
        status="retry_wait",
        current_step="delete_opensearch",
        step_attempts=1,
        next_retry_at=object(),
        last_error_code="DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE",
    )
    session.get_results[(Document, target_document_id)] = SimpleNamespace(
        deletion_status="deleting"
    )
    for unrelated_job_id in unrelated_job_ids:
        session.get_results[(DocumentDeletionJob, unrelated_job_id)] = SimpleNamespace(
            status="pending"
        )

    assertion = getattr(phase10_fixtures, "assert_owned_job_retry_wait", None)
    assert callable(assertion)
    assertion(
        session,
        job_id=target_job_id,
        document_id=target_document_id,
        expected_error_code="DOCUMENT_DELETION_OPENSEARCH_UNAVAILABLE",
    )

    assert session.get_calls == [
        (DocumentDeletionJob, target_job_id),
        (Document, target_document_id),
    ]
