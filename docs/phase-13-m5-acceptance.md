# Phase 13 M5 开发与验收记录

日期：2026-09-28。范围：直接替换单轮前端，接入 M4 同步会话 API。

## 结论与边界

M5 工程验收完成：新的 `/rag` 和 `/rag/[threadId]` 支持独立会话、连续提问、
持久历史、刷新/新浏览器上下文恢复、分页、显式重试、来源删除提示及会话切换竞态防护。
没有保留第二套单轮前端，没有 SSE、WebSocket、后台任务或跨 thread 长期记忆。

业务库未迁移，`rag_system` 只读核验仍为 `0008_phase10_enforce`，不存在
`langgraph_checkpoints` schema；实际配置 `conversation_enabled=False`。
实际 `.env`、现有业务进程和业务容器未修改。工程完成不等于已在业务环境启用。

## 实际文件清单

本节按本轮开始时的工作区分类。M1–M4 原有未提交文件不算本轮新增。

| 分类 | 文件 | 内容 |
|---|---|---|
| 新增页面 | `frontend/app/rag/layout.tsx`、`frontend/app/rag/[threadId]/page.tsx` | 共享紧凑导航、UUID 动态路由 |
| 修改入口 | `frontend/app/rag/page.tsx` | 多轮会话首页 |
| 新增组件 | `frontend/components/QaSessionList.tsx`、`RagWorkspace.tsx` | 创建、列表、改名、切换、游标分页 |
| 新增组件 | `frontend/components/RagChatPanel.tsx`、`QaMessageList.tsx`、`RagAnswerPanel.tsx` | 消息、恢复、表单、独立回答证据 |
| 新增客户端 | `frontend/lib/qa-sessions.ts`、`qa-chat.ts`、`qa-pending.ts`、`rag-evidence.ts` | 七 API、严格请求字段、会话状态、最小 pending、通用证据类型 |
| 修改展示 | `frontend/components/GraphEvidencePanel.tsx` | 仅替换通用类型 import，原图谱展示复用 |
| 修改导航 | `frontend/components/AppShell.tsx` | 新入口文案；聊天专用紧凑导航，其他页面布局保留 |
| 删除 | `frontend/components/RagAskPanel.tsx`、`frontend/lib/rag.ts` | 原单轮提交、单次 answer/loading 状态及旧专用类型 |
| 最小旧问题修复 | `frontend/components/KnowledgeItemsPanel.tsx` | 错误读取函数只约束 error 字段，修复联合响应类型错误 |
| 最小旧问题修复 | `frontend/components/DocumentParseResults.tsx` | props 变化时条件重置派生分页，移除同步 setState effect |
| 修改字体/元数据 | `frontend/app/layout.tsx`、`frontend/app/globals.css` | 去除构建失败的远程 Google 字体，沿用系统正文/等宽字体 |
| 测试依赖与命令 | `frontend/package.json`、`frontend/package-lock.json`、`frontend/playwright.config.ts` | Playwright、typecheck/E2E 命令、回环启动 |
| 新增前端测试 | `frontend/tests/mock-conversations.ts`、`contracts.spec.ts`、`chat.spec.ts`、`real-chat.spec.ts` | 契约、模拟浏览器、真实 HTTP 浏览器验证 |
| 后端最小扩展 | `backend/app/schemas/conversations.py`、`backend/app/services/conversations.py` | 状态响应增加数据库保存的 input 元数据 |
| 新增后端测试 | `backend/tests/test_conversations_resume_input.py` | 原问题/有效 K/document_id 精确恢复及响应兼容 |
| 新增联调夹具 | `backend/tests/phase13_m5_support.py`、`backend/tests/phase13_browser/conftest.py`、`test_m5_browser.py` | 复用安全门，真实本机服务与 PostgreSQL/浏览器组合 |
| 前端说明 | `frontend/.gitignore`、`.env.local.example`、`README.md`、`DESIGN.md`、`UX-CONTRACT.md` | 追踪公开配置示例、当前界面及测试契约 |
| 阶段文档 | `docs/phase-13-design.md`、本文件 | M5 实际契约与验收 |

`.env.local.example` 原为忽略文件，本轮开始纳入版本控制；实际 `.env.local` 未改。
本轮只新增开发依赖 `@playwright/test==1.63.0`（playwright/core 同版本），
未升级 Next 16.2.9、React 19.2.4 或任何后端模型依赖。实际 Node 24.18.0、npm 11.16.0。

