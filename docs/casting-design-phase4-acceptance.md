# 浇冒系统接入第四阶段验收

日期：2026-09-29。范围：Provider/transport 原生工具协议、LangGraph v3 工具分支与内部结果交接。**业务数据库迁移由用户后续单独执行，本轮没有执行。** 未修改实际 `.env`，未重启业务服务，未启用工程聊天功能；没有新建 Python 虚拟环境。

## 已完成的代码

| 文件 | 第四阶段内容 |
|---|---|
| `backend/app/llm/openai_chat_transport.py` | tools 序列化、assistant tool_calls 与 tool_call_id、允许工具轮 content=null、响应工具白名单和单次调用限制 |
| `backend/app/llm/messages.py` | 严格工具参数 JSON：对象、重复键、非法 Unicode、非有限数、深度与大小限制 |
| `backend/app/llm/api.py`、`local.py`、`configuration.py` | 显式工具能力与布尔配置校验，保留普通文本协议、代理及客户端生命周期 |
| `backend/app/core/config.py`、`backend/.env.example` | `LLM_REMOTE_SUPPORTS_TOOLS`、`LLM_LOCAL_SUPPORTS_TOOLS` 默认 false，登记 casting_v1_v3 |
| `backend/app/schemas/casting_graph.py` | 严格工具入参及路由/工具 artifact 元数据，不接受任意 JSON 载荷 |
| `backend/app/rag/casting_prompt.py` | 工具 Schema、路由策略、历史输入复用/改参约束、完整工具结果请求构造 |
| `backend/app/rag/casting_tool.py` | 每次执行绑定服务端 session/turn/attempt，工具仅接受 input_file_id |
| `backend/app/rag/casting_nodes.py` | 原生调用验证、真实 ToolNode、结果引用持久化、幂等重放和内部 handoff |
| `backend/app/rag/casting_projection.py` | 从持久化 recommendation 复制事实、摘要淘汰原因、校验 SHA 与预算 |
| `backend/app/rag/conversation_graph.py`、`conversation_state.py`、`conversation_nodes.py` | v3 分支/状态，v2 版本与指纹兼容，原 RAG 节点复用，禁止把已有执行改用另一图版本 |
| `backend/app/services/conversation_repository.py` | 复用 context kind，独立 casting artifact key/schema，保留原 RAG 元数据约束 |
| `backend/app/services/conversation_recovery.py`、`conversations.py` | 版本身份比较、请求指纹/附件回显对齐，阶段五发布前保留入口拒绝 |
| `backend/app/services/casting_design.py`、`backend/app/main.py` | 计算服务绑定执行 lease 的 session_factory，以及应用内图服务注入 |
| `backend/tests/test_casting_tool_protocol.py`、`test_casting_projection.py` | 工具协议、输入边界、真实黄金结果投影测试 |
| `backend/tests/phase13_integration/test_casting_graph_postgresql.py` | PostgreSQL + Checkpoint + ToolNode + 原 Python 引擎集成 |
| `backend/tests/manual_casting_tool_probe.py` | 明确手动执行的真实配置模型探针，不自动纳入 pytest |

未修改供应商计算算法、RDF/OWL/SHACL、规则文件、Hybrid/OpenSearch/BGE、文档入库、前端、Docker Compose。本阶段不新增迁移。LangChain Core / LangGraph Prebuilt 沿用项目已经固定的版本。

## 验证结果

以下批次有重叠，不把批次数量相加当作不同测试总数。最终无未解决的测试失败。

| 批次 | 结果 | 范围 |
|---|---:|---|
| 协议/投影/既有 LLM 与会话 API/schema | **184 passed / 2.67 秒** | casting projection/tool protocol、llm messages/provider、api provider、conversations API/schema |
| 原会话/RAG 回归 | **194 passed / 101.29 秒** | execution lock、lifespan、M3 contract/state、resume input、persistence、LLM provider，以及 M3/M4 PostgreSQL 集成 |
| 最终工具图与相关协议/API | **59 passed / 22.11 秒** | 包含新工具图 **14 项 PostgreSQL 集成测试**；其余为相关协议/API 测试 |
| 冻结资源核验 | **2 passed / 0.42 秒** | 17 个 vendor 文件、启用规则及黄金样例哈希 |
| 当前配置模型真实调用 | **9/9 passed** | api / gpt-4o-mini，真实 Chat Completions 工具定义、返回工具调用、回传结果、继续回答 |
| `git diff --check` | 通过 | 无空白错误；仅有仓库既有 Windows 换行提示 |

隔离集成的 LLM 与检索使用明确的合成 provider；计算引擎、子进程、PostgreSQL、Checkpoint、ToolNode 是真实实现。对象存储使用内存替身；真实 MinIO 的产物验证见第三阶段，本轮不把它重复计为已验收。真实模型探针不运行数据库或引擎，不发送用户工程附件或知识库内容。

工具图重点验证：

