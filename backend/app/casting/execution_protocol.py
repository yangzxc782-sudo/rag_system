"""Versioned, standard-library-only protocol shared by web and engine processes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
OUTPUT_FILES = (
    "admission-report.json", "shacl-report.json", "shacl-report.ttl", "shacl-report.txt",
    "knowledge-graph.ttl", "knowledge-graph.owl", "recommendation.json", "recommendation.md",
)


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: float = 30
    max_combinations: int = 20_000
    max_attempts: int = 16
    max_output_bytes: int = 32 * 1024 * 1024
    max_recommendation_bytes: int = 4 * 1024 * 1024
    max_log_bytes: int = 1024 * 1024  # Per stream; included in directory budget.

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) not in (float, int) or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 300:
            raise ValueError("Invalid engine timeout")
        for name, value in asdict(self).items():
            if name != "timeout_seconds" and (type(value) is not int or value <= 0):
                raise ValueError("Invalid engine budget")
        if self.max_recommendation_bytes > self.max_output_bytes:
            raise ValueError("Recommendation budget exceeds directory budget")


class CastingExecutionError(Exception):
    """Safe public error. Filesystem paths and tracebacks stay in local audit files."""

    def __init__(self, code: str, category: str, message: str, *, retryable: bool = False,
                 issues: list[dict[str, str]] | None = None, run_id: str | None = None):
        super().__init__(message)
        self.code, self.category, self.message = code, category, message
        self.retryable, self.issues, self.run_id = retryable, issues or [], run_id
        self.run_directory: Path | None = None

    def public_dict(self) -> dict[str, Any]:
        return dict(category=self.category, code=self.code, message=self.message,
                    retryable=self.retryable, run_id=self.run_id, issues=self.issues)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(json_bytes(value))
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def strict_json(raw: bytes) -> Any:
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate JSON key")
            obj[key] = value
        return obj

    def invalid(_):
        raise ValueError("Non-finite JSON number")

    def walk(value, depth=0):
        if depth > 64:
            raise ValueError("JSON depth exceeded")
        if isinstance(value, dict):
            for key, item in value.items():
                key.encode("utf-8")
                walk(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, depth + 1)
        elif isinstance(value, str):
            value.encode("utf-8")
        elif type(value) in (int, float) and not math.isfinite(value):
            raise ValueError("Non-finite JSON number")

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=invalid)
    walk(value)
    return value


def read_bounded(path: Path, maximum: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected regular artifact file")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise CastingExecutionError("CASTING_OUTPUT_LIMIT", "capacity", "计算产物超过容量上限")
    return raw


def confined_path(root: Path, relative: str) -> Path:
    # Reject Windows absolute/drive/UNC syntax on every platform, plus traversal.
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Invalid resource path")
    path = Path(relative)
    if path.is_absolute() or any(part in ("..", ".") for part in relative.split("/")):
        raise ValueError("Invalid resource path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Resource escapes root")
    return resolved


def verify_engine(assets: Path, manifest: dict[str, Any]) -> Path:
    if manifest["manifest_version"] != 1 or manifest["engine_id"] != "casting-v5.1-cli":
        raise ValueError("Unsupported engine")
    vendor = confined_path(assets, manifest["vendor_root"])
    files = manifest["files"]
    if not isinstance(files, dict) or len(files) != manifest["file_count"] or len(files) != 17:
        raise ValueError("Incomplete engine manifest")
    for name, expected in files.items():
        if digest(read_bounded(confined_path(vendor, name), 1024 * 1024)) != expected:
            raise ValueError("Engine resource hash mismatch")
    computed = digest(json.dumps(files, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    if computed != manifest["content_sha256"]:
        raise ValueError("Engine content hash mismatch")
    return vendor


def admission_issues(report: dict[str, Any]) -> list[dict[str, str]]:
    issues = []
    for item in report.get("issues", [])[:100]:
        path = str(item.get("path", ""))
        # URI-level SHACL failures have no useful user field; retain URI internally.
        if "://" in path or path == "input/rules":
            path = "input"
        elif not path.startswith(("input.", "rules.")):
            path = "input." + path
        issues.append(dict(field_path=path[:256], error_code=str(item.get("code", "Invalid"))[:64],
                           message=str(item.get("message", "输入未通过工程准入"))[:512]))
    return issues
