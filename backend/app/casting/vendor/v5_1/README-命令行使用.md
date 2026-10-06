# V5.1 浇冒系统本体：Python 命令行交付包

本包不依赖网页控制台。用户复制并编辑输入和规则 JSON，运行 `generate_design.py`，即可得到初步浇冒系统方案、RDF/OWL 图谱和校验报告。随包数据、规格目录与阈值是**合成试验示例**，使用真实项目时必须替换并确认来源。

## 环境与首次运行

需要 Python 3.11 或更新版本。在解压后的本目录打开终端：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe generate_design.py --input input-v1.json --rules rules-v1.json --out results-v1
```

如果 PowerShell 当前位于压缩包解开的**外层** `casting-v5.1-python-cli` 目录，先运行 `cd .\casting-v5.1-python`，再执行以上命令。`ModuleNotFoundError: No module named 'rdflib'` 表示当前使用的 Python 尚未安装依赖；使用上面创建的 `.venv\Scripts\python.exe` 即可。不要只输入 `python generate_design.py`：主程序必须同时指定 `--input`、`--rules`、`--out`。

macOS/Linux 可将 `.\.venv\Scripts\python.exe` 换成 `.venv/bin/python`。也可使用已安装依赖的 Python 执行同样命令。

首次示例应得到 4 套候选方案，推荐 `PMP-S1-C01`；其 HS-03 冒口为 R190。打开 `results-v1/recommendation.md` 查看排名，在 `recommendation.json` 中查看每处冒口、浇道、规则检查和淘汰原因。

## 配置新项目

1. 复制根目录的 `input-v1.json` 和 `rules-v1.json` 为项目文件，不要直接覆盖这两个文件：准入模块还用它们作为字段结构参照。参照《配置字段说明.md》填入已确认的铸件、热节、允许位置、规格目录和项目规则；`source_refs` 应指向真实资料版本。
2. 新输入使用新的 `snapshot_id`；规则内容修改时使用新的 `version`。规则的材料、工艺和浇注方式适用范围必须与输入匹配。
3. 为每次运行指定**新的** `--out` 目录，以保留旧结果：

```powershell
.\.venv\Scripts\python.exe generate_design.py --input my-input.json --rules my-rules.json --out my-results-001
```

若要比较改版并在图谱中记录旧方案受影响的事实，增加 `--previous-dir`：

```powershell
.\.venv\Scripts\python.exe generate_design.py --input examples/input-v2.json --rules examples/rules-v2.json --out results-v2 --previous-dir results-v1
```

`examples/input-only-change.json` 与 `examples/rules-only-change.json` 分别演示只改输入、只改规则。无需比较时可以省略 `--previous-dir`。输入文件也可使用同结构的 CSV 或 Markdown，但建议先用 JSON；复杂列表在 CSV/Markdown 中仍须写成合法 JSON 值。

## 输出文件

| 文件 | 用途 |
|---|---|
| `recommendation.md` | 人工阅读的方案排名、初筛范围与工程边界 |
| `recommendation.json` | 完整候选、参数、计算解释、硬规则结果及淘汰原因 |
| `admission-report.json` | 生成前的参数准入、规则适用和单位换算结果 |
| `knowledge-graph.ttl` | 本次输入、规则执行、方案与构件的 RDF 图谱 |
| `knowledge-graph.owl` | 同一图谱的 RDF/XML 格式，可在 Protégé 打开 |
| `shacl-report.json/.ttl/.txt` | 生成后图谱的 SHACL 校验报告；`PASS` 是约束通过 |

`schema-v5.1.ttl` 是基础本体；`ontology-inputs.ttl` 描述输入映射；`rules-screening.ttl` 描述初筛规则及依赖；`shapes.ttl` 定义约束。这四个文件必须与三个 Python 脚本放在同一目录。本包没有网页、HTTP 服务或运行归档清单；无需 `design_service.py`。

若无满足规则的方案，`recommendation.json` 中 `recommended_candidate_id` 为 `null`，并在 `rejected_attempts` 说明原因。若参数准入或 SHACL 校验失败，命令会报错，应先纠正输入或规则，不应把不完整输出视为已完成方案。

本程序只进行参数驱动的**初步方案筛排**。冒口颈、真实补缩距离、CAD 干涉、充型凝固 CAE、过滤器承载量和人工审查未自动完成；候选状态维持 `NeedsReview`，不能直接用于投产批准。