旧单轮组件和 `ragAsk` 客户端没有运行时残留引用；`/rag/single` 返回 404。
公共 citations/graph/LLM DTO 提取至 `rag-evidence.ts`。旧后端 `/api/v1/rag/ask`、Hybrid、
BGE、LLM Provider、Graph 节点和 0009/0010 迁移均保持本轮开始时的内容。

## API 与恢复契约

浏览器直接调用 `{success,data,error}` API，不经 Next.js 代理：

| 方法/路径（均在 `/api/v1`） | 前端行为 |
|---|---|
| POST `/rag/sessions` | 创建 request_id 和 turn request_id 分开；失败保留创建 ID |
| GET `/rag/sessions` | limit=50；cursor 编码传输；thread_id 去重、更新时间/ID 稳定排序 |
| GET `/rag/sessions/{thread_id}` | 标题、active_request、刷新恢复 |
| PATCH 同一路径 | 手动修改标题；成功后以服务器标题展示 |
| GET `.../messages` | 最近 50 条，before_seq 更早页，返回正序 |
| POST `.../turns` | 固定 request_id/question/limit/document_id；同步完整结果 |
| GET `.../requests/{request_id}` | 202 查询、断线对账、恢复许可和持久结果 |

客户端类型涵盖 SessionPage/Detail、MessagePage、QaMessage、RequestStatus、
ConversationAnswer、EvidenceSource 和 TurnInput。保留稳定 message_id/sequence_no、
三种 outcome、来源 tombstone、模型信息。没有上传历史、Checkpoint 或 attempt 的入口。

必要的后端补充：原 M4 状态缺少 limit/document_id，关闭浏览器后无法安全重建原请求。
新增可选响应字段 `input: TurnCreateRequest | null`，当前服务从 qa_turns 总是填入原 question、
request_id、有效 retrieval_limit、document_id。包含空白的原问题也不改写。
这是响应元数据补充，没有新路由、新字段迁移、执行流程或事务改动。

每个 thread 的 ChatSession 和 React 表单独立挂载。离开时 AbortController 取消读取和轮询，
所有异步更新先检查取消信号；A 的迟到请求不能写 B 的消息、引用、状态或输入。
POST 取消不会触发后端终止命令，返回 A 时重新读数据库状态。

消息页面按 message_id 合并、sequence_no 排序；加载更早消息以 scrollHeight 差值保留视口。
近底部时跟随新消息，阅读早期历史时不强制滚动。错误不清空已提交历史。
发送/恢复成功后重新 GET messages，展示业务已提交结果；不会把临时草稿当作回答。
三种成功 outcome 都从持久消息显示，澄清补答继续使用当前 thread。

sessionStorage 只保存当前未完成的 thread/request/question/limit/document_id 和创建请求 ID。
成功后清除。没有聊天历史、引用、图谱或 HTTP 响应缓存。浏览器存储不可用时当前页面仍可重试；
若创建响应丢失且浏览器存储也被禁用后关闭页面，无法恢复创建 ID，应先检查会话列表。
已保存 turn 的恢复参数可从服务器取得，因此新浏览器上下文不依赖原 sessionStorage。

## 状态、失败与显式重试

- 202 使用 JSON status_url（或 Location），限定同一 API origin 和当前 thread/request。
- Retry-After 可读时限制为 1–30 秒；否则 1、2、4、8、10 秒上限退避。每轮最多十次 GET，
  然后显示暂停及手动查询按钮；没有无限轮询或自动 POST 接管。
- 读取超时 15 秒、POST 等待上限 120 秒；超时/断线先查询状态，不认定后端已失败。
- running/finalizing/needs_recovery、状态不明或当前提交过程中，禁止发送新问题。
- can_retry=true 且用户点击才 POST 原请求；false 不显示恢复按钮。请求确实未保存时可显式原 ID 重交。
- THREAD_BUSY 加载真实活动请求；IDEMPOTENCY_CONFLICT 保留明确提示并禁止盲重试。
- 历史冲突、来源失效、连接丢失、服务关闭和本机边界错误均提供中文提示；不回显内部堆栈或 Prompt。
- 没有自动命名、自动开新会话、自动改 attempt 或前端后台任务。

