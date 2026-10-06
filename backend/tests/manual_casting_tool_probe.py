"""Explicit, bounded live protocol probe; never collected by pytest.

Run from backend: .venv/Scripts/python.exe tests/manual_casting_tool_probe.py --run
Uses the configured model/credentials, synthetic questions and made-up IDs only.
Does not access databases, storage or the engine, or change configuration files.
"""
import argparse
import json
import logging
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings
from app.core.errors import BusinessError
from app.llm.configuration import resolve_active_llm_metadata
from app.llm.provider import build_llm_provider
from app.rag.casting_nodes import route_result
from app.rag.casting_prompt import routing_request, tool_result_request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Authorize up to nine configured-model requests")
    args = parser.parse_args()
    if not args.run:
        parser.error("Pass --run to make live model requests")
    # Probe output contains only safe metadata, never endpoint/key/raw errors.
    logging.disable(logging.CRITICAL)
    settings = Settings()
    metadata = resolve_active_llm_metadata(settings)
    field = "llm_remote_supports_tools" if metadata.provider == "api" else "llm_local_supports_tools"
    provider = build_llm_provider(settings.model_copy(update={field: True}))
    fid = "00000000-0000-4000-8000-000000000001"
    rid = "00000000-0000-4000-8000-000000000002"
    cases = [
        ("knowledge_without_file", "什么是冒口？", None, "none", [], "rag"),
        ("knowledge_with_file", "冒口有什么作用？只解释概念。", fid, "current_message", [], "rag"),
        ("calculate_with_file", "请根据本次上传的 JSON 计算浇冒系统方案。", fid, "current_message", [], "calculate"),
        ("calculate_without_file", "请计算一个浇冒系统方案。", None, "none", [], "input_required"),
        ("changed_parameters_without_new_file", "把质量改为 500 kg，再重新计算方案。", fid, "session_history", [], "input_required"),
        ("knowledge_about_adjustments", "调整冒口有哪些原则？只解释概念。", fid, "session_history", [], "rag"),
        ("recalculate_unchanged_history", "请用之前的输入重新计算一次浇冒系统方案，参数保持原样。", fid, "session_history", [], "calculate"),
        ("explain_previous", "解释一下刚才推荐方案为什么选这个冒口数量。", fid, "session_history", [{"run_id": rid, "turn_id": fid}], "explain_existing"),
    ]
    results, call_message = [], None
    try:
        for name, question, file_id, source, runs, expected in cases:
            context = dict(question=question, effective_input_file_id=file_id, input_source=source, recent_runs=runs)
            started = perf_counter()
            try:
                response = provider.generate(routing_request(context))
                route = route_result(response.message, context)
                passed = route.route == expected
                if expected == "calculate" and passed:
                    call_message = response.message
                item = dict(case=name, passed=passed, route=route.route,
                            native_tool_calls=len(response.message.tool_calls))
            except BusinessError as exc:
                item = dict(case=name, passed=False, error_code=exc.code)
            item["elapsed_seconds"] = round(perf_counter() - started, 2)
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
        if call_message is not None:
            marker = "PROBE_RESULT_314159"
            try:
                response = provider.generate(tool_result_request("协议验收：仅复述工具结果中的 probe_marker 值。",
                    call_message, {"status": "protocol_probe", "probe_marker": marker}))
                item = dict(case="tool_result_roundtrip", passed=marker in response.text and not response.message.tool_calls)
            except BusinessError as exc:
                item = dict(case="tool_result_roundtrip", passed=False, error_code=exc.code)
        else:
            item = dict(case="tool_result_roundtrip", passed=False, skipped="native_call_missing")
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    finally:
        provider.close()
    report = dict(configured_provider=metadata.provider, configured_model=metadata.model,
                  passed=sum(x["passed"] for x in results), total=len(results), results=results)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if all(x["passed"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
