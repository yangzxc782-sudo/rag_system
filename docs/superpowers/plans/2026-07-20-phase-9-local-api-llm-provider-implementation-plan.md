# Phase 9 Local/API LLM Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development or superpowers:executing-plans
> to implement this plan task-by-task.

**Goal:** 在不改变本地 embedding、OpenSearch、MinerU、数据库 schema 和现有 REST 契约的前提下，实现 message-first 的 Local/API 双通道 LLM Provider。

**Architecture:** 上层业务依赖统一 `LLMProvider`。`LocalLLMProvider` 和 `APILLMProvider` 分别负责 active 配置与 capability，共享 `OpenAIChatTransport` 进行 OpenAI-compatible Chat Completions 文本传输。Provider 和 SDK client 作为进程内惰性单例复用，通过显式 cache clear 和 FastAPI shutdown 幂等关闭。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic Settings v2、OpenAI Python SDK、HTTPX MockTransport、pytest。

## Execution Status（2026-07-21）

本表是里程碑实际执行状态；后文复选框保留为实施时的验收定义，不应再被解释为当前未完成状态。

| 检查点 | 状态 | 独立提交/证据 |
|---|---|---|
| 文档落盘门禁 | 已验收 | `2789b6b` |
| M0 | 已验收 | `3de9323` |
| M1A | 已验收 | `ee939c8` |
| M1B | 已验收 | `7254a72` |
| M2 | 已验收 | `fdbabeb` |
| M3 | 已验收 | `74bbf5c` |
| M4 | 已验收 | `a337ce6` |
| M5 | 自动回归与文档完成，待负责人终审 | 当前仅文档 diff，未提交 |

## Global Constraints

- 本计划只改造 LLM generation，不增加 API embedding。
- Embedding model 固定为 `Qwen3-Embedding-0.6B`，dimension 固定为 `1024`。
- 不重新向量化已有数据，不修改 PostgreSQL embedding。
- 不修改 OpenSearch index、alias、mapping 或已有 documents。
- 不修改 MinerU、block-aware chunking 或解析数据流。
- 不新增 migration。
- 不实现运行时 Provider 切换或 Local/API fallback。
- 不实现 streaming、SSE、Responses API、Agent loop 或工具执行。
- 不开放多轮 REST API，不修改 `/rag/ask` 接收历史 messages。
- 不实现会话持久化、历史裁剪、查询重写或多轮 RAG。
- 不实现多模态上传、图片转发或本地文件自动读取。
- 自动化测试不得调用真实远程 API。
- API key 不得进入 Git、日志、响应、测试快照或前端。
- 每个里程碑都必须独立测试并停在负责人检查点。
- 未经项目负责人单独授权，不执行 commit、push、amend、reset、stash 或 clean。

---

## 1. 实施前基线

当前本地 LLM 通过 OpenAI Python SDK 调用 Ollama 的 `/v1/chat/completions`。现有业务调用点只有 `backend/app/services/rag.py` 和 `backend/app/services/knowledge_extraction.py`。

现有正式请求以 `prompt/system_prompt` 为主，结果以 `text` 为主。`think` 已经通过 SDK `extra_body` 发送，现有测试错误地把它断言为 SDK 顶层参数。

实施前命令：

~~~powershell
cd D:\rag_system
git branch --show-current
git rev-parse HEAD
git status --short
git diff --check
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_provider.py -q -p no:cacheprovider
~~~

预期基线是 `test_generate_passes_optional_json_mode_and_think_parameters` 因 fake 中缺少顶层 `think` 而失败。若基线不同，停止并调查。

## 2. 最终内部接口

### 2.1 Message types

创建 `backend/app/llm/messages.py`：

