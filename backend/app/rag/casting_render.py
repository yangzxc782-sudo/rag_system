"""Model selects references; only this deterministic renderer writes engineering facts."""
import html
import json

from app.casting.execution_protocol import digest
from app.schemas.casting_answer import CastingNarrative

RENDERER_VERSION = "casting_facts_v1"


def shown(value) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")), quote=False).replace("`", "&#96;")


def fact_catalog(projection: dict) -> dict[str, str]:
    if projection.get("summary_unavailable"):
        return {"boundary": "完整结果超过摘要预算。本次仅展示运行状态及推荐标识；请下载 recommendation.json 查看全部参数、待补证据和淘汰记录。"}
    status = projection["status"]
    if status not in {"success", "no_feasible_candidate"}:
        error = projection.get("error") or {}
        issues = error.get("issues", [])
        return {"error": "本次计算未生成可用方案。" + html.escape(str(error.get("message", "请检查运行记录。"))) +
                ("\n字段问题：" + shown(issues[:20]) if issues else "") +
                ("；其余字段问题见结构化错误详情。" if projection.get("error_issues_total", len(issues)) > min(20, len(issues)) else "")}
    facts = {"boundary": "计算适用边界：" + shown(projection["generation_boundary"]) + "。"}
    candidate = projection.get("viewed_candidate") or projection.get("recommended_candidate")
    if candidate:
        c = candidate
        facts["candidate"] = (f"当前查看候选 {shown(c['id'])}，程序排序第 {c['rank']}。"
            f"冒口数量为 {c['riser_count']}，冒口金属质量为 {c['riser_metal_mass_kg']} kg，"
            f"总浇注质量为 {c['gross_pour_mass_kg']} kg，出品率为 {c['yield_percent']}%。")
        facts["risers"] = "冒口参数：\n" + "\n".join(
            f"- 位置 {shown(r['site_id'])}，规格 {shown(r['catalog_id'])}：直径 {r['diameter_mm']} mm，高度 {r['height_mm']} mm，"
            f"模数 {r['modulus_mm']} mm，要求模数 {r['required_modulus_mm']} mm，金属质量 {r['metal_mass_kg']} kg；补缩热节 {shown(r['feeds'])}。"
            for r in c["risers"])
        g = c["gating"]
        facts["gating"] = (f"浇道形式为 {shown(g['form'])}；直浇道喉口面积 {g['sprue_throat_area_mm2']} mm²，"
            f"等效直径 {g['sprue_equivalent_diameter_mm']} mm。横浇道 {g['runner_count']} 条，截面 {shown(g['runner_section_mm'])} mm；"
            f"内浇口 {g['ingate_count']} 个，截面 {shown(g['ingate_section_mm'])} mm。浇注时间 {g['pour_time_s']} s，"
            f"有效压头 {g['effective_head_mm']} mm，浇注温度 {g['pour_temperature_degC']} ℃。\n内浇口位置：{shown(g['ingates'])}")
        facts["checks"] = ("程序规则检查：" + shown(c["checks"]) + "。排序依据：" + shown(projection["ranking_basis"]) +
            "。当前本体状态为 " + shown(c["ontology_status"]) + "，真实 CAE 状态为 " + shown(c["real_cae"]) + "。")
        facts["pending"] = "尚需补充的证据：" + shown(c["pending_evidence"]) + "。"
        if projection.get("other_candidates"):
            facts["comparison"] = "其他候选比较（所示为程序返回的摘要）：" + shown(projection["other_candidates"]) + "。"
    if projection.get("rejected_summary"):
        facts["rejected"] = "未通过的尝试及原因（按程序结果归组）：" + shown(projection["rejected_summary"]) + "。"
    if projection.get("admission_conversions"):
        facts["conversions"] = "程序准入时执行的单位转换：" + shown(projection["admission_conversions"]) + "。"
    return facts


def facts_hash(facts: dict) -> str:
    return digest(json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode())


def validate_narrative(value: dict, facts: dict) -> CastingNarrative:
    narrative = CastingNarrative.model_validate(value)
    if not narrative.fact_refs or any(ref not in facts for ref in narrative.fact_refs):
        raise ValueError("Unknown, empty or inapplicable fact references")
    return narrative


def render_answer(info, facts: dict, narrative: CastingNarrative) -> str:
    if info.result_status == "input_required":
        return "请上传或明确选择符合格式的工程输入 JSON。若要修改工程参数，请在 JSON 中修改并重新上传；本次没有执行计算。"
    if info.result_status == "clarify_selection":
        return "请明确要解释哪次计算结果或哪个候选方案。本次没有重新计算。"
    source = "本次使用当前消息明确选定的输入" if not info.input_reused else "本次复用当前会话最近一次明确选定且已通过准入的输入"
    if info.route == "explain_existing":
        source = "本次读取本会话已有计算结果进行解释，没有重新计算；该结果使用的输入"
    paragraphs = [f"{source}：{info.input_file_id}。规则：{shown(info.rule_id)}，版本 {shown(info.rule_version)}。运行编号：{info.run_id}。"]
    if info.result_status == "success":
        paragraphs.append(f"程序共返回 {info.candidate_count} 个可行候选，推荐方案为 {shown(info.recommended_candidate_id)}。")
    elif info.result_status == "no_feasible_candidate":
        paragraphs.append("计算和验证已经完成，但没有方案通过筛选；本次没有推荐方案。")
    # Mandatory safety/identity facts cannot be omitted by model selection.
    refs = list(narrative.fact_refs)
    required = [r for r in ("candidate", "rejected", "pending", "error") if r in facts]
    refs += [r for r in required if r not in refs]
    if any(r not in facts for r in refs):
        raise ValueError("Unknown fact reference")
    paragraphs.extend(facts[r] for r in refs)
    if "boundary" in facts:
        paragraphs.append(facts["boundary"])
    text = "\n\n".join(paragraphs)
    if len(text.encode()) > 65536:
        raise ValueError("Rendered answer exceeds budget")
    return text
