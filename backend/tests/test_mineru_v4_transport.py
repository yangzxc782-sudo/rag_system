from __future__ import annotations

import json
import traceback
from dataclasses import replace
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from pydantic import SecretStr

from app.ingestion.mineru import (
    MinerUClient,
    MinerUConfigError,
    MinerUParseRequest,
    MinerURemoteError,
    MinerUTimeoutError,
    MinerUV4Transport,
)

TOKEN = "test-token-not-real"
SIGNED_UPLOAD_URL = "https://upload.example/signed/file?secret=upload-secret"
FULL_ZIP_URL = "https://download.example/result.zip?secret=download-secret"


def _assert_exception_is_sanitized(
    exception: BaseException,
    *sensitive_values: str,
) -> None:
    assert exception.__cause__ is None
    assert exception.__context__ is None
    pending = [exception]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        rendered = "".join(
            traceback.format_exception(
                type(current),
                current,
                current.__traceback__,
            )
        )
        for value in sensitive_values:
            assert value not in str(current)
            assert value not in repr(current)
            assert value not in rendered
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _request(*, parse_mode: str = "auto") -> MinerUParseRequest:
    return MinerUParseRequest(
        filename="casting.pdf",
        content=b"%PDF-official-v4-test",
        mime_type="application/pdf",
        parse_mode=parse_mode,
        enable_ocr=True,
        save_intermediate=True,
    )


def _zip_result() -> bytes:
    buffer = BytesIO()
    content_list = [
        {
            "type": "equation",
            "text": "$$Q = mc\\Delta T$$",
            "text_format": "latex",
            "page_idx": 0,
        }
    ]
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("nested/full.md", "# Casting")
        archive.writestr(
            "nested/casting_content_list.json",
            json.dumps(content_list),
        )
    return buffer.getvalue()


def _client(
    handler,
    *,
    max_poll_attempts: int = 10,
    sleeper=lambda _: None,
    max_archive_bytes: int = 1024 * 1024,
) -> MinerUClient:
    mock_transport = httpx.MockTransport(handler)
    transport = MinerUV4Transport(
        base_url="https://mineru.net",
        api_key=TOKEN,
        timeout_seconds=120,
        api_transport=mock_transport,
        external_transport=mock_transport,
        data_id_factory=lambda: "safe-data-1",
        max_archive_bytes=max_archive_bytes,
    )
    return MinerUClient(
        base_url="https://mineru.net",
        api_key=SecretStr(TOKEN),
        timeout_seconds=120,
        poll_interval_seconds=5,
        max_poll_attempts=max_poll_attempts,
        transport=transport,
        sleeper=sleeper,
    )


def test_v4_local_upload_poll_and_zip_flow_matches_official_contract() -> None:
    requests: list[httpx.Request] = []
    states = iter(["waiting-file", "pending", "running", "converting", "done"])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/v4/file-urls/batch"
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            assert request.headers["content-type"].startswith("application/json")
            body = json.loads(request.content.decode("utf-8"))
            assert body == {
                "files": [
                    {
                        "name": "casting.pdf",
                        "data_id": "safe-data-1",
                        "is_ocr": True,
                    }
                ],
                "model_version": "vlm",
            }
            assert "parse_mode" not in body
            assert "enable_ocr" not in body
            assert "save_intermediate" not in body
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            assert str(request.url) == SIGNED_UPLOAD_URL
            assert request.content == b"%PDF-official-v4-test"
            assert "authorization" not in request.headers
            assert "content-type" not in request.headers
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch-1":
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            state = next(states)
            target = {
                "file_name": "casting.pdf",
                "data_id": "safe-data-1",
                "state": state,
                "err_msg": "",
            }
            if state == "done":
                target["full_zip_url"] = FULL_ZIP_URL
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-1",
                        "extract_result": [
                            {
                                "file_name": "other.pdf",
                                "data_id": "other-data",
                                "state": "done",
                                "full_zip_url": "https://ignored.example/result.zip",
                            },
                            target,
                        ],
                    },
                },
            )
        if request.method == "GET" and request.url.host == "download.example":
            assert str(request.url) == FULL_ZIP_URL
            assert "authorization" not in request.headers
            return httpx.Response(200, content=_zip_result())
        raise AssertionError(f"Unexpected request: {request.method} {request.url.path}")

    client = _client(handler, sleeper=sleeps.append)
    result = client.parse_file(_request())

    assert result.parser_name == "mineru_api"
    assert result.parse_mode == "vlm"
    assert result.markdown_text == "# Casting"
    assert result.content_list[0]["type"] == "equation"
    assert result.content_list[0]["text"] == "$$Q = mc\\Delta T$$"
    assert result.raw_metadata["batch_id"] == "batch-1"
    assert FULL_ZIP_URL not in repr(result)
    assert SIGNED_UPLOAD_URL not in repr(result)
    assert len([request for request in requests if request.method == "POST"]) == 1
    assert len([request for request in requests if request.method == "PUT"]) == 1
    assert sleeps == [5, 5, 5, 5]


