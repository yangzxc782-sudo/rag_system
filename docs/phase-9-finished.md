# Phase 9 完成基线：Local/API 双通道 LLM Provider

**日期：** 2026-07-21
**阶段：** Phase 9 / M5
**状态：** Phase 9 / M0—M5 已完成并通过项目负责人验收
**最终审查状态：** `PHASE9_FINISHED`

## 1. 交接目的

本文记录 Phase 9 的 as-built 能力、自动化证据、只读数据快照、人工验收边界和后续禁止扩展项。它不授权开始 Chat API、LangChain、LangGraph、Agent、多轮 RAG、工具执行、多模态或任何新阶段。

## 2. Git 起点与提交边界

M5 开始时：

```text
branch: main
HEAD: a337ce655d9fd5d12db68d8c5bece3a0e6ec855a
origin/main: 2789b6bd847c9a76ef66b65f388899464640dd2c
staged: empty
tracked modifications: empty
untracked: empty
git diff --check: pass
```

本地 HEAD 领先 `origin/main`，本轮没有 push。

Phase 9 独立提交：

| 边界 | 提交 | 内容 |
|---|---|---|
| 设计与计划 | `2789b6b` | 双通道 Provider 设计和实施计划 |
| M0 | `3de9323` | `think` SDK/wire 测试契约 |
| M1A | `ee939c8` | message-first 正式契约 |
| M1B | `7254a72` | Local/config/startup/transport/cache |
| M2 | `fdbabeb` | Generic API Adapter |
| M3 | `74bbf5c` | 业务兼容迁移和正式 Provider 接线 |
| M4 | `a337ce6` | 错误、安全和能力可观测性 |

M5 文档与自动化证据已由提交 `d1263bc` 形成 Phase 9 完成基线；本文后续统一命名为 `docs/phase-9-finished.md`。

## 3. Phase 9 完整验收清单

### 3.1 Provider 与配置

- [x] 正式 Provider 为 `local` 和 `api`；
- [x] `openai_compatible` 仅作为 deprecated Local alias；
- [x] 只校验 active Provider 配置；
- [x] Local 不要求 Remote key；
- [x] API 必须配置 Remote base URL、key 和 model；
- [x] `create_app()` 在 route imports 前主动校验；
- [x] factory 防御性复验；
- [x] 修改 `.env` 后需要重启；
- [x] 不支持运行时动态切换；
- [x] 不自动 fallback；
- [x] Remote JSON capability 默认 false。

### 3.2 Message-first 契约

- [x] system/user/assistant/tool 受控消息结构；
- [x] text/image content parts；
- [x] assistant tool calls、tool call ID 和 tool message；
- [x] 全序列 tool call ID 唯一；
- [x] tool response 只能引用此前 call，且同一 call 只有一个 response；
- [x] tool schema parameters 是 strict JSON object，并防御性深复制；
- [x] `LLMGenerateRequest.messages` 是唯一正式输入；
- [x] `from_prompt()` 是当前单轮业务兼容入口；
- [x] `LLMGenerateResult.message` 返回完整 assistant message；
- [x] `result.text` 返回唯一 assistant text part；
- [x] Local/API wire 保持 system/user/assistant 文本多轮与 assistant 历史顺序；
- [x] 当前 REST schema 不开放 messages/history。

### 3.3 Capabilities 与 transport

- [x] Local JSON mode=true、think=true、tools=false、parallel tools=false、image=false；
- [x] API JSON mode 由配置控制，think/tools/parallel/image=false；
- [x] capability rejection 在 payload/SDK/HTTP 前完成；
- [x] Local `think` 通过 SDK `extra_body`，最终 wire 顶层为 `think`；
- [x] API advisory think 被省略，required think 被拒绝；
- [x] Generic API 不发送 `think` 或 `extra_body`；
- [x] API JSON capability=false 时 knowledge extraction 零网络拒绝；
- [x] API JSON capability=true 时发送 `response_format={"type":"json_object"}`；
- [x] SDK `max_retries=0`；
- [x] 没有 streaming、Responses API、retry 或 fallback。

### 3.4 生命周期与 no-context

