# 浇冒系统智能设计：第一阶段实现与后续接入契约

日期：2026-09-29。第一阶段为**原程序归档、规则注册、输入契约、独立引擎黄金验收**。

后续进展：第二阶段独立计算层已实现，详见 `casting-design-phase2.md`。本文保留第一阶段的范围及后续接入契约。

## 1. 范围与已确认决策

- 延续本机单用户系统。后续工程附件按会话归属检查；本阶段不增加登录或 `user_id`。
- 项目负责人已明确将附件规则作为当前项目正式规则使用：启用 `PMP-TRIAL-RULES@1`，项目键为 `project-default`。原始规则的来源、版本和“合成项目试验阈值”说明原样保留。
- 后续无新附件时，只复用当前会话最近一次明确选定的有效输入，并在回答中说明来源。
- 所有工程计算由原 Python 程序完成。模型后续只承担意图、工具调用和解释，不写入或覆盖工程参数。
- 本阶段不修改现有 Provider/transport、LangGraph、问答 API、数据库或前端；独立环境中的真实引擎验收不代表聊天工具调用已经上线。

## 2. 文件与职责

下表路径相对于仓库根目录。

| 文件/目录 | 第一阶段内容 |
| --- | --- |
| `backend/app/casting/vendor/v5_1/` | 附件全部 17 个文件，字节不变，包含代码、TTL、示例及原始文档 |
| `backend/app/casting/engine-manifest.json` | 压缩包 SHA256、各文件 SHA256、内容指纹、引擎与验证环境标识 |
| `backend/app/casting/.gitattributes` | 禁止 Git 转换 vendor 和活动规则的换行符，避免 Windows 检出导致哈希失配 |
| `backend/app/casting/rules/registry.json` | 显式项目默认、规则版本、适用范围、启用状态、文件路径、哈希和引擎指纹 |
| `backend/app/casting/rules/project-default/PMP-TRIAL-RULES/1/rules.json` | 当前活动规则；与附件 `rules-v1.json` 字节相同 |
| `backend/app/schemas/casting_design.py` | `CastingDesignInput`、`parse_casting_input()`、结构化输入错误、JSON Schema 导出 |
| `backend/app/casting/schemas/input-v1.schema.json` | 由上述 Pydantic 模型生成的 Draft 2020-12 结构 Schema |
| `backend/requirements-casting.txt` | 独立计算解释器的八项锁定依赖，不加入 Web 后端依赖 |
| `backend/tests/test_casting_contracts.py` | 输入边界、原包/规则哈希、Schema 同步与黄金样例来源校验 |
| `backend/tests/test_casting_engine.py` | 在独立解释器中调用原 `run()`；完整黄金结果比对和失败场景 |
| `backend/tests/fixtures/casting/` | 来自未修改原程序的基准、单位换算、无候选完整输出及输入、来源记录 |
| `backend/pyproject.toml` | 引擎测试 marker、资源打包规则；vendor 作为资源而非 Web Python 模块发现 |
| `.gitignore` | 忽略本地 `backend/.venv-casting/`；测试临时产物沿用 `*.tmp` 规则 |

`app/casting/__init__.py` 不导入引擎。测试通过 `-I -B` 的独立解释器执行，Web 后端不需要安装 rdflib/pyshacl。打包资源包含原有 Python 文件是为了后续独立执行，不是为了在 Web 进程中 import。

## 3. 冻结的计算链

归档中的 `generate_design.py:run(input_path, rules_path, outdir, previous_dir=None, run_id=None)` 按以下顺序执行：

1. `read_json()` 读取输入和规则，创建输出目录。
2. `ontology_runtime.py:admission()` 深拷贝输入，读取本体字段元数据，转换显式单位，检查结构/交叉引用/规则适用性，构建输入 RDF 并执行 SHACL。
3. 准入成功后写 `admission-report.json`；准入失败抛 `AdmissionError.report`，原程序不会把此失败报告写入目录。
4. `generate()` 经 `select_site_sets()`、`size_risers()`、`size_gating()` 生成并筛选候选，按既有出品率、冒口数和策略顺序排序。
5. 可选历史版本处理、可选 `run_id` 候选编号，随后生成 `explanations()`。
6. `export_graph()` 构建结果 RDF、执行 SHACL 并写出图和报告。
7. 图验证通过后写 `recommendation.json`，最后 `write_report()` 写 Markdown 并返回。