回答按原文转义呈现。引用和图谱分别在每条助手消息内展示，原 citation_id 不重编号。
来源删除后正文保留，tombstone 解释摘录/图谱不可用，剩余来源继续显示。
GraphEvidencePanel 只读服务器 DTO，展开/折叠不产生网络请求。

## 本机配置与安全

实际前端 `.env.local` 为 `http://127.0.0.1:8000`，后端 CORS 默认允许本机 3000 两种主机写法。
新 `npm run dev/start` 显式 `--hostname 127.0.0.1`。审计发现旧开发进程仍监听 `::`:3000，
旧后端为回环 uvicorn 入口；本轮未擅自重启它们。M4 会话安全入口尚未在业务服务启用。
新页面只在浏览器直连 API，不在 Next 服务端读取/代理聊天历史，因此没有通过旧前端监听绕开
M4 socket peer 的途径。后续启用时需关闭旧启动方式并使用回环命令及 `python -m app.local_server`。

不生成 Forwarded/X-Forwarded-*；不删除 M4 peer 校验；没有把 CORS、Host 或 UUID 当作鉴权。
默认 CORS 没有 expose_headers，浏览器使用 JSON status_url 与退避 fallback 即可。
真实测试只为测试进程设置 `http://127.0.0.1:3306` Origin，不修改实际配置。

## 自动化结果

| 检查 | 实际结果 |
|---|---|
| 全量 TypeScript `npm run typecheck` | 通过，零错误 |
| 全量 ESLint `npm run lint` | 通过，零错误/警告 |
| Next 生产构建 `npm run build` | 通过；包含 `/rag` 与 `/rag/[threadId]` |
| Playwright mock-api | **44 passed**：26 个浏览器用例 + 18 个纯契约用例 |
| 真实浏览器 + 本机 HTTP + 隔离 PostgreSQL | **1 passed** 浏览器综合用例；外层 pytest **1 passed**，不是两个独立场景计数 |
| 完整安全后端 | **1633 passed**，包含本轮新增 input 契约 2 项 |
| M1–M4 联合 PostgreSQL | **128 passed**（M1 35、M2 19、M3 39、M4 35） |
| pip check | No broken requirements found |
| Git whitespace / 输入工作区保护 | 通过；未覆盖或撤销原阶段修改 |

模拟浏览器覆盖：创建超时/刷新幂等、列表游标去重、标题刷新、连续四轮、直接 URL、
消息顺序/64 条分页/滚动、202、网络中断/相同参数重交、原存储丢失、活动请求刷新、
can_retry 两分支、request 不存在、THREAD_BUSY、幂等冲突、A 的迟到 POST 与历史 GET、
三 outcome、来源删除不重编号、独立图谱 disclosure/键盘、移动端无横向溢出、有界轮询、
外域 status URL 拒绝、存储不可用的创建幂等、三种功能/本机入口拒绝。

真实浏览器没有模拟会话 API：真实 Uvicorn/FastAPI lifespan、本机传输包装、
Repository、Graph、Checkpoint、snapshot、最终发布与 PostgreSQL 读写全部执行。
验证 7 个独立会话的 **86 条消息、43 个已完成轮次**；包括 32 轮旧历史分页，
连续四轮真实 Graph 编排、澄清/刷新补答、no_context、新浏览器上下文恢复、
用户消息已提交但无 Checkpoint 的同 attempt 恢复、幂等完成重放、来源清理后回读、
慢 Provider 活动状态查询、转发头拒绝。数据库还核验恢复参数/attempt=1、Checkpoint 存在、
结果 artifacts，以及实际使用“冒口尺寸怎么确定？”、“冒口有哪些限制条件？”和新话题 query。
恢复/删除测试仅操作合成来源的 PostgreSQL 记录，没有外部存储删除。

外部 LLM、Embedding/OpenSearch 输入采用确定性合成 Provider/Harness，图谱真实服务未启用；
这不是实际模型语义或真实 Neo4j 验收。浏览器回归只运行 Chromium（含 390px 移动视口），
没有声明 Firefox/WebKit、真实移动设备或多用户认证验收。

初次检查确实存在两处旧 TS/ESLint 错误，已最小修复。原 Google 字体构建下载在沙箱内外均失败，
移除远程依赖后完整构建通过。浏览器开发过程中修正了 Next 路由通知导致的测试定位歧义、
测试项目匹配、退避等待窗口及合成 Provider 的第三轮来源选择；最终套件无遗留失败。
没有修改、删除或放宽 M1–M4/Phase12 旧测试。

