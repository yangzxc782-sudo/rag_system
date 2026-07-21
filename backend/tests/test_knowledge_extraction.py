from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.core.errors import (
    KNOWLEDGE_ITEM_CONFIG_INVALID,
    KNOWLEDGE_ITEM_DUPLICATE,
    KNOWLEDGE_ITEM_EXTRACTION_FAILED,
    KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED,
    KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND,
    KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND,
    LLM_PARAMETER_UNSUPPORTED,
    BusinessError,
)
from app.extraction.knowledge_parser import parse_extraction_json, strip_json_code_fence
from app.extraction.knowledge_prompt import build_knowledge_extraction_prompt
from app.llm.messages import LLMMessage, LLMTextContentPart
from app.llm.provider import LLMGenerateResult, build_llm_provider
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_item import KnowledgeItem
from app.schemas.knowledge_item import KnowledgeExtractionRequest
from app.services import knowledge_extraction


DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
OTHER_DOCUMENT_ID = UUID("550e8400-e29b-41d4-a716-446655440999")
CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440001")
SECOND_CHUNK_ID = UUID("660e8400-e29b-41d4-a716-446655440002")


class FakeScalarResult:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def first(self) -> object | None:
        return self.items[0] if self.items else None

    def all(self) -> list[object]:
        return self.items


class FakeDb:
    def __init__(
        self,
        *,
        documents: dict[UUID, object] | None = None,
        chunks: dict[UUID, object] | None = None,
        duplicate_items: list[KnowledgeItem] | None = None,
    ) -> None:
        self.documents = documents or {}
        self.chunks = chunks or {}
        self.duplicate_items = duplicate_items or []
        self.items: dict[UUID, KnowledgeItem] = {}
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushed = 0

    def get(self, model: object, item_id: UUID) -> object | None:
        if model is Document:
            return self.documents.get(item_id)
        if model is DocumentChunk:
            return self.chunks.get(item_id)
        if model is KnowledgeItem:
            return self.items.get(item_id)
        return None

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flushed += 1
        for item in self.added:
            if getattr(item, "id", None) is None:
                item.id = uuid4()
            if isinstance(item, KnowledgeItem):
                self.items[item.id] = item

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, _item: object) -> None:
        return None

    def scalars(self, statement: object) -> FakeScalarResult:
        statement_text = str(statement)
        if "knowledge_items" in statement_text:
            return FakeScalarResult(self.duplicate_items)
        if "document_chunks" in statement_text:
            return FakeScalarResult(list(self.chunks.values()))
        return FakeScalarResult([])


class FakeLLMProvider:
    provider_name = "fake"

    def __init__(self, text: str, exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.calls: list[object] = []

    def generate(self, request: object) -> LLMGenerateResult:
        self.calls.append(request)
        if self.exc is not None:
            raise self.exc
        return LLMGenerateResult(
            message=LLMMessage(
                role="assistant",
                content=(LLMTextContentPart(text=self.text),),
            ),
            provider="fake",
            model="fake-model",
        )


class FakeSDKCompletions:
    def __init__(self, content: str = '{"items":[]}') -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="chatcmpl-extraction-business",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=self.content,
                        tool_calls=None,
                    )
                )
            ],
            usage=None,
        )


class FakeSDKClient:
    def __init__(self, content: str = '{"items":[]}') -> None:
        self.completions = FakeSDKCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def make_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "knowledge_extraction_max_chunks": 20,
        "knowledge_extraction_max_chars": 12000,
        "knowledge_extraction_default_status": "draft",
        "llm_provider": "openai_compatible",
        "llm_base_url": "http://localhost:11434/v1",
        "llm_model": "qwen3:8b",
        "llm_api_key": "",
        "llm_temperature": 0.2,
        "llm_max_tokens": 2048,
        "llm_timeout_seconds": 120,
        "llm_remote_base_url": "https://remote.example.invalid/v1",
        "llm_remote_api_key": "test-key-not-real",
        "llm_remote_model": "remote-model",
        "llm_remote_timeout_seconds": 60,
        "llm_remote_supports_json_mode": False,
        "llm_remote_allow_insecure_http": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_document(document_id: UUID = DOCUMENT_ID) -> SimpleNamespace:
    return SimpleNamespace(id=document_id, original_filename="casting.md")


def make_chunk(
    chunk_id: UUID = CHUNK_ID,
    document_id: UUID = DOCUMENT_ID,
    chunk_index: int = 1,
    content: str = "Risers should feed hot spots through a clear feeding path.",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=chunk_id,
        document_id=document_id,
        chunk_index=chunk_index,
        content=content,
    )


