"""Copy-only projection of the persisted engine result. Never design or recalculate."""
from copy import deepcopy
import json

from app.casting.execution_protocol import digest, strict_json
from app.schemas.casting_storage import CastingStorageError

PROJECTION_VERSION = "casting_summary_v1"
MAX_PROJECTION_BYTES = 24 * 1024
COMPARISON_FIELDS = ("id", "rank", "strategy", "riser_count", "riser_metal_mass_kg", "gross_pour_mass_kg",
                     "gating_metal_estimate_kg", "yield_percent", "sand_metal_ratio", "ontology_status", "real_cae")
RECOMMENDED_FIELDS = COMPARISON_FIELDS + ("risers", "gating", "checks", "preliminary_feasible", "pending_evidence",
                                         "used_input_snapshot", "used_rule_version", "site_ids")


def projection_bytes(value: dict) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_PROJECTION_BYTES:
        raise CastingStorageError("CASTING_PROJECTION_TOO_LARGE", "计算结果摘要超过预算，完整结果已保留", status=422, category="capacity")
    return raw


def project_result(service, sid, rid, *, candidate_rank=None) -> dict:
    run = service.get_run(sid, rid)
    result = dict(status=run.status, run_id=str(rid), input_file_id=str(run.input_file_id),
        result_file_id=str(run.result_file_id) if run.result_file_id else None, result_sha256=run.result_sha256,
        rule={"id": run.rule_id, "version": run.rule_version, "sha256": run.rule_sha256},
        projection={"version": PROJECTION_VERSION, "truncated": False})
    if run.status not in {"succeeded", "no_feasible_candidate"}:
        result["error"] = run.error.model_dump(mode="json") if run.error else None
        issues = result["error"]["issues"] if result["error"] else []
        result["error_issues_total"] = len(issues)
        while True:
            try:
                projection_bytes(result)
                return result
            except CastingStorageError as exc:
                if exc.code != "CASTING_PROJECTION_TOO_LARGE" or not issues:
                    raise
                # The full typed error remains in the run and public answer metadata.
                issues = issues[:len(issues) // 2]
                result["error"]["issues"] = issues
                result["projection"]["truncated"] = True
    raw, sha = service.recommendation(sid, rid)
    if sha != run.result_sha256 or digest(raw) != sha:
        raise CastingStorageError("CASTING_OUTPUT_INVALID", "推荐结果与运行记录不一致", category="integrity")
    data = strict_json(raw)
    candidates = data["candidates"]
    recommended = candidates[0] if candidates else None
    if (len(candidates) != run.candidate_count or data["recommended_candidate_id"] != run.recommended_candidate_id
            or (recommended and recommended["id"] != run.recommended_candidate_id)):
        raise CastingStorageError("CASTING_OUTPUT_INVALID", "推荐候选与运行记录不一致", category="integrity")
    rejected = {}
    for attempt in data["rejected_attempts"]:
        group = {key: attempt[key] for key in ("stage", "reason", "failed_rules") if key in attempt}
        key = json.dumps(group, sort_keys=True, ensure_ascii=False)
        if key not in rejected:
            rejected[key] = {**deepcopy(group), "count": 0}
        rejected[key]["count"] += 1
    result.update(status="success" if candidates else "no_feasible_candidate",
        recommended_candidate_id=data["recommended_candidate_id"], candidate_count=len(candidates),
        recommended_candidate={k: deepcopy(recommended[k]) for k in RECOMMENDED_FIELDS} if recommended else None,
        other_candidates=[{k: deepcopy(c[k]) for k in COMPARISON_FIELDS} for c in candidates[1:4]],
        rejected_summary=list(rejected.values()), pending_evidence=deepcopy(recommended["pending_evidence"]) if recommended else [],
        ranking_basis=data["ranking_basis"], generation_boundary=data["generation_boundary"],
        admission_conversions=deepcopy(data["admission"]["conversions"]))
    result["projection"].update(other_candidates_returned=len(result["other_candidates"]),
                               other_candidates_total=max(0, len(candidates) - 1), truncated=len(candidates) > 4)
    if candidate_rank is not None:
        viewed = next((c for c in candidates if c["rank"] == candidate_rank), None)
        if viewed is None:
            raise CastingStorageError("CASTING_CANDIDATE_NOT_FOUND", "该运行没有指定排名的候选", status=422, category="file")
        result["viewed_candidate"] = {k: deepcopy(viewed[k]) for k in RECOMMENDED_FIELDS}
    try:
        projection_bytes(result)
    except CastingStorageError as exc:
        if exc.code != "CASTING_PROJECTION_TOO_LARGE":
            raise
        # Explicit bounded template handoff; original result is still persisted.
        result = {k: result[k] for k in ("status", "run_id", "input_file_id", "result_file_id", "result_sha256",
            "rule", "recommended_candidate_id", "candidate_count")}
        result.update(summary_unavailable=True, rejected_attempt_count=len(data["rejected_attempts"]),
            projection={"version": PROJECTION_VERSION, "truncated": True})
        projection_bytes(result)
    return result