## 隔离环境与复现

新实例 `phase13-m5-test-ab87c3d941e6`，回环端口 **60531**；root database
`phase13_m1_test_ab87c3d941e6`，role `phase13_m1`，cluster `phase13-m1-test-ab87c3d941e6`。
门槛复用 `phase13_support.verified_engine`，子进程重新核验；没有 DATABASE_URL 默认回退。
测试专用卷 `7ea3d14927db8e9b609c977def6bd5b088c39d1f3074af23e64e9b559fa8b82b` 保留。
测试结束后本轮容器已停止（Exited 0），3305/3306/18005 测试端口释放，四个业务容器仍运行。
浏览器专用子库明确经过 **0008 → 0009 → 0010**。完整联合测试也覆盖迁移、安全降级和缺表拒绝。
浏览器网络守卫拒绝任何非隔离 18005 会话 API，即使使用了错误的旧前端构建也不发送业务写入。

前端（先安装开发依赖及专属 Chromium）：

```powershell
cd D:\rag_system\frontend
npm run typecheck
npm run lint
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\rag_system\backend\.phase13-m5.tmp\browsers'
npx playwright install chromium
$env:NEXT_PUBLIC_API_BASE_URL = 'http://127.0.0.1:18005'
npm run build
npm run test:e2e -- --project=mock-api
```

后端（连接串只由显式专属实例提供，不使用业务配置）：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --tb=short `
  --ignore=tests/integration --ignore=tests/phase13_integration --ignore=tests/phase13_browser `
  --basetemp .phase13-m5.tmp/safe-final --junitxml=.phase13-m5.tmp/safe-final.xml

$env:PHASE13_TEST_DATABASE_URL = '<已验证专属实例连接串>'
$env:PHASE13_TEST_CLUSTER = 'phase13-m1-test-ab87c3d941e6'
$env:PHASE13_TEST_CONFIRMED_DATABASE = 'phase13_m1_test_ab87c3d941e6'
.\.venv\Scripts\python.exe -B -m pytest tests/phase13_integration -q -p no:cacheprovider `
  --basetemp .phase13-m5.tmp/pg-joint-final --junitxml=.phase13-m5.tmp/pg-joint-final.xml

$env:PHASE13_BROWSER_E2E = '1'
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\rag_system\backend\.phase13-m5.tmp\browsers'
$env:PYTHONPATH = 'tests'
$env:PYTHONIOENCODING = 'utf-8'
.\.venv\Scripts\python.exe -B -m pytest tests/phase13_browser -q -s -p no:cacheprovider `
  --basetemp .phase13-m5.tmp/browser-context-final --junitxml=.phase13-m5.tmp/browser-context-final.xml
```

复跑使用新的 basetemp，避免覆盖证据。Playwright 自动启动独占测试端口，真实测试使用 3306/18005；
Windows 浏览器/子进程回收在获准的非沙箱执行环境验证。JUnit、浏览器日志/manifest/journal 和
桌面/移动截图保留在忽略的 `.phase13-m5.tmp/`。不得将这些测试参数复制到实际 `.env`。
最终另用实际未修改的前端配置重新构建通过；`.next` 不残留隔离 API 地址作为默认预览目标。
入场 71 个文件的哈希比较只出现两处后端响应扩展与 `docs/phase-13-design.md` 变化，其他原阶段内容和三份实际
环境文件逐一保持一致；旧依赖锁定条目没有升级。

## M6 准备与未执行项目

M6 可直接复用七个 API、上述 DTO、ChatSession/恢复组件、模拟 E2E 与安全隔离浏览器驱动。
最终联调需要明确授权的数据库环境、独立演示文档和可用的真实 Embedding/OpenSearch/BGE/LLM/Neo4j，
使用独立测试集核验多轮语义、延迟与真实引用。Phase12 Final 数据集及验收许可未使用。

真实模型评测、真实多存储破坏性删除、业务迁移、业务功能启用、生产部署和远程认证本轮均未执行。
它们不是本轮模拟成绩的一部分，也不会通过页面启动隐式发生。新浏览器无存储恢复已覆盖，
真实设备兼容性与用户使用体验可在 M6 增补。是否升级业务库及正式启用仍需用户单独授权。
