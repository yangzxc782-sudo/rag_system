from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import (
    DOCUMENT_ASSET_NOT_FOUND,
    DOCUMENT_BLOCK_NOT_FOUND,
    DOCUMENT_PARSE_FAILED,
    DOCUMENT_PARSE_RUN_NOT_FOUND,
    DOCUMENT_PARSER_CONFIG_INVALID,
    DOCUMENT_PARSER_UNAVAILABLE,
)
from app.ingestion.mineru import (
    FakeMinerUClient,
    MinerUAssetResult,
    MinerUClient,
    MinerUClientProtocol,
    MinerUConfigError,
    MinerUParseRequest,
    MinerUParseResult,
    MinerURemoteError,
    MinerUResultFile,
    MinerUTimeoutError,
    normalize_mineru_model_version,
)


def _request() -> MinerUParseRequest:
    return MinerUParseRequest(
        filename="casting.pdf",
        content=b"test-pdf-content",
        mime_type="application/pdf",
        parse_mode="auto",
        enable_ocr=True,
        save_intermediate=True,
    )


def _result() -> MinerUParseResult:
    return MinerUParseResult(
        parser_name="mineru_api",
        parser_version="test-v1",
        parse_mode="auto",
        markdown_text="# Casting",
        text="Casting",
        content_list=({"type": "title", "text": "Casting"},),
        result_files=(
            MinerUResultFile(
                file_type="markdown",
                filename="output.md",
                content_type="text/markdown",
            ),
        ),
        assets=(
            MinerUAssetResult(
                asset_type="image",
                asset_key="images/page-1.png",
                filename="page-1.png",
                mime_type="image/png",
                page_number=1,
            ),
        ),
        raw_metadata={"task_id": "fake-task", "api_version": "test"},
        page_count=1,
    )


def test_settings_define_mineru_configuration_defaults() -> None:
    fields = Settings.model_fields

    assert fields["document_parser_provider"].default == "mineru_api"
    assert fields["mineru_api_timeout_seconds"].default == 300
    assert fields["mineru_api_poll_interval_seconds"].default == 5
    assert fields["mineru_api_max_poll_attempts"].default == 120
    assert fields["mineru_output_prefix"].default == "parsed-assets"
    assert fields["mineru_parse_mode"].default == "auto"
    assert fields["mineru_enable_ocr"].default is True
    assert fields["mineru_save_intermediate"].default is True


def test_mineru_error_codes_are_available() -> None:
    assert DOCUMENT_PARSER_CONFIG_INVALID == "DOCUMENT_PARSER_CONFIG_INVALID"
    assert DOCUMENT_PARSER_UNAVAILABLE == "DOCUMENT_PARSER_UNAVAILABLE"
    assert DOCUMENT_PARSE_FAILED == "DOCUMENT_PARSE_FAILED"
    assert DOCUMENT_PARSE_RUN_NOT_FOUND == "DOCUMENT_PARSE_RUN_NOT_FOUND"
    assert DOCUMENT_BLOCK_NOT_FOUND == "DOCUMENT_BLOCK_NOT_FOUND"
    assert DOCUMENT_ASSET_NOT_FOUND == "DOCUMENT_ASSET_NOT_FOUND"


@pytest.mark.parametrize(
    ("configured", "official"),
    [
        ("auto", "vlm"),
        ("vlm", "vlm"),
        ("pipeline", "pipeline"),
        ("MinerU-HTML", "MinerU-HTML"),
        ("mineru-html", "MinerU-HTML"),
    ],
)
def test_parse_mode_maps_to_official_model_version(
    configured: str,
    official: str,
) -> None:
    assert normalize_mineru_model_version(configured) == official


@pytest.mark.parametrize(
    ("base_url", "api_key"),
    [
        (None, SecretStr("secret")),
        ("https://mineru.invalid", None),
        ("", SecretStr("secret")),
        ("https://mineru.invalid", SecretStr("")),
    ],
)
def test_client_rejects_missing_configuration(
    base_url: str | None,
    api_key: SecretStr | None,
) -> None:
    with pytest.raises(MinerUConfigError):
        MinerUClient(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=300,
            poll_interval_seconds=5,
            max_poll_attempts=120,
            transport=_CompletedTransport(),
        )