- [x] Provider/SDK client 进程内惰性缓存；
- [x] 锁保护构造和 cache 摘除；
- [x] clear/close 幂等；
- [x] clear 后下次 get 创建新实例；
- [x] FastAPI shutdown 关闭业务实际使用的 client；
- [x] startup 不构造 client；
- [x] RAG no-context 使用 `resolve_active_llm_metadata()`；
- [x] local/api/legacy metadata 规范化正确；
- [x] no-context 不构造 Provider、transport/client 或填充 cache；
- [x] no-context 仍为 HTTP 200。

### 3.5 错误、安全与可观测性

- [x] 配置、Provider、请求、capability、上游和响应错误分类稳定；
- [x] RAG 与 knowledge-items 共享 Provider 中立 LLM HTTP 映射；
- [x] 429 为 HTTP 429、`retryable=true`，但不自动重试；
- [x] BusinessError 不保存 SDK exception/request/response；
- [x] 映射后的 BusinessError `__cause__` 和 `__context__` 均为空；
- [x] Remote key 在 Settings/transport 中保持 `SecretStr`；
- [x] 日志不记录 messages、prompt、tools、image URL、key、Authorization、upstream body/headers；
- [x] 日志只使用受控 provider/model/operation/latency/usage/request ID/error/capability/status 等字段；
- [x] 不承诺 Python 物理擦除密钥内存。

### 3.6 URL 安全

- [x] HTTPS 默认允许；
- [x] loopback HTTP 默认允许；
- [x] 其他 HTTP 需要显式 insecure opt-in；
- [x] insecure warning 每进程一次，并只记录 scheme/hostname；
- [x] userinfo、query、fragment、缺 host 和非 HTTP(S) scheme 被拒绝。

### 3.7 业务与受保护子系统

- [x] RAG 使用 `from_prompt()` 和缓存 Provider；
- [x] knowledge extraction 使用 `from_prompt()`、JSON mode 和 advisory `think=False`；
- [x] Local/API 使用同一业务路径；
- [x] 公开 REST 请求/响应 schema 不变；
- [x] embedding model/dimension 未改变；
- [x] OpenSearch mapping/alias/index 未改变；
- [x] MinerU、block-aware chunking 和 parse 状态未改变；
- [x] 没有 migration。

## 4. 自动化测试结果

### 4.1 Backend 全量

命令：

```powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase9-m5-final-pytest-tmp
```

结果：

```text
583 passed, 2 warnings in 5.10s
```

两条 warning 都是既有 Starlette/FastAPI deprecation warning，不是测试失败。

### 4.2 Phase 9 聚焦集

覆盖 messages、Local/API Provider、配置、启动、cache、RAG、knowledge extraction 和 API envelope：

```text
252 passed, 2 warnings in 4.62s
```

### 4.3 Embedding/OpenSearch/MinerU 隔离集

覆盖 embedding、document embedding、vector/hybrid search、search index、parse models、MinerU client/normalizer/orchestration 和 parse API：

```text
145 passed, 2 warnings in 1.41s
```

首次未指定 `--basetemp` 的聚焦运行因系统 pytest 临时目录拒绝访问产生 6 个 setup error；使用工作区内 `.venv\phase9-m5-isolation-pytest-tmp` 重跑后全部通过。没有为此修改代码或测试。

### 4.4 MockTransport 证据

自动化测试已经证明：

- Local 四轮 system/user/assistant/user 顺序不变；
- Local JSON mode 与 wire 顶层 `think=false` 同时存在；
- 未提供 think 时不发送；
- API 四轮历史顺序不变；
- API JSON capability false 为零网络 rejection；
- API JSON capability true 发送 `json_object`；
- API advisory think true/false 均不发送 `think/extra_body`；
- tools/image/parallel/required think 在网络前拒绝；
- 401/403/404/timeout/connection/429/5xx/其他 4xx 分类和脱敏正确；
- usage 与受控 request ID 保留；
- client cache、clear、close 和 shutdown 生命周期正确。

所有证据来自 fake client/SDK mock/HTTPX MockTransport，没有真实远程调用。

## 5. 前端验证

### 5.1 Production build

