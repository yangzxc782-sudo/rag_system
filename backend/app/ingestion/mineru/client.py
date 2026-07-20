from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import SecretStr

from app.ingestion.mineru.models import (
    MinerUClientError,
    MinerUConfigError,
    MinerUParseRequest,
    MinerUParseResult,
    MinerURemoteError,
    MinerUTimeoutError,
    MinerUTransportProtocol,
    normalize_mineru_model_version,
)
from app.ingestion.mineru.v4_transport import MinerUV4Transport

_SUCCEEDED_STATUSES = frozenset({"completed", "done", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"cancelled", "error", "failed", "rejected"})
_PENDING_STATUSES = frozenset(
    {"created", "pending", "processing", "queued", "running", "submitted"}
)
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Backward-compatible import name; the implementation is the official V4 adapter.
MinerUHTTPTransport = MinerUV4Transport


class MinerUClient:
    """Stable internal MinerU client used by later ingestion orchestration."""

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: SecretStr | str | None,
        timeout_seconds: int,
        poll_interval_seconds: int,
        max_poll_attempts: int,
        transport: MinerUTransportProtocol | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        secret = _secret_value(api_key)
        self._validate_config(
            base_url=base_url,
            api_key=secret,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
        )
        assert base_url is not None

        self._api_key = secret
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts
        self._sleeper = sleeper
        self._transport = transport or MinerUV4Transport(
            base_url=base_url,
            api_key=secret,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        transport: MinerUTransportProtocol | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> MinerUClient:
        return cls(
            base_url=settings.mineru_api_base_url,
            api_key=settings.mineru_api_key,
            timeout_seconds=settings.mineru_api_timeout_seconds,
            poll_interval_seconds=settings.mineru_api_poll_interval_seconds,
            max_poll_attempts=settings.mineru_api_max_poll_attempts,
            transport=transport,
            sleeper=sleeper,
        )

    def parse_file(self, request: MinerUParseRequest) -> MinerUParseResult:
        self._validate_request(request)
        result: MinerUParseResult | None = None
        safe_error: MinerUClientError | None = None
        try:
            submitted = self._transport.submit(request)
            task_id = _task_id(submitted)
            submitted_status = _status(submitted)

            if submitted_status in _FAILED_STATUSES:
                raise _remote_task_error(submitted)
            if submitted_status in _SUCCEEDED_STATUSES:
                completed = submitted
            else:
                if task_id is None:
                    raise MinerURemoteError(
                        "MinerU API response did not include a task identifier"
                    )
                completed = self._poll_until_complete(task_id)

            completed_task_id = task_id or _task_id(completed)
            if completed_task_id is None:
                raise MinerURemoteError(
                    "MinerU API response did not include a task identifier"
                )
            result = self._transport.get_result(completed_task_id, completed)
        except MinerUClientError as exc:
            safe_message = _redact_message(str(exc), self._api_key)
            safe_error = type(exc)(safe_message)
        except Exception:
            safe_error = MinerURemoteError("MinerU API client failed")

        if safe_error is not None:
            # Raise after leaving the except block so the original transport
            # exception cannot remain reachable through __context__.
            raise safe_error
        if result is None:
            raise MinerURemoteError("MinerU API client returned no result")
        return result

    def _poll_until_complete(self, task_id: str) -> Mapping[str, Any]:
        for attempt in range(self._max_poll_attempts):
            task = self._transport.get_task(task_id)
            status = _status(task)
            if status in _SUCCEEDED_STATUSES:
                return task
            if status in _FAILED_STATUSES:
                raise _remote_task_error(task)
            if status not in _PENDING_STATUSES:
                raise MinerURemoteError(
                    f"MinerU API returned unsupported task status: {status or 'missing'}"
                )
            if attempt + 1 < self._max_poll_attempts:
                self._sleeper(self._poll_interval_seconds)
        raise MinerUTimeoutError(
            "MinerU task did not finish within the configured polling limit"
        )

    @staticmethod
    def _validate_config(
        *,
        base_url: str | None,
        api_key: str,
        timeout_seconds: int,
        poll_interval_seconds: int,
        max_poll_attempts: int,
    ) -> None:
        if not base_url or not base_url.strip():
            raise MinerUConfigError("MINERU_API_BASE_URL is required")
        if not api_key:
            raise MinerUConfigError("MINERU_API_KEY is required")
        if timeout_seconds <= 0:
            raise MinerUConfigError("MINERU_API_TIMEOUT_SECONDS must be positive")
        if poll_interval_seconds < 0:
            raise MinerUConfigError(
                "MINERU_API_POLL_INTERVAL_SECONDS cannot be negative"
            )
        if max_poll_attempts <= 0:
            raise MinerUConfigError(
                "MINERU_API_MAX_POLL_ATTEMPTS must be positive"
            )

    @staticmethod
    def _validate_request(request: MinerUParseRequest) -> None:
        if not request.filename.strip():
            raise MinerUConfigError("MinerU parse filename is required")
        if not request.content:
            raise MinerUConfigError("MinerU parse content cannot be empty")
        normalize_mineru_model_version(request.parse_mode)


class FakeMinerUClient:
    """Deterministic test client implementing the same parse_file interface."""

    def __init__(
        self,
        *,
        result: MinerUParseResult | None = None,
        error: MinerUClientError | None = None,
    ) -> None:
        if result is None and error is None:
            raise MinerUConfigError("FakeMinerUClient requires a result or error")
        self._result = result
        self._error = error
        self.requests: list[MinerUParseRequest] = []

    def parse_file(self, request: MinerUParseRequest) -> MinerUParseResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _secret_value(api_key: SecretStr | str | None) -> str:
    if isinstance(api_key, SecretStr):
        return api_key.get_secret_value()
    return api_key or ""


def _status(payload: Mapping[str, Any]) -> str:
    return str(payload.get("status") or "").strip().lower()


def _task_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("task_id") or payload.get("id")
    return str(value) if value is not None else None


def _remote_task_error(payload: Mapping[str, Any]) -> MinerURemoteError:
    status = _status(payload) or "failed"
    code = payload.get("code") or payload.get("error_code")
    suffix = f", code={code}" if isinstance(code, (str, int)) else ""
    reason = payload.get("error_message")
    if isinstance(reason, str) and reason.strip():
        suffix += f", reason={reason.strip()[:300]}"
    return MinerURemoteError(f"MinerU task failed (status={status}{suffix})")


def _redact_message(message: str, api_key: str) -> str:
    redacted = message.replace(api_key, "[REDACTED]") if api_key else message
    redacted = _URL_PATTERN.sub("[REDACTED_URL]", redacted)
    return redacted[:500]