def test_v4_http_200_with_nonzero_code_is_a_remote_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 1001, "msg": "invalid request", "data": {}},
        )

    with pytest.raises(MinerURemoteError, match="code=1001"):
        _client(handler).parse_file(_request())


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"batch_id": "batch-1"},
        {"batch_id": "batch-1", "file_urls": []},
        {"batch_id": "../unsafe", "file_urls": [SIGNED_UPLOAD_URL]},
    ],
)
def test_v4_submission_validates_batch_id_and_upload_urls(data) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": data})

    with pytest.raises(MinerURemoteError):
        _client(handler).parse_file(_request())


def test_v4_signed_upload_failure_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-upload-fail",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            assert "authorization" not in request.headers
            assert "content-type" not in request.headers
            return httpx.Response(403)
        raise AssertionError("Polling must not start after an upload failure")

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    assert SIGNED_UPLOAD_URL not in str(exc_info.value)
    assert "upload-secret" not in str(exc_info.value)


def test_v4_signed_upload_request_error_discards_sensitive_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-upload-request-error",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            raise httpx.ConnectError(
                f"upload failed for {SIGNED_UPLOAD_URL} using {TOKEN}",
                request=request,
            )
        raise AssertionError("Polling must not start after an upload failure")

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    _assert_exception_is_sanitized(
        exc_info.value,
        SIGNED_UPLOAD_URL,
        "upload-secret",
        TOKEN,
    )


@pytest.mark.parametrize("failure_stage", ["submit", "poll"])
def test_v4_api_request_error_discards_token_and_exception_chain(
    failure_stage: str,
) -> None:
    api_url = (
        "https://mineru.net/api/v4/file-urls/batch"
        if failure_stage == "submit"
        else "https://mineru.net/api/v4/extract-results/batch/batch-api-error"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if failure_stage == "submit" and request.method == "POST":
            raise httpx.ConnectError(
                f"request failed for {api_url} using {TOKEN}",
                request=request,
            )
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-api-error",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        raise httpx.ConnectError(
            f"request failed for {api_url} using {TOKEN}",
            request=request,
        )

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    _assert_exception_is_sanitized(exc_info.value, api_url, TOKEN)


def test_v4_failed_state_uses_short_redacted_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-failed",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "batch-failed",
                    "extract_result": [
                        {
                            "file_name": "casting.pdf",
                            "data_id": "safe-data-1",
                            "state": "failed",
                            "err_msg": (
                                f"parse rejected {TOKEN} {SIGNED_UPLOAD_URL} "
                                + ("detail " * 200)
                            ),
                        }
                    ],
                },
            },
        )

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    message = str(exc_info.value)
    assert TOKEN not in message
    assert SIGNED_UPLOAD_URL not in message
    assert "upload-secret" not in message
    assert len(message) <= 500


def test_v4_polling_stops_at_configured_attempt_limit() -> None:
    poll_count = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-timeout",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        poll_count += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "batch-timeout",
                    "extract_result": [
                        {
                            "file_name": "casting.pdf",
                            "data_id": "safe-data-1",
                            "state": "waiting-file",
                        }
                    ],
                },
            },
        )

    with pytest.raises(MinerUTimeoutError):
        _client(
            handler,
            max_poll_attempts=3,
            sleeper=sleeps.append,
        ).parse_file(_request())

    assert poll_count == 3
    assert sleeps == [5, 5]


def test_v4_done_result_requires_full_zip_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-no-zip",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "batch-no-zip",
                    "extract_result": [
                        {
                            "file_name": "casting.pdf",
                            "data_id": "safe-data-1",
                            "state": "done",
                        }
                    ],
                },
            },
        )

    with pytest.raises(MinerURemoteError, match="full_zip_url"):
        _client(handler).parse_file(_request())


def test_v4_archive_download_limit_is_enforced_without_url_leak() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-large-zip",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-large-zip",
                        "extract_result": [
                            {
                                "file_name": "casting.pdf",
                                "data_id": "safe-data-1",
                                "state": "done",
                                "full_zip_url": FULL_ZIP_URL,
                            }
                        ],
                    },
                },
            )
        return httpx.Response(200, content=b"12345")

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler, max_archive_bytes=4).parse_file(_request())

    assert FULL_ZIP_URL not in str(exc_info.value)
    assert "download-secret" not in str(exc_info.value)


def test_v4_unknown_parse_mode_fails_before_http_request() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError("No HTTP request expected")

    with pytest.raises(MinerUConfigError, match="parse mode"):
        _client(handler).parse_file(replace(_request(), parse_mode="guess"))

    assert request_count == 0


