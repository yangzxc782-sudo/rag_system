# 浇冒系统接入第三阶段验收

日期：2026-09-29。范围：工程文件、业务持久化、查询/下载 API；代码已实现并在隔离资源验证，业务库迁移及生产启用未执行。

## 已实现

| 文件 | 主要内容 |
|---|---|
| `backend/app/models/casting_design_file.py` | 会话所属工程输入与产物元数据、上传幂等键、对象 SHA256、pending/ready |
| `backend/app/models/casting_design_run.py` | turn/run 身份、冻结规则/引擎/依赖版本、状态、结果引用和受控错误 |
| `backend/alembic/versions/0011_casting_storage.py` | 两张新表、qa_turns 三个可空列、会话复合外键；增量 DDL，无自动破坏性 downgrade |
| `backend/app/services/casting_repository.py` | 短事务访问、归属校验、幂等、有效历史输入选择 |
| `backend/app/services/casting_files.py` | 独立 MinIO namespace、有界读取、原字节存储、SHA256 校验、上传补偿 |
| `backend/app/services/casting_design.py` | 内部执行编排、稳定运行身份、规则快照、结果/审计落库、恢复时复用完成产物 |
| `backend/app/schemas/casting_storage.py` | 文件列表、运行摘要及安全错误模型 |
| `backend/app/api/v1/casting_design.py` | 上传、分页列出输入、读取运行、下载 recommendation 四个接口；上传总请求上限 320 KiB，JSON 上限 256 KiB |
| `backend/app/services/casting_engine.py` | 增加服务器专用 frozen_rules 参数，恢复不重新选当前规则 |
| `backend/app/services/conversation_repository.py` | 事务内冻结 requested/effective 文件；旧请求 fingerprint 不变 |
| `backend/app/models/qa_turn.py` | 延迟加载的新列及兼容旧 0010 INSERT/SELECT 的默认值处理 |
| `backend/app/schemas/conversations.py` / `backend/app/services/conversations.py` | 预留 casting_input_file_id；第四阶段前明确拒绝带工程附件的聊天执行 |
| `backend/app/api/v1/conversations.py` / `router.py` | 复用本地 transport 边界，保留安全 field issues，注册新路由 |
| `backend/app/core/config.py` / `backend/.env.example` / `backend/app/main.py` | 默认关闭工程开关，服务器路径配置及资源生命周期 |
| `backend/app/db/langgraph.py` | 允许普通 RAG 使用 0010/0011；工程启用要求 0011，SELECT-only readiness |
| `backend/app/services/object_storage.py` | 可选 max_bytes 参数，旧调用保持原行为 |
| `backend/app/models/__init__.py` | 注册工程模型 |

工程 JSON 和结果未写入 Document、chunk、向量、Checkpoint 内容或文档删除清单。无登录/user_id 改动。现有前端、LLM Provider/transport、LangGraph 节点/边、SSE 行为均未扩展。

## 验证结果

以下合计 **224 个不同测试通过**，无未解决失败。各批次存在重叠，合计已去除重复测试。

1. 159 passed，52.21 秒：第一/二阶段计算合同与真实进程测试，以及现有 conversation persistence/API/schema/status/lifespan 回归。
2. 54 passed，75.59 秒：第三阶段初始 19 项 PostgreSQL 测试 + 现有 35 项 M4 PostgreSQL/真实本机 HTTP/多进程锁/恢复测试。
3. 最终第三阶段完整集合 **30 passed，19.78 秒**：5 项离线合同测试 + 25 项隔离 PostgreSQL 测试，包含真实引擎与真实 MinIO 验证。JUnit：`backend/.casting-phase3-final-2d9c5dcda2e741b9b32e58f8acbec7b0.tmp/results.xml`（忽略目录，仅本机验收产物）。

重点覆盖：

