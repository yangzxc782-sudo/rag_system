# 浇冒系统接入第六阶段验收

日期：2026-09-30。范围：聊天工程 JSON 附件、输入来源回显、工程结果状态与字段错误、下载、前端恢复和最小浏览器闭环。

本轮未迁移业务库，未修改实际 `.env` / `.env.local`，未启用业务工程功能或重启业务服务，没有新建 Python 环境。以下“完成”指代码与隔离环境验收通过。

## 用户流程

1. 在现有聊天问题旁展开“工程 JSON 附件”，选择一个 `.json` 文件，最大 256 KiB。
2. 原始文件通过独立工程上传接口保存。上传/结构检查完成后，界面显示文件名与 file_id；工程准入仍在计算时执行。
3. 点击发送，仅把 file_id 连同原有问题、检索参数和 request_id 送入 turn，不在问答请求中复制 JSON 内容。
4. 工程回答沿用原聊天安全文本展示，增加工程状态、输入来源、规则版本、运行编号和字段问题列表。没有候选是业务结果，准入失败可修正文件后新提问。
5. 有结果文件时，可以下载后端保存的原 recommendation.json。浏览器不解析后重写下载内容。

附件区域默认折叠并按需读取工程文件，普通问答不额外依赖工程文件 API。可以明确选择本会话已上传文件，支持分页；没有本轮附件时，由后端按既有规则选择最近明确选定且通过准入的输入。移除附件不删除服务器文件。

## 请求与恢复边界

- 上传 File 字节仅在当前组件内存中持有，不存入 sessionStorage/localStorage。上传重试沿用同一上传 request_id 和原 File；取消等待、改选或切换会话后，迟到响应不能再次选中文件。
- 已完成上传的文件可从会话文件列表重新选择。未完成上传时刷新会丢弃浏览器 File，需要重新选择；不会从浏览器历史恢复文件正文。
- turn 请求冻结 casting_input_file_id。聊天网络异常先查询状态；显式重试发送原 request_id、问题、检索参数和附件 ID，不再次上传文件、不换成后来上传的文件。
- pending v1 保持旧四字段记录可读，新记录只增加受控 UUID；白名单序列化不保存文件正文、结果、额外属性或服务器路径。
- 状态响应不能将原请求的附件 ID 替换成另一文件。跨会话文件/响应被拒绝。已确认后端没有受理的文件错误允许修正，不将用户锁在不能修改的请求中。
- 202 按原有有限轮询读取状态，不再次 POST 计算；没有权限重试时不自动执行。

## 后端的两个增量读取字段

现有文件列表只提供上传时间排序，无法表示“最近明确选定”的顺序；历史消息只有文件 ID，不能可靠恢复文件名。为此新增：

- `CastingInputFilesView.reusable_input`：复用原 `CastingRepository.select_input(sid, None)`，返回后端真正会选择的有效文件元数据；即使此文件不在当前分页中，也可正确提示。
- `ConversationMessage.casting_input_filename`：从同会话的 requested/effective 输入文件读取原始文件名，供消息刷新和翻页显示。

不改变选择算法，不新建表或迁移；普通旧会话在工程关闭、0010 数据库下不会查询工程列/表。恢复请求仍保存 file_id，文件名只是显示数据。

## 文件清单

| 文件 | 改动 |
|---|---|
| `frontend/lib/casting-design.ts`（新增） | 工程请求/响应类型、受限文件 API、会话归属校验、原字节下载 |
| `frontend/lib/casting-attachments.ts`（新增） | 每会话独立上传状态、取消、原上传重试、列表分页、后端复用提示 |
| `frontend/components/CastingInputAttachment.tsx`（新增） | 单附件选择、状态、错误、历史选择和移除 |
| `frontend/components/CastingAnswerDetails.tsx`（新增） | 结果状态、来源、规则、字段错误及下载按钮 |
| `frontend/components/RagChatPanel.tsx` | 接入附件，上传未完成时阻止发送，重试显示原附件 |
| `frontend/components/QaMessageList.tsx`、`RagAnswerPanel.tsx` | 显示用户文件名及工程 outcome，保留原引用与证据删除提示 |
| `frontend/lib/qa-sessions.ts` | 可选工程类型、Multipart 共用传输、安全错误白名单 |
| `frontend/lib/qa-chat.ts`、`qa-pending.ts` | 冻结附件、响应一致性检查、恢复和旧记录兼容 |
| `backend/app/schemas/casting_storage.py`、`services/casting_files.py` | 文件列表返回 reusable_input |
| `backend/app/schemas/conversations.py`、`services/conversations.py` | 会话消息返回同会话文件名 |
| `frontend/tests/casting-design.spec.ts`（新增） | 19 项附件/恢复/错误/结果浏览器及合同测试 |
| `frontend/tests/real-casting.spec.ts`（新增） | 实际浏览器到真实隔离后端的计算流程 |
| `frontend/tests/mock-conversations.ts`、`chat.spec.ts`、`playwright.config.ts` | 工程 fixture、独立测试输出目录及可选真实工程 project；原聊天断言保留 |
| `backend/tests/casting_browser_support.py`、`tests/phase13_browser/test_casting_browser.py`（新增） | 受保护的隔离 HTTP/模型替身/真实引擎与存储验收 |
| `backend/tests/phase13_integration/test_casting_answers_postgresql.py` | 验证历史文件名，以及分页外的真实 reusable_input |
| `docs/casting-design-phase6.md`、本文（新增） | 设计、测试与启用边界 |