def extraction_json(*, item_type: str = "process_rule", chunk_id: UUID = CHUNK_ID) -> str:
    return (
        '{"items":[{'
        f'"item_type":"{item_type}",'
        '"title":"Riser feeding rule",'
        '"content":"Place risers near hot spots to support feeding.",'
        '"confidence":0.82,'
        f'"source_chunk_ids":["{chunk_id}"]'
        "}]} "
    )


def test_prompt_requires_json_and_source_chunk_ids() -> None:
    system_prompt, user_prompt = build_knowledge_extraction_prompt(
        [make_chunk()],
        item_types=["process_rule", "defect_solution"],
    )

    assert "JSON" in system_prompt
    assert "source_chunk_ids" in system_prompt
    assert "process_rule" in system_prompt
    assert "defect_solution" in system_prompt
    assert str(CHUNK_ID) in user_prompt


def test_strip_json_code_fence_supports_json_blocks() -> None:
    assert strip_json_code_fence('```json\n{"items":[]}\n```') == '{"items":[]}'


def test_parse_extraction_json_success() -> None:
    items = parse_extraction_json(extraction_json(), allowed_chunk_ids={CHUNK_ID})

    assert len(items) == 1
    assert items[0].item_type == "process_rule"
    assert items[0].source_chunk_ids == [CHUNK_ID]