- 0010 中已有会话、turn、fingerprint 在升级 0011 后保持；普通 RAG 在两种迁移版本上均可用。
- 开启工程功能但仍在 0010 时启动检查拒绝；完成迁移后的完整 FastAPI lifespan、JSON 上传、普通 RAG 回答及资源关闭通过。
- 同一上传 request_id 的串行/并行请求只产生一个文件，内容变更返回冲突；跨会话文件/运行/下载拒绝；复合外键阻止跨会话写入。
- 未选定上传不参与复用；明确选定且成功通过 admission 的输入可复用；显式无效文件不回退；重放已冻结 turn 不重新选输入。
- 存储写入中断、上传对象写完但 DB 更新前中断，可用原 request_id 补存；运行最终 SQL commit 故障回滚后复用完整计算产物，无第二次引擎调用。
- 已冻结规则不能被新规则替换；同 turn 并发 reserve 返回同一 run；不完整运行目录标记 interrupted，禁止盲目重算。
- 真实计算得到 4 候选，空候选返回 no_feasible_candidate，真实 admission 失败保留 field issues；超时/引擎异常状态通过受控故障注入验证。
- JSON 字节预算、multipart 总预算（含无 Content-Length）、格式错误、扩展名错误、安全错误响应。
- 独立真实 MinIO 新 bucket 保存输入/规则/引擎/RDF/SHACL/日志/推荐全套产物；逐个读回 SHA 相符；recommendation 下载保持原字节；篡改和超限读取被拒绝。
- 普通 RAG 采用已有 synthetic Provider/retrieval 测试设施；**未调用真实 GPT-4o-mini，未进行真实模型 Tool Calling 验收**。

测试发现并修复：延迟列在事务结束后的访问问题；无 Content-Length 上传超限错误被 multipart 解析器改写的问题。Windows 下最初测试参数默认 ID 过长导致临时目录错误，已为参数使用短 ID，最终重跑全部通过。

## 隔离资源

- PostgreSQL：`casting-phase3-test-59b7702eca71`，`127.0.0.1:54145`，专用账号/数据库/cluster 通过 `phase13_support.verified_engine` 三项身份核验；每批测试只创建新的专用测试库。
- MinIO：`casting-phase3-minio-d4a612983e82`，`127.0.0.1:62344`，只创建随机 `casting-test-*` 测试 bucket。
- 两个实例使用本轮生成的临时凭据，无旧凭据读取，无业务库/bucket 操作。没有新建 Python 虚拟环境，复用 backend/.venv 和已有 .venv-casting。
- 早期尝试复用旧测试实例时，自动审批拒绝从 Docker 环境读取密码；该操作未执行，随后改为上述独立实例。验收完成后，两个新测试容器及本轮启动的旧测试容器均已停止；测试数据库、bucket 和容器保留，未删除。

## 尚未执行与下一步

1. **未迁移业务数据库。** AGENTS.md 规定数据库迁移“由用户手动确认后执行”。待单独确认后，才从 `backend` 使用现有环境执行 `python -m alembic upgrade 0011_casting_storage`；当前只是提供可审查的增量迁移。回退采用关闭工程开关并保留新表；不自动删除审计数据。
2. 未修改实际 `.env`，未重启业务后端，未启用工程功能。启用工程 API 需已批准执行 0011、`CONVERSATION_ENABLED=true`、`CASTING_DESIGN_ENABLED=true` 和已存在的 MinIO bucket。阶段三接口没有公共执行入口。
3. 第四阶段才扩展 Provider/transport、LangGraph 工具调用及附件状态适配。当前向聊天 POST casting_input_file_id 返回 `CASTING_TOOL_NOT_READY`，不会把工程计算请求当作已经执行的结果。
4. 仓储层已具备 turn 受理事务内的附件冻结；实际 HTTP 问答入口的启用、工程结果说明/发布/追问及前端附件选择分别接后续阶段。
   第四阶段需同时对齐 `Conversations._validate_request` 的 `turn_fingerprint`、`_status` 的附件回显和 `start_turn` 的 casting_enabled 参数，再移除当前入口拒绝分支；不能只解除 CASTING_TOOL_NOT_READY 就声称支持聊天计算。
5. 不完整计算目录和终态错误保留审计且不自动重算；execution_no 暂为 1，显式重算协议后续实现。磁盘/对象的长期保留与清理策略未实施。