保留完整 RDF/SHACL 链。`.owl` 是图的 XML 序列化；实际验证采用 `inference="rdfs"`，不能把它描述为完整 OWL 推理。第一阶段没有拆分计算、图导出和 SHACL。

CLI 支持 `--input --rules --out [--previous-dir]`，没有 `--run-id` 参数；后续 worker 应在隔离进程中 import 后调用 `run(..., run_id=...)`。程序以 `__file__` 定位配套资源，外部输入/输出仍应传绝对路径。原程序固定输出文件名且允许复用已有目录，后续服务必须分配全新独立目录，不能仅凭旧 `recommendation.json` 的存在判断成功。

## 4. 输入契约

`input-v1.json` 在原包中是示例兼类型模板，并非 JSON Schema。新 Schema 定义 Web 接入的结构边界，原 `admission()` 仍是工程准入的最终判断者。

| 字段 | 类型、约束、默认单位 |
| --- | --- |
| `case_id`, `snapshot_id` | 必填非空字符串，最多 128 字符，`[A-Za-z0-9_.:-]+` |
| `material_family`, `manufacturing_process`, `pouring_method` | 必填非空文本，最多 512 字符；与所选规则精确匹配由原 admission 判断 |
| `casting_mass_kg`, `sand_mass_estimate_kg` | 必填有限正数，kg |
| `gating_metal_estimate_kg` | 必填有限非负数，kg，允许零 |
| `main_wall_mm`, `min_wall_mm`, `max_wall_mm`, `effective_head_mm`, `sand_cover_mm` | 必填有限正数，mm；壁厚顺序由原程序在单位换算后校验 |
| `liquid_density_kg_m3` | 必填有限正数，kg/m³ |
| `target_pour_time_s` | 必填有限正数，s |
| `pour_temperature_degC` | 必填有限正数，°C，沿用原 admission 约束 |
| `casting_envelope_mm`, `print_block_mm` | 必填数组，恰好三个有限正数，mm |
| `source_refs` | 必填对象，`material/geometry/process/printer` 四项均为非空文本 |
| `hotspots` | 必填数组，1–16 项；每项 `id`, 正数 `modulus_mm`, 非空 `geometry_ref` |
| `riser_sites` | 必填数组，0–24 项；每项 `id`, `feeds`（1–16 个热节 ID）, 正数 `max_diameter_mm`, `geometry_ref` |
| `ingate_sites` | 必填数组，0–32 项；每项 `id`, 非空 `region`, `geometry_ref` |
| `forbidden_gate_regions` | 必填数组，0–64 个非空文本 |
| `parameter_metadata` | 可省略，最多 64 项；键为原本体支持的参数路径，值为元数据对象 |
| `product_name`, `thermal_curve_status` | 可省略或 null；提供文本时须非空且最多 512 字符。原程序不以它们判定工程证据是否完成 |

元数据对象只接受可省略的 `unit/status/source_ref/missing_reason`；不能用 null 代替省略。`unit` 最多 32 字符，非数值参数的规范单位允许空字符串。状态枚举为 `Confirmed/Derived/Proposed/Missing/Conflicted`；状态在枚举内只说明结构合法，并不代表 admission 通过。参与筛选的参数仍须满足原 SHACL 的确认与来源要求。

支持元数据的路径为：`material_family`、`casting_mass_kg`、三项壁厚、`liquid_density_kg_m3`、`casting_envelope_mm`、`target_pour_time_s`、`effective_head_mm`、`pour_temperature_degC`、`gating_metal_estimate_kg`、`sand_mass_estimate_kg`、`print_block_mm`、`sand_cover_mm`、`riser_sites`、`ingate_sites`、`forbidden_gate_regions`，以及 `hotspots.<实际索引>.modulus_mm/geometry_ref`。

原程序支持 `m→mm`、`cm→mm`、`g→kg`、`min→s`、`g/cm3→kg/m3`。`riser_sites.0.max_diameter_mm` 并不是受支持的元数据路径，不应擅自扩展转换。

`parse_casting_input(raw: bytes)` 另行执行：