def test_v4_invalid_base_url_discards_parser_exception_chain() -> None:
    class InvalidBaseUrl:
        def strip(self) -> str:
            raise ValueError("sensitive parser implementation detail")

    with pytest.raises(MinerUConfigError) as exc_info:
        MinerUV4Transport(
            base_url=InvalidBaseUrl(),  # type: ignore[arg-type]
            api_key=TOKEN,
            timeout_seconds=120,
        )

    _assert_exception_is_sanitized(
        exc_info.value,
        TOKEN,
        "sensitive parser implementation detail",
    )


def test_v4_base_url_must_be_an_origin_without_api_path() -> None:
    with pytest.raises(MinerUConfigError, match="must not include an API path"):
        MinerUV4Transport(
            base_url="https://mineru.net/api/v4",
            api_key=TOKEN,
            timeout_seconds=120,
        )


def test_v4_zip_download_http_failure_is_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-download-fail",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-download-fail",
                        "extract_result": [
                            {
                                "file_name": "casting.pdf",
                                "data_id": "safe-data-1",
                                "state": "done",
                                "full_zip_url": FULL_ZIP_URL,
                            }
                        ],
                    },
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(502)

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    assert FULL_ZIP_URL not in str(exc_info.value)
    assert "download-secret" not in str(exc_info.value)


def test_v4_zip_download_request_error_discards_sensitive_exception_chain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-download-request-error",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-download-request-error",
                        "extract_result": [
                            {
                                "file_name": "casting.pdf",
                                "data_id": "safe-data-1",
                                "state": "done",
                                "full_zip_url": FULL_ZIP_URL,
                            }
                        ],
                    },
                },
            )
        raise httpx.ConnectError(
            f"download failed for {FULL_ZIP_URL} using {TOKEN}",
            request=request,
        )

    with pytest.raises(MinerURemoteError) as exc_info:
        _client(handler).parse_file(_request())

    _assert_exception_is_sanitized(
        exc_info.value,
        FULL_ZIP_URL,
        "download-secret",
        TOKEN,
    )


@pytest.mark.parametrize(
    "extract_result",
    [
        [
            {
                "file_name": "casting.pdf",
                "data_id": "safe-data-1",
                "state": "done",
                "full_zip_url": FULL_ZIP_URL,
            }
        ],
        [
            {
                "file_name": "casting.pdf",
                "state": "done",
                "full_zip_url": FULL_ZIP_URL,
            }
        ],
        [
            {
                "file_name": "different-name.pdf",
                "state": "done",
                "full_zip_url": FULL_ZIP_URL,
            }
        ],
        [
            {
                "file_name": "casting.pdf",
                "data_id": "other-data",
                "state": "failed",
                "err_msg": "wrong candidate",
            },
            {
                "file_name": "casting.pdf",
                "data_id": "safe-data-1",
                "state": "done",
                "full_zip_url": FULL_ZIP_URL,
            },
        ],
    ],
)
def test_v4_extract_result_matching_uses_data_id_filename_then_single_item(
    extract_result,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-match",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        if request.url.host == "mineru.net":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-match",
                        "extract_result": extract_result,
                    },
                },
            )
        return httpx.Response(200, content=_zip_result())

    result = _client(handler).parse_file(_request())

    assert result.raw_metadata["batch_id"] == "batch-match"


@pytest.mark.parametrize(
    "response_factory",
    [
        lambda: httpx.Response(503),
        lambda: httpx.Response(200, content=b"not-json"),
        lambda: httpx.Response(200, json=["not", "a", "mapping"]),
    ],
)
def test_v4_http_and_json_errors_are_normalized(response_factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response_factory()

    with pytest.raises(MinerURemoteError):
        _client(handler).parse_file(_request())


@pytest.mark.parametrize(
    ("extract_result", "message"),
    [
        ([], "extract_result"),
        ({"state": "running"}, "extract_result"),
        (
            [
                {"file_name": "a.pdf", "data_id": "a", "state": "running"},
                {"file_name": "b.pdf", "data_id": "b", "state": "running"},
            ],
            "submitted file",
        ),
        (
            [
                {
                    "file_name": "casting.pdf",
                    "data_id": "safe-data-1",
                    "state": "unexpected-state",
                }
            ],
            "unsupported task status",
        ),
    ],
)
def test_v4_polling_rejects_invalid_extract_results(
    extract_result,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-invalid-result",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "batch-invalid-result",
                    "extract_result": extract_result,
                },
            },
        )

    with pytest.raises(MinerURemoteError, match=message):
        _client(handler).parse_file(_request())


def test_v4_polling_rejects_mismatched_batch_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "batch_id": "batch-expected",
                        "file_urls": [SIGNED_UPLOAD_URL],
                    },
                },
            )
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "batch_id": "batch-other",
                    "extract_result": [
                        {
                            "file_name": "casting.pdf",
                            "data_id": "safe-data-1",
                            "state": "running",
                        }
                    ],
                },
            },
        )

    with pytest.raises(MinerURemoteError, match="mismatched batch_id"):
        _client(handler).parse_file(_request())
