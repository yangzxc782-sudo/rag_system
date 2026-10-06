# 浇冒系统接入：第三阶段设计

本阶段实现工程文件、运行记录和查询/下载 API；不启用 Provider、LangGraph 工具分支或前端附件按钮。沿用本机单用户及会话归属边界。工程 JSON 不进入 Document、解析、向量、知识库删除清单。

## 数据与迁移

`casting_design_files` 保存 UUID、会话、上传幂等 request_id、原始文件名、大小、SHA256、MinIO 定位和 `pending/ready` 状态。计算产物还绑定 run、execution_no、固定 artifact_name；输入通过 admission 后记录 admission_passed。上传本身只检查结构/预算，不代表工程准入通过。

`casting_design_runs` 保存会话/turn、稳定 call_key、输入 ID/hash、规则和引擎版本/hash、依赖清单 hash、运行状态、结果 ID/hash、摘要、受控错误与时间。完整 recommendation 和审计文件存对象存储；数据库不重复存完整工程结果。run_id 即主键。复合外键保证 turn、输入和结果属于同一会话。

`qa_turns` 增加可空、延迟加载的 graph_version、requested/effective_casting_input_file_id。旧 turn 保持 NULL；普通问答在 0010 数据库上仍可运行。0011 使用增量建表/加列，不改旧数据；downgrade 明确拒绝自动删除，恢复方式为关闭工程开关并保留新表。业务库迁移须另行确认。

## 冻结与恢复

在创建 turn 的同一短事务中冻结文件：显式文件优先，必须为本会话 ready 输入；否则选择历史 turn 最近明确选定且已通过 admission 的输入。只上传未选定的输入不参与回退；显式无效输入报错。重放 request_id 返回原 turn，不能重新选择输入。未带附件的旧 request_fingerprint 保持不变。

计算服务通过服务器持有的 session/turn/effective input 创建运行，call_key 绑定 turn、输入原始 hash、规则 hash、引擎 manifest hash；不依赖模型 tool_call_id。一个 turn 只允许一个工程运行。重试使用该运行冻结的规则与引擎快照，不重新选当前规则。跨会话不复用计算。

文件先提交 pending 元数据，再上传到确定性对象 key 并读回核验 SHA，最后标记 ready。上传请求携带同一 request_id、文件名和原始字节即可补偿 DB/存储间断点；相同 request_id 不同内容返回 409。

运行按 pending → running → persisting → succeeded/no_feasible_candidate；错误区分 admission_failed/engine_failed/timed_out/interrupted。服务对 run 持有独立文件锁，计算仍使用第二阶段全局进程锁，不在 SQL 事务内等待计算或存储。中断后有完整 worker 完成标记则核验并补存，不盲目重算；有不完整运行目录则标记 interrupted，保留审计，需明确后续重试策略。第一次尚未创建计算目录时可继续同一执行。

对象 key：`casting/{session_id}/inputs/{file_id}.json`；`casting/{session_id}/runs/{run_id}/{execution_no}/{artifact_name}`。规则/引擎快照、原始输入、worker 标记、execution/runtime/capacity、日志和 RDF/SHACL/推荐产物均保留。只暴露 recommendation 下载，内部路径/日志不进入 API。

## API 与部署边界

- `POST /api/v1/rag/sessions/{thread_id}/casting-inputs`：multipart `request_id` + `file`；256 KiB 文件预算和有界 multipart 总请求预算。
- `GET .../casting-inputs`：分页列出本会话输入元数据。
- `GET .../casting-runs/{run_id}`：状态、版本、摘要、错误、结果 ID。
- `GET .../casting-runs/{run_id}/recommendation`：核验 SHA/大小后返回原字节下载。

复用 ConversationRoute 的本地 transport/peer 检查。新增 `CASTING_DESIGN_ENABLED=false`；开启要求 conversation_enabled 和已执行 0011。不开启时旧 RAG 继续运行；0011 readiness 同时检查新增表/列。此阶段 HTTP turn 请求带 casting_input_file_id 明确返回 CASTING_TOOL_NOT_READY，避免落入普通 RAG 冒充计算；仓储的冻结能力已实现并测试，第四阶段接入。

错误响应仅包含受控 code/category/message/retryable/field issues；不泄露 SQL、文件路径、对象 key 或 traceback。存储错误 503、跨会话/不存在 404、幂等或未完成冲突 409、格式错误 422、大小超限 413。GET 运行记录正常返回 200，其中的 admission_failed/timed_out 等状态表达计算结局；本阶段没有 HTTP 执行入口。第四阶段工具适配器再映射规则/准入错误及执行超时。无可行方案为正常完成状态。

本阶段终态错误不会通过 execute_turn 自动重算；execution_no 暂固定为 1，为后续显式重算协议预留。内部生成失败的 error.retryable 保留引擎分类，不代表本阶段已经开放 HTTP 重算接口。规则选择在创建 run 前失败时返回受控引擎错误，不伪造规则版本来填运行记录。

## 验收

在已核验的独立 PostgreSQL 测试实例新建专用测试数据库，应用 0010、留存旧会话后升级 0011；不清理测试库。测试文件归属、复合外键、幂等、冻结、并发、上传/结果 DB 提交断点、恢复不重复计算、下载 hash、普通 RAG readiness。真实 MinIO 如不可用必须区分内存存储替身测试，不声称外部存储验收。