- UTF-8（允许 BOM）、文件最大 256 KiB、嵌套最大 16 层。
- 拒绝重复 JSON 键、NaN/Infinity、溢出的数字、无效 Unicode、未知字段，以及用布尔/文本冒充数字。
- 不换单位，不填默认元数据，不改变 int/float 表示；验证后返回原始解析对象。调用方还应保存原始文件字节和 SHA256。
- 结构失败抛 `CastingInputError`，包含 `issues: tuple[CastingInputIssue, ...]`；每项有 `field_path/error_code/message`，最多返回 100 项。

JSON Schema 不能表达字节数、重复键、某个热节索引是否存在或工程规则适用性；不能只依赖浏览器 Schema 校验。原包允许额外字段，新接入契约显式禁止；这是接入层的有意收紧，不修改原计算程序。

## 5. 规则注册与版本边界

`registry.json` 的 `project_defaults` 指向明确的规则键；`entries` 保存项目、可选企业、材料/造型/浇注方式、版本、启用状态、相对路径、内容哈希与引擎内容指纹。当前只有一条活动规则，文件来自原包。

第一阶段只完成注册资产与一致性测试，**尚无运行时规则选择器**。后续服务读取该注册表：按受信任的项目配置选择版本，检查三项适用范围，校验文件路径位于规则根目录内、字节 SHA256 和引擎兼容性。没有适用规则或配置不唯一时返回明确错误；不得偷偷改用 `rules-v1.json`。

规则目录版本不可原地覆盖。未来新材料、工艺、企业版本使用新条目和目录，旧文件保持可审计。当前启用表示项目配置决策，不修改附件内的来源说明。输入文件不允许上传 `rules.json` 或指定服务器路径。

## 6. 黄金样例与已知限制

三个完整黄金输出来自归档前、未修改的附件程序。`provenance.json` 保留源输出哈希及夹具哈希；测试只按 `rule_id` 排序 `admission.rule_selection`（RDF 遍历顺序不稳定），其他工程数值、候选排序、检查、拒绝原因、解释和指纹均完整比较。

| 样例 | 结果 |
| --- | --- |
| 原 `input-v1 + rules-v1` | 4 个候选，推荐 `PMP-S1-C01`；出品率 65.51%，冒口 6 个，冒口金属质量 241.72 kg |
| 质量 630000 g + 元数据 unit=g | 原 admission 转为 630.0 kg，4 个候选，完整输出匹配黄金样例 |
| `riser_sites=[]` | admission 和结果 SHACL 通过；0 个候选、推荐 ID 为 null，保留 `rejected_attempts` |
| 缺少质量、Proposed 状态、非法单位、规则范围不匹配 | 保留结构化 admission 错误，没有发布 recommendation |
| 包内三个变更示例各自独立计算 | v2：3；input-only：3；rules-only：4 个候选 |

已知上游问题：基准目录作为 `previous_dir`，仅变更规则版本时，合并旧 RDF 后出现 17 项 `numberValue` 的 `sh:maxCount` 违反，在当前锁定依赖下抛 ValueError，不生成 recommendation。测试精确断言这一失败及其报告，未使用宽泛 xfail、未修补算法，也未跳过 SHACL。第一版服务契约禁用 `previous_dir`，追问读取已保存结果，需要重算时创建新 run。未来解耦图导出时单独修复和验收该问题。

原候选仍标记 `NeedReview`，真实 CAE 为 `Pending`，有 `pending_evidence`；模型不能把其解释成制造批准。实际输出中的 `model_ref` 是拟议引用，不表示已生成 CAD 文件。

## 7. 计算容量与后续服务契约（尚未实施）

数组上限只是结构边界，不足以阻止组合爆炸。`select_site_sets()` 对经过 `max_hotspots_per_riser` 过滤的 n 个位置枚举到 k 个冒口的组合，复杂度受 `Σ C(n,i), i=1..min(n,k)` 支配；随后每个有效布局乘四种当前选型策略，还会生成解释与 RDF。

当前规则 k=6。即使 n≤24，枚举上界仍为 190050。后续服务在 admission/规则选择后必须检查组合预算，建议第一版限制 20000 个组合、16 个布局×策略尝试；超出返回容量错误，不截断计算后宣称全局最优。所有阈值应为受信任配置，不能由 LLM 放宽。规则目录规模也需受限。

