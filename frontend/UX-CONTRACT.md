# Phase 13 M5 交互契约

| 能力 | 唯一责任模块 | 数据与交互契约 |
|---|---|---|
| 创建/列表/改名 | QaSessionList | 后端会话事实；创建失败保留 request_id；同 ID 去重，稳定排序 |
| 聊天视图 | RagChatPanel + ChatSession | 每个 thread 单独挂载，离开时取消查询、轮询并忽略晚到响应 |
| 消息 | QaMessageList | 后端 sequence_no；按 message ID 合并；已提交消息不会因请求失败消失 |
| 本轮问题 | 表单 + qa-pending | 固定原始问题/检索参数；新 request UUID；不上传历史或执行内部状态 |
| 恢复 | ChatSession | 先 GET 状态，再根据 can_retry 显式 POST 原请求；不把 fetch 取消当后端停止 |
| 202 | ChatSession | status_url/Location、Retry-After 或有界退避；最多十次，然后可手动查询 |
| 刷新 | session detail + messages + request status | URL 指定 thread；sessionStorage 仅辅助未完成输入，不能代替历史 |
| 滚动 | 聊天区 + GraphEvidencePanel | prepend 保留阅读位置；近底部跟随，阅读旧消息时不跳动 |
| 引用/图谱 | RagAnswerPanel + GraphEvidencePanel | 仅显示当前可用快照，删除来源有文字状态；不会查询 Neo4j/LLM |
| 错误 | qaErrorMessage + 内联状态区 | 中文提示，不回显内部异常；can_retry=false 不允许自动或显式恢复 |
| 本机边界 | 浏览器直连 + M4 socket peer 校验 | 不增加代理，不生成 Forwarded/X-Forwarded-*，不把 UUID 当鉴权 |

回答、no_context、clarification 均来自已提交 assistant 消息。澄清补答仍在同一 thread。
输入区在 running/finalizing/needs_recovery 或网络状态不明时阻止新的不同问题。
failed 请求可查询/按许可重试，也可提出新问题；旧请求是否仍可恢复由后端决定。

原生 disclosure 支持键盘；错误和等待有文字及辅助技术状态标记。图谱关系保留完整端点，
文本依据保留引用编号。页面不开辟另一套单轮提问流程。

检查包括 TypeScript、全量 ESLint、Next 构建、Playwright 模拟 API、真实隔离 PostgreSQL +
本机 HTTP 浏览器测试。模拟语义结果不等同于真实模型评测，详见 M5 验收记录。