@pytest.mark.parametrize(
    "raw_text",
    [
        "[]",
        "{}",
        '{"items":{}}',
        (
            '{"items":[{"item_type":"bad","title":"T","content":"C",'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
        (
            '{"items":[{"item_type":"process_rule","title":"","content":"C",'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
        (
            '{"items":[{"item_type":"process_rule","title":"T","content":"",'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
        (
            '{"items":[{"item_type":"process_rule","title":"T","content":"C","confidence":2,'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
        '{"items":[{"item_type":"process_rule","title":"T","content":"C","source_chunk_ids":[]}]}',
        (
            '{"items":[{"item_type":"process_rule","title":"T","content":"C","status":"approved",'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
        (
            '{"items":[{"item_type":"process_rule","title":"T","content":"C","approved":true,'
            '"source_chunk_ids":["660e8400-e29b-41d4-a716-446655440001"]}]}'
        ),
    ],
)
def test_parse_extraction_json_rejects_invalid_output(raw_text: str) -> None:
    with pytest.raises(BusinessError) as exc_info:
        parse_extraction_json(raw_text, allowed_chunk_ids={CHUNK_ID})

    assert exc_info.value.code == KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED


def test_parse_extraction_json_rejects_unknown_chunk_id_and_disallowed_type() -> None:
    with pytest.raises(BusinessError) as unknown_chunk_exc:
        parse_extraction_json(extraction_json(chunk_id=uuid4()), allowed_chunk_ids={CHUNK_ID})
    assert unknown_chunk_exc.value.code == KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED

    with pytest.raises(BusinessError) as type_exc:
        parse_extraction_json(
            extraction_json(item_type="defect_solution"),
            allowed_chunk_ids={CHUNK_ID},
            allowed_item_types={"process_rule"},
        )
    assert type_exc.value.code == KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED


def test_select_chunks_requires_valid_mode_inputs_and_limits() -> None:
    db = FakeDb(documents={DOCUMENT_ID: make_document()}, chunks={CHUNK_ID: make_chunk()})

    with pytest.raises(BusinessError) as missing_document_exc:
        knowledge_extraction.select_chunks_for_extraction(
            db,
            KnowledgeExtractionRequest(mode="document"),
            make_settings(),
        )
    assert missing_document_exc.value.code == KNOWLEDGE_ITEM_CONFIG_INVALID

    with pytest.raises(BusinessError) as missing_chunks_exc:
        knowledge_extraction.select_chunks_for_extraction(
            db,
            KnowledgeExtractionRequest(mode="chunks"),
            make_settings(),
        )
    assert missing_chunks_exc.value.code == KNOWLEDGE_ITEM_CONFIG_INVALID

    with pytest.raises(BusinessError) as max_chunks_exc:
        knowledge_extraction.select_chunks_for_extraction(
            db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID], max_chunks=51),
            make_settings(),
        )
    assert max_chunks_exc.value.code == KNOWLEDGE_ITEM_CONFIG_INVALID


def test_select_chunks_validates_source_document_and_chunk_boundaries() -> None:
    with pytest.raises(BusinessError) as document_exc:
        knowledge_extraction.select_chunks_for_extraction(
            FakeDb(),
            KnowledgeExtractionRequest(mode="document", document_id=DOCUMENT_ID),
            make_settings(),
        )
    assert document_exc.value.code == KNOWLEDGE_ITEM_SOURCE_DOCUMENT_NOT_FOUND

    with pytest.raises(BusinessError) as chunk_exc:
        knowledge_extraction.select_chunks_for_extraction(
            FakeDb(),
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
            make_settings(),
        )
    assert chunk_exc.value.code == KNOWLEDGE_ITEM_SOURCE_CHUNK_NOT_FOUND

    cross_document_db = FakeDb(
        chunks={
            CHUNK_ID: make_chunk(),
            SECOND_CHUNK_ID: make_chunk(SECOND_CHUNK_ID, OTHER_DOCUMENT_ID, 2),
        }
    )
    with pytest.raises(BusinessError) as cross_document_exc:
        knowledge_extraction.select_chunks_for_extraction(
            cross_document_db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID, SECOND_CHUNK_ID]),
            make_settings(),
        )
    assert cross_document_exc.value.code == KNOWLEDGE_ITEM_CONFIG_INVALID

    long_chunk_db = FakeDb(chunks={CHUNK_ID: make_chunk(content="x" * 20)})
    with pytest.raises(BusinessError) as char_limit_exc:
        knowledge_extraction.select_chunks_for_extraction(
            long_chunk_db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
            make_settings(knowledge_extraction_max_chars=10),
        )
    assert char_limit_exc.value.code == KNOWLEDGE_ITEM_CONFIG_INVALID


def test_extract_returns_zero_when_llm_returns_no_items() -> None:
    db = FakeDb(chunks={CHUNK_ID: make_chunk()})
    llm = FakeLLMProvider('{"items":[]}')

    result = knowledge_extraction.extract_knowledge_items(
        db,
        KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
        settings=make_settings(),
        llm_provider=llm,
    )

    assert result.items == []
    assert result.skipped_duplicates == []
    assert result.auto_submit is False
    assert result.status == "draft"
    assert len(llm.calls) == 1
    assert llm.calls[0].json_mode is True
    assert getattr(llm.calls[0], "think") is False
    assert llm.calls[0].think_required is False
    assert [message.role for message in llm.calls[0].messages] == ["system", "user"]


def test_generate_extraction_uses_from_prompt_with_json_and_advisory_think(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class RequestFactorySpy:
        @classmethod
        def from_prompt(
            cls,
            prompt: str,
            system_prompt: str | None = None,
            **kwargs: object,
        ) -> object:
            captured.append(
                {
                    "prompt": prompt,
                    "system_prompt": system_prompt,
                    **kwargs,
                }
            )
            return SimpleNamespace()

    monkeypatch.setattr(
        knowledge_extraction,
        "LLMGenerateRequest",
        RequestFactorySpy,
    )
    provider = FakeLLMProvider('{"items":[]}')

    result = knowledge_extraction._generate_extraction_json(
        "extraction system",
        "extraction user",
        make_settings(),
        provider,
    )

    assert result.text == '{"items":[]}'
    assert captured == [
        {
            "prompt": "extraction user",
            "system_prompt": "extraction system",
            "temperature": 0.0,
            "max_tokens": 2048,
            "json_mode": True,
            "think": False,
            "think_required": False,
        }
    ]


def test_generate_extraction_uses_no_arg_provider_getter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeLLMProvider('{"items":[]}')
    get_calls = 0

    def fake_get_provider() -> FakeLLMProvider:
        nonlocal get_calls
        get_calls += 1
        return provider

    monkeypatch.setattr(
        knowledge_extraction,
        "get_llm_provider",
        fake_get_provider,
    )

    result = knowledge_extraction._generate_extraction_json(
        "extraction system",
        "extraction user",
        make_settings(),
        None,
    )

    assert result.text == '{"items":[]}'
    assert get_calls == 1


def test_api_json_capability_false_rejects_extraction_before_network() -> None:
    db = FakeDb(chunks={CHUNK_ID: make_chunk()})
    client = FakeSDKClient()
    provider = build_llm_provider(
        make_settings(llm_provider="api", llm_remote_supports_json_mode=False),
        client=client,
    )

    with pytest.raises(BusinessError) as exc_info:
        knowledge_extraction.extract_knowledge_items(
            db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
            settings=make_settings(),
            llm_provider=provider,
        )

    assert exc_info.value.code == LLM_PARAMETER_UNSUPPORTED
    assert client.completions.calls == []


@pytest.mark.parametrize(
    ("provider_name", "settings"),
    [
        ("local", make_settings(llm_provider="local")),
        (
            "api",
            make_settings(
                llm_provider="api",
                llm_remote_supports_json_mode=True,
            ),
        ),
    ],
)
def test_local_and_api_use_the_same_extraction_business_path(
    provider_name: str,
    settings: SimpleNamespace,
) -> None:
    db = FakeDb(chunks={CHUNK_ID: make_chunk()})
    client = FakeSDKClient()
    provider = build_llm_provider(settings, client=client)

    result = knowledge_extraction.extract_knowledge_items(
        db,
        KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
        settings=settings,
        llm_provider=provider,
    )

    assert result.created == 0
    assert result.llm_provider == provider_name
    assert client.completions.calls[0]["response_format"] == {
        "type": "json_object"
    }
    if provider_name == "local":
        assert client.completions.calls[0]["extra_body"] == {"think": False}
    else:
        assert "extra_body" not in client.completions.calls[0]


def test_extract_creates_draft_and_auto_submit_creates_pending_review() -> None:
    for auto_submit, expected_status in [(False, "draft"), (True, "pending_review")]:
        db = FakeDb(documents={DOCUMENT_ID: make_document()}, chunks={CHUNK_ID: make_chunk()})
        llm = FakeLLMProvider(extraction_json())

        result = knowledge_extraction.extract_knowledge_items(
            db,
            KnowledgeExtractionRequest(
                mode="chunks",
                chunk_ids=[CHUNK_ID],
                auto_submit=auto_submit,
                created_by="system",
            ),
            settings=make_settings(),
            llm_provider=llm,
        )

        assert len(result.items) == 1
        assert result.items[0].status == expected_status
        assert result.items[0].status != "approved"
        assert result.created == 1
        assert result.llm_provider == "fake"
        assert result.llm_model == "fake-model"
        assert llm.calls[0].json_mode is True
        assert getattr(llm.calls[0], "think") is False


def test_extract_skips_duplicates_without_failing() -> None:
    duplicate = KnowledgeItem(
        id=uuid4(),
        item_type="process_rule",
        title="Riser feeding rule",
        content="Place risers near hot spots to support feeding.",
        content_hash="duplicate",
        status="draft",
        version=1,
    )
    db = FakeDb(
        documents={DOCUMENT_ID: make_document()},
        chunks={CHUNK_ID: make_chunk()},
        duplicate_items=[duplicate],
    )
    llm = FakeLLMProvider(extraction_json())

    result = knowledge_extraction.extract_knowledge_items(
        db,
        KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
        settings=make_settings(),
        llm_provider=llm,
    )

    assert result.created == 0
    assert len(result.skipped_duplicates) == 1
    assert result.skipped_duplicates[0].reason == "duplicate"


def test_parser_failure_does_not_write_half_finished_items() -> None:
    db = FakeDb(chunks={CHUNK_ID: make_chunk()})
    llm = FakeLLMProvider('{"items":[{"bad":true}]}')

    with pytest.raises(BusinessError) as exc_info:
        knowledge_extraction.extract_knowledge_items(
            db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
            settings=make_settings(),
            llm_provider=llm,
        )

    assert exc_info.value.code == KNOWLEDGE_ITEM_EXTRACTION_PARSE_FAILED
    assert db.added == []


def test_llm_failure_maps_to_extraction_failed_for_unexpected_errors() -> None:
    db = FakeDb(chunks={CHUNK_ID: make_chunk()})
    llm = FakeLLMProvider("{}", exc=RuntimeError("down"))

    with pytest.raises(BusinessError) as exc_info:
        knowledge_extraction.extract_knowledge_items(
            db,
            KnowledgeExtractionRequest(mode="chunks", chunk_ids=[CHUNK_ID]),
            settings=make_settings(),
            llm_provider=llm,
        )

    assert exc_info.value.code == KNOWLEDGE_ITEM_EXTRACTION_FAILED


def test_extraction_service_does_not_reference_rag_hybrid_or_retrieval_logs() -> None:
    source = inspect.getsource(knowledge_extraction)

    assert "hybrid_search" not in source
    assert "services.rag" not in source
    assert "retrieval_logs" not in source
    assert "opensearch" not in source.lower()
    assert "generate_document_embeddings" not in source
    assert "create_or_update_search_index" not in source
    assert "sync_document_chunks" not in source
    assert "rebuild" not in source.lower()
