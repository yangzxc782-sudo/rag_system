from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.ingestion.mineru.archive_reader import (
    DEFAULT_MAX_ARCHIVE_BYTES,
    MinerUArchiveReader,
)
from app.ingestion.mineru.models import (
    MinerUConfigError,
    MinerUParseRequest,
    MinerUParseResult,
    MinerURemoteError,
    MinerUTimeoutError,
    normalize_mineru_model_version,
)

_REMOTE_STATE_MAP = {
    "waiting-file": "pending",
    "pending": "pending",
    "running": "running",
    "converting": "processing",
    "done": "succeeded",
    "failed": "failed",
}
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _SubmissionContext:
    filename: str
    data_id: str
    parse_mode: str
    save_intermediate: bool


class MinerUV4Transport:
    """Official MinerU V4 local-file batch upload transport."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        api_transport: httpx.BaseTransport | None = None,
        external_transport: httpx.BaseTransport | None = None,
        archive_reader: MinerUArchiveReader | None = None,
        data_id_factory: Callable[[], str] = lambda: uuid4().hex,
        max_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        if not api_key:
            raise MinerUConfigError("MINERU_API_KEY is required")
        if timeout_seconds <= 0:
            raise MinerUConfigError("MINERU_API_TIMEOUT_SECONDS must be positive")
        if max_archive_bytes <= 0:
            raise MinerUConfigError("MinerU ZIP size limit must be positive")
        self._base_url = _validate_base_url(base_url)
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._api_transport = api_transport
        self._external_transport = external_transport
        self._archive_reader = archive_reader or MinerUArchiveReader(
            max_archive_bytes=max_archive_bytes
        )
        self._data_id_factory = data_id_factory
        self._max_archive_bytes = max_archive_bytes
        self._submissions: dict[str, _SubmissionContext] = {}
        self._result_urls: dict[str, str] = {}

    def submit(self, request: MinerUParseRequest) -> Mapping[str, Any]:
        model_version = normalize_mineru_model_version(request.parse_mode)
        filename = _safe_filename(request.filename)
        data_id = _safe_data_id(self._data_id_factory())
        payload = self._request_api_json(
            "POST",
            "/api/v4/file-urls/batch",
            json={
                "files": [
                    {
                        "name": filename,
                        "data_id": data_id,
                        "is_ocr": request.enable_ocr,
                    }
                ],
                "model_version": model_version,
            },
        )
        data = self._response_data(payload, operation="upload URL request")
        batch_id = _validated_batch_id(data.get("batch_id"))
        file_urls = data.get("file_urls")
        if not isinstance(file_urls, list) or not file_urls:
            raise MinerURemoteError(
                "MinerU upload URL response did not include file_urls"
            )
        upload_url = _required_text(file_urls[0], "file_urls")
        _validate_external_url(upload_url, label="upload URL")

        self._upload_file(upload_url, request.content)
        self._submissions[batch_id] = _SubmissionContext(
            filename=filename,
            data_id=data_id,
            parse_mode=model_version,
            save_intermediate=request.save_intermediate,
        )
        return {
            "task_id": batch_id,
            "status": "pending",
            "parse_mode": model_version,
        }

    def get_task(self, task_id: str) -> Mapping[str, Any]:
        context = self._submissions.get(task_id)
        if context is None:
            raise MinerURemoteError("MinerU batch submission context is unavailable")
        safe_task_id = quote(task_id, safe="")
        payload = self._request_api_json(
            "GET",
            f"/api/v4/extract-results/batch/{safe_task_id}",
        )
        data = self._response_data(payload, operation="batch result query")
        response_batch_id = _validated_batch_id(data.get("batch_id"))
        if response_batch_id != task_id:
            raise MinerURemoteError("MinerU batch result returned a mismatched batch_id")

        extract_result = data.get("extract_result")
        if not isinstance(extract_result, list) or not extract_result:
            raise MinerURemoteError(
                "MinerU batch result did not include extract_result"
            )
        target = _find_extract_result(extract_result, context)
        remote_state = _required_text(target.get("state"), "state").lower()
        status = _REMOTE_STATE_MAP.get(remote_state)
        if status is None:
            raise MinerURemoteError(
                f"MinerU API returned unsupported task status: {_safe_label(remote_state)}"
            )
        if status == "failed":
            return {
                "task_id": task_id,
                "status": "failed",
                "remote_state": remote_state,
                "error_message": self._safe_message(target.get("err_msg")),
            }
        if status == "succeeded":
            full_zip_url = _required_text(
                target.get("full_zip_url"),
                "full_zip_url",
            )
            _validate_external_url(full_zip_url, label="result ZIP URL")
            self._result_urls[task_id] = full_zip_url
        return {
            "task_id": task_id,
            "status": status,
            "remote_state": remote_state,
            "parse_mode": context.parse_mode,
        }

    def get_result(
        self,
        task_id: str,
        task: Mapping[str, Any],
    ) -> MinerUParseResult:
        if str(task.get("status") or "").lower() != "succeeded":
            raise MinerURemoteError("MinerU task result was requested before completion")
        context = self._submissions.get(task_id)
        full_zip_url = self._result_urls.get(task_id)
        if context is None or full_zip_url is None:
            raise MinerURemoteError("MinerU completed result context is unavailable")
        archive_bytes = self._download_archive(full_zip_url)
        return self._archive_reader.read(
            archive_bytes,
            batch_id=task_id,
            filename=context.filename,
            parse_mode=context.parse_mode,
            save_intermediate=context.save_intermediate,
        )

    def _request_api_json(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        request_error: MinerURemoteError | MinerUTimeoutError | None = None
        response: httpx.Response | None = None
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout_seconds,
                transport=self._api_transport,
            ) as client:
                response = client.request(method, path, **kwargs)
        except httpx.TimeoutException:
            request_error = MinerUTimeoutError("MinerU API request timed out")
        except httpx.RequestError:
            request_error = MinerURemoteError("MinerU API is unavailable")

        if request_error is not None:
            raise request_error
        if response is None:
            raise MinerURemoteError("MinerU API returned no response")

        if response.is_error:
            raise MinerURemoteError(
                f"MinerU API returned HTTP {response.status_code}"
            )
        invalid_json = False
        try:
            payload = response.json()
        except ValueError:
            invalid_json = True
            payload = None
        if invalid_json:
            raise MinerURemoteError("MinerU API returned invalid JSON")
        if not isinstance(payload, Mapping):
            raise MinerURemoteError("MinerU API returned an invalid response shape")
        return payload

    def _response_data(
        self,
        payload: Mapping[str, Any],
        *,
        operation: str,
    ) -> Mapping[str, Any]:
        code = payload.get("code")
        if code != 0:
            safe_code = code if isinstance(code, (str, int)) else "unknown"
            message = self._safe_message(payload.get("msg"))
            suffix = f", message={message}" if message else ""
            raise MinerURemoteError(
                f"MinerU {operation} failed (code={safe_code}{suffix})"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MinerURemoteError(
                f"MinerU {operation} response did not include valid data"
            )
        return data

    def _upload_file(self, upload_url: str, content: bytes) -> None:
        upload_error: MinerURemoteError | MinerUTimeoutError | None = None
        response: httpx.Response | None = None
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                follow_redirects=True,
                transport=self._external_transport,
            ) as client:
                response = client.put(upload_url, content=content)
        except httpx.TimeoutException:
            upload_error = MinerUTimeoutError("MinerU signed upload timed out")
        except httpx.RequestError:
            upload_error = MinerURemoteError("MinerU signed upload failed")

        if upload_error is not None:
            raise upload_error
        if response is None:
            raise MinerURemoteError("MinerU signed upload returned no response")
        if not 200 <= response.status_code < 300:
            raise MinerURemoteError(
                f"MinerU signed upload returned HTTP {response.status_code}"
            )

    def _download_archive(self, full_zip_url: str) -> bytes:
        download_error: MinerURemoteError | MinerUTimeoutError | None = None
        chunks: list[bytes] = []
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                follow_redirects=True,
                transport=self._external_transport,
            ) as client:
                with client.stream("GET", full_zip_url) as response:
                    if not 200 <= response.status_code < 300:
                        raise MinerURemoteError(
                            "MinerU result ZIP download returned "
                            f"HTTP {response.status_code}"
                        )
                    content_length = _optional_int(
                        response.headers.get("content-length")
                    )
                    if (
                        content_length is not None
                        and content_length > self._max_archive_bytes
                    ):
                        raise MinerURemoteError(
                            "MinerU result ZIP exceeds the configured size limit"
                        )
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self._max_archive_bytes:
                            raise MinerURemoteError(
                                "MinerU result ZIP exceeds the configured size limit"
                            )
                        chunks.append(chunk)
        except MinerURemoteError:
            raise
        except httpx.TimeoutException:
            download_error = MinerUTimeoutError(
                "MinerU result archive download timed out"
            )
        except httpx.RequestError:
            download_error = MinerURemoteError(
                "MinerU result archive download failed"
            )

        if download_error is not None:
            raise download_error
        return b"".join(chunks)

    def _safe_message(self, value: Any) -> str:
        message = str(value or "")
        if self._api_key:
            message = message.replace(self._api_key, "[REDACTED]")
        message = _URL_PATTERN.sub("[REDACTED_URL]", message)
        return " ".join(message.split())[:300]


def _find_extract_result(
    items: list[Any],
    context: _SubmissionContext,
) -> Mapping[str, Any]:
    candidates = [item for item in items if isinstance(item, Mapping)]
    if not candidates:
        raise MinerURemoteError("MinerU extract_result has an invalid shape")

    for item in candidates:
        if _optional_text(item.get("data_id")) == context.data_id:
            return item
    for item in candidates:
        if _optional_text(item.get("file_name")) == context.filename:
            return item
    if len(candidates) == 1:
        return candidates[0]
    raise MinerURemoteError(
        "MinerU batch result did not include the submitted file"
    )


def _validate_base_url(base_url: str) -> str:
    config_error: MinerUConfigError | None = None
    url: httpx.URL | None = None
    try:
        url = httpx.URL(base_url.strip())
    except Exception:
        config_error = MinerUConfigError("MINERU_API_BASE_URL is invalid")
    if config_error is not None:
        raise config_error
    if url is None:
        raise MinerUConfigError("MINERU_API_BASE_URL is invalid")
    if url.scheme not in {"http", "https"} or not url.host:
        raise MinerUConfigError("MINERU_API_BASE_URL must be an HTTP(S) origin")
    if url.path not in {"", "/"}:
        raise MinerUConfigError(
            "MINERU_API_BASE_URL must not include an API path"
        )
    return str(url).rstrip("/")


def _validate_external_url(value: str, *, label: str) -> None:
    invalid_url = False
    try:
        url = httpx.URL(value)
    except Exception:
        invalid_url = True
        url = None
    if invalid_url or url is None:
        raise MinerURemoteError(f"MinerU returned an invalid {label}")
    if url.scheme not in {"http", "https"} or not url.host:
        raise MinerURemoteError(f"MinerU returned an invalid {label}")


def _safe_filename(value: str) -> str:
    filename = PurePosixPath(value.replace("\\", "/")).name
    filename = "".join(character for character in filename if ord(character) >= 32)
    if not filename:
        raise MinerUConfigError("MinerU parse filename is required")
    return filename[:255]


def _safe_data_id(value: Any) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")
    return (sanitized or uuid4().hex)[:128]


def _validated_batch_id(value: Any) -> str:
    batch_id = _required_text(value, "batch_id")
    if len(batch_id) > 128 or re.fullmatch(r"[A-Za-z0-9_.-]+", batch_id) is None:
        raise MinerURemoteError("MinerU response included an invalid batch_id")
    return batch_id


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise MinerURemoteError(
            f"MinerU response did not include {field_name}"
        )
    return text


def _optional_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, bytes, bytearray)):
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:80] or "missing"
