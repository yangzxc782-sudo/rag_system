"""Generate and rank preliminary gating/riser layouts from input and rules.

This is a transparent engineering *screening* engine, not a CAD/CAE solver.
No candidate is released automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from decimal import Decimal
from pathlib import Path

from rdflib import Graph, Literal, Namespace, OWL, RDF, XSD
from pyshacl import validate
from config_formats import load_config
from ontology_runtime import admission, build_input_graph, explanations, add_provenance, shacl_report, shapes

ROOT = Path(__file__).resolve().parent
EX = Namespace("https://example.org/casting#")

def read_json(path):
    return load_config(path)

def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def fingerprint(obj):
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def input_facts(inp):
    return {
        "casting_mass_kg": inp["casting_mass_kg"],
        "main_wall_mm": inp["main_wall_mm"],
        "max_wall_mm": inp["max_wall_mm"],
        "min_wall_mm": inp["min_wall_mm"],
        "liquid_density_kg_m3": inp["liquid_density_kg_m3"],
        "target_pour_time_s": inp["target_pour_time_s"],
        "effective_head_mm": inp["effective_head_mm"],
        "gating_metal_estimate_kg": inp["gating_metal_estimate_kg"],
        "sand_mass_estimate_kg": inp["sand_mass_estimate_kg"],
        "hotspot_moduli_mm": {h["id"]: h["modulus_mm"] for h in inp["hotspots"]},
        "riser_sites": [[s["id"], s["feeds"], s["max_diameter_mm"]] for s in inp["riser_sites"]],
        "ingate_sites": [[s["id"], s["region"]] for s in inp["ingate_sites"]],
        "forbidden_gate_regions": inp["forbidden_gate_regions"],
        "print_block_mm": inp["print_block_mm"],
        "sand_cover_mm": inp["sand_cover_mm"],
    }

def changed_input_fields(before, after):
    changed = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if key == "hotspot_moduli_mm" and isinstance(a, dict) and isinstance(b, dict):
            changed.extend(f"{key}.{h}" for h in sorted(set(a) | set(b)) if a.get(h) != b.get(h))
        elif a != b:
            changed.append(key)
    return changed

def require(condition, message):
    if not condition:
        raise ValueError(message)

def validate_contract(inp, rules):
    require(inp["material_family"] == rules["scope_material_family"], "材料不在规则适用范围")
    require(inp["manufacturing_process"] == rules["scope_process"], "造型方式不在规则适用范围")
    require(inp["pouring_method"] == rules["scope_pouring_method"], "浇注方式不在规则适用范围")
    for key in ("casting_mass_kg", "liquid_density_kg_m3", "effective_head_mm",
                "target_pour_time_s", "sand_mass_estimate_kg"):
        require(inp[key] > 0, f"{key} 必须大于 0")
    hotspots = inp["hotspots"]
    ids = [x["id"] for x in hotspots]
    require(ids and len(ids) == len(set(ids)), "热节 ID 必须非空且唯一")
    require(all(h["modulus_mm"] > 0 for h in hotspots), "热节模数必须大于 0")
    known = set(ids)
    for site in inp["riser_sites"]:
        require(site["feeds"] and set(site["feeds"]) <= known,
                f"冒口位置 {site['id']} 的补缩目标无效")
    require(len({x["id"] for x in inp["riser_sites"]}) == len(inp["riser_sites"]), "冒口位置 ID 重复")
    require(len({x["id"] for x in inp["ingate_sites"]}) == len(inp["ingate_sites"]), "内浇口位置 ID 重复")
    require(0 < rules["discharge_coefficient"] <= 1, "流量系数须在 (0,1] 内")
    for key in ("riser_catalog", "sprue_area_catalog_mm2", "ingate_profiles_mm", "runner_profiles_mm"):
        require(rules[key], f"规则目录 {key} 不能为空")

def riser_geometry(item, density):
    r = item["diameter_mm"] / 2
    h = item["height_mm"]
    volume_mm3 = math.pi * r * r * h
    cooling_area_mm2 = 2 * math.pi * r * h + math.pi * r * r
    return {**item, "modulus_mm": volume_mm3 / cooling_area_mm2,
            "metal_mass_kg": volume_mm3 * 1e-9 * density}

def select_site_sets(inp, rules):
    hotspot_ids = {h["id"] for h in inp["hotspots"]}
    allowed = [s for s in inp["riser_sites"] if len(s["feeds"]) <= rules["max_hotspots_per_riser"]]
    max_count = min(rules["max_riser_count"], len(allowed))
    layouts = []
    for count in range(1, max_count + 1):
        for combo in itertools.combinations(allowed, count):
            covered = [h for site in combo for h in site["feeds"]]
            # One deliberate feed assignment per hotspot in this screening model.
            if set(covered) == hotspot_ids and len(covered) == len(hotspot_ids):
                layouts.append(list(combo))
    return layouts

def size_risers(layout, inp, rules, strategy):
    density = inp["liquid_density_kg_m3"]
    catalog = sorted((riser_geometry(x, density) for x in rules["riser_catalog"]),
                     key=lambda x: x["metal_mass_kg"])
    moduli = {h["id"]: h["modulus_mm"] for h in inp["hotspots"]}
    ratio = rules["riser_modulus_ratio"]
    choices = []
    for site in layout:
        required = ratio * max(moduli[h] for h in site["feeds"])
        feasible = [x for x in catalog if x["modulus_mm"] >= required and
                    x["diameter_mm"] <= site["max_diameter_mm"]]
        if not feasible:
            return None, f"{site['id']} 无满足模数和位置尺寸的冒口规格"
        choices.append((site, required, feasible))
    selected = []
    if strategy.startswith("uniform"):
        shared = [x for x in catalog if all(x in opts for _, _, opts in choices)]
        if not shared:
            return None, "无可用于所有补缩位置的统一冒口规格"
        index = 1 if strategy.endswith("plus-one") else 0
        if index >= len(shared):
            return None, "统一冒口没有下一档规格"
        selected = [shared[index]] * len(choices)
    else:
        index = 1 if strategy.endswith("plus-one") else 0
        for _, _, opts in choices:
            if index >= len(opts):
                return None, "局部冒口没有下一档规格"
            selected.append(opts[index])
    result = []
    for (site, required, _), riser in zip(choices, selected):
        result.append({"site_id": site["id"], "site_ref": site["geometry_ref"],
                       "feeds": site["feeds"], "catalog_id": riser["id"],
                       "diameter_mm": riser["diameter_mm"], "height_mm": riser["height_mm"],
                       "modulus_mm": round(riser["modulus_mm"], 3),
                       "required_modulus_mm": round(required, 3),
                       "metal_mass_kg": round(riser["metal_mass_kg"], 3)})
    return result, None

def size_gating(inp, rules, gross_kg):
    rho = inp["liquid_density_kg_m3"]
    time = inp["target_pour_time_s"]
    head_m = inp["effective_head_mm"] / 1000
    cd = rules["discharge_coefficient"]
    min_sprue = gross_kg / (rho * time * cd * math.sqrt(2 * 9.81 * head_m)) * 1e6
    safe_sites = [s for s in inp["ingate_sites"] if s["region"] not in inp["forbidden_gate_regions"]]
    n_gate = rules["ingate_count"]
    if len(safe_sites) < n_gate:
        return None, f"允许布置的内浇口位置只有 {len(safe_sites)} 个，少于规则要求 {n_gate} 个"
    selected_sites = safe_sites[:n_gate]
    for sprue_area in sorted(rules["sprue_area_catalog_mm2"]):
        if sprue_area < min_sprue * rules["min_sprue_area_margin"]:
            continue
        for iw, ih in sorted(rules["ingate_profiles_mm"], key=lambda x: x[0] * x[1]):
            ingate_total = n_gate * iw * ih
            gate_ratio = ingate_total / sprue_area
            if not (rules["ingate_area_ratio_range"][0] <= gate_ratio <= rules["ingate_area_ratio_range"][1] and
                    gate_ratio >= rules["ingate_area_ratio_target"]):
                continue
            for rw, rh in sorted(rules["runner_profiles_mm"], key=lambda x: x[0] * x[1]):
                runner_total = rules["runner_count"] * rw * rh
                runner_ratio = runner_total / sprue_area
                if not (rules["runner_area_ratio_range"][0] <= runner_ratio <= rules["runner_area_ratio_range"][1] and
                        runner_ratio >= rules["runner_area_ratio_target"]):
                    continue
                return {
                    "form": "gravity-bottom-gated",
                    "sprue_throat_area_mm2": sprue_area,
                    "sprue_equivalent_diameter_mm": round(math.sqrt(4 * sprue_area / math.pi), 2),
                    "required_sprue_area_mm2": round(min_sprue, 2),
                    "runner_count": rules["runner_count"],
                    "runner_section_mm": [rw, rh], "runner_total_area_mm2": runner_total,
                    "ingate_count": n_gate, "ingate_section_mm": [iw, ih],
                    "ingate_total_area_mm2": ingate_total,
                    "area_ratio": [1, round(runner_ratio, 3), round(gate_ratio, 3)],
                    "ingates": [{"site_id": x["id"], "region": x["region"],
                                  "geometry_ref": x["geometry_ref"],
                                  "section_mm": [iw, ih]} for x in selected_sites],
                    "pour_time_s": time, "effective_head_mm": inp["effective_head_mm"],
                    "pour_temperature_degC": inp["pour_temperature_degC"],
                    "filter_count": rules["filter_count"], "filter_type": rules["filter_type"],
                    "pouring_basin_count": rules["pouring_basin_count"],
                }, None
    return None, "目录中无同时满足水口流量和截面比的浇道规格"

def generate(inp, rules):
    validate_contract(inp, rules)
    site_sets = select_site_sets(inp, rules)
    rejected = []
    candidates = []
    if not site_sets:
        rejected.append({"stage": "site-selection", "reason": "无覆盖全部热节的许可冒口布置"})
    for layout in site_sets:
        for strategy in rules["riser_sizing_strategies"]:
            risers, error = size_risers(layout, inp, rules, strategy)
            if error:
                rejected.append({"stage": "riser-sizing", "strategy": strategy, "reason": error})
                continue
            gross = inp["casting_mass_kg"] + sum(x["metal_mass_kg"] for x in risers) + inp["gating_metal_estimate_kg"]
            gating, error = size_gating(inp, rules, gross)
            if error:
                rejected.append({"stage": "gating-sizing", "strategy": strategy, "reason": error})
                continue
            yield_fraction = inp["casting_mass_kg"] / gross
            sand_ratio = inp["sand_mass_estimate_kg"] / gross
            checks = {
                "R-RISER-01": all(x["modulus_mm"] + 1e-9 >= x["required_modulus_mm"] for x in risers),
                "R-COVER-01": {h for r in risers for h in r["feeds"]} == {h["id"] for h in inp["hotspots"]},
                "R-FLOW-01": gating["sprue_throat_area_mm2"] >= gating["required_sprue_area_mm2"],
                "R-RATIO-01": rules["runner_area_ratio_range"][0] <= gating["area_ratio"][1] <= rules["runner_area_ratio_range"][1] and
                              rules["ingate_area_ratio_range"][0] <= gating["area_ratio"][2] <= rules["ingate_area_ratio_range"][1],
                "R-YIELD-01": rules["yield_range"][0] <= yield_fraction <= rules["yield_range"][1] and
                              rules["sand_metal_ratio_range"][0] <= sand_ratio <= rules["sand_metal_ratio_range"][1],
                "R-PRINT-01": all(a <= b for a, b in zip(inp["print_block_mm"], rules["max_print_block_mm"])) and
                              inp["sand_cover_mm"] >= rules["min_sand_cover_mm"],
                "R-EXCLUSION-01": all(x["region"] not in inp["forbidden_gate_regions"] for x in gating["ingates"]),
            }
            candidate = {
                "strategy": strategy, "site_ids": [x["site_id"] for x in risers],
                "risers": risers, "riser_count": len(risers), "gating": gating,
                "gross_pour_mass_kg": round(gross, 2),
                "riser_metal_mass_kg": round(sum(x["metal_mass_kg"] for x in risers), 2),
                "gating_metal_estimate_kg": inp["gating_metal_estimate_kg"],
                "yield_percent": round(100 * yield_fraction, 2),
                "sand_metal_ratio": round(sand_ratio, 2),
                "checks": checks, "preliminary_feasible": all(checks.values()),
                "trace": {
                    "input_parameters": ["PD_MaterialRequirement", "PD_CastingMass",
                                         "PD_CastingModulus", "PD_LiquidMetalDensity"],
                    "other_input_fields": ["target_pour_time_s", "effective_head_mm",
                                           "gating_metal_estimate_kg", "print_block_mm",
                                           "riser_sites", "ingate_sites"],
                    "rule_ids": list(checks),
                    "selection_reason": "每处热节按项目模数比从允许位置及规格目录选型；浇道按估算总浇注量与截面目录选型"
                },
                "ontology_status": "NeedsReview", "real_cae": "Pending",
                "used_input_snapshot": inp["snapshot_id"],
                "used_rule_version": rules["version"],
                "pending_evidence": ["真实温度相关热物性曲线", "CAD三维干涉与补缩距离校核",
                                     "充型凝固CAE", "过滤器容量和砂型试制", "客户NDT验收等级确认"],
            }
            if candidate["preliminary_feasible"]:
                candidates.append(candidate)
            else:
                rejected.append({"stage": "hard-check", "strategy": strategy,
                                 "site_ids": candidate["site_ids"],
                                 "failed_rules": [k for k, passed in checks.items() if not passed]})
    # Deduplicate equivalent physical layouts, then rank by highest yield.
    unique = {}
    for c in candidates:
        key = (tuple(c["site_ids"]), tuple(x["catalog_id"] for x in c["risers"]),
               c["gating"]["sprue_throat_area_mm2"])
        unique.setdefault(key, c)
    ranked = sorted(unique.values(), key=lambda c: (-c["yield_percent"], c["riser_count"], c["strategy"]))
    for index, c in enumerate(ranked, 1):
        c["id"] = f"{inp['snapshot_id']}-C{index:02d}"
        c["rank"] = index
        c["model_ref"] = f"cad://proposed/{inp['snapshot_id']}/candidate-{index:02d}"
    return {
        "case_id": inp["case_id"], "snapshot_id": inp["snapshot_id"],
        "rule_set": rules["rule_set_id"], "rule_version": rules["version"],
        "input_fingerprint": fingerprint(inp), "rules_fingerprint": fingerprint(rules),
        "input_facts": input_facts(inp),
        "input_digest": {"casting_mass_kg": inp["casting_mass_kg"],
                         "hotspots": len(inp["hotspots"]), "available_riser_sites": len(inp["riser_sites"]),
                         "available_ingate_sites": len(inp["ingate_sites"])},
        "ranking_basis": "先满足项目硬约束；其后按出品率降序、冒口数量升序排序",
        "recommended_candidate_id": ranked[0]["id"] if ranked else None,
        "candidates": ranked, "rejected_attempts": rejected,
        "generation_boundary": "仅为参数驱动的初步方案生成；未生成可制造CAD实体，未运行真实CAE，未批准发布",
    }

def export_graph(inp, rules, result, outdir, previous_dir=None):
    g = Graph().parse(ROOT / "schema-v5.1.ttl")
    if previous_dir:
        previous_graph = Path(previous_dir) / "knowledge-graph.ttl"
        require(previous_graph.is_file(), "旧版本知识图谱不存在")
        g += Graph().parse(previous_graph)
    g.bind("ex", EX)
    def typ(n, cls): g.add((EX[n], RDF.type, EX[cls]))
    def link(a, p, b): g.add((EX[a], EX[p], EX[b]))
    def lit(a, p, v, dt=None): g.add((EX[a], EX[p], Literal(v, datatype=dt)))
    def val(n, pd, subject, parent, value, kind="numeric", unit=None, status="Confirmed", source=None):
        # A rule-only update reuses the identical input snapshot. Keep its
        # existing RDF values instead of adding a second decimal lexical form.
        if parent.startswith("Snapshot_") and (EX[n], RDF.type, EX.ParameterValue) in g:
            return
        typ(n, "ParameterValue")
        link(n, "parameterDefinition", pd)
        link(n, "aboutEntity", subject)
        link(n, "inSnapshot" if parent.startswith("Snapshot_") else "inCandidate", parent)
        lit(n, "status", status)
        if source: lit(n, "sourceRef", source)
        if kind == "missing": lit(n, "missingReason", str(value))
        elif kind == "numeric":
            lit(n, "numberValue", Decimal(str(value)), XSD.decimal)
            lit(n, "unitSymbol", unit)
        elif kind == "structured": lit(n, "structuredValue", json.dumps(value, ensure_ascii=False))
        elif kind == "geometry": lit(n, "geometryRef", value)
        else: lit(n, "textValue", str(value))
    snap = f"Snapshot_{inp['snapshot_id']}"
    task = f"Task_{inp['snapshot_id']}"
    casting = f"Casting_{inp['case_id']}"
    rule = f"Rule_{rules['rule_set_id']}_v{rules['version']}"
    run_tag = result.get("run_id", inp["snapshot_id"])
    app = f"RuleUse_{run_tag}"
    activity = f"Generation_{run_tag}"
    typ(casting, "Casting")
    typ(snap, "InputSnapshot")
    lit(snap, "recordId", inp["snapshot_id"])
    lit(snap, "materialFamily", inp["material_family"])
    lit(snap, "manufacturingProcess", inp["manufacturing_process"])
    typ(task, "DesignTask")
    link(task, "forCasting", casting)
    link(task, "usesSnapshot", snap)
    req = f"Requirement_{inp['snapshot_id']}"
    typ(req, "DesignRequirement")
    lit(req, "requirementKind", "Objective")
    lit(req, "requirementText", "在项目试验规则约束下生成并排序可评估的浇冒系统方案")
    link(task, "hasRequirement", req)
    input_graph, input_map = build_input_graph(inp)
    # Migrate legacy parameter IDs when a prior graph used the same snapshot.
    # Redirect its dependency edges before merging the canonical input graph.
    for canonical in input_map.values():
        definition = input_graph.value(canonical, EX.parameterDefinition)
        subject = input_graph.value(canonical, EX.aboutEntity)
        for old in list(g.subjects(EX.inSnapshot, EX[snap])):
            if old != canonical and g.value(old, EX.parameterDefinition) == definition and g.value(old, EX.aboutEntity) == subject:
                for a, p in list(g.subject_predicates(old)):
                    g.remove((a, p, old)); g.add((a, p, canonical))
                g.remove((old, None, None))
    g += input_graph
    input_nodes = [str(n).split("#")[-1] for n in input_map.values()]
    thermal = f"PV_{inp['snapshot_id']}_thermal"
    val(thermal, "PD_ThermalProperties", casting, snap,
        "真实温度相关曲线缺失", "missing", status="Missing")
    typ(rule, "EngineeringRule")
    lit(rule, "ruleFamilyId", rules["rule_set_id"])
    lit(rule, "version", rules["version"])
    lit(rule, "status", "Active")
    lit(rule, "ruleKind", "Selection")
    lit(rule, "sourceRef", rules["source_ref"])
    lit(rule, "sourceEdition", rules["source_edition"])
    lit(rule, "sourceClause", "composite-generation-rules")
    lit(rule, "scopeMaterialFamily", rules["scope_material_family"])
    lit(rule, "scopeProcess", rules["scope_process"])
    lit(rule, "evaluatorId", "generate_design.py")
    for pd in ("PD_MaterialRequirement", "PD_CastingMass", "PD_CastingModulus", "PD_LiquidMetalDensity"):
        link(rule, "ruleRequires", pd)
    for pd in ("PD_RiserCount", "PD_SprueArea"):
        link(rule, "ruleProduces", pd)
    typ(app, "RuleApplication")
    link(app, "appliesRule", rule)
    link(app, "checkedSnapshot", snap)
    lit(app, "applicationResult", "Applicable")
    lit(app, "reason", "材料和工艺匹配；必需输入已确认")
    for node in input_nodes: link(app, "applicationInput", node)
    for stage in ("InputPreparation", "RuleSelection", "Generation"):
        n = f"{stage}_{run_tag}"
        typ(n, "DesignActivity")
        lit(n, "activityType", stage)
        lit(n, "status", "Completed" if stage != "Generation" or result["candidates"] else "Planned")
        link(n, "activityTask", task)
    link(f"RuleSelection_{run_tag}", "dependsOn", f"InputPreparation_{run_tag}")
    link(activity, "dependsOn", f"RuleSelection_{run_tag}")
    link(activity, "activityRuleApplication", app)
    for rank, c in enumerate(result["candidates"], 1):
        n = f"Candidate_{c['id']}"
        system = f"System_{c['id']}"
        typ(n, "CandidateDesign")
        typ(system, "GatingRiseringSystem")
        link(n, "generatedBy", activity)
        link(n, "candidateInput", snap)
        link(n, "candidateSystem", system)
        link(n, "candidateAssumption", thermal)
        lit(n, "version", rules["version"])
        lit(n, "status", "NeedsReview")
        lit(n, "modelRef", c["model_ref"])
        lit(n, "reason", f"自动生成；排名 {rank}；真实CAE待运行")
        riser_count = f"PV_{c['id']}_riser_count"
        val(riser_count, "PD_RiserCount", system, n, c["riser_count"], unit="one", status="Proposed")
        link(app, "applicationOutput", riser_count)
        for index, r in enumerate(c["risers"], 1):
            comp = f"Riser_{c['id']}_{index}"
            typ(comp, "SystemComponent")
            lit(comp, "componentType", "Riser")
            link(system, "hasComponent", comp)
            for fed in r["feeds"]: link(comp, "feedsRegion", f"Region_{inp['case_id']}_{fed}")
            val(f"PV_{c['id']}_riser_spec_{index}", "PD_RiserSpecification", comp, n,
                {"diameter_mm": r["diameter_mm"], "height_mm": r["height_mm"]},
                "structured", status="Proposed")
            val(f"PV_{c['id']}_riser_site_{index}", "PD_FeedingLocation", comp, n,
                r["site_ref"], "geometry", status="Proposed")
        gate = c["gating"]
        val(f"PV_{c['id']}_ingate_count", "PD_IngateCount", system, n,
            gate["ingate_count"], unit="one", status="Proposed")
        for index, item in enumerate(gate["ingates"], 1):
            comp = f"Ingate_{c['id']}_{index}"
            typ(comp, "SystemComponent")
            lit(comp, "componentType", "Ingate")
            link(system, "hasComponent", comp)
            val(f"PV_{c['id']}_ingate_area_{index}", "PD_IngateArea", comp, n,
                math.prod(item["section_mm"]), unit="mm2", status="Proposed")
            val(f"PV_{c['id']}_ingate_dim_{index}", "PD_IngateDimensions", comp, n,
                item["section_mm"], "structured", status="Proposed")
            val(f"PV_{c['id']}_ingate_pos_{index}", "PD_IngatePosition", comp, n,
                item["geometry_ref"], "geometry", status="Proposed")
        for index in range(1, gate["runner_count"] + 1):
            comp = f"Runner_{c['id']}_{index}"
            typ(comp, "SystemComponent")
            lit(comp, "componentType", "Runner")
            link(system, "hasComponent", comp)
            val(f"PV_{c['id']}_runner_area_{index}", "PD_RunnerArea", comp, n,
                math.prod(gate["runner_section_mm"]), unit="mm2", status="Proposed")
            val(f"PV_{c['id']}_runner_dim_{index}", "PD_RunnerDimensions", comp, n,
                gate["runner_section_mm"], "structured", status="Proposed")
        sprue = f"Sprue_{c['id']}"
        typ(sprue, "SystemComponent")
        lit(sprue, "componentType", "Sprue")
        link(system, "hasComponent", sprue)
        sprue_val = f"PV_{c['id']}_sprue_area"
        val(sprue_val, "PD_SprueArea", sprue, n,
            gate["sprue_throat_area_mm2"], unit="mm2", status="Proposed")
        val(f"PV_{c['id']}_sprue_dim", "PD_SprueDimensions", sprue, n,
            {"equivalent_diameter_mm": gate["sprue_equivalent_diameter_mm"]},
            "structured", status="Proposed")
        for index in range(1, gate["filter_count"] + 1):
            comp = f"Filter_{c['id']}_{index}"
            typ(comp, "SystemComponent")
            lit(comp, "componentType", "Filter")
            link(system, "hasComponent", comp)
        for index in range(1, gate["pouring_basin_count"] + 1):
            comp = f"Basin_{c['id']}_{index}"
            typ(comp, "SystemComponent")
            lit(comp, "componentType", "PouringBasin")
            link(system, "hasComponent", comp)
        for suffix, pd, raw, kind, unit in (
            ("form", "PD_GatingForm", gate["form"], "text", None),
            ("time", "PD_DesignPouringTime", gate["pour_time_s"], "numeric", "s"),
            ("ratio", "PD_GatingAreaRatio", gate["area_ratio"], "structured", None),
            ("head", "PD_PressureHead", gate["effective_head_mm"], "numeric", "mm"),
            ("temperature", "PD_DesignPouringTemperature", gate["pour_temperature_degC"], "numeric", "degC"),
        ):
            val(f"PV_{c['id']}_{suffix}", pd, system, n, raw, kind, unit, status="Proposed")
        evaluation = f"Evaluation_{c['id']}"
        typ(evaluation, "Evaluation")
        link(evaluation, "evaluatesCandidate", n)
        lit(evaluation, "evaluationKind", "Simulation")
        lit(evaluation, "evaluationBasis", "Illustrative")
        lit(evaluation, "outcome", "Pending")
        lit(evaluation, "modelRef", c["model_ref"])
    if previous_dir:
        previous = read_json(Path(previous_dir) / "recommendation.json")
        old_snap = f"Snapshot_{previous['snapshot_id']}"
        old_rule = f"Rule_{previous['rule_set']}_v{previous['rule_version']}"
        changes = result["incremental_update"]
        if changes["input_changed"]:
            link(snap, "supersedesSnapshot", old_snap)
        if changes["rule_changed"]:
            link(rule, "supersedesRule", old_rule)
            g.set((EX[old_rule], EX.status, Literal("Retired")))
        for mode, before, after, enabled in (
            ("Input", old_snap, snap, changes["input_changed"]),
            ("Rule", old_rule, rule, changes["rule_changed"]),
        ):
            if not enabled:
                continue
            event = f"{mode}Changed_{run_tag}"
            typ(event, "ChangeEvent")
            link(event, f"old{'Snapshot' if mode == 'Input' else 'Rule'}", before)
            link(event, f"new{'Snapshot' if mode == 'Input' else 'Rule'}", after)
            lit(event, "reason", f"{mode} 版本更新后自动识别旧候选依赖")
            if mode == "Input":
                changed = changes["changed_input_fields"]
                mapping = {
                    "casting_mass_kg": "PD_CastingMass",
                    "max_wall_mm": "PD_CastingMaxWall",
                    "main_wall_mm": "PD_CastingMainWall",
                    "min_wall_mm": "PD_CastingMinWall",
                    "liquid_density_kg_m3": "PD_LiquidMetalDensity",
                }
                for field in changed:
                    pd = "PD_CastingModulus" if field.startswith("hotspot_moduli_mm.") else mapping.get(field)
                    if pd: link(event, "changedParameter", pd)
            for old in previous["candidates"]:
                link(event, "affectsCandidate", f"Candidate_{old['id']}")
        if previous["candidates"] and result["candidates"]:
            link(f"Candidate_{result['candidates'][0]['id']}", "supersedesCandidate",
                 f"Candidate_{previous['candidates'][0]['id']}")
    add_provenance(g, inp, rules, result)
    report_data, report_graph = shacl_report(g)
    save_json(outdir / "shacl-report.json", report_data)
    report_graph.serialize(outdir / "shacl-report.ttl", format="turtle")
    require(report_data["conforms"], "本体业务约束不通过: " + report_data["summary"][:1000])
    g.serialize(outdir / "knowledge-graph.ttl", format="turtle")
    g.serialize(outdir / "knowledge-graph.owl", format="xml")
    (outdir / "shacl-report.txt").write_text("PASS", encoding="utf-8")
    return len(g)

def write_report(result, outdir):
    lines = [f"# {result['case_id']} 自动浇冒系统初步方案", "",
             f"输入版：{result['snapshot_id']}；规则版：{result['rule_version']}。",
             f"推荐：{result['recommended_candidate_id'] or '无满足规则的方案'}。",
             "", "| 排名 | 方案 | 冒口数 | 冒口金属 kg | 水口 mm² | 出品率 | 状态 |",
             "|---:|---|---:|---:|---:|---:|---|"]
    for c in result["candidates"]:
        lines.append(f"| {c['rank']} | {c['id']} ({c['strategy']}) | {c['riser_count']} | "
                     f"{c['riser_metal_mass_kg']} | {c['gating']['sprue_throat_area_mm2']} | "
                     f"{c['yield_percent']}% | {c['ontology_status']} |")
    lines += ["", f"被淘汰的生成尝试：{len(result['rejected_attempts'])}。",
              "", "推荐依据：" + result["ranking_basis"] + "。",
              "", "当前仅检查已配置的模数、覆盖、流量、截面比、出品率、砂钢比、打印尺寸与禁布区。",
              "真实热物性曲线、三维CAD干涉与补缩距离、充型凝固CAE、客户验收和试生产均未完成。",
              "本结果不是可直接投产的工艺文件。"]
    if "incremental_update" in result:
        u = result["incremental_update"]
        changed = "输入和规则" if u["input_changed"] and u["rule_changed"] else (
            "输入" if u["input_changed"] else "规则")
        lines += ["", f"增量更新：旧方案 {', '.join(u['affected_old_candidates'])} 受{changed}改版影响；"
                  f"新推荐为 {u['replacement_top_candidate']}。"]
        if u["changed_input_fields"]:
            lines.append("变化的输入字段：" + "、".join(u["changed_input_fields"]) + "。")
    (outdir / "recommendation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def run(input_path, rules_path, outdir, previous_dir=None, run_id=None):
    inp, rules = read_json(input_path), read_json(rules_path)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    inp, admission_report = admission(inp, rules)
    save_json(outdir / "admission-report.json", admission_report)
    result = generate(inp, rules)
    result["admission"] = admission_report
    run_tag = inp["snapshot_id"]
    if previous_dir:
        previous = read_json(Path(previous_dir) / "recommendation.json")
        require(previous["case_id"] == inp["case_id"], "旧结果属于另一个铸件案例")
        require(previous["rule_set"] == rules["rule_set_id"], "规则集 ID 不一致")
        require("input_fingerprint" in previous and "rules_fingerprint" in previous,
                "旧结果缺少版本指纹，请先用当前程序重跑首版")
        input_changed = previous["snapshot_id"] != inp["snapshot_id"]
        rule_changed = previous["rule_version"] != rules["version"]
        if not input_changed:
            require(previous["input_fingerprint"] == result["input_fingerprint"],
                    "输入内容已变但 snapshot_id 未变，请创建新的输入版本 ID")
        if not rule_changed:
            require(previous["rules_fingerprint"] == result["rules_fingerprint"],
                    "规则内容已变但规则 version 未变，请提升规则版本")
        require(input_changed or rule_changed, "输入和规则版本均未变化，无需增量更新")
        if rule_changed and not input_changed:
            run_tag = f"{inp['snapshot_id']}_R{rules['version']}"
            for c in result["candidates"]:
                c["id"] = f"{inp['snapshot_id']}-R{rules['version']}-C{c['rank']:02d}"
                c["model_ref"] = f"cad://proposed/{inp['snapshot_id']}/rule-{rules['version']}/candidate-{c['rank']:02d}"
            result["recommended_candidate_id"] = result["candidates"][0]["id"] if result["candidates"] else None
        result["incremental_update"] = {
            "input_changed": input_changed, "rule_changed": rule_changed,
            "old_snapshot": previous["snapshot_id"], "new_snapshot": inp["snapshot_id"],
            "old_rule_version": previous["rule_version"], "new_rule_version": rules["version"],
            "changed_input_fields": changed_input_fields(previous.get("input_facts", {}), result["input_facts"]) if input_changed else [],
            "affected_old_candidates": [x["id"] for x in previous["candidates"]],
            "replacement_top_candidate": result["recommended_candidate_id"],
        }
    if run_id:
        run_tag = run_id
        for c in result["candidates"]:
            c["id"] = f"{run_id}-C{c['rank']:02d}"
            c["model_ref"] = f"cad://proposed/{run_id}/candidate-{c['rank']:02d}"
        result["recommended_candidate_id"] = result["candidates"][0]["id"] if result["candidates"] else None
    result["run_id"] = run_tag
    explanations(inp, rules, result)
    result["ontology_triples"] = export_graph(inp, rules, result, outdir, previous_dir)
    save_json(outdir / "recommendation.json", result)
    write_report(result, outdir)
    return result

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--rules", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--previous-dir", type=Path)
    args = p.parse_args()
    answer = run(args.input, args.rules, args.out, args.previous_dir)
    print(json.dumps({"recommended": answer["recommended_candidate_id"],
                      "candidates": len(answer["candidates"]),
                      "rejected_attempts": len(answer["rejected_attempts"]),
                      "ontology_triples": answer["ontology_triples"]}, ensure_ascii=False))
