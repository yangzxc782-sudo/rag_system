# 第四阶段：Provider 与 LangGraph 工具分支

范围：native tool_call、受控路由、真实 ToolNode、服务端会话绑定、确定性结果投影、v2/v3 分派。业务数据库迁移由用户后续单独执行；本阶段仅在隔离测试库应用已有迁移，不增加业务迁移。

`LLM_REMOTE_SUPPORTS_TOOLS` / `LLM_LOCAL_SUPPORTS_TOOLS` 默认 false，由明确配置启用；只支持单次工具调用，关闭并行工具。文本请求的 payload 和默认能力保持原样。保持本地 HTTPX trust_env=False、远程代理语义、客户端所有权和零 SDK 重试。模型在工具轮允许 content=null；总结轮不提供 tools，意外工具返回仍拒绝。

新图版本 `casting_v1_v3` 在现有 RAG 链前加路由：普通问题回到 load_context/understand_question 及原检索/生成节点；计算意图且冻结输入匹配时才执行 generate_casting_design；缺输入、历史解释、选择不明确分别生成有界路由交接结果。路由的唯一计算许可是合法 native tool_call，不能把 JSON 文本中的 calculate 当作许可。

工具 Schema 仅 input_file_id。session/turn/attempt 来自图上下文，不能由模型传入。外层节点构造短生命周期 AIMessage，调用 ToolNode(handle_tool_errors=False) 并传父 RunnableConfig；ToolMessage 只在内存中存在。持久化元数据使用现有 qa_turn_artifacts 的 context kind、独立 key 和严格类型，不增加任意 JSON 通道。

State v3 仅新增 effective_input_file_id、route_artifact_id、tool_artifact_id、source_run_id 和有界路由枚举。Checkpoint 不保存完整 messages、附件内容、推荐 JSON 或模型文本。新图读写前验证 turn/request/attempt、graph_version 和冻结文件。已有 v2 turn（包括 graph_version=NULL 的旧记录）继续按 v2 执行与恢复；v2 指纹不变。

结果投影只复制已保存 recommendation 的字段：推荐候选、冒口与浇道、检查、证据待补项、前三个其他候选摘要、淘汰分组、单位转换。投影大小有上限，超出报预算错误并保留 run，不能静默改值或重新挑推荐。完整原结果仍由第三阶段保存与下载。

第五阶段才实施工程回答草稿、事实引用渲染、正式消息发布和完整追问/错误恢复。第四阶段的计算分支以 `casting_ready` 结束，结果可由内部 get_casting_result 读取；普通 RAG 分支仍产生原 result_staged。HTTP 聊天入口暂不接受 v3 工程执行，避免把未完成来源校验的模型文本直接发布。第五阶段接线前不能单独移除 CASTING_TOOL_NOT_READY。

实际模型验收使用当前配置的模型、合成问题与虚构 UUID，不读取用户工程附件或发送知识库数据。真实模型、SDK 模拟、隔离引擎/数据库验收分别记录，不以 mock 代替真实工具协议结果。

历史输入复用由服务器冻结的 effective_input_file_id 决定。意图路由明确允许“参数不变，再算一次”；文字改变参数时，`requires_updated_input` 为保守服务端拦截，既传给路由模型，也在接收 native tool_call 时独立检查。它不解析或修改工程值；知识解释仍可进入 RAG。该拦截不能代替完整自然语言语义验收，未覆盖的表达继续由模型判断，工具始终只能读取冻结 JSON。

手动真实模型探针：从 backend 执行 `.venv/Scripts/python.exe tests/manual_casting_tool_probe.py --run`。最多九个模型请求，仅在探针进程中启用工具能力，不改 `.env`，不访问业务数据库、存储或计算引擎。它验证路由与工具消息协议，不验证工程回答发布或计算结果语义。具体结果见 [第四阶段验收](casting-design-phase4-acceptance.md)。