未引入依赖，没有更改计算算法、规则、RDF/SHACL、工具路由、知识库上传/检索、Docker Compose 或数据库迁移文件。

## 验证结果

| 检查 | 结果 |
|---|---|
| 前端 typecheck | 通过 |
| 前端 ESLint | 通过 |
| Next.js production build | 通过；测试完成后已按原本地配置重新构建，未保留隔离 API 地址作为当前构建目标 |
| 原聊天/合同与新增工程测试 | 69 项整批通过；随后补充的 2 项通过，合计 71 个不同用例 |
| 后端离线 API/schema/pending/storage 合同 | 37 passed / 1.49 秒 |
| 真实工程浏览器 + 文件元数据 + 原 M4 PostgreSQL 回归 | 37 passed / 102.99 秒 |
| 其中真实工程浏览器 | 1 passed / 16.1 秒；浏览器测试本体 14.3 秒 |
| 桌面/手机布局 | 已查看实际截图；390 像素视口无横向溢出 |
| `git diff --check` | 通过 |

测试批次可能覆盖同一功能，不相加宣称全系统测试总数。

前端模拟测试涵盖：上传原字节与只传 file_id；文件名/来源/规则刷新恢复；下载；无候选/准入错误；非法类型/超限；后端结构错误；上传中阻止发送；取消/切换会话隔离迟到上传；原上传重试；聊天网络中断、刷新、202 和原请求重试；历史文件分页选择；最新未选上传不替换真正复用输入；带 JSON 的知识问题继续显示原引用；功能关闭可继续普通问答；跨会话响应和附件替换拒绝；pending 兼容与错误详情白名单。既有普通问答、来源删除、引用、历史分页和有限轮询用例继续通过。

真实浏览器流程在新建专用数据库和 bucket 中执行，使用真实 FastAPI/本机安全入口、LangGraph/PostgresSaver、MinIO、原 Python 进程和 RDF/SHACL：

1. 页面上传中文文件名 JSON，计算得到 4 候选，冒口直径 170 mm，保存两条消息。
2. 刷新后仍显示文件名；下载 recommendation 的 SHA256 与数据库保存值相同。
3. 追问第二候选，正文包含该候选真实 ID，未重新运行引擎。
4. 上传空冒口位置样例，显示“暂无可行方案”。
5. 普通知识问题继续产生原 RAG 回答。
6. 上传 Proposed 参数样例，显示准入失败及 casting_mass_kg 字段问题。
7. 数据库最终 5 个已完成 turn、10 条消息；run 依次为 succeeded、no_feasible_candidate、admission_failed；引擎恰好执行 3 次。

**模型与检索/Embedding 使用合成测试替身。** 本轮没有新增真实 GPT-4o-mini 语义验收，也没有对业务知识库或全部外部服务做端到端验收；这部分留到第七阶段。

## 测试环境处理

首次浏览器运行使用了系统默认的不存在浏览器目录，改用仓库已有 `backend/.phase13-m5.tmp/browsers` 后通过，未安装新浏览器。测试服务退出时遇到 Windows 进程清理问题，只结束了已核验属于本轮的 3307/3308 Next.js 进程；之后使用正确 PATH/权限的测试自动收尾正常。初次后端浏览器 fixture 缺少 `PYTHONPATH=tests`，补齐现有测试导入路径后通过。这些前置失败没有作为功能验收通过记录。

复用专用容器 `casting-phase3-test-59b7702eca71` / `casting-phase3-minio-d4a612983e82`，本次映射端口 51385 / 51390。测试先核验专用账号/数据库/cluster，迁移只进入新增测试库；MinIO 只使用新增随机 `casting-test-*` bucket。验收完成后，两个容器已停止，测试数据与运行目录保留，未删除 volume、数据库或对象。

本机留存记录：

- 69 项整批报告：`backend/.casting-phase6-browser-a81dc699f36a4cce8374161530c45885.tmp/report.xml`。
- 真实浏览器日志/校验/截图：`backend/.casting-phase6-real-fbf045cd393746d48afc9ecd66405ad4.tmp/pytest/test_real_casting_browser0/`。
- 测试构建临时使用隔离 API `127.0.0.1:18005`，浏览器 guard 拒绝其他 API 目标；结束后在未覆盖环境变量的情况下重新运行 build，实际配置文件保持原样。

## 尚未执行

业务库的 0011 / 0012 迁移仍由用户后续单独执行。功能开关、正式部署和第七阶段完整启用验收未执行。复杂方案卡片、工程表格渲染、异步 worker、SSE/WebSocket 和上传草稿跨刷新恢复不在本阶段范围。
