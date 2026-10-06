# 浇冒系统智能设计第二阶段验收记录

日期：2026-09-29。第二阶段：独立计算服务，与数据库无关。

## 交付结果

`CastingEngine.execute()` 已能通过既有独立计算环境执行冻结引擎，选择并保存规则快照、限制计算规模与时间、核验完整输出并保存本地审计记录。相关职责、目录、错误码与调用示例见 `casting-design-phase2.md`。

现有 `.venv-casting` 被复用，没有新增计算虚拟环境。原包 17 个文件与附件 ZIP 再次逐字节比对一致；本轮只在外层新增服务和 worker。

## 验证记录

环境：Windows，Python 3.13.9，Pydantic 2.13.4，pytest 8.4.2，rdflib 7.6.0，pyshacl 0.40.1。

主要回归命令（在 `backend` 下，使用新建的临时 basetemp）：

```powershell
.\.venv\Scripts\python.exe -B -m pytest `
  tests/test_casting_contracts.py `
  tests/test_casting_engine.py `
  tests/test_casting_execution.py `
  tests/test_llm_messages.py `
  tests/test_conversations_schema.py `
  -q -p no:cacheprovider --basetemp <新建临时目录>/pytest --tb=short --durations=10
```

| 批次 | 结果 |
| --- | --- |
| 第一阶段契约/原引擎 + 第二阶段服务 + 现有消息/会话回归 | **156 passed in 49.24s** |
| 随后新增的跨后端进程槽位互斥测试 | **1 passed in 0.85s** |
| 补充非法规则注册结构并复核全部规则选择场景 | **9 passed in 1.13s**（其中 8 项属于前述批次的复核） |

分批共覆盖 **158 个不同用例，0 失败、0 跳过**。第一阶段用例 60 项、第二阶段 41 项、现有消息/会话契约 57 项。尚未执行整个仓库的全量测试。

主要测试产物：`backend/.casting-phase2-acceptance-3d9c89dfdbce49ae9340da4402b60449.tmp/pytest/`。其他边界测试也使用新建的 `.casting-*.tmp` 目录。这些目录均为忽略的本机验证产物。

## 真实引擎验收

- 基准计算通过：4 个候选；推荐冒口数 6、冒口金属质量 241.72 kg、出品率 65.51%。除服务端 run_id 带来的方案编号、CAD 引用和 RDF output_node 标识外，候选所有字段与原始黄金样例一致。
- 单位换算通过：630000 g 由原 admission 转成 630.0 kg，原输入文件字节保持不变。
- 无候选通过：返回 `no_feasible_candidate`，保留拒绝记录和完整 RDF/SHACL 输出。
- 未确认参数通过：捕获完整 `AdmissionError.report`，落盘原报告，公开字段问题不包含内部 traceback。
- 超预算在调用原 `run()` 前拒绝，没有 output 目录，不截断候选后继续运行。
- 规则不适用、禁用、歧义、越界路径、哈希/版本/能力/配置结构错误均被明确拒绝。
- 依赖版本不匹配归系统错误，不接受未经固定环境验证的计算。
- 完成标记缺失、退出码异常、身份/哈希不符、SHACL false、候选排名不符、缺少产物及非有限输出都不能发布。
- 重用已有执行目录被拒绝；新的执行序号及独立 run 使用不同目录，旧目录保留。

## 真实进程与并发验收

- 同一工作根目录并发请求得到 `CASTING_BUSY`，不创建第二个运行目录；前一运行完成后可启动新 run。
- 两个独立后端 Python 进程共享同一计算槽；并发实例不会绕过单进程锁。
- 超时后的实际 Python 进程已退出，服务记录失败并释放槽位。
- stdout 超限与运行目录超限会终止子进程；日志文件不超过配置上限。
- Windows 父进程被终止后，已启动的 worker 与其子进程均退出。测试使用进程句柄确认，不只检查启动器或日志。
- 启动失败可分类，计算槽及句柄按异常路径回收。

测试期间修正了 Windows 生命周期边界：虚拟环境启动器可能生成实际解释器，因此先暂停创建、加入 Job 再恢复；Job 活动进程归零与进程句柄退出信号之间也可能存在短暂间隔，因此等待两者。最终上述场景已通过。

## 故障注入验收

以下分类通过临时 wrapper 注入故障验证，未修改 vendor，不能表述为真实外部事故验收：

- 内部 Python 异常 → `CASTING_ENGINE_FAILED`。
- 写入失败 SHACL 报告后抛错 → `CASTING_SHACL_FAILED`。
- recommendation 已写出、Markdown 后续失败 → 仍为失败，不能把已存在的 JSON 发布成成功。
- 写出阶段 OSError → `CASTING_STORAGE_ERROR`。

原引擎 `previous_dir` 的既有 SHACL 故障依旧由第一阶段真实引擎测试覆盖，第二阶段始终传 None。

## 打包和静态检查

- wheel 构建通过。
- wheel 解包后，17 个 vendor 文件及新增 worker/服务/协议/结果模型与工作区文件字节一致。
- 从解包 wheel 的模块和资源路径执行真实独立引擎通过：4 个候选、推荐出品率 65.51%。
- 新增 Python 的 AST、行尾空白检查与 `git diff --check` 通过。

解包执行产物位于 `backend/.casting-phase2-package.tmp/`。补充非法注册结构处理后重建的最终 wheel 位于 `backend/.casting-phase2-final-package.tmp/`，最终包再次通过 vendor 和新增模块字节校验。

## 保留边界

结果目前保存在本地运行目录；数据库 run 记录、MinIO 对象、file_id/result_file_id、会话权限及查询下载 API 尚未接入。本阶段没有执行数据库迁移、真实模型调用、HTTP/前端端到端验收或部署。

当前容量是受控预算：目录大小通过轮询监测，不能代替磁盘硬配额。30 秒是执行预算，进程退出确认有额外的受限回收等待。没有做最大规模、持续负载或跨操作系统验收。

下一步为原计划第三阶段：会话工程文件、运行记录、存储及查询 API。业务数据库迁移仍需单独确认后执行。
