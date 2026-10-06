# 浇冒系统智能设计：第二阶段独立计算服务

日期：2026-09-29。本阶段对应原计划“阶段二：独立计算服务”。

## 实现范围

已实现与数据库无关的同步计算层：规则选择与快照、隔离进程、资源预算、原引擎执行、完成核验及本地审计产物保存。沿用第一阶段 `.venv-casting`，没有新建第二套计算环境。

本阶段没有接入文件上传、MinIO、ORM、HTTP、LangGraph、Provider/transport 或前端。调用者是后端内部代码；第三阶段解析并校验会话所属 `file_id` 后，才能把原始输入字节交给本层。不能把本层直接开放成“输入路径执行”接口。

## 文件级交付

| 路径（相对仓库根目录） | 内容 |
| --- | --- |
| `backend/app/services/casting_engine.py` | `CastingEngine.execute()`、运行目录与快照、执行审计、`verify_completion()`、原始结果返回 |
| `backend/app/services/casting_rules.py` | `CastingRuleSelector.select()`、唯一活动版本、scope 与能力检查、规则和原包哈希核验 |
| `backend/app/casting/engine_worker.py` | 独立 worker、规模预检、`check_budget()`、原 `run()`、完整 admission 错误保存、原子完成标记 |
| `backend/app/casting/process_control.py` | 跨进程计算槽、Windows Job Object、子进程与日志/目录预算、超时及回收 |
| `backend/app/casting/execution_protocol.py` | 标准库共享协议、容量默认值、错误、严格 JSON、文件完整性辅助函数 |
| `backend/app/schemas/casting_execution.py` | 冻结引擎 recommendation 与 candidate 结果结构检查；不重写输出数值 |
| `backend/app/casting/engine-manifest.json` | 补充锁定依赖版本及已验证的 scope；原 17 个文件及内容指纹不变 |
| `backend/tests/test_casting_execution.py` | 真实引擎测试、故障注入、进程回收、并发、规则及结果完整性验收 |

## 内部调用方式

在后端进程中构造服务，配置路径来自服务端，且所有实例应使用同一个绝对工作根目录：

```python
from pathlib import Path
from app.services.casting_engine import CastingEngine

engine = CastingEngine(
    python_executable=Path("D:/rag_system/backend/.venv-casting/Scripts/python.exe"),
    work_root=Path("D:/rag_system/backend/.casting-local.tmp"),
)
result = engine.execute(original_input_bytes, project_key="project-default")
# result.status: success | no_feasible_candidate
# result.recommendation: 原始 recommendation.json 解析结果
# result.result_sha256: 原始 recommendation.json 文件字节哈希
```

示例工作根目录仅用于本机验证；最终存储生命周期、会话权限及 MinIO 对象键由第三阶段定义。构造函数不创建进程、不修改 cwd、不修改 Web 进程 sys.path，也不加载 rdflib/pyshacl。

`run_id` 默认由服务生成 UUID；后续 repository 可传服务端生成的 UUID 与正整数 `execution_no`。同一 `<run_id>/<execution_no>` 目录已存在则拒绝覆盖；失败重试应使用新的执行序号。第一版没有跨 run 自动去重；请求幂等归第三阶段。

## 执行链与运行目录

```text
原始 JSON 字节
  → 第一阶段 parse_casting_input（不改单位或默认元数据）
  → CastingRuleSelector：校验引擎资源、唯一活动规则及 scope
  → 获取该工作根目录的跨进程计算槽
  → 创建全新 UUID/执行序号目录并冻结输入、规则、引擎清单
  → 独立解释器运行 engine_worker
       → 再核验原包、快照与运行依赖
       → 目录规模、组合数与布局×策略预算预检
       → 原 generate_design.run(original files, run_id, previous_dir=None)
       → 原 admission / calculation / screening / RDF / SHACL / JSON / Markdown
       → 原子写 worker-result.json
  → 主进程核验退出码、完成标记、全套产物、哈希、身份及结果结构
  → 原子写 execution.json 终态并返回
```

工作目录为：

```text
<work_root>/.calculation.lock
<work_root>/<run_uuid>/<execution_no>/
    input.json
    rules.json
    engine-manifest.json
    request.json
    execution.json
    runtime.json
    capacity.json
    stdout.log
    stderr.log
    worker-result.json
    admission-error.json       # 仅原 admission 失败时保存
    output/
        admission-report.json
        shacl-report.json / .ttl / .txt
        knowledge-graph.ttl / .owl
        recommendation.json / .md
```

`execution.json` 保留输入/规则/注册表/引擎哈希、规则版本、依赖版本、worker 哈希、时间和终态；成功时另存退出码、进程 ID、推荐 ID、候选数和结果哈希。`runtime.json` 记录实际计算解释器的 Python 与依赖版本。

完整原始 recommendation 保存在工作目录中。`result_file_id`、数据库运行记录及下载权限本阶段尚未建立；本地文件存在不代表对外可下载或业务记录已提交。失败后保留目录便于审计，暂不自动清理。

## 成功发布条件

主进程必须同时满足：

1. 子进程退出码为 0，完成标记版本、UUID、执行序号和输入/规则/引擎清单哈希匹配。
2. 八个预期输出文件全部存在且非空，其字节大小、SHA256 与完成标记一致。
3. 输入、规则和引擎清单快照没有被修改。
4. admission 和结果 SHACL 都为 `conforms=true` 且无问题条目；recommendation 内 admission 与独立报告一致。
5. recommendation 完整结构合法，case/snapshot/rule/run 身份匹配，候选编号和排序号一致、规则检查通过、保留 `NeedsReview/Pending` 及待验证证据。
6. 候选非空时推荐第一个候选；候选为空时推荐必须为 null 且有拒绝记录，返回 `no_feasible_candidate`。
7. 最后原子写入父进程的成功审计记录。调用方仅在方法正常返回后接受结果。