~~~python
LLMTextContentPart(type: Literal["text"], text: str)
LLMImageURLContentPart(
    type: Literal["image_url"],
    image_url: str,
    detail: Literal["auto", "low", "high"] = "auto",
)
LLMFunctionCall(name: str, arguments: str)
LLMToolCall(
    id: str,
    type: Literal["function"],
    function: LLMFunctionCall,
)
LLMFunctionTool(
    name: str,
    description: str | None,
    parameters: Mapping[str, JSONValue],
)
LLMMessage(
    role: Literal["system", "user", "assistant", "tool"],
    content: tuple[LLMContentPart, ...] = (),
    tool_calls: tuple[LLMToolCall, ...] = (),
    tool_call_id: str | None = None,
)
~~~

纯文本模式下每条消息恰好一个 text part，不拼接多个 text parts。多个 parts 只为未来含 image 的 content array 预留；当前 Provider 在网络调用前拒绝 image input。

### 2.2 Request and result

~~~python
LLMCapabilities(
    supports_json_mode: bool,
    supports_think: bool,
    supports_tools: bool,
    supports_parallel_tool_calls: bool,
    supports_image_input: bool,
)
LLMGenerateRequest(
    messages: tuple[LLMMessage, ...],
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    think: bool | None = None,
    think_required: bool = False,
    timeout_seconds: float | None = None,
    stop: tuple[str, ...] | None = None,
    tools: tuple[LLMFunctionTool, ...] = (),
    parallel_tool_calls: bool | None = None,
)
LLMGenerateResult(
    message: LLMMessage,
    provider: Literal["local", "api"],
    model: str,
    usage: LLMUsage | None = None,
    request_id: str | None = None,
)
~~~

`LLMGenerateRequest.from_prompt(prompt, system_prompt, ...)` 是当前业务兼容构造器。`LLMGenerateResult.text` 是只读兼容属性，返回唯一 assistant text part。

### 2.3 Tool linkage

Request 构造时必须保证：

- assistant tool call ID 全消息序列唯一；
- tool message 引用此前 assistant tool call；
- 同一 tool call 只有一个 tool response；
- tool call 可以暂时没有 response；
- 单 assistant message 多 calls 需要 parallel capability。

`LLMFunctionTool.parameters` 必须是可 JSON 序列化 object，通过 `allow_nan=False` 的 JSON round-trip 形成防御性拷贝。

## 3. 完整配置

~~~dotenv
LLM_PROVIDER=local
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048

LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120

LLM_REMOTE_BASE_URL=
LLM_REMOTE_API_KEY=
LLM_REMOTE_MODEL=
LLM_REMOTE_TIMEOUT_SECONDS=60
LLM_REMOTE_SUPPORTS_JSON_MODE=false
LLM_REMOTE_ALLOW_INSECURE_HTTP=false
~~~

解析规则：

| Input | Normalized | Active fields |
|---|---|---|
| `local` | local | `LLM_BASE_URL/MODEL/API_KEY/TIMEOUT` |
| `api` | api | `LLM_REMOTE_*` |
| `openai_compatible` | local | Local fields，警告一次 |
| 其他 | error | `LLM_PROVIDER_INVALID` |

Local key 可以为空，Adapter 使用非秘密 `ollama` 占位值。Local 不校验 remote key。API 只使用 remote key，不回退读取本地占位值。

Generic API JSON mode 由 `LLM_REMOTE_SUPPORTS_JSON_MODE` 控制，默认 false。False 时 `json_mode=True` 在网络前返回 `LLM_PARAMETER_UNSUPPORTED`。

### 3.1 远程 URL 安全

- HTTPS 默认允许；
- HTTP loopback `localhost`、`127.0.0.1`、`[::1]` 允许；
- 其他 HTTP 仅在 `LLM_REMOTE_ALLOW_INSECURE_HTTP=true` 时允许；
- 非 loopback HTTP 每进程记录一次只含 scheme/hostname 的警告；
- 拒绝 userinfo、query、fragment；
- 拒绝非 HTTP(S) scheme；
- 错误和日志不得输出完整 URL、key 或 Authorization。