`npm.cmd run build` 的 JavaScript 编译成功，随后 TypeScript 在既有文件失败：

```text
frontend/components/KnowledgeItemsPanel.tsx:270
ApiEnvelope<KnowledgeItemChunksData | KnowledgeItemVersionsData | KnowledgeItemReviewsData>
联合类型不能传给只接受 KnowledgeItemChunksData 的 responseError 参数。
```

该文件最后修改提交为 `669b392`（Phase 7），在 `2789b6b..a337ce6` Phase 9 范围中无 diff。

### 5.2 ESLint

`npm.cmd run lint` 在既有文件失败：

```text
frontend/components/DocumentParseResults.tsx:254
react-hooks/set-state-in-effect
```

该文件最后修改提交为 `58d11b6`（Phase 8），在 Phase 9 范围中无 diff。

Phase 9 实际修改的前端文件：

```text
frontend/lib/rag.ts
frontend/lib/knowledge-items.ts
```

定向命令 `npx.cmd eslint lib/rag.ts lib/knowledge-items.ts` 通过。`frontend/package.json` 没有 frontend test script。

M5 未修改上述两个既有债务文件。

## 6. PostgreSQL embedding 只读快照

快照在显式 read-only transaction 中取得，没有调用 embedding service：

| 指标 | M5 前 | M5 文档后 |
|---|---:|---:|
| document_chunks | 110 | 110 |
| `embedding_status=embedded` | 106 | 106 |
| 非空 vector | 106 | 106 |
| 非空 model 集合 | `Qwen3-Embedding-0.6B` | 相同 |
| 非空 dimension 集合 | `1024` | 相同 |
| 有序 `chunk_id + vector` SHA-256 | `37468b3772d0188d74512254342f672c53093171581fac59942c88c9cf27a9fb` | 相同 |

Phase 8 没有记录全库向量 checksum。这里的前后相同证明 M5 未改变当前向量；不能声称与不存在的 Phase 8 历史 checksum 对比。

两个 Phase 8 真实 PDF：

| 文档 | parse run | pages | blocks | assets | chunks/embedded/vectors |
|---|---|---:|---:|---:|---:|
| `test1.pdf` | `5f574d15-af67-469e-a761-0bb9648a557a` | 4 | 88 | 15 | 25/25/25 |
| `test.pdf` | `bc0833e6-78ff-40c6-a5a7-da297b843786` | 15 | 183 | 17 | 78/78/78 |

两个 parse run 均保持 `mineru_api`、`vlm`、`succeeded`、`is_active=true`。

## 7. OpenSearch 只读快照

快照只使用 alias/mapping/count/search aggregation GET/read API，没有 create、delete、bulk、rebuild 或 sync：

| 指标 | M5 前 | M5 文档后 |
|---|---|---|
| alias | `casting_chunks_current` | 相同 |
| alias target | `casting_chunks_v1` | 相同 |
| document count | 105 | 105 |
| duplicate `chunk_id` buckets | 0 | 0 |
| mapping SHA-256 | `ca3156f6515f3a5b864f997997da14bd855a24b9ce0ac631010c9a36afdd4e8a` | 相同 |
| vector dimension | 1024 | 1024 |
| vector space | `cosinesimil` | 相同 |
| content analyzer/search analyzer | `ik_max_word` / `ik_smart` | 相同 |

OpenSearch document count 105 与 Phase 8 final handoff 一致。

## 8. MinerU 隔离结论

- 没有调用 parse/reparse API；
- 没有调用 MinerU 远程 API；
- 没有修改 MinerU transport、normalizer、orchestration 或 block-aware chunker；
- 两个已验收 active parse run 的状态和计数与 Phase 8 handoff 一致；
- Phase 9 commit range 不包含 embedding、OpenSearch mapping、MinerU 或 chunker 生产代码变更。

当前环境的 `docker` CLI 不在 PATH，因此没有使用 `docker compose ps`；PostgreSQL 和 OpenSearch 服务通过应用配置的端点完成了只读核验。

## 9. 配置摘要

Local：

```env
LLM_PROVIDER=local
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
```

Generic API：