结果模型只用于校验，返回原 JSON 数据，不以模型序列化结果替换工程数值。原 `run()` 后续 Markdown 写入失败时，即使 recommendation 已经存在，也按失败处理。没有直接调用 `generate()` 绕过原执行链。

## 规则和资源边界

- 当前继续启用 `PMP-TRIAL-RULES@1`；保留原始来源、notice 和版本。
- 规则由项目默认配置与输入三项 scope 确定。多个适用启用版本、无匹配版本、内容哈希或元数据冲突均拒绝；不比较版本字符串来猜“最新”。
- 冻结引擎已验证 scope 为 `ZG270-500 / 3d-printed-sand / gravity`。新增规则不能仅改 scope 来假装支持新算法；应同时升级引擎能力清单并验收。
- 输入数组限制沿用第一阶段；规则冒口目录最多 32 项，各浇道目录最多 16 项，策略最多 4 项，规则数量字段另有限制。
- `Σ C(n,k)` 最多 20000；完整覆盖热节的布局数×策略数最多 16。只做与原 `select_site_sets()` 相同覆盖谓词的计数，超预算即拒绝，不裁掉方案后计算。
- 默认执行超时 30 秒；同一工作根目录的所有服务实例/进程最多占用一个计算槽，忙时立即返回可重试错误。不同工作根目录各自独立，部署时应统一配置。
- 整个运行目录默认 32 MiB、最多 64 个目录/文件项，recommendation 默认 4 MiB，stdout/stderr 各最多 1 MiB。
- 日志流按字节截断写入并终止超限 worker；目录容量以 50 ms 轮询并在结束时复核，属于运行预算，不能当作文件系统硬配额。轮询间隔内可能暂时超过目录阈值。

## Windows 进程回收

Windows 下先以 `CREATE_SUSPENDED | CREATE_NO_WINDOW` 创建启动器，加入带 `KILL_ON_JOB_CLOSE` 的 Job Object，再恢复主线程并发送 worker 启动信号。暂停创建避免虚拟环境启动器在加入 Job 之前生成实际 Python 进程。`CREATE_SUSPENDED` 与 `ResumeThread` 的语义采用 [Microsoft 的进程创建标志说明](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags)。

超时或结束时终止整个 Job，等待活动进程数归零，并等待已获取的进程句柄进入退出状态，随后回收管道和日志线程。测试发现“Job 活动数为零”可能先于进程句柄发出退出信号，因此没有只等待启动器或仅检查活动数。

父进程被终止的测试覆盖已启动的 worker 和其子进程；不存在任务窗口。非 Windows 分支使用独立进程组及 killpg，本次 Windows 验收不代表其他操作系统已经验收。

## 错误与恢复

`CastingExecutionError.public_dict()` 只提供 `category/code/message/retryable/run_id/issues`。内部 `run_directory`、traceback 和完整 admission 报告不进入公开错误对象。字段问题保留 `field_path/error_code/message`；原始 path/node/summary 留在 `admission-error.json`。

| 错误码 | 类别 | 处理 |
| --- | --- | --- |
| `CASTING_INPUT_INVALID` | file | JSON 结构、类型、容量或编码错误；附字段问题 |
| `CASTING_RULE_NOT_APPLICABLE`, `CASTING_ENGINE_UNSUPPORTED` | admission | 无适用的已启用规则或已验证引擎能力 |
| `CASTING_ADMISSION_FAILED` | admission | 原 AdmissionError，保存完整报告 |
| `CASTING_CAPACITY_EXCEEDED`, `CASTING_OUTPUT_LIMIT`, `CASTING_LOG_LIMIT` | capacity | 超出规模或产物预算，禁止截断后发布 |
| `CASTING_SHACL_FAILED` | engine | 输出本体验证失败 |
| `CASTING_ENGINE_FAILED` | engine | 原程序内部异常，包括 JSON 写出后续失败 |
| `CASTING_BUSY`, `CASTING_TIMEOUT`, `CASTING_ENGINE_UNAVAILABLE` | system | 槽位、超时或启动问题，允许上层显式重试 |
| `CASTING_ENGINE_ENVIRONMENT` | system | 依赖缺失或版本不匹配，先修复计算环境 |
| `CASTING_STORAGE_ERROR` | system | 文件读写失败；磁盘连审计都写不了时，以抛出的错误为准 |
| `CASTING_RULE_AMBIGUOUS` | system | 规则配置不唯一，先修复配置 |
| `CASTING_RULE_INTEGRITY`, `CASTING_ENGINE_INTEGRITY`, `CASTING_INPUT_INTEGRITY`, `CASTING_OUTPUT_INVALID`, `CASTING_RUN_CONFLICT` | integrity | 拒绝执行或发布，保留已有数据 |
| `CASTING_TERMINATION_FAILED` | system | 未能按时确认进程退出，需检查主机状态 |

本阶段不映射 HTTP 状态码、不自动重试，也不修改会话恢复状态。第三阶段应把本协议接入业务记录，再由后续 LangGraph 阶段消费受控摘要。

## 验收边界

验收分三类记录：真实引擎计算；真实 Windows 进程控制；故障注入。SHACL、内部异常及写出后异常的服务分类使用临时测试 wrapper 注入，未改动 vendor；原附件历史图合并 SHACL 故障仍由第一阶段原样引擎测试覆盖。

当前 `previous_dir` 始终为 None；不修复或启用历史图合并。没有执行真实模型调用、数据库/MinIO 集成、HTTP 或浏览器端到端测试。

最终命令和数量见 `casting-design-phase2-acceptance.md`。