- 合法原生调用绑定当前冻结 file_id，调用一次引擎；同轮再次 invoke 复用运行与路由。
- 带附件/不带附件的知识问题进入原 RAG 分支，继续产生原有 result_staged 和受来源约束的草稿。
- 无输入产生 input_required 内部结果，不产生工程方案或业务回答。
- 未知工具、多调用、额外参数、错误 file_id 均无法到达计算引擎。
- 推荐 artifact 提交后、Checkpoint 写入前中断，resume 复用已持久化运行，禁止第二次计算。
- 检查该会话全部 checkpoint、blob 和 pending writes，不含 ToolMessage、AIMessage、tool_calls、输入正文或推荐结果。
- 真实无候选计算得到 no_feasible_candidate，摘要保留淘汰原因，不返回伪造推荐候选。
- 历史有效输入在 turn 接受时冻结，后续未选定上传不改变该输入；文字改参即使收到模型工具调用，也被拦截为 input_required。
- v2 turn 在配置 v3 后仍按 v2 执行，指纹保持不变；v3 与后续 v2 执行不会混入旧 casting 状态。
- 功能关闭后，不能把已存在的同轮 v3 Checkpoint 覆盖为 v2。

## 真实模型结果

最终探针采用应用实际配置的 `api / gpt-4o-mini`，仅在探针进程内将工具能力设为 true。模型名称是配置标识；测试确认的是当前端点的实际协议行为，不推断转发服务内部使用何种模型。

| 合成场景 | 最终结果 |
|---|---|
| 无附件知识问题 | rag，无工具调用 |
| 有附件知识问题 | rag，无工具调用 |
| 有本轮输入，明确要求计算 | calculate，1 次 native tool_call，参数为给定文件 UUID |
| 计算意图但缺输入 | input_required，无工具调用 |
| 仅文字改质量，沿用历史输入 | input_required，无工具调用 |
| 历史输入下询问“调整冒口的原则” | rag，无工具调用 |
| 参数保持不变，复用历史输入重算 | calculate，1 次 native tool_call |
| 解释已有方案 | explain_existing，引用给定 run_id，无工具调用 |
| assistant 调用 + tool 结果回传 | 模型正确返回仅存在于工具结果内的探针标记，无进一步调用 |

测试中发现并修正了两处语义问题：第一次模型错误允许文字改参使用旧输入；扩展测试时，模型又把允许复用的历史输入误当成缺输入。增加独立服务端改参拦截、明确历史输入有效性及对应路由示例后，最终九项通过。该结果是有限场景验收，不代表所有自然语言表达都已覆盖；第七阶段仍需真实用户表达与完整链路验收。

工具接口遵循 [OpenAI Function Calling 文档](https://developers.openai.com/api/docs/guides/function-calling) 的工具定义、调用消息和结果消息协议；[GPT-4o mini 模型文档](https://developers.openai.com/api/docs/models/gpt-4o-mini)列出 function calling 能力。本次通过结论来自实际端点请求，不仅来自模型文档。

## 结果投影与持久化边界

选择摘要方案 B，保留完整 recommendation 的第三阶段持久化与下载。投影只复制工程值，不做单位转换、计算或重新排名。对比黄金样例（UUID 长度固定）：

| 样例 | 原 recommendation 字节 | 投影字节 |
|---|---:|---:|
| baseline | 112367 | 4879 |
| converted | 112544 | 4975 |
| no_candidate | 3754 | 1013 |

投影上限 24 KiB；只显示前三个其他候选时显式记录返回数、总数和截断状态；推荐候选工程字段不静默删减。预算超限保留原结果并返回受控错误。检查结果文件 SHA、候选数量和推荐 ID；不一致停止读取。所有阶段元数据仅保存 ID、hash、版本和有界状态，完整 JSON 仍留在工程对象存储。

## 运行资源及未实施项

复用第三阶段专用 PostgreSQL 容器 `casting-phase3-test-59b7702eca71` 和 MinIO 容器 `casting-phase3-minio-d4a612983e82`。本次重启后的随机主机端口为 52273 / 52274；首轮测试因沿用上次端口连接超时，改用实际端口后全部通过。PostgreSQL 测试先验证专用库、cluster 和确认标识，仅在新建隔离测试库应用已有迁移。

验收结束已停止这两个专用容器，保留容器、测试数据库和对象数据。业务数据库和 bucket 未修改；没有执行删除或清理操作。

**当前第四阶段计算分支结束于 casting_ready，并通过 `ConversationGraph.get_casting_result()` 提供经过核验的投影和完整 tool-result 请求。尚未将模型工程说明发布到聊天界面。** HTTP 工程入口继续受 CASTING_TOOL_NOT_READY / CASTING_PUBLICATION_NOT_READY 保护，防止将未验证工程来源的模型文本发布为正式消息。原 v2 请求仍可以恢复与重放。

第五阶段继续实现 CastingAnswerDraft、工程 outcome、事实引用与确定性渲染、run 来源校验、正式发布、历史追问和工程错误恢复策略，然后接通 HTTP 问答入口。前端 JSON 附件闭环在第六阶段。不能仅打开配置或移除入口保护就视为全部功能完成。