## 4. Capability 行为

| Capability | Local | Generic API |
|---|---:|---:|
| system/user/assistant text | true | true |
| JSON mode | true | 配置控制，默认 false |
| think | true | false |
| tools | false | false |
| parallel tool calls | false | false |
| image input | false | false |

所有 capability preflight 在 payload/SDK 调用之前完成。Advisory `think=False` 在不支持 think 的 API 上省略；`think_required=True` 则明确拒绝。Tools、tool messages、assistant tool calls、parallel calls 和 image input 当前均在网络前拒绝。

## 5. 错误码、HTTP 和安全

| Code | HTTP | 自动重试 |
|---|---:|---:|
| `LLM_CONFIG_INVALID` | 400/启动失败 | 否 |
| `LLM_PROVIDER_INVALID` | 400/启动失败 | 否 |
| `LLM_REQUEST_INVALID` | 400 | 否 |
| `LLM_PARAMETER_UNSUPPORTED` | 400 | 否 |
| `LLM_AUTHENTICATION_FAILED` | 502 | 否 |
| `LLM_PERMISSION_DENIED` | 502 | 否 |
| `LLM_MODEL_NOT_FOUND` | 502 | 否 |
| `LLM_TIMEOUT` | 504 | 否 |
| `LLM_UNAVAILABLE` | 503 | 否 |
| `LLM_RATE_LIMITED` | 429 | 否 |
| `LLM_UPSTREAM_FAILED` | 502 | 否 |
| `LLM_REQUEST_REJECTED` | 502 | 否 |
| `LLM_RESPONSE_INVALID` | 502 | 否 |
| `LLM_EMPTY_CONTENT` | 502 | 否 |
| `LLM_JSON_INVALID` | 502 | 否 |
| `LLM_GENERATION_FAILED` | 500 | 否 |

SDK 必须设置 `max_retries=0`，不自动 fallback。

日志只允许 Provider、model、latency、usage、request ID、安全 status、error code、capability 和脱敏 hostname。禁止记录 key、Authorization、完整 headers、URL query/userinfo、prompt/messages/response、image URL、tool arguments/parameters、完整 remote body 或未经脱敏的 `str(exc)`。

## 6. 文件地图

### 新建

- `backend/app/llm/messages.py`
- `backend/app/llm/configuration.py`
- `backend/app/llm/openai_chat_transport.py`
- `backend/app/llm/local.py`
- `backend/app/llm/api.py`
- `backend/tests/test_llm_messages.py`
- `backend/tests/test_llm_config.py`
- `backend/tests/test_llm_startup.py`
- `backend/tests/test_api_llm_provider.py`

### 修改

- `backend/app/llm/provider.py`
- `backend/app/llm/__init__.py`
- `backend/app/core/config.py`
- `backend/app/core/errors.py`
- `backend/app/main.py`
- `backend/app/services/rag.py`
- `backend/app/services/knowledge_extraction.py`
- 两个相关 API route 的错误映射
- 相关 RAG/knowledge extraction/API 测试
- `.env.example` 和 `backend/.env.example`
- README、本地开发和人工验收文档

### 删除

- `backend/app/llm/openai_compatible.py`，仅在 M1B 完成职责迁移后删除。

## 7. 文档落盘门禁

**目标：** 确认设计和计划已经批准，仓库只有两份计划文档变更。

**修改文件：**

- `docs/superpowers/specs/2026-07-20-phase-9-local-api-llm-provider-design.md`
- `docs/superpowers/plans/2026-07-20-phase-9-local-api-llm-provider-implementation-plan.md`

**禁止范围：** 所有业务代码、测试、`.env`、migration、embedding、OpenSearch 和 MinerU。

**实施步骤：**

- [ ] 检查文档没有未解释占位符、外部草稿引用或截断章节。
- [ ] 运行 `git diff --check`。
- [ ] 运行 `git status --short`，确认只有两份文档。
- [ ] 等待项目负责人书面批准进入 M0。

