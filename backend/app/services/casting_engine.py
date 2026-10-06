"""Database-independent casting execution. Not an API or a LangGraph tool.

The next layer must resolve session-owned file IDs before passing original bytes
here. All executable, assets and work paths are trusted server configuration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from app.casting.execution_protocol import (
    OUTPUT_FILES, PROTOCOL_VERSION, CastingExecutionError, ExecutionLimits, atomic_json,
    digest, read_bounded, strict_json,
)
from app.casting.process_control import calculation_slot, run_child
from app.schemas.casting_design import CastingInputError, parse_casting_input
from app.schemas.casting_execution import CastingRecommendation
from app.services.casting_rules import CastingRuleSelector, FrozenRules

WORKER = Path(__file__).resolve().parents[1] / "casting/engine_worker.py"


@dataclass(frozen=True)
class CastingExecution:
    run_id: str
    execution_no: int
    status: Literal["success", "no_feasible_candidate"]
    directory: Path
    recommendation: dict[str, Any]
    result_sha256: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _invalid_result() -> CastingExecutionError:
    return CastingExecutionError("CASTING_OUTPUT_INVALID", "integrity", "计算结果或完成标记校验失败")


def verify_completion(directory: Path, request: dict, rules: FrozenRules, data: dict,
                      return_code: int, limits: ExecutionLimits) -> CastingExecution:
    try:
        marker = strict_json(read_bounded(directory / "worker-result.json", 256 * 1024))
        for name in ("protocol_version", "run_id", "execution_no", "input_sha256", "rules_sha256", "engine_manifest_sha256"):
            if type(marker[name]) is not type(request[name]) or marker[name] != request[name]:
                raise _invalid_result()
        if marker["status"] == "failed":
            if return_code != 1:
                raise _invalid_result()
            error = marker["error"]
            allowed_codes = {
                "CASTING_ENGINE_ENVIRONMENT", "CASTING_CAPACITY_EXCEEDED", "CASTING_OUTPUT_LIMIT",
                "CASTING_INPUT_INTEGRITY", "CASTING_ENGINE_INTEGRITY", "CASTING_ADMISSION_FAILED",
                "CASTING_STORAGE_ERROR", "CASTING_SHACL_FAILED", "CASTING_ENGINE_FAILED",
            }
            if (error["code"] not in allowed_codes or error["category"] not in {"admission", "capacity", "integrity", "engine", "system"}
                    or not isinstance(error["message"], str) or len(error["message"]) > 512
                    or not isinstance(error["issues"], list) or len(error["issues"]) > 100 or type(error["retryable"]) is not bool
                    or any(not isinstance(x, dict) or set(x) != {"field_path", "error_code", "message"}
                           or any(not isinstance(value, str) or len(value) > 512 for value in x.values()) for x in error["issues"])):
                raise _invalid_result()
            raise CastingExecutionError(error["code"], error["category"], error["message"],
                                        retryable=error["retryable"], issues=error["issues"])
        if return_code != 0 or marker["status"] not in ("success", "no_feasible_candidate"):
            raise _invalid_result()
        if set(marker["artifacts"]) != set(OUTPUT_FILES):
            raise _invalid_result()
        for name in OUTPUT_FILES:
            maximum = limits.max_recommendation_bytes if name == "recommendation.json" else limits.max_output_bytes
            raw = read_bounded(directory / "output" / name, maximum)
            if not raw or marker["artifacts"][name] != {"sha256": digest(raw), "size_bytes": len(raw)}:
                raise _invalid_result()
        for name, expected in (("input.json", request["input_sha256"]), ("rules.json", request["rules_sha256"]),
                               ("engine-manifest.json", request["engine_manifest_sha256"])):
            if digest(read_bounded(directory / name, 256 * 1024)) != expected:
                raise _invalid_result()
        result_raw = read_bounded(directory / "output/recommendation.json", limits.max_recommendation_bytes)
        result = strict_json(result_raw)
        CastingRecommendation.model_validate(result)  # Validate only; return original JSON values.
        shacl = strict_json(read_bounded(directory / "output/shacl-report.json", limits.max_output_bytes))
        admission = strict_json(read_bounded(directory / "output/admission-report.json", limits.max_output_bytes))
        if (shacl["conforms"] is not True or shacl["issues"] != [] or admission["conforms"] is not True
                or admission["issues"] != [] or result["admission"] != admission
                or result["run_id"] != request["run_id"] or result["case_id"] != data["case_id"]
                or result["snapshot_id"] != data["snapshot_id"] or result["rule_set"] != rules.rule_id
                or result["rule_version"] != rules.version or "incremental_update" in result):
            raise _invalid_result()
        canonical_rules = json.dumps(strict_json(rules.raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if result["rules_fingerprint"] != digest(canonical_rules):
            raise _invalid_result()
        candidates = result["candidates"]
        if not isinstance(candidates, list) or len(candidates) > limits.max_attempts or not isinstance(result["rejected_attempts"], list):
            raise _invalid_result()
        for rank, candidate in enumerate(candidates, 1):
            if (candidate["id"] != f"{request['run_id']}-C{rank:02d}" or type(candidate["rank"]) is not int
                    or candidate["rank"] != rank or candidate["preliminary_feasible"] is not True
                    or not candidate["checks"] or not all(v is True for v in candidate["checks"].values())
                    or candidate["ontology_status"] != "NeedsReview" or candidate["real_cae"] != "Pending"
                    or candidate["used_input_snapshot"] != data["snapshot_id"]
                    or candidate["used_rule_version"] != rules.version
                    or candidate["riser_count"] != len(candidate["risers"])
                    or not isinstance(candidate["risers"], list) or not isinstance(candidate["gating"], dict)
                    or not isinstance(candidate["pending_evidence"], list)):
                raise _invalid_result()
        expected_status = "success" if candidates else "no_feasible_candidate"
        if (marker["status"] != expected_status or result["recommended_candidate_id"] != (candidates[0]["id"] if candidates else None)
                or (not candidates and not result["rejected_attempts"])):
            raise _invalid_result()
        return CastingExecution(request["run_id"], request["execution_no"], expected_status,
                                directory, result, digest(result_raw))
    except CastingExecutionError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise _invalid_result() from exc


class CastingEngine:
    def __init__(self, *, python_executable: Path, work_root: Path,
                 rules: CastingRuleSelector | None = None, limits: ExecutionLimits | None = None):
        self.python_executable = python_executable.resolve()
        self.work_root = work_root.resolve()
        self.rules = rules or CastingRuleSelector()
        self.limits = limits or ExecutionLimits()

    def execute(self, raw_input: bytes, *, project_key: str = "project-default",
                run_id: UUID | None = None, execution_no: int = 1,
                frozen_rules: FrozenRules | None = None) -> CastingExecution:
        """Synchronous internal API; run_id/execution_no are server-owned identities."""
        if run_id is not None and not isinstance(run_id, UUID):
            raise ValueError("run_id must be a server UUID")
        if type(execution_no) is not int or not 1 <= execution_no <= 1000:
            raise ValueError("Invalid execution number")
        try:
            data = parse_casting_input(raw_input)
        except CastingInputError as exc:
            raise CastingExecutionError("CASTING_INPUT_INVALID", "file", "工程输入 JSON 未通过结构检查",
                                        issues=[asdict(issue) for issue in exc.issues]) from exc
        # A persisted run supplies its server-owned immutable snapshot on replay.
        rules = frozen_rules or self.rules.select(data, project_key)
        if not self.python_executable.is_file():
            raise CastingExecutionError("CASTING_ENGINE_UNAVAILABLE", "system", "独立计算解释器不可用", retryable=True)
        identity = str(run_id or uuid4())
        directory = self.work_root / identity / str(execution_no)
        audit = dict(protocol_version=PROTOCOL_VERSION, run_id=identity, execution_no=execution_no,
                     status="prepared", created_at=_now(), input_sha256=digest(raw_input), rules_sha256=rules.sha256,
                     rule_id=rules.rule_id, rule_version=rules.version, project_key=rules.project_key,
                     registry_sha256=rules.registry_sha256, engine_id=rules.engine_manifest["engine_id"],
                     engine_content_sha256=rules.engine_manifest["content_sha256"],
                     runtime_dependencies=rules.engine_manifest["runtime_dependencies"],
                     worker_sha256=digest(WORKER.read_bytes()))
        created = False
        try:
            self.work_root.mkdir(parents=True, exist_ok=True)
            with calculation_slot(self.work_root):
                if not directory.resolve().is_relative_to(self.work_root):
                    raise CastingExecutionError("CASTING_RUN_CONFLICT", "integrity", "运行目录不合法")
                try:
                    directory.mkdir(parents=True, exist_ok=False)
                except FileExistsError as exc:
                    raise CastingExecutionError("CASTING_RUN_CONFLICT", "integrity", "运行目录已存在，禁止覆盖") from exc
                created = True
                (directory / "input.json").write_bytes(raw_input)
                (directory / "rules.json").write_bytes(rules.raw)
                (directory / "engine-manifest.json").write_bytes(rules.engine_manifest_bytes)
                request = dict(protocol_version=PROTOCOL_VERSION, run_id=identity, execution_no=execution_no,
                               input_sha256=audit["input_sha256"], rules_sha256=rules.sha256,
                               engine_manifest_sha256=digest(rules.engine_manifest_bytes),
                               assets_root=str(rules.assets), limits=asdict(self.limits))
                atomic_json(directory / "request.json", request)
                audit.update(status="running", started_at=_now())
                atomic_json(directory / "execution.json", audit)
                return_code, pid = run_child([str(self.python_executable), "-I", "-B", str(WORKER), str(directory)],
                                             directory, self.limits)
                result = verify_completion(directory, request, rules, data, return_code, self.limits)
                audit.update(status=result.status, finished_at=_now(), pid=pid, exit_code=return_code,
                             result_sha256=result.result_sha256, candidate_count=len(result.recommendation["candidates"]),
                             recommended_candidate_id=result.recommendation["recommended_candidate_id"])
                atomic_json(directory / "execution.json", audit)
                return result
        except (CastingExecutionError, OSError) as exc:
            error = exc if isinstance(exc, CastingExecutionError) else CastingExecutionError(
                "CASTING_STORAGE_ERROR", "system", "计算目录或产物写入失败", retryable=True)
            error.run_id, error.run_directory = identity, directory if created else None
            if created:
                audit.update(status="failed", finished_at=_now(), error=error.public_dict())
                try:
                    atomic_json(directory / "execution.json", audit)
                except OSError:
                    pass  # The raised error remains authoritative when the disk cannot record it.
            if error is exc:
                raise
            raise error from exc
