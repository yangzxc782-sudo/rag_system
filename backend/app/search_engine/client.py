from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.errors import SEARCH_ENGINE_CONFIG_INVALID, BusinessError


class SearchEngineClientProtocol(Protocol):
    """Minimal surface used by search index and search services."""

    indices: Any

    def search(self, *, index: str, body: dict[str, Any], **kwargs: Any) -> Any: ...

    def bulk(self, *, body: Any, **kwargs: Any) -> Any: ...

    def delete_by_query(self, *, index: str, body: dict[str, Any], **kwargs: Any) -> Any: ...


def validate_search_engine_settings(settings: Any) -> None:
    provider = _get_required_str(settings, "search_engine_provider").lower()
    if provider != "opensearch":
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "Unsupported search engine provider.",
            detail={"provider": provider, "supported_providers": ["opensearch"]},
            status_code=400,
        )

    url = _get_required_str(settings, "search_engine_url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "search_engine_url must be an HTTP or HTTPS URL.",
            detail={"search_engine_url": url},
            status_code=400,
        )

    username = str(getattr(settings, "search_engine_username", "") or "").strip()
    password = str(getattr(settings, "search_engine_password", "") or "").strip()
    if bool(username) != bool(password):
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "search_engine_username and search_engine_password must be provided together.",
            status_code=400,
        )

    timeout_seconds = int(getattr(settings, "search_engine_timeout_seconds", 0))
    if timeout_seconds <= 0:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "search_engine_timeout_seconds must be greater than 0.",
            detail={"search_engine_timeout_seconds": timeout_seconds},
            status_code=400,
        )

    max_retries = int(getattr(settings, "search_engine_max_retries", -1))
    if max_retries < 0:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "search_engine_max_retries must be greater than or equal to 0.",
            detail={"search_engine_max_retries": max_retries},
            status_code=400,
        )


def create_search_engine_client(settings: Any) -> SearchEngineClientProtocol:
    """Create an OpenSearch client without pinging or creating indexes."""

    validate_search_engine_settings(settings)

    try:
        from opensearchpy import OpenSearch
    except ImportError as exc:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            "opensearch-py dependency is not installed.",
            status_code=500,
        ) from exc

    parsed = urlparse(settings.search_engine_url)
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    username = str(getattr(settings, "search_engine_username", "") or "").strip()
    password = str(getattr(settings, "search_engine_password", "") or "").strip()
    http_auth = (username, password) if username and password else None

    return OpenSearch(
        hosts=[{"host": parsed.hostname, "port": port}],
        http_auth=http_auth,
        use_ssl=scheme == "https",
        verify_certs=bool(settings.search_engine_verify_ssl),
        timeout=int(settings.search_engine_timeout_seconds),
        max_retries=int(settings.search_engine_max_retries),
        retry_on_timeout=True,
    )


def get_search_engine_client(settings: Any) -> SearchEngineClientProtocol:
    return create_search_engine_client(settings)


def _get_required_str(settings: Any, field_name: str) -> str:
    value = str(getattr(settings, field_name, "") or "").strip()
    if not value:
        raise BusinessError(
            SEARCH_ENGINE_CONFIG_INVALID,
            f"{field_name} must not be empty.",
            detail={"field": field_name},
            status_code=400,
        )
    return value