**测试命令：**

~~~powershell
git diff --check
git status --short
git --no-pager diff --stat
~~~

**验收标准：** 只有两份 Markdown 文档为 untracked/modified，内容完整自包含。

**停止点：** `AWAITING_PROJECT_OWNER_DOCUMENT_REVIEW`。

**回退原则：** 不自动清理；负责人要求修订时只编辑这两份文档。

---

## 8. M0：既有 LLM 测试基线

**目标：** 修复 `think` fake 断言边界，锁定真实 wire semantics。

**修改文件：** `backend/tests/test_llm_provider.py`。

**禁止范围：** 不修改 Provider 实现、业务 service、配置、embedding、OpenSearch、MinerU 或 migration。

**实施步骤：**

- [ ] 运行现有测试，确认单一 `KeyError: think` 红灯。
- [ ] 把 fake 断言改为 `calls[0]["extra_body"]["think"] is False`。
- [ ] 添加真实 OpenAI SDK + HTTPX MockTransport 测试。
- [ ] 断言最终 JSON 顶层包含 `"think": false`。
- [ ] 断言 JSON response format 同时存在。
- [ ] 添加普通请求不发送 think 的测试。
- [ ] 运行该测试文件，确认全部通过。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_provider.py -q -p no:cacheprovider
~~~

**验收标准：** 测试全部通过，业务代码 diff 为空，不把 think 改成 SDK 顶层参数。

**停止点：** M0 测试 diff 负责人审查。

**回退原则：** 只撤销 M0 测试文件变更。

---

## 9. M1A：Message-first 契约

**目标：** 建立独立于 Provider/SDK 的 message、tool、request/result 类型和结构校验。

**修改文件：**

- 创建 `backend/app/llm/messages.py`
- 修改 `backend/app/llm/provider.py`
- 修改 `backend/app/llm/__init__.py`
- 修改 `backend/app/core/errors.py`
- 创建 `backend/tests/test_llm_messages.py`
- 修改 `backend/tests/test_llm_provider.py`

**禁止范围：** 不创建 Adapter，不改 Settings、FastAPI 启动或业务 service，不调用模型。

**实施步骤：**

- [ ] 添加 system/user/assistant/tool 合法结构测试。
- [ ] 添加空 text 和非法 role 字段组合测试。
- [ ] 添加两个纯 text parts 被拒绝的测试。
- [ ] 添加 image URL 拒绝 file/local path 的测试。
- [ ] 添加 tool call ID 全序列重复测试。
- [ ] 添加 tool message 引用不存在或后续 call 的测试。
- [ ] 添加同一 call 两个 responses 的测试。
- [ ] 添加允许未响应 tool call 的测试。
- [ ] 添加 function parameters 顶层非 object、不可序列化和 NaN 测试。
- [ ] 添加原 parameters 深层修改不影响内部状态的测试。
- [ ] 实现 content、tool、message 类型和角色校验。
- [ ] 实现 request-level tool linkage 线性扫描。
- [ ] 添加 `from_prompt()` 单 user 和 system→user 顺序测试。
- [ ] 实现 message-based Request 和兼容构造器。
- [ ] 添加 Result assistant message 和 `result.text` 测试。
- [ ] 移除正式 Request prompt/system_prompt 字段和 Result raw 字段。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_messages.py tests/test_llm_provider.py -q -p no:cacheprovider
~~~

**验收标准：** 正式输入只有 messages；多 text parts 不拼接；tool linkage 和 parameters 防御复制通过；没有 transport、配置或业务变更。

**停止点：** M1A 契约负责人审查。

**回退原则：** M1A 文件作为一个边界整体回退，不触碰 M0 语义。

---

## 10. M1B：Local、配置、启动、transport 和 cache

**目标：** 实现 Local Adapter、配置规范化、启动校验、metadata、共享 transport 和并发安全缓存。