基准进程约 2.35 秒，无候选约 1.25 秒，变更样例约 2.2–2.4 秒。这些是本机样例数据，不能视为最大耗时。后续采用可终止的独立进程，初始执行超时建议 30 秒、输出预算 32 MiB、受控并发；不把计算放到 FastAPI 事件循环。是否保持请求同步等待，应沿用会话当前同步恢复契约，并在压力测试后确定。

后续 `CastingDesignService` 应完成：会话文件归属检查 → 读取原始字节/哈希 → 本契约检查 → 规则选择 → 原 admission → 容量准入 → 新 `run_id` 和独占空目录 → 完整 `run()` → 检查进程成功及结果一致性 → 持久化 → 返回模型摘要。原 admission 可在同一个隔离 worker 中执行；不得在 Web 进程重复引入引擎依赖。

推荐返回摘要方案 B：`status/run_id/result_file_id/recommended_candidate_id/candidate_count/recommended_candidate/other_candidates/rejected_summary/pending_evidence`，完整 recommendation 单独保存。基准完整输出约 112 KB，并重复携带各目录选型解释，直接塞入模型会增加上下文。摘要中工程数值必须从程序输出复制，摘要结构另行版本化；本阶段未实施摘要提取器。

后续错误分类应保留：文件错误；结构错误；`AdmissionError.report`；`no_feasible_candidate`；引擎异常；系统超时/磁盘/服务不可用。`candidates=[]` 是完成了计算的业务结果，不是异常。原 admission 的 `path/code/message` 映射为 `field_path/error_code/message`，同时保留完整原报告。禁止解析异常字符串来代替报告。

## 8. 与现有系统的兼容边界（后续阶段）

`backend/app/rag/conversation_graph.py`、`conversation_state.py` 使用版本化图及仅含引用的状态；`backend/app/api/v1/conversations.py` 有本机访问、请求幂等和错误恢复约束。第一阶段不更改这些文件。

后续对 `backend/app/llm/provider.py`、`openai_chat_transport.py` 增加工具定义发送、调用解析及工具结果回传时，先验证实际配置的 `gpt-4o-mini` 接口；模型能力不能代替本地 transport 的实现验收。工具接入后再扩展图版本，不能直接把 recommendation 或完整消息数组写入现有 Checkpoint，不能绕过现有业务消息发布与 request_id 恢复规则。

工程输入需独立于 RAG 文档入库；后续新增会话工程文件记录与计算 run 记录，继续复用现有会话/turn/message 标识。具体 ORM 与迁移在下一阶段实施前补充设计，迁移执行由用户单独确认。文件存储保留原始输入、规则快照标识、完整结果；运行记录同时保存引擎内容指纹、依赖版本和输入哈希。

本阶段没有注册 HTTP 路由、工具、任务或全局服务，前端仍没有工程 JSON 附件入口。后续最小 UI 是问题加 JSON 附件和 Markdown 回答；方案卡片等另行迭代。

## 9. 本机复现

在 `D:\rag_system\backend` 执行；要求已有后端 `.venv` 和 Python 3.11+，本次实际验证为 Python 3.13.9。

```powershell
.\.venv\Scripts\python.exe -m venv .venv-casting
.\.venv-casting\Scripts\python.exe -m pip install -r requirements-casting.txt

$castingTestRoot = Join-Path (Get-Location) ('.casting-check-' + [guid]::NewGuid().ToString('N') + '.tmp')
New-Item -ItemType Directory -Path $castingTestRoot | Out-Null
.\.venv\Scripts\python.exe -m pytest tests/test_casting_contracts.py tests/test_casting_engine.py tests/test_llm_messages.py tests/test_conversations_schema.py -p no:cacheprovider --basetemp (Join-Path $castingTestRoot 'pytest') -q
```

测试默认寻找 `.venv-casting`，也支持 `CASTING_TEST_PYTHON` 显式指定独立解释器。默认环境缺失会明确 skip 引擎测试；指定了无效解释器则失败。第一阶段验收要求引擎测试全部实际执行，不能以 skip 宣称通过。

测试产物写入新建临时目录，不使用生产存储，不连接数据库，不调用外部模型。`-p no:cacheprovider` 避免本机已有 pytest 缓存权限问题。

回退第一阶段仅需撤回本阶段新增代码/资产及两项构建配置修改，无数据库或现有会话数据迁移。引擎源码及活动规则禁止手工改动；后续升级应新增版本、重做黄金验收和哈希注册。
