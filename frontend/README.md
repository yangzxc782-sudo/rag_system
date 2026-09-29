# 铸型工艺知识库前端

Next.js 16.2.9、React 19.2.4、Tailwind CSS 4。`/rag` 为持久化多轮聊天入口，
`/rag/[threadId]` 可直接打开历史会话。会话、消息和回答详情从后端数据库恢复。

## 本机开发

```powershell
npm ci
npm run dev
```

开发和生产预览命令均绑定 `127.0.0.1`，默认端口 3000。浏览器直接访问
`NEXT_PUBLIC_API_BASE_URL`（示例见 `.env.local.example`），没有 Next.js API 代理。
API 默认回环 8000；后端 CORS 默认允许 `http://127.0.0.1:3000` 和 `http://localhost:3000`。
更换前端端口时，需在隔离环境中显式配置对应本机 Origin。

多轮接口仅接受 M4 的 `python -m app.local_server` 安全入口。普通 uvicorn 入口、
转发头或非本机访问会被拒绝。当前 Phase 13 开关仍默认关闭，业务库仍为 0008；
本阶段没有升级业务数据库或部署功能。启用前需另行授权数据库准备。
不要通过公开反向代理转发无鉴权会话接口，也不要把 CORS 当作身份认证。

## 检查和测试

```powershell
npm run typecheck
npm run lint
npm run build
```

构建使用系统字体，不依赖 Google 字体网络请求。
Playwright 1.63.0 为开发依赖，浏览器仅在显式安装测试环境时下载：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = 'D:\rag_system\backend\.phase13-m5.tmp\browsers'
npx playwright install chromium
$env:NEXT_PUBLIC_API_BASE_URL = 'http://127.0.0.1:18005'
npm run build
npm run test:e2e -- --project=mock-api
```

模拟 API 套件拦截所有会话请求，不使用业务库。测试独占回环 3305，不能复用已有服务。
真实 HTTP + PostgreSQL + 浏览器验证由 `backend/tests/phase13_browser/` 驱动，
必须通过 M1–M4 专用数据库地址、角色、cluster 与名称确认门槛，并显式设置
`PHASE13_BROWSER_E2E=1`。只有该驱动生成隔离 manifest 后才选择真实项目；
浏览器另有网络目标校验，旧构建误指向业务 API 会被拒绝。
完整命令、测试结果和 M6 准备见 [M5 验收记录](../docs/phase-13-m5-acceptance.md)。

## 前端契约

- `qa-sessions.ts` 对接七个会话 API，处理 envelope、HTTP 状态、超时和归属检查。
- `qa-chat.ts` 每次挂载只管理一个 thread；完整消息真相来自服务器。
- sessionStorage 只保存未完成请求的固定参数；成功后清除。没有历史缓存或自动 POST 重试。
- 202 最多查询十次；暂停后可手动查询。只有用户点击恢复且服务器允许，才用原 request_id 重试。
- 已删除来源以 tombstone 展示，保留回答正文和剩余引用的原编号。

旧单轮前端已删除，旧后端接口及共享 RAG 服务保留。未增加流式输出或前端后台任务。