**修改文件：**

- 创建 `backend/app/llm/configuration.py`
- 创建 `backend/app/llm/openai_chat_transport.py`
- 创建 `backend/app/llm/local.py`
- 删除 `backend/app/llm/openai_compatible.py`
- 修改 `backend/app/llm/provider.py`
- 修改 `backend/app/llm/__init__.py`
- 修改 `backend/app/core/config.py`
- 修改 `backend/app/main.py`
- 修改两个 `.env.example`
- 创建 `backend/tests/test_llm_config.py`
- 创建 `backend/tests/test_llm_startup.py`
- 修改 `backend/tests/test_llm_provider.py`

**禁止范围：** 不实现 API Adapter，不修改业务 service，不修改真实 `.env`。

**Interfaces：**

~~~python
ActiveLLMMetadata(provider, model)
resolve_active_llm_metadata(settings)
validate_active_llm_configuration(settings)
build_llm_provider(settings, *, client=None)
get_llm_provider()
clear_llm_provider_cache()
~~~

**实施步骤：**

- [ ] 添加 local/api/legacy/unknown metadata 测试并实现 metadata resolver。
- [ ] 添加 local 不要求 remote key、api 缺必填项和 alias 警告一次测试。
- [ ] 添加 HTTPS、loopback HTTP、insecure HTTP flag 测试。
- [ ] 添加 userinfo/query/fragment/非 HTTP(S) 拒绝测试。
- [ ] 实现 active-only `validate_active_llm_configuration()`。
- [ ] 添加 `create_app(settings=...)` 最小 local 启动测试。
- [ ] 添加 local 无 remote key 可导入/启动测试。
- [ ] 添加 api 无 key 启动失败测试。
- [ ] 添加干净环境不依赖开发者 `backend/.env` 的测试。
- [ ] 将 `api_router` import 移到 active 校验之后。
- [ ] 添加 factory 绕过 app 时仍拒绝无效配置的测试。
- [ ] 实现 `build_llm_provider()` 防御性复验。
- [ ] 添加并发 get 只构造一个 Provider/client 的测试。
- [ ] 添加未创建 clear、连续 clear、clear 后重建测试。
- [ ] 添加旧 client 只 close 一次和 Provider close 幂等测试。
- [ ] 添加 FastAPI shutdown 清理且不泄漏 client 的测试。
- [ ] 实现锁保护构造和 cache 摘除；在锁外执行 close。
- [ ] 把共享传输迁移到 `openai_chat_transport.py`。
- [ ] 创建 Local Adapter，声明 JSON/think true，tools/parallel/image false。
- [ ] 添加 Local 四轮文本 wire-level 测试，断言 role、顺序和 text 不变。
- [ ] 添加 Local tool/image capability 零网络测试。
- [ ] 更新两个环境变量示例，key 为空。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_messages.py tests/test_llm_provider.py tests/test_llm_config.py tests/test_llm_startup.py -q -p no:cacheprovider
~~~

**验收标准：** create_app 主动校验；factory 防御复验；干净 CI 不依赖真实 env；Local 多轮 wire 正确；cache/clear/close/shutdown 幂等并发安全；旧模块名移除但 alias 兼容。

**停止点：** M1B Local/config/lifecycle 负责人审查。

**回退原则：** 恢复原 Adapter 和 factory；不修改环境文件或用户数据。

---

## 11. M2：Generic API Adapter

**目标：** 实现配置驱动 capabilities 的 OpenAI-compatible API Adapter。

**修改文件：**

- 创建 `backend/app/llm/api.py`
- 修改 `backend/app/llm/configuration.py`
- 修改 `backend/app/llm/provider.py`
- 修改 `backend/app/llm/openai_chat_transport.py`
- 修改 `backend/app/core/errors.py`
- 修改两个 `.env.example`
- 创建 `backend/tests/test_api_llm_provider.py`
- 扩展 `backend/tests/test_llm_config.py`