def test_api_key_is_not_exposed_by_configuration_errors() -> None:
    api_key = "mineru-super-secret"

    with pytest.raises(MinerUConfigError) as exc_info:
        MinerUClient(
            base_url="",
            api_key=SecretStr(api_key),
            timeout_seconds=300,
            poll_interval_seconds=5,
            max_poll_attempts=120,
            transport=_CompletedTransport(),
        )

    assert api_key not in str(exc_info.value)


def test_request_and_result_repr_hide_document_content() -> None:
    request = _request()
    result = _result()

    assert "test-pdf-content" not in repr(request)
    assert "# Casting" not in repr(result)


def test_fake_client_returns_parse_result_without_network() -> None:
    expected = _result()
    client = FakeMinerUClient(result=expected)

    result = client.parse_file(_request())

    assert isinstance(client, MinerUClientProtocol)
    assert result == expected
    assert client.requests == [_request()]


def test_fake_client_can_raise_remote_error() -> None:
    client = FakeMinerUClient(error=MinerURemoteError("remote parse failed"))

    with pytest.raises(MinerURemoteError, match="remote parse failed"):
        client.parse_file(_request())


def test_fake_client_can_raise_timeout_error() -> None:
    client = FakeMinerUClient(error=MinerUTimeoutError("polling timed out"))

    with pytest.raises(MinerUTimeoutError, match="polling timed out"):
        client.parse_file(_request())


class _CompletedTransport:
    def submit(self, request: MinerUParseRequest) -> Mapping[str, Any]:
        return {"task_id": "task-1", "status": "pending"}

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        return {"task_id": task_id, "status": "succeeded"}

    def get_result(
        self,
        task_id: str,
        task: Mapping[str, Any],
    ) -> MinerUParseResult:
        return _result()


class _PendingTransport(_CompletedTransport):
    def __init__(self) -> None:
        self.poll_count = 0

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        self.poll_count += 1
        return {"task_id": task_id, "status": "running"}


def test_polling_never_exceeds_configured_attempt_limit() -> None:
    transport = _PendingTransport()
    client = MinerUClient(
        base_url="https://mineru.invalid",
        api_key=SecretStr("secret"),
        timeout_seconds=300,
        poll_interval_seconds=0,
        max_poll_attempts=3,
        transport=transport,
        sleeper=lambda _: None,
    )

    with pytest.raises(MinerUTimeoutError):
        client.parse_file(_request())

    assert transport.poll_count == 3


def test_client_keeps_parse_file_result_interface_stable() -> None:
    client = MinerUClient(
        base_url="https://mineru.invalid",
        api_key=SecretStr("secret"),
        timeout_seconds=300,
        poll_interval_seconds=0,
        max_poll_attempts=3,
        transport=_CompletedTransport(),
        sleeper=lambda _: None,
    )

    result = client.parse_file(_request())

    assert isinstance(client, MinerUClientProtocol)
    assert isinstance(result, MinerUParseResult)
    assert result.parser_name == "mineru_api"
    assert result.markdown_text == "# Casting"
    assert result.content_list == ({"type": "title", "text": "Casting"},)
    assert result.raw_metadata == {
        "task_id": "fake-task",
        "api_version": "test",
    }


def test_client_rejects_unknown_parse_mode_before_transport_call() -> None:
    transport = _CompletedTransport()
    client = MinerUClient(
        base_url="https://mineru.invalid",
        api_key=SecretStr("secret"),
        timeout_seconds=300,
        poll_interval_seconds=0,
        max_poll_attempts=3,
        transport=transport,
        sleeper=lambda _: None,
    )

    with pytest.raises(MinerUConfigError, match="parse mode"):
        client.parse_file(replace(_request(), parse_mode="unsupported"))
