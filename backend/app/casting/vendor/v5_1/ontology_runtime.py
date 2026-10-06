"""Ontology admission, field metadata and execution provenance shared by CLI/API."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import re
from pathlib import Path
from functools import lru_cache
from rdflib import Graph, Literal, Namespace, RDF, RDFS, XSD
from rdflib.plugins.sparql import prepareQuery
from pyshacl import validate

ROOT = Path(__file__).resolve().parent
EX = Namespace("https://example.org/casting#")
SH = Namespace("http://www.w3.org/ns/shacl#")


@lru_cache(maxsize=128)
def prepared_query(text, namespaces):
    return prepareQuery(text, initNs=dict(namespaces))


class ValidationGraph(Graph):
    """Reuse parsed SHACL queries; focus bindings remain unique to each call."""
    def query(self, query_object, *args, **kwargs):
        if isinstance(query_object, str):
            namespaces = kwargs.get('initNs') or dict(self.namespaces())
            query_object = prepared_query(query_object, tuple(sorted(namespaces.items())))
        return super().query(query_object, *args, **kwargs)


class AdmissionError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("；".join(x["path"] + ": " + x["message"] for x in report["issues"][:8]))


def schema():
    return Graph().parse(ROOT / "schema-v5.1.ttl") + Graph().parse(ROOT / "ontology-inputs.ttl")


def shapes():
    return Graph().parse(ROOT / "shapes.ttl") + Graph().parse(ROOT / "ontology-inputs.ttl")


def metadata():
    g = schema()
    fields = []
    for pd, _, path in g.triples((None, EX.inputPath, None)):
        def prop(name):
            return str(g.value(pd, EX[name]) or "")
        fields.append(dict(path=str(path), definition=str(pd), label=str(g.value(pd, RDFS.label)),
                           kind=prop("parameterKind"), subject=prop("subjectKind"),
                           quantity=prop("quantityKind"), unit=prop("canonicalUnit"),
                           source_group=prop("sourceGroup"), required=True))
    return sorted(fields, key=lambda x: x["path"])


def issue(path, message, code="Invalid", node=None):
    return dict(path=path, message=message, code=code, severity="Violation", node=node)


def shacl_report(graph):
    working = ValidationGraph()
    working += graph
    for prefix, namespace in graph.namespaces(): working.bind(prefix, namespace)
    ok, report, summary = validate(working, shacl_graph=shapes(), inference="rdfs", inplace=True)
    items = []
    for r in report.subjects(RDF.type, SH.ValidationResult):
        node = report.value(r, SH.focusNode)
        path = graph.value(node, EX.fieldPath) or report.value(r, SH.resultPath) or node
        items.append(issue(str(path), str(report.value(r, SH.resultMessage)),
                           "SHACL", str(node)))
    return dict(conforms=bool(ok), issues=items, summary=str(summary)), report


def walk_type(value, example, path, issues):
    if isinstance(example, dict):
        if not isinstance(value, dict):
            issues.append(issue(path, "需要对象")); return
        for key, sample in example.items():
            if key in ("note", "notice", "product_name", "thermal_curve_status"):
                continue
            if key not in value:
                issues.append(issue(path + "." + key, "缺少必需字段", "Missing"))
            else:
                walk_type(value[key], sample, path + "." + key, issues)
    elif isinstance(example, list):
        if not isinstance(value, list):
            issues.append(issue(path, "需要数组")); return
        if path.endswith(("range", "profiles_mm", "casting_envelope_mm", "print_block_mm", "max_print_block_mm")):
            if path.endswith("range") and len(value) != 2:
                issues.append(issue(path, "范围必须包含两个数"))
            if path.endswith(("casting_envelope_mm", "print_block_mm", "max_print_block_mm")) and len(value) != 3:
                issues.append(issue(path, "尺寸必须包含三个数"))
        for i, item in enumerate(value):
            walk_type(item, example[0], f"{path}.{i}", issues)
            if path.endswith("profiles_mm") and isinstance(item, list) and len(item) != 2:
                issues.append(issue(f"{path}.{i}", "截面必须包含宽、高两个数"))
    elif isinstance(example, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            issues.append(issue(path, "需要有限数值"))
        elif value < 0 or (value == 0 and not path.endswith("gating_metal_estimate_kg")):
            issues.append(issue(path, "数值必须大于零（浇道估算质量允许为零）"))
    elif not isinstance(value, str) or not value.strip():
        issues.append(issue(path, "需要非空文本"))


def admission(raw, rules):
    inp = deepcopy(raw)
    issues, conversions = [], []
    if not isinstance(inp, dict) or not isinstance(rules, dict):
        raise AdmissionError(dict(conforms=False, issues=[issue("input/rules", "输入和规则必须是对象")]))
    # Optional per-field metadata uses JSON paths, e.g. hotspots.2.modulus_mm.
    metas = inp.get("parameter_metadata", {})
    if not isinstance(metas, dict):
        issues.append(issue("parameter_metadata", "需要对象")); metas = {}
    expanded = []
    for field in metadata():
        paths = [field["path"]]
        if "*" in field["path"]:
            hs = inp.get("hotspots", [])
            paths = [field["path"].replace("*", str(i)) for i in range(len(hs))] if isinstance(hs, list) else []
        expanded.extend((path, field) for path in paths)
    bindings = dict(expanded)
    factors = {("m", "mm"): 1000, ("cm", "mm"): 10, ("g", "kg"): .001,
               ("min", "s"): 60, ("g/cm3", "kg/m3"): 1000}
    for path, meta in metas.items():
        if path not in bindings or not isinstance(meta, dict):
            issues.append(issue("parameter_metadata." + path, "未知参数路径或元数据格式错误")); continue
        canonical = bindings[path]["unit"]
        unit = meta.get("unit", canonical)
        if unit != canonical:
            factor = factors.get((unit, canonical))
            if factor is None:
                issues.append(issue(path, f"单位 {unit} 与 {canonical} 不兼容或尚不支持", "Unit")); continue
            try:
                parent = inp
                parts = path.split(".")
                for p in parts[:-1]:
                    parent = parent[int(p)] if isinstance(parent, list) else parent[p]
                key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
                before = deepcopy(parent[key])
                vals = before if isinstance(before, list) else [before]
                if any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in vals):
                    raise ValueError()
                parent[key] = [v * factor for v in vals] if isinstance(before, list) else before * factor
                conversions.append(dict(path=path, original_value=before, original_unit=unit,
                                        value=parent[key], unit=canonical))
                meta["unit"] = canonical
            except (KeyError, IndexError, TypeError, ValueError):
                issues.append(issue(path, "无法对该参数执行单位换算"))
    for name, obj in (("input", inp), ("rules", rules)):
        walk_type(obj, json.loads((ROOT / ("input-v1.json" if name == "input" else "rules-v1.json")).read_text(encoding="utf8")), name, issues)
    if issues:
        raise AdmissionError(dict(conforms=False, issues=issues, conversions=conversions))
    for key in ("case_id", "snapshot_id"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", inp[key]):
            issues.append(issue("input." + key, "标识只允许字母、数字及 _ . : -"))
    for key in ("rule_set_id", "version"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", rules[key]):
            issues.append(issue("rules." + key, "标识只允许字母、数字及 _ . : -"))
    for name, values in [("hotspots", inp["hotspots"]), ("riser_sites", inp["riser_sites"]),
                         ("ingate_sites", inp["ingate_sites"]), ("riser_catalog", rules["riser_catalog"])]:
        ids = [v["id"] for v in values]
        if len(ids) != len(set(ids)):
            issues.append(issue(name, "同一对象标识存在多个记录，不能确定唯一值", "Conflicted"))
        for i, ident in enumerate(ids):
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", ident):
                issues.append(issue(f"{name}.{i}.id", "对象标识含非法字符"))
    for k in ("ingate_count", "runner_count", "max_riser_count", "max_hotspots_per_riser", "filter_count", "pouring_basin_count"):
        if isinstance(rules[k], bool) or not isinstance(rules[k], int):
            issues.append(issue("rules." + k, "数量必须为正整数"))
    for k in ("yield_range", "sand_metal_ratio_range", "runner_area_ratio_range", "ingate_area_ratio_range"):
        if rules[k][0] > rules[k][1]:
            issues.append(issue("rules." + k, "范围下限不能大于上限"))
    if not inp["min_wall_mm"] <= inp["main_wall_mm"] <= inp["max_wall_mm"]:
        issues.append(issue("input.main_wall_mm", "壁厚应满足 最小 ≤ 主体 ≤ 最大"))
    if not rules["riser_sizing_strategies"] or set(rules["riser_sizing_strategies"]) - {"local-min", "uniform", "local-plus-one", "uniform-plus-one"}:
        issues.append(issue("rules.riser_sizing_strategies", "未知或空选型策略"))
    if not inp['hotspots']:
        issues.append(issue('input.hotspots', '至少需要一个热节', 'Missing'))
    for key in ('riser_catalog', 'sprue_area_catalog_mm2', 'ingate_profiles_mm', 'runner_profiles_mm'):
        if not rules[key]: issues.append(issue('rules.' + key, '选型目录不能为空', 'Missing'))
    if rules['discharge_coefficient'] > 1:
        issues.append(issue('rules.discharge_coefficient', '流量系数须在 (0, 1] 内'))
    if rules['min_sprue_area_margin'] < 1:
        issues.append(issue('rules.min_sprue_area_margin', '面积裕量不能小于 1'))
    if rules['yield_range'][1] > 1:
        issues.append(issue('rules.yield_range', '出品率上限不能大于 1'))
    if rules.get('status', 'Active') != 'Active':
        issues.append(issue('rules.status', '只有 Active 规则可以用于生成', 'Excluded'))
    for i, site in enumerate(inp["riser_sites"]):
        if not site["feeds"] or set(site["feeds"]) - {h["id"] for h in inp["hotspots"]}:
            issues.append(issue(f"input.riser_sites.{i}.feeds", "补缩目标必须引用本铸件已登记热节", "Reference"))
    for a, b in (("material_family", "scope_material_family"), ("manufacturing_process", "scope_process"), ("pouring_method", "scope_pouring_method")):
        if inp[a] != rules[b]:
            issues.append(issue("rules." + b, "规则适用范围与输入不匹配", "Excluded"))
    if issues:
        raise AdmissionError(dict(conforms=False, issues=issues, conversions=conversions))
    g, nodes = build_input_graph(inp)
    # Eligibility is evaluated against concrete RDF rules before any calculation.
    registry = rule_registry()
    for rule_id, item in registry.items():
        rule = EX[f"AdmissionRule_{rule_id}"]
        app = EX[f"AdmissionUse_{rule_id}"]
        g.add((rule, RDF.type, EX.EngineeringRule))
        g.add((rule, RDFS.label, Literal(item['label'], lang='zh')))
        for p, value in dict(ruleFamilyId=rule_id, version=rules['version'], status=rules.get('status','Active'),
            ruleKind=item['kind'], sourceRef=rules['source_ref'], sourceEdition=rules['source_edition'],
            scopeMaterialFamily=rules['scope_material_family'], scopeProcess=rules['scope_process'],
            scopePouringMethod=rules['scope_pouring_method'], evaluatorId='generate_design.py:'+rule_id).items():
            g.add((rule, EX[p], Literal(value)))
        for definition in item['definitions']: g.add((rule,EX.ruleRequires,EX[definition]))
        g.add((app,RDF.type,EX.RuleApplication)); g.add((app,EX.appliesRule,rule))
        g.add((app,EX.admissionCheck,Literal(True)))
        g.add((app,EX.checkedSnapshot,EX['Snapshot_'+inp['snapshot_id']]))
        g.add((app,EX.applicationResult,Literal('Applicable'))); g.add((app,EX.reason,Literal('生成前的规则准入判定')))
        for node in nodes.values():
            if str(g.value(node,EX.parameterDefinition)).split('#')[-1] in item['definitions']:
                g.add((app,EX.applicationInput,node))
    report, _ = shacl_report(g)
    report["conversions"] = conversions
    report["rule_selection"] = [dict(rule_id=k, result="Applicable", version=rules["version"],
        reason="材料、造型及浇注方式匹配；RDF 规则所需输入通过 SHACL 准入") for k in registry]
    if not report["conforms"]:
        raise AdmissionError(report)
    return inp, report


def build_input_graph(inp):
    g = schema()
    snap, casting = EX["Snapshot_" + inp["snapshot_id"]], EX["Casting_" + inp["case_id"]]
    system = EX["InputSystem_" + inp["snapshot_id"]]
    for node, cls in ((snap, EX.InputSnapshot), (casting, EX.Casting), (system, EX.GatingRiseringSystem)):
        g.add((node, RDF.type, cls))
    for p, v in ((EX.recordId, inp["snapshot_id"]), (EX.materialFamily, inp["material_family"]), (EX.manufacturingProcess, inp["manufacturing_process"]), (EX.pouringMethod, inp['pouring_method'])):
        g.add((snap, p, Literal(v)))
    g.add((snap, EX.inputSystem, system))
    nodes = {}
    for f in metadata():
        paths = [f["path"]]
        if "*" in f["path"]:
            paths = [f["path"].replace("*", str(i)) for i in range(len(inp["hotspots"]))]
        for path in paths:
            value = inp
            for key in path.split("."):
                value = value[int(key)] if isinstance(value, list) else value[key]
            subject = casting if f["subject"] == "Casting" else system
            if f["subject"] == "CastingRegion":
                h = inp["hotspots"][int(path.split(".")[1])]
                subject = EX[f"Region_{inp['case_id']}_{h['id']}"]
                g.add((subject, RDF.type, EX.CastingRegion)); g.add((subject, EX.regionOf, casting))
            n = EX[f"PV_{inp['snapshot_id']}_{path}"]
            nodes[path] = n
            meta = inp.get("parameter_metadata", {}).get(path, {})
            status = meta.get("status", "Confirmed")
            for p, v in ((RDF.type, EX.ParameterValue), (EX.parameterDefinition, EX[f["definition"].split("#")[-1]]),
                         (EX.aboutEntity, subject), (EX.inSnapshot, snap), (EX.status, Literal(status)),
                         (EX.fieldPath, Literal(path)), (EX.screeningRequired, Literal(True))):
                g.add((n, p, v))
            source = meta.get("source_ref", inp["source_refs"].get(f["source_group"], ""))
            if source: g.add((n, EX.sourceRef, Literal(source)))
            if status == "Missing":
                g.add((n, EX.missingReason, Literal(meta.get("missing_reason", "待补充"))))
            elif f["kind"] == "numeric":
                g.add((n, EX.numberValue, Literal(Decimal(str(value)), datatype=XSD.decimal)))
                g.add((n, EX.unitSymbol, Literal(f["unit"])))
            else:
                prop = {"text": EX.textValue, "structured": EX.structuredValue, "geometry": EX.geometryRef}[f["kind"]]
                g.add((n, prop, Literal(json.dumps(value, ensure_ascii=False) if f["kind"] == "structured" else value)))
    return g, nodes


def rule_registry():
    g = schema() + Graph().parse(ROOT / "rules-screening.ttl")
    registry = {}
    for node, _, ident in g.triples((None, EX.ruleFamilyId, None)):
        definitions = sorted(str(x).split('#')[-1] for x in g.objects(node,EX.ruleRequires))
        registry[str(ident)] = dict(kind=str(g.value(node,EX.ruleKind)), label=str(g.value(node,RDFS.label) or ident), definitions=definitions,
            paths=[str(g.value(EX[d],EX.inputPath)) for d in definitions])
    return registry


def explanations(inp, rules, result):
    from generate_design import riser_geometry
    catalog = sorted((riser_geometry(x, inp["liquid_density_kg_m3"]) for x in rules["riser_catalog"]), key=lambda x: x["metal_mass_kg"])
    for c in result["candidates"]:
        c["explanations"] = []
        for index, r in enumerate(c["risers"], 1):
            site = next(s for s in inp["riser_sites"] if s["id"] == r["site_id"])
            hs = [dict(id=h["id"], modulus_mm=h["modulus_mm"], path=f"hotspots.{i}.modulus_mm") for i, h in enumerate(inp["hotspots"]) if h["id"] in r["feeds"]]
            for h in hs:
                h['source_ref'] = inp.get('parameter_metadata', {}).get(h['path'], {}).get('source_ref', inp['source_refs']['geometry'])
            required = max(h["modulus_mm"] for h in hs) * rules["riser_modulus_ratio"]
            options = []
            for item in catalog:
                reasons = []
                if item["modulus_mm"] < required: reasons.append("模数不足")
                if item["diameter_mm"] > site["max_diameter_mm"]: reasons.append("超过位置直径上限")
                options.append(dict(catalog_id=item["id"], modulus_mm=item["modulus_mm"], diameter_mm=item["diameter_mm"],
                    selected=item["id"] == r["catalog_id"], reasons=reasons,
                    outcome="Excluded" if reasons else "Selected" if item["id"] == r["catalog_id"] else "FeasibleAlternative"))
            c["explanations"].append(dict(kind="riser", rule_id="R-RISER-01", version=rules["version"],
                site_id=r["site_id"], output_node=f"PV_{c['id']}_riser_spec_{index}", inputs=hs,
                required_modulus_mm=required, ratio=rules["riser_modulus_ratio"], max_diameter_mm=site["max_diameter_mm"],
                formula="M_required = ratio × max(M_hotspot); M_catalog = r × h / (2h + r)",
                strategy=c["strategy"], selected=r["catalog_id"], options=options,
                source_ref=rules["source_ref"], source_edition=rules["source_edition"],
                reason={"local-min":"每个位置按金属质量选最小可行规格", "local-plus-one":"每个位置选最小可行规格的下一档", "uniform":"所有位置可共同使用的最小规格", "uniform-plus-one":"所有位置可共同使用的最小规格的下一档"}[c["strategy"]]))
        gross = inp["casting_mass_kg"] + sum(r["metal_mass_kg"] for r in c["risers"]) + inp["gating_metal_estimate_kg"]
        c["explanations"].append(dict(kind="gating", rule_id="R-FLOW-01", version=rules["version"],
            formula="A = G / (rho × t × Cd × sqrt(2 × 9.81 × H_m)) × 1e6",
            inputs=dict(gross_mass_kg=gross, density_kg_m3=inp["liquid_density_kg_m3"], time_s=inp["target_pour_time_s"],
                head_mm=inp["effective_head_mm"], discharge_coefficient=rules["discharge_coefficient"]),
            margin=rules["min_sprue_area_margin"], required_area_mm2=c["gating"]["required_sprue_area_mm2"],
            selected_area_mm2=c["gating"]["sprue_throat_area_mm2"], source_ref=rules["source_ref"], source_edition=rules["source_edition"],
            reason="按直浇道面积、内浇口面积、横浇道面积升序搜索首个满足流量裕量与截面比的组合；总质量包含所选冒口"))


def add_provenance(g, inp, rules, result):
    input_graph, nodes = build_input_graph(inp)
    g += input_graph
    activity = EX["Generation_" + result.get("run_id", inp["snapshot_id"])]
    snapshot = EX["Snapshot_" + inp["snapshot_id"]]
    fields = {f["path"]: f for f in metadata()}
    def lit(n, p, v): g.add((n, EX[p], Literal(v)))
    for c in result["candidates"]:
        for rule_id, item in rule_registry().items():
            patterns = item['paths']
            rule = EX[f"Rule_{rules['rule_set_id']}_{rule_id}_v{rules['version']}"]
            g.add((rule, RDF.type, EX.EngineeringRule))
            g.add((rule, RDFS.label, Literal(item['label'], lang='zh')))
            for key, value in dict(ruleFamilyId=rule_id, version=rules["version"], status="Active",
                ruleKind=item['kind'],
                sourceRef=rules["source_ref"], sourceEdition=rules["source_edition"], sourceClause=rule_id,
                scopeMaterialFamily=rules["scope_material_family"], scopeProcess=rules["scope_process"], scopePouringMethod=rules['scope_pouring_method'], evaluatorId="generate_design.py:"+rule_id,
                ruleConfiguration=json.dumps(rules, ensure_ascii=False)).items(): lit(rule, key, value)
            for pattern in patterns: g.add((rule, EX.ruleRequires, EX[fields[pattern]["definition"].split("#")[-1]]))
            records = [e for e in c["explanations"] if e["rule_id"] == rule_id] or [dict(rule_id=rule_id, passed=c["checks"][rule_id])]
            for index, record in enumerate(records, 1):
                app = EX[f"RuleUse_{c['id']}_{rule_id}_{index}"]
                g.add((app, RDF.type, EX.RuleApplication)); g.add((activity, EX.activityRuleApplication, app))
                g.add((app, EX.appliesRule, rule)); g.add((app, EX.checkedSnapshot, snapshot))
                lit(app, "applicationResult", "Applicable"); lit(app, "reason", json.dumps(record, ensure_ascii=False))
                for path, node in nodes.items():
                    matches = any(re.fullmatch(re.escape(p).replace(r"\*", r"\d+"), path) for p in patterns)
                    if matches and (record.get("kind") != "riser" or not path.startswith("hotspots.") or path in [h["path"] for h in record["inputs"]]):
                        g.add((app, EX.applicationInput, node))
                if rule_id == "R-RISER-01":
                    output = EX[record["output_node"]]
                    g.add((app, EX.applicationOutput, EX[f"PV_{c['id']}_riser_site_{index}"]))
                elif rule_id == "R-FLOW-01":
                    output = EX[f"PV_{c['id']}_sprue_area"]
                    g.add((app, EX.applicationOutput, EX[f"PV_{c['id']}_sprue_dim"]))
                    for j in range(1, len(c["risers"])+1):
                        g.add((app, EX.applicationInput, EX[f"PV_{c['id']}_riser_spec_{j}"]))
                else:
                    output = EX[f"PV_{c['id']}_{rule_id}_check"]
                    g.add((output, RDF.type, EX.ParameterValue)); g.add((output, EX.parameterDefinition, EX.PD_ScreeningResult))
                    g.add((output, EX.aboutEntity, EX["System_" + c["id"]])); g.add((output, EX.inCandidate, EX["Candidate_" + c["id"]]))
                    lit(output, "status", "Derived"); lit(output, "structuredValue", json.dumps(record))
                    if rule_id in ("R-YIELD-01", "R-COVER-01"):
                        for j in range(1, len(c["risers"])+1): g.add((app, EX.applicationInput, EX[f"PV_{c['id']}_riser_spec_{j}"]))
                    if rule_id == "R-RATIO-01":
                        g.add((app, EX.applicationInput, EX[f"PV_{c['id']}_sprue_area"]))
                        g.add((app, EX.applicationOutput, EX[f"PV_{c['id']}_ratio"]))
                        for kind, count in (("ingate", c['gating']['ingate_count']), ("runner", c['gating']['runner_count'])):
                            for j in range(1, count+1):
                                for prop in ('area','dim'):
                                    g.add((app, EX.applicationOutput, EX[f"PV_{c['id']}_{kind}_{prop}_{j}"]))
                    if rule_id == "R-EXCLUSION-01":
                        for j in range(1, c['gating']['ingate_count']+1):
                            g.add((app, EX.applicationOutput, EX[f"PV_{c['id']}_ingate_pos_{j}"]))
                g.add((app, EX.applicationOutput, output))


def graph_view(g):
    """A human-readable projection of RDF. Stable RDF IRIs stay in `id`."""
    def local(term):
        return str(term).rsplit('#', 1)[-1]

    def compact_number(value):
        return format(Decimal(str(value)).normalize(), 'f')

    def structured_summary(definition, raw):
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return str(raw)
        if definition == EX.PD_RiserSpecification and isinstance(value, dict):
            return f"Ø{compact_number(value['diameter_mm'])} × {compact_number(value['height_mm'])} mm"
        if definition == EX.PD_SprueDimensions and isinstance(value, dict):
            return f"等效直径 Ø{compact_number(value['equivalent_diameter_mm'])} mm"
        if definition == EX.PD_GatingAreaRatio and isinstance(value, list):
            return ' : '.join(compact_number(x) for x in value)
        if definition in (EX.PD_IngateDimensions, EX.PD_RunnerDimensions,
                          EX.PD_PartEnvelope, EX.PD_PrintBlock) and isinstance(value, list):
            return ' × '.join(compact_number(x) for x in value) + ' mm'
        if definition in (EX.PD_RiserSites, EX.PD_IngateSites) and isinstance(value, list):
            return f'{len(value)} 个允许位置'
        if definition == EX.PD_ForbiddenRegions and isinstance(value, list):
            return f'{len(value)} 个禁布区域'
        if isinstance(value, (dict, list)):
            return f'{len(value)} 项结构化数据'
        return str(value)

    type_labels = {
        'ParameterValue': '参数值', 'EngineeringRule': '工程规则',
        'RuleApplication': '规则执行', 'CandidateDesign': '候选方案',
        'GatingRiseringSystem': '浇冒系统', 'SystemComponent': '构件',
        'CastingRegion': '热节', 'Casting': '铸件', 'InputSnapshot': '输入版本',
        'DesignActivity': '设计活动', 'DesignTask': '设计任务',
    }
    predicates = {EX.regionOf, EX.feedsRegion, EX.hasComponent, EX.candidateSystem, EX.generatedBy,
                  EX.activityRuleApplication, EX.appliesRule, EX.applicationInput, EX.applicationOutput,
                  EX.aboutEntity, EX.candidateInput, EX.affectsCandidate, EX.supersedesCandidate}
    edges = [dict(source=str(s), target=str(o), relation=str(p).split("#")[-1]) for s,p,o in g if p in predicates]
    ids = {e[k] for e in edges for k in ("source", "target")}
    rule_names = {rule_id: item['label'] for rule_id, item in rule_registry().items()}
    nodes = []
    for n in sorted(set(s for s,p,o in g if str(s) in ids) | set(o for s,p,o in g if str(o) in ids), key=str):
        name = local(n)
        kind = local(g.value(n, RDF.type) or '')
        definition = g.value(n, EX.parameterDefinition)
        field_path = str(g.value(n, EX.fieldPath) or '')
        candidate = g.value(n, EX.inCandidate)
        scope = local(candidate).rsplit('-', 1)[-1] if candidate else ''
        subject = g.value(n, EX.aboutEntity)
        status = str(g.value(n, EX.status) or '')
        source = str(g.value(n, EX.sourceRef) or '')
        unit = str(g.value(n, EX.unitSymbol) or '')
        raw = ''
        value = ''
        label = str(g.value(n, RDFS.label) or name)
        type_label = type_labels.get(kind, kind)
        if kind == 'ParameterValue':
            label = str(g.value(definition, RDFS.label) or local(definition))
            if field_path.startswith('hotspots.'):
                label += f" · {local(subject).rsplit('_', 1)[-1]}"
            elif subject and str(g.value(subject, RDF.type) or '').endswith('#SystemComponent'):
                component = str(g.value(subject, EX.componentType) or '')
                if component == 'Riser':
                    feeds = [local(region).rsplit('_', 1)[-1] for region in g.objects(subject, EX.feedsRegion)]
                    if feeds:
                        label += ' · ' + ', '.join(feeds)
                elif component in ('Ingate', 'Runner', 'Filter', 'PouringBasin'):
                    component_label = {'Ingate': '内浇口', 'Runner': '横浇道',
                                       'Filter': '过滤器', 'PouringBasin': '浇口杯'}[component]
                    label += f" · {component_label} {local(subject).rsplit('_', 1)[-1]}"
            numeric = g.value(n, EX.numberValue)
            text_value = g.value(n, EX.textValue)
            structured = g.value(n, EX.structuredValue)
            geometry = g.value(n, EX.geometryRef)
            if numeric is not None:
                raw = str(numeric)
                display_unit = {'mm2':'mm²', 'degC':'°C', 'one':'个'}.get(unit, unit)
                value = compact_number(numeric) + (f' {display_unit}' if display_unit else '')
            elif text_value is not None:
                raw = str(text_value)
                value = {'gravity-bottom-gated': '重力底注式'}.get(raw, raw)
            elif structured is not None:
                raw = str(structured)
                if definition == EX.PD_ScreeningResult:
                    try:
                        check = json.loads(raw)
                    except ValueError:
                        check = {}
                    rule_id = check.get('rule_id', '')
                    label = rule_names.get(rule_id, rule_id or label)
                    value = '通过' if check.get('passed') is True else '未通过' if check.get('passed') is False else '待判断'
                    type_label = '规则检查'
                else:
                    value = structured_summary(definition, raw)
            elif geometry is not None:
                raw = str(geometry)
                value = 'CAD 定位：' + raw.rstrip('/').rsplit('/', 1)[-1]
            elif status == 'Missing':
                value = '缺失：' + str(g.value(n, EX.missingReason) or '待补充')
        elif kind == 'EngineeringRule':
            rule_id = str(g.value(n, EX.ruleFamilyId) or '')
            label = str(g.value(n, RDFS.label) or rule_names.get(rule_id, rule_id or name))
            value = f"{rule_id} · v{g.value(n, EX.version)}"
            source = str(g.value(n, EX.sourceRef) or '')
        elif kind == 'RuleApplication':
            rule = g.value(n, EX.appliesRule)
            rule_id = str(g.value(rule, EX.ruleFamilyId) or '')
            label = rule_names.get(rule_id, rule_id or '规则执行')
            outcome = str(g.value(n, EX.applicationResult) or '')
            value = {'Applicable':'适用', 'Excluded':'不适用', 'InsufficientInput':'输入不足'}.get(outcome, outcome)
            reason = str(g.value(n, EX.reason) or '')
            try:
                detail = json.loads(reason)
                if isinstance(detail, dict) and detail.get('site_id'):
                    label += ' · ' + detail['site_id']
                    value += ' · ' + detail.get('selected', '')
            except ValueError:
                pass
            raw = reason
        elif kind == 'CandidateDesign':
            label = '候选方案 ' + name.rsplit('-', 1)[-1]
            value = {'NeedsReview':'待工程复核', 'Released':'已发布', 'Superseded':'已被替代'}.get(status, status)
        elif kind == 'SystemComponent':
            component = str(g.value(n, EX.componentType) or '')
            label = {'Riser':'冒口', 'Sprue':'直浇道', 'Runner':'横浇道', 'Ingate':'内浇口',
                     'Filter':'过滤器', 'PouringBasin':'浇口杯', 'RiserNeck':'冒口颈'}.get(component, component or '构件')
            feeds = [local(x).rsplit('_', 1)[-1] for x in g.objects(n, EX.feedsRegion)]
            if feeds:
                label += ' · ' + ', '.join(feeds)
            elif name.rsplit('_', 1)[-1].isdigit():
                label += ' ' + name.rsplit('_', 1)[-1]
            spec = next((x for x in g.subjects(EX.aboutEntity, n) if g.value(x, EX.parameterDefinition) == EX.PD_RiserSpecification), None)
            if spec is not None:
                value = structured_summary(EX.PD_RiserSpecification, str(g.value(spec, EX.structuredValue)))
        elif kind == 'CastingRegion':
            label = '热节 ' + name.rsplit('_', 1)[-1]
        elif kind == 'InputSnapshot':
            label = '输入快照'
            value = str(g.value(n, EX.recordId) or '').split('-sha')[0]
        elif kind == 'GatingRiseringSystem':
            label = '输入系统' if name.startswith('InputSystem_') else '浇冒系统 ' + name.rsplit('-', 1)[-1]
        elif kind == 'Casting':
            label = '铸件 ' + name.removeprefix('Casting_')
        elif kind == 'DesignActivity':
            label = {'Generation':'方案生成', 'RuleSelection':'规则选择', 'InputPreparation':'输入准备'}.get(str(g.value(n, EX.activityType)), '设计活动')
            value = {'Completed':'已完成', 'Planned':'待执行'}.get(status, status)
        nodes.append(dict(id=str(n), type=kind, typeLabel=type_label, label=label, value=value,
                          status=status, source=source, unit=unit, fieldPath=field_path,
                          definition=str(definition or ''), subject=str(subject or ''), rawValue=raw, scope=scope))
    return dict(nodes=nodes, edges=sorted(edges, key=lambda e:(e["source"],e["relation"],e["target"])))