**禁止范围：** 不接业务 service，不实现 tools/image transport、streaming、retry 或 fallback。

**实施步骤：**

- [ ] 添加 remote JSON capability 默认 false 和显式 true 测试。
- [ ] 添加 false + json_mode 零网络 unsupported 测试。
- [ ] 添加 true 时 response_format wire 测试。
- [ ] 添加 API 四轮文本消息顺序测试。
- [ ] 添加 advisory think 省略和 required think 拒绝测试。
- [ ] 添加 tools/tool message/parallel/image 零网络测试。
- [ ] 实现 API Adapter capability 和 transport 接线。
- [ ] 添加 assistant message、usage、request ID 测试。
- [ ] 添加 401、403、model、timeout、connection、429、5xx 测试。
- [ ] 添加非 JSON response、空 choices/content、非法 JSON/object 测试。
- [ ] 添加意外 tool calls response invalid 测试。
- [ ] 断言 SDK `max_retries=0`。
- [ ] 断言 API 不读取 Local `LLM_API_KEY`。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_messages.py tests/test_llm_provider.py tests/test_llm_config.py tests/test_llm_startup.py tests/test_api_llm_provider.py -q -p no:cacheprovider
~~~

**验收标准：** API JSON capability 默认 false；能力不支持时零 HTTP；API 多轮 wire、错误和脱敏通过；无真实 API 调用。

**停止点：** M2 API payload/capability/error 负责人审查。

**回退原则：** 移除 API Adapter 和 remote factory 分支，保留 Local。

---

## 12. M3：Factory 与业务接线

**目标：** 让 RAG 和 knowledge extraction 只依赖 message-first Provider，同时保持 REST schema。

**修改文件：**

- `backend/app/services/rag.py`
- `backend/app/services/knowledge_extraction.py`
- `backend/tests/test_rag_service.py`
- `backend/tests/test_rag_api.py`
- `backend/tests/test_knowledge_extraction.py`
- `backend/tests/test_knowledge_item_api.py`

**禁止范围：** 不修改 hybrid search、embedding/index/parse/chunking，不新增 REST messages/history 字段。

**实施步骤：**

- [ ] 更新 service fake 以接收 message Request。
- [ ] 添加 RAG `from_prompt()` system→user 顺序测试。
- [ ] 将正常生成路径改为缓存 `get_llm_provider()`。
- [ ] 添加 no-context local/api/legacy metadata 测试。
- [ ] 断言 no-context 不调用 build/get Provider、不构造 client、不填 cache。
- [ ] 实现 no-context `resolve_active_llm_metadata()`。
- [ ] 添加 knowledge extraction `from_prompt()` 顺序测试。
- [ ] 将 raw response format 改为 `json_mode=True`，保留 advisory think false。
- [ ] 添加 API JSON capability false 明确拒绝和 true 成功测试。
- [ ] 继续通过 `result.text` 调用领域 parser。
- [ ] 断言现有 RAG/extraction REST schemas 不变。
- [ ] 断言 Provider 切换不调用 embedding/index 写函数。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_rag_service.py tests/test_rag_api.py tests/test_knowledge_extraction.py tests/test_knowledge_item_api.py -q -p no:cacheprovider
~~~

**验收标准：** 所有业务调用经过统一 Provider；no-context metadata 正确且零构造；REST 不变；受保护子系统无 diff。

**停止点：** M3 业务接线负责人审查。

**回退原则：** 只恢复两个 service 的调用方式，不回退 Provider。

---

## 13. M4：错误、安全和可观测性

**目标：** 完成 API error envelope、日志脱敏和 Provider 中立错误表达。

**修改文件：**

