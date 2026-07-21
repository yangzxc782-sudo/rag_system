from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit

from app.core.errors import (
    LLM_CONFIG_INVALID,
    LLM_PROVIDER_INVALID,
    BusinessError,
)


logger = logging.getLogger(__name__)
_warning_lock = Lock()
_legacy_alias_warning_emitted = False
_insecure_http_warning_emitted = False


@dataclass(frozen=True, slots=True)
class ActiveLLMMetadata:
    provider: Literal["local", "api"]
    model: str


def _config_error(field: str, message: str, *, reason: str | None = None) -> None:
    detail = {"field": field}
    if reason is not None:
        detail["reason"] = reason
    raise BusinessError(
        LLM_CONFIG_INVALID,
        message,
        detail=detail,
        status_code=400,
    )


def _emit_legacy_alias_warning_once() -> None:
    global _legacy_alias_warning_emitted
    with _warning_lock:
        if _legacy_alias_warning_emitted:
            return
        _legacy_alias_warning_emitted = True
    logger.warning(
        "Deprecated LLM provider alias is in use.",
        extra={
            "event": "deprecated_llm_provider_alias",
            "configured_provider": "openai_compatible",
            "normalized_provider": "local",
        },
    )


def _emit_insecure_http_warning_once(hostname: str) -> None:
    global _insecure_http_warning_emitted
    with _warning_lock:
        if _insecure_http_warning_emitted:
            return
        _insecure_http_warning_emitted = True
    logger.warning(
        "Insecure HTTP transport is enabled for the remote LLM provider.",
        extra={
            "event": "insecure_remote_transport_enabled",
            "provider": "api",
            "scheme": "http",
            "hostname": hostname,
        },
    )


def _normalize_provider(settings: Any) -> Literal["local", "api"]:
    configured = str(getattr(settings, "llm_provider", "")).strip().lower()
    if configured == "local":
        return "local"
    if configured == "api":
        return "api"
    if configured == "openai_compatible":
        _emit_legacy_alias_warning_once()
        return "local"
    raise BusinessError(
        LLM_PROVIDER_INVALID,
        "Unsupported LLM provider.",
        detail={"field": "llm_provider"},
        status_code=400,
    )


def _required_text(settings: Any, field: str) -> str:
    value = getattr(settings, field, "")
    resolved = str(value or "").strip()
    if not resolved:
        _config_error(field, f"{field} must not be empty.")
    return resolved


def _require_secret(settings: Any, field: str) -> None:
    value = getattr(settings, field, None)
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        resolved_secret = str(get_secret_value() or "").strip()
    else:
        resolved_secret = str(value or "").strip()
    is_missing = not resolved_secret
    resolved_secret = ""
    if is_missing:
        _config_error(field, f"{field} must not be empty.")


def _positive_number(settings: Any, field: str) -> float:
    value = getattr(settings, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        _config_error(field, f"{field} must be greater than 0.")
    return float(value)


def _validate_shared_defaults(settings: Any) -> None:
    temperature = getattr(settings, "llm_temperature", None)
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or temperature < 0
        or temperature > 2
    ):
        _config_error("llm_temperature", "llm_temperature must be between 0 and 2.")
    max_tokens = getattr(settings, "llm_max_tokens", None)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        _config_error("llm_max_tokens", "llm_max_tokens must be greater than 0.")


def _validate_remote_url(base_url: str, *, allow_insecure_http: bool) -> None:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
    except ValueError:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL is invalid.",
            reason="host",
        )
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL must use HTTP or HTTPS.",
            reason="scheme",
        )
    if parsed.username is not None or parsed.password is not None:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL must not contain user information.",
            reason="userinfo",
        )
    if parsed.query:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL must not contain a query.",
            reason="query",
        )
    if parsed.fragment:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL must not contain a fragment.",
            reason="fragment",
        )
    if not hostname:
        _config_error(
            "llm_remote_base_url",
            "Remote LLM base URL must contain a host.",
            reason="host",
        )
    if scheme == "https":
        return
    if hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        return
    if not allow_insecure_http:
        _config_error(
            "llm_remote_base_url",
            "Remote HTTP requires explicit insecure transport opt-in.",
            reason="insecure_http",
        )
    _emit_insecure_http_warning_once(hostname.lower())


def resolve_active_llm_metadata(settings: Any) -> ActiveLLMMetadata:
    provider = _normalize_provider(settings)
    model_field = "llm_model" if provider == "local" else "llm_remote_model"
    model = str(getattr(settings, model_field, "")).strip()
    return ActiveLLMMetadata(provider=provider, model=model)


def validate_active_llm_configuration(settings: Any) -> ActiveLLMMetadata:
    metadata = resolve_active_llm_metadata(settings)
    _validate_shared_defaults(settings)

    if metadata.provider == "local":
        _required_text(settings, "llm_base_url")
        _required_text(settings, "llm_model")
        _positive_number(settings, "llm_timeout_seconds")
        return metadata

    remote_base_url = _required_text(settings, "llm_remote_base_url")
    _require_secret(settings, "llm_remote_api_key")
    _required_text(settings, "llm_remote_model")
    _positive_number(settings, "llm_remote_timeout_seconds")
    allow_insecure_http = getattr(settings, "llm_remote_allow_insecure_http", False)
    if not isinstance(allow_insecure_http, bool):
        _config_error(
            "llm_remote_allow_insecure_http",
            "llm_remote_allow_insecure_http must be a boolean.",
        )
    supports_json_mode = getattr(settings, "llm_remote_supports_json_mode", False)
    if not isinstance(supports_json_mode, bool):
        _config_error(
            "llm_remote_supports_json_mode",
            "llm_remote_supports_json_mode must be a boolean.",
        )
    _validate_remote_url(
        remote_base_url,
        allow_insecure_http=allow_insecure_http,
    )
    return metadata
