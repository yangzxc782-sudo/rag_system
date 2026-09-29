---
version: phase13-m5
name: "铸型工艺知识库 RAG 管理系统"
description: "持久化多轮问答与可追溯证据工作台"
---

# 多轮问答界面

中文技术知识工作台。沿用 `app/globals.css` 和 Tailwind 的灰白色面板、细边框、
系统 Arial/Helvetica 字体与 slate/amber 状态颜色。状态总有文字说明。
远程 Google 字体已移除，本机构建不再依赖字体下载。

`/rag` 提供历史会话与新建入口；`/rag/[threadId]` 提供独立聊天。桌面为左侧历史、
右侧消息与底部输入；移动端纵向排列。聊天使用 AppShell 的紧凑导航，其他页面布局保留。

- `QaSessionList`：后端生成会话 UUID、手动标题、cursor 分页与去重。
- `RagChatPanel`：每个 thread 独立挂载，控制提问、恢复、状态、表单与滚动。
- `QaMessageList`：按 sequence_no 排序，稳定 message ID 用作 React key。
- `RagAnswerPanel`：各条回答的正文、outcome、模型、引用和来源删除状态。
- `GraphEvidencePanel`：复用已有图谱表格，不产生网络请求；展示服务器保留的证据。

用户消息浅灰并右缩进，助手消息白色细边框；澄清和 no_context 明确标注。
回答 → 图谱 → 文本依据为稳定阅读顺序。文字按原文转义呈现，不执行 HTML。
证据编号按数据库返回值显示；缺失来源不重编号。

图谱使用原生 details/summary 与可键盘聚焦的滚动表格，每条新回答默认折叠。
聊天记录使用有限高度滚动区，加载旧页保留可见位置；阅读旧消息时不强制跳到最末。
表单失败不移除已提交历史。发送成功后本轮检索设置恢复默认，避免隐藏继承过滤条件。

见 [交互契约](UX-CONTRACT.md) 与 [M5 验收记录](../docs/phase-13-m5-acceptance.md)。