- `backend/app/core/errors.py`
- `backend/app/llm/messages.py`
- `backend/app/llm/configuration.py`
- `backend/app/llm/openai_chat_transport.py`
- `backend/app/llm/local.py`
- `backend/app/llm/api.py`
- RAG/knowledge-items API route 错误映射
- 对应测试
- 仅在文案写死本地模型时修改 `frontend/lib/rag.ts` 和 `frontend/lib/knowledge-items.ts`

**禁止范围：** 不修改 `DocumentParseResults.tsx`、`KnowledgeItemsPanel.tsx` 或其他无关前端问题。

**实施步骤：**

- [ ] 为全部 LLM error code 添加 API 映射测试。
- [ ] 添加 upstream 错误安全 detail 断言。
- [ ] 添加 key、Authorization、prompt/messages/response、URL query、image URL、tool 内容不进入 error 的负断言。
- [ ] 使用 `caplog` 添加同样的日志负断言。
- [ ] 添加允许的结构化日志字段正断言。
- [ ] 按 SDK exception 类型映射，禁止直接暴露 `str(exc)`。
- [ ] 添加 insecure HTTP 和 legacy alias 只警告一次的测试。
- [ ] 确认 cache 日志不含配置值或 key。
- [ ] 将直接相关前端文案改为 Provider 中立表达。
- [ ] 对实际修改的 frontend lib 运行 targeted ESLint。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe tests/test_llm_provider.py tests/test_llm_config.py tests/test_llm_startup.py tests/test_api_llm_provider.py tests/test_rag_api.py tests/test_knowledge_item_api.py -q -p no:cacheprovider
cd D:\rag_system\frontend
npx.cmd eslint lib/rag.ts lib/knowledge-items.ts
~~~

**验收标准：** 错误和 HTTP 映射完整；自动重试为零；敏感信息负断言通过；无关前端文件无 diff。

**停止点：** M4 error/log/frontend 文案负责人审查。

**回退原则：** 保留 Provider 功能，单独回退错误映射、日志或文案。

---

## 14. M5：全量回归、文档与人工验收

**目标：** 验证 Local 兼容、API 闭环、缓存生命周期以及 embedding/OpenSearch/MinerU 不变量。

**修改文件：**

- 两份 Phase 9 文档
- README、本地开发和人工验收文档
- 仅补充验收所需测试

**禁止范围：** 不修改 migration、真实 `.env`、无关前端债务、embedding、OpenSearch 或 MinerU。

**实施步骤：**

- [ ] 运行全部 targeted tests。
- [ ] 运行 backend 全量测试。
- [ ] 运行 embedding、vector、hybrid search 和 index 隔离回归。
- [ ] 记录现有前端债务，不在 Phase 9 修复。
- [ ] 更新配置、错误、安全和 as-built 文档。
- [ ] Local 模式完成启动、RAG、JSON、think 和文本多轮人工验收。
- [ ] 经授权后完成 API RAG 和 JSON capability false/true 验收。
- [ ] 切换前后比较 PostgreSQL embedding count/checksum。
- [ ] 比较 OpenSearch document count、alias 和 mapping。
- [ ] 比较 MinerU parse run 状态。
- [ ] 检查日志、前端和错误响应中没有 key。
- [ ] 运行 `git diff --check` 和最终 status。

**测试命令：**

~~~powershell
cd D:\rag_system\backend
.\.venv\Scripts\pytest.exe -q -p no:cacheprovider --basetemp=.venv\phase9-final-pytest-tmp
.\.venv\Scripts\pytest.exe tests/test_embeddings.py tests/test_document_embeddings.py tests/test_vector_search.py tests/test_hybrid_search.py tests/test_search_index.py -q -p no:cacheprovider
~~~

**验收标准：** backend 全量通过；Local/API 多轮 wire、URL、JSON capability、startup 和 cache 通过；REST 不变；embedding checksum、OpenSearch 和 MinerU 不变；无 secret。

**停止点：** Phase 9 最终项目负责人验收。

