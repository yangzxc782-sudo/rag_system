"""Isolated worker entry point. Only the parent service may construct its request."""
from __future__ import annotations

import importlib.metadata
import itertools
import math
from pathlib import Path
import platform
import sys
import traceback

# -I deliberately excludes PYTHONPATH and the calling directory. This is child-only.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.casting.execution_protocol import (  # noqa: E402
    OUTPUT_FILES, PROTOCOL_VERSION, CastingExecutionError, ExecutionLimits, admission_issues,
    atomic_json, digest, read_bounded, strict_json, verify_engine,
)


def check_budget(data: dict, rules: dict, limits: ExecutionLimits) -> dict[str, int]:
    def reject():
        raise CastingExecutionError("CASTING_CAPACITY_EXCEEDED", "capacity", "输入与规则组合超过计算容量，请减少位置或规格数量")

    for key, maximum in (("riser_catalog", 32), ("sprue_area_catalog_mm2", 16),
                         ("ingate_profiles_mm", 16), ("runner_profiles_mm", 16), ("riser_sizing_strategies", 4)):
        if not isinstance(rules.get(key), list) or not 1 <= len(rules[key]) <= maximum:
            reject()
    for key, maximum in (("max_riser_count", 24), ("max_hotspots_per_riser", 16), ("ingate_count", 32),
                         ("runner_count", 32), ("filter_count", 32), ("pouring_basin_count", 16)):
        if type(rules.get(key)) is not int or not 1 <= rules[key] <= maximum:
            reject()
    allowed = [site for site in data["riser_sites"] if len(site["feeds"]) <= rules["max_hotspots_per_riser"]]
    count = min(len(allowed), rules["max_riser_count"])
    combinations = sum(math.comb(len(allowed), k) for k in range(1, count + 1))
    if combinations > limits.max_combinations:
        reject()
    # Count exactly the upstream coverage predicate, without computing parameters.
    required = {hotspot["id"] for hotspot in data["hotspots"]}
    layouts = 0
    for k in range(1, count + 1):
        for combo in itertools.combinations(allowed, k):
            covered = [h for site in combo for h in site["feeds"]]
            if set(covered) == required and len(covered) == len(required):
                layouts += 1
                if layouts * len(rules["riser_sizing_strategies"]) > limits.max_attempts:
                    reject()
    return dict(combinations=combinations, layouts=layouts, attempts=layouts * len(rules["riser_sizing_strategies"]))


def execute(directory: Path, request: dict) -> dict:
    limits = ExecutionLimits(**request["limits"])
    manifest_raw = read_bounded(directory / "engine-manifest.json", 64 * 1024)
    try:
        if digest(manifest_raw) != request["engine_manifest_sha256"]:
            raise ValueError("Engine snapshot mismatch")
        manifest = strict_json(manifest_raw)
        vendor = verify_engine(Path(request["assets_root"]), manifest)
    except (KeyError, ValueError) as exc:
        raise CastingExecutionError("CASTING_ENGINE_INTEGRITY", "integrity", "运行时引擎完整性校验失败") from exc
    dependencies = {name: importlib.metadata.version(name) for name in manifest["runtime_dependencies"]}
    if dependencies != manifest["runtime_dependencies"]:
        raise CastingExecutionError("CASTING_ENGINE_ENVIRONMENT", "system", "独立引擎依赖版本不匹配")
    atomic_json(directory / "runtime.json", dict(python=platform.python_version(), dependencies=dependencies,
                                                  worker_sha256=digest(Path(__file__).read_bytes())))
    raw = read_bounded(directory / "input.json", 256 * 1024)
    rule_raw = read_bounded(directory / "rules.json", 256 * 1024)
    if digest(raw) != request["input_sha256"] or digest(rule_raw) != request["rules_sha256"]:
        raise CastingExecutionError("CASTING_INPUT_INTEGRITY", "integrity", "运行快照哈希校验失败")
    data, rules = strict_json(raw), strict_json(rule_raw)
    atomic_json(directory / "capacity.json", check_budget(data, rules, limits))
    sys.path.insert(0, str(vendor))
    from generate_design import run
    from ontology_runtime import AdmissionError
    try:
        # Always pass original files. No pre-normalization, bypass or monkeypatch.
        result = run(directory / "input.json", directory / "rules.json", directory / "output",
                     previous_dir=None, run_id=request["run_id"])
    except AdmissionError as exc:
        atomic_json(directory / "admission-error.json", exc.report)
        raise CastingExecutionError("CASTING_ADMISSION_FAILED", "admission", "输入未通过工程准入",
                                    issues=admission_issues(exc.report)) from exc
    except OSError as exc:
        raise CastingExecutionError("CASTING_STORAGE_ERROR", "system", "计算文件读写失败", retryable=True) from exc
    except Exception as exc:
        report_path = directory / "output/shacl-report.json"
        if report_path.is_file():
            report = strict_json(read_bounded(report_path, limits.max_output_bytes))
            if report.get("conforms") is False:
                raise CastingExecutionError("CASTING_SHACL_FAILED", "engine", "计算结果未通过本体验证") from exc
        raise CastingExecutionError("CASTING_ENGINE_FAILED", "engine", "计算程序执行失败") from exc
    artifacts = {}
    for name in OUTPUT_FILES:
        raw = read_bounded(directory / "output" / name,
                           limits.max_recommendation_bytes if name == "recommendation.json" else limits.max_output_bytes)
        artifacts[name] = dict(sha256=digest(raw), size_bytes=len(raw))
    return dict(status="success" if result["candidates"] else "no_feasible_candidate", artifacts=artifacts)


def main() -> int:
    # Parent first attaches the process to a Job Object, then sends this byte.
    # Parent death before attachment closes stdin; no calculation is started.
    if sys.stdin.buffer.read(1) != b"G":
        return 2
    directory = Path(sys.argv[1]).resolve()
    request = strict_json(read_bounded(directory / "request.json", 64 * 1024))
    if request["protocol_version"] != PROTOCOL_VERSION:
        return 2
    base = dict(protocol_version=PROTOCOL_VERSION, run_id=request["run_id"], execution_no=request["execution_no"],
                input_sha256=request["input_sha256"], rules_sha256=request["rules_sha256"],
                engine_manifest_sha256=request["engine_manifest_sha256"])
    try:
        payload = execute(directory, request)
        atomic_json(directory / "worker-result.json", {**base, **payload})
        return 0
    except Exception as exc:
        traceback.print_exc()  # Parent caps this internal diagnostic stream.
        if isinstance(exc, CastingExecutionError):
            error = exc
        elif isinstance(exc, importlib.metadata.PackageNotFoundError):
            error = CastingExecutionError("CASTING_ENGINE_ENVIRONMENT", "system", "独立引擎缺少运行依赖")
        elif isinstance(exc, OSError):
            error = CastingExecutionError("CASTING_STORAGE_ERROR", "system", "计算文件读写失败", retryable=True)
        else:
            error = CastingExecutionError("CASTING_ENGINE_FAILED", "engine", "计算程序执行失败")
        atomic_json(directory / "worker-result.json", {**base, "status": "failed", "error": error.public_dict()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