```env
LLM_PROVIDER=api
LLM_REMOTE_BASE_URL=https://api.example.invalid/v1
LLM_REMOTE_API_KEY=
LLM_REMOTE_MODEL=example-chat-model
LLM_REMOTE_TIMEOUT_SECONDS=60
LLM_REMOTE_SUPPORTS_JSON_MODE=false
LLM_REMOTE_ALLOW_INSECURE_HTTP=false
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048
```

真实 key 只写入未跟踪的本机 `.env`。普通 RAG 不要求 JSON mode；knowledge extraction 要求 JSON object，只有供应商真实兼容时才把 capability 设置为 true。切换 Provider 需要重启，不存在 fallback。Embedding 始终是本地 Qwen3 模型。

## 10. 安全结论

- Remote key 在 Settings/transport 中保持 `SecretStr`；
- 配置校验局部短暂读取只用于非空检查，不保存、不返回、不记录；
- SDK client 惰性创建时才把明文直接传给 factory；
- Python 不能保证物理擦除密钥内存；
- 错误、日志、traceback 和 API response 不记录 messages/prompt/tool/image/key/Authorization/upstream body/headers；
- 429 `retryable=true` 不会触发自动 retry；
- HTTPS 默认允许，loopback HTTP 允许，其他 HTTP 需显式 opt-in 并只记录脱敏 hostname；
- `openai_compatible` 只是 deprecated Local alias。

## 11. 已实现能力

- Local/API 双通道 Provider；
- env + restart 选择；
- message-first 内部契约；
- system/user/assistant 文本多轮与 assistant 历史；
- `from_prompt()`；
- `result.message` 与 `result.text`；
- Local Ollama 与 Generic API Adapter；
- 配置驱动 API JSON capability；
- Local think 扩展；
- capability preflight；
- Provider/client cache 与 shutdown close；
- startup validation 与 URL 安全；
- 错误分类、脱敏和结构化日志；
- RAG/knowledge extraction 正式接线；
- 既有 REST schema 兼容。

## 12. 未实现能力

- 对外无状态 Chat API；
- REST messages/history；
- 会话/消息持久化；
- 多轮 RAG、查询重写或历史裁剪；
- LangChain Adapter；
- LangGraph 或 Agent loop；
- 工具注册、工具执行或 tool calling wire transport；
- 多模态上传、图片转发或本地文件读取；
- streaming/SSE；
- 自动 retry；
- Local/API fallback；
- 运行时 Provider 热切换；
- API embedding。

## 13. 保留的人工验证边界（不阻塞 Phase 9 完成）

Phase 9 已基于自动化与只读数据证据通过验收。以下真实端到端步骤仅供后续在负责人另行授权时补充验证，不构成 Phase 9 待审状态：

1. 启动真实 Local Ollama，验证普通 RAG、knowledge extraction、no-context、JSON mode 和 `think=false`；
2. 提供临时 Remote test key 并明确授权后，验证普通 RAG；
3. 验证 Remote JSON capability false/true 两条 knowledge extraction 路径；
4. 使用受控非生产 endpoint 验证 401/429/timeout 展示；
5. 人工检查浏览器、日志和错误中没有 key；
6. 按 `docs/manual-acceptance.md` 在真实服务切换前后复核数据快照。

没有负责人授权时，不得调用真实远程 API，也不得把 MockTransport 证据表述为真实供应商验收。

## 14. M5 文档修改范围

- `README.md`
- `docs/local-development.md`
- `docs/manual-acceptance.md`
- `docs/phase-9-finished.md`（Phase 9 唯一完成基线）
- `docs/superpowers/specs/2026-07-20-phase-9-local-api-llm-provider-design.md`
- `docs/superpowers/plans/2026-07-20-phase-9-local-api-llm-provider-implementation-plan.md`

没有修改业务代码、测试、真实 `.env`、migration、embedding、OpenSearch 或 MinerU。

## 15. 最终停止点

Phase 9 / M0—M5 已完成并通过项目负责人验收。后续阶段统一以本文作为 Phase 9 完成基线；本文不授权扩大 Phase 9 范围。

`PHASE9_FINISHED`