**回退原则：** 经授权的独立提交使用非破坏性 `git revert`；否则保留 diff 由负责人指定恢复范围。禁止 reset、clean 或删除数据。

### 14.1 M5 实际执行记录（2026-07-21）

- [x] Git 门禁通过，M0—M4 均为独立提交，起始工作区干净；
- [x] Backend 全量测试：`583 passed, 2 warnings`；
- [x] LLM/RAG/knowledge extraction 聚焦集：`252 passed, 2 warnings`；
- [x] embedding/OpenSearch/MinerU 隔离集：`145 passed, 2 warnings`；
- [x] 使用只读 SQL 与 OpenSearch GET 取得 M5 前后数据快照；
- [x] PostgreSQL embedding checksum、OpenSearch alias/count/mapping 和 MinerU parse 状态前后相同；
- [x] Phase 9 frontend lib 定向 ESLint 通过；
- [x] 记录两个不在 Phase 9 diff 中的既有前端失败，不扩大范围修复；
- [x] 更新设计、计划、README、本地开发、人工验收和 Phase 9 final handoff 文档；
- [ ] 真实 Local 端到端人工验收：需负责人按人工验收清单启动本地服务并确认；
- [ ] 真实 Remote API 人工验收：需负责人提供临时测试 key 并单独授权；
- [ ] M5 最终负责人验收与提交。

自动化覆盖 Local/API success、multi-turn wire、JSON capability false/true、Local `think`、capability 零网络拒绝、startup/cache/shutdown、错误映射和脱敏。所有远程路径使用 fake/MockTransport，未调用真实 API。

首次运行隔离聚焦集时未指定 `--basetemp`，系统临时目录 `C:\Users\32884\AppData\Local\Temp\pytest-of-32884` 权限导致 6 个 `tmp_path` setup error；使用工作区内 `--basetemp=.venv\phase9-m5-isolation-pytest-tmp` 重跑后 `145 passed`。这不是代码失败，也没有通过改代码规避。

## 15. 自动化测试矩阵

| 场景 | 测试文件 |
|---|---|
| think extra_body/wire | `test_llm_provider.py` |
| message roles/content | `test_llm_messages.py` |
| 多 text parts 不拼接 | `test_llm_messages.py` |
| tool ID/link/response | `test_llm_messages.py` |
| parameters 防御复制 | `test_llm_messages.py` |
| local/api/legacy/unknown | `test_llm_config.py` |
| remote URL 安全 | `test_llm_config.py` |
| API JSON capability | config/API tests |
| create_app 主动校验 | `test_llm_startup.py` |
| 干净环境 local 启动 | `test_llm_startup.py` |
| factory 防御复验 | `test_llm_provider.py` |
| cache 并发和幂等 clear | `test_llm_provider.py` |
| shutdown close | `test_llm_startup.py` |
| Local/API 多轮 wire | 两个 Provider tests |
| capability 零网络 | 两个 Provider tests |
| API success/errors | `test_api_llm_provider.py` |
| no-context 零构造 | `test_rag_service.py` |
| RAG/extraction 统一 Provider | 相关 service/API tests |
| 敏感信息脱敏 | Provider/API tests + caplog |
| embedding/index 不变量 | 隔离回归和人工快照 |

## 16. 人工验收边界

真实 API 调用只在 M5、负责人明确授权并提供临时本地 key 后执行。自动化测试和 CI 永远不需要真实 key。

Local/API 切换只通过：

~~~text
修改本地 .env
→ 完整重启后端
→ 验证规范化 Provider/model
~~~

不实现文件监听、热切换或 fallback。

## 17. 后续阶段

以下能力另行设计：

- 无状态 Chat API；
- 多轮 RAG、历史裁剪和查询重写；
- 会话持久化；
- 聊天前端；
- tool calling 和执行循环；
- 多模态上传、对象存储和 URL 转发。

Phase 9 只为 tools 和 image input 建立内部类型和 capability rejection，不宣称已提供运行能力。
