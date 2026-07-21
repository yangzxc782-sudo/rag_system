# Phase 9：本地/API 双通道大语言模型 Provider 设计

**日期：** 2026-07-20  
**状态：** 待项目负责人审查  
**阶段：** Phase 9  
**适用仓库：** `D:\rag_system`

## 1. 背景

当前系统已经完成文档上传、MinerU V4 解析、block-aware chunking、本地 embedding、OpenSearch 索引、混合检索、单轮 RAG 和 knowledge-items 抽取。现有大语言模型调用通过 OpenAI Python SDK 访问本机 Ollama 暴露的 OpenAI-compatible Chat Completions 接口。

当前两处 LLM 业务调用为：

1. RAG 答案生成；
2. knowledge-items JSON 抽取。

当前实现已有 `LLMProvider` Protocol、`LLMGenerateRequest`、`LLMGenerateResult` 和简单 factory，但只支持 `openai_compatible` 一个配置值，正式请求仍以 `prompt/system_prompt` 为中心，返回值仍以文本字符串为中心。

Phase 9 只增加 LLM 的 Local/API 双通道能力，并把内部契约升级为可扩展、受控的 message-first 契约。Embedding 继续固定使用本地 Qwen3-Embedding-0.6B、1024 维，不接入 API embedding。

## 2. 已保护的 Phase 8 边界

以下能力不得因 Phase 9 改造而改变：

~~~text
documents
→ document_parse_runs
→ document_blocks / document_assets
→ document_chunks
→ document_chunk_blocks
→ embedding
→ OpenSearch
→ search / RAG
~~~

- 文档解析、embedding 和 OpenSearch 索引同步继续是显式分离的操作；
- 解析成功后不得自动触发 embedding、索引或 RAG；
- embedding model 保持 `Qwen3-Embedding-0.6B`；
- embedding dimension 保持 `1024`；
- OpenSearch physical index 保持 `casting_chunks_v1`；
- alias 保持 `casting_chunks_current`；
- 不重新向量化已有数据；
- 不修改 MinerU、block-aware chunking、OpenSearch mapping 或已有 PostgreSQL 向量。

## 3. 目标

Phase 9 完成后：

- `LLM_PROVIDER=local` 使用本地 Ollama；
- `LLM_PROVIDER=api` 使用远程 OpenAI-compatible Chat Completions；
- 修改 `.env` 后通过重启后端切换；
- 上层业务只依赖统一 `LLMProvider` 契约；
- Local/API Adapter 都支持文本类型的 system、user 和 assistant 历史消息；
- Provider 内部支持文本多轮消息的有序传输；
- RAG 和 knowledge extraction 使用 `from_prompt()` 平滑迁移；
- RAG no-context 路径无需构造 Provider 即可返回正确 Provider/model；
- API key、远程错误正文、完整 prompt/response 不泄漏；
- 自动化测试不调用真实远程 API。

## 4. 非目标

Phase 9 不实现：

- API embedding 或多 embedding 模型；
- 运行时动态切换 Provider；
- Local/API 自动 fallback；
- 多 API 厂商智能路由或 registry；
- 流式输出、SSE 或后台队列；
- Responses API、Anthropic/Gemini 原生协议；
- 对外多轮 REST API；
- `/rag/ask` 历史 messages 输入；
- 独立无状态 Chat API；
- 会话或消息持久化；
- 历史裁剪、查询重写或多轮 RAG；
- Agent loop、工具注册、工具执行器或 tool choice；
- 多模态上传 UI、图片对象存储转发或本地文件读取；
- 数据库密钥管理、计费或 token 配额；
- migration；
- 与 Phase 9 无关的前端债务修复。

## 5. 方案比较

### 5.1 单一 OpenAI-compatible Adapter

本地和远程使用一个 Adapter，只替换 base URL、key、model 和 timeout。优点是改动少。缺点是 Provider 语义被配置细节掩盖，Ollama `think` 扩展容易误发到远程服务，active-only 配置校验、远程 URL 安全和能力声明不清晰。

### 5.2 统一 Port + Local/API 两个 Adapter

~~~text
LLMProvider
├── LocalLLMProvider
└── APILLMProvider
     ↓
OpenAIChatTransport
~~~

两个 Adapter 分别声明配置和 capabilities，复用同一个 Chat Completions transport。该方案边界清晰，能显式处理 `think`、JSON mode、URL 安全和错误分类，同时避免复制 SDK 传输代码。

### 5.3 Provider Registry

Registry 能为大量未来 Provider 提供注册和路由能力，但 Phase 9 只有两个固定通道，且不允许运行时路由。当前引入 registry 会增加无即时价值的抽象和测试。

### 5.4 结论

采用方案 5.2。保留简单 factory，不实现 registry。

## 6. 模块结构

~~~text
backend/app/llm/
├── messages.py
├── configuration.py
├── provider.py
├── openai_chat_transport.py
├── local.py
├── api.py
└── __init__.py
~~~

职责：

- `messages.py`：content parts、message、tool 类型和结构校验；
- `configuration.py`：Provider 规范化、active metadata、active-only 校验和远程 URL 安全；
- `provider.py`：capabilities、request/result、Protocol、factory 和进程缓存；
- `openai_chat_transport.py`：共享 Chat Completions 文本传输、响应解析和 SDK 错误翻译；
- `local.py`：Local/Ollama 配置和 capability；
- `api.py`：Generic API 配置和 capability。

原 `openai_compatible.py` 在迁移完成后删除。`openai_compatible` 只保留为 deprecated 配置别名，避免与共享 transport 混淆。

## 7. 消息模型

内部消息类型使用受控 dataclass，不把 OpenAI SDK 类型泄漏给业务层。

### 7.1 Content parts

~~~python
@dataclass(frozen=True, slots=True)
class LLMTextContentPart:
    type: Literal["text"] = "text"
    text: str


@dataclass(frozen=True, slots=True)
class LLMImageURLContentPart:
    type: Literal["image_url"] = "image_url"
    image_url: str
    detail: Literal["auto", "low", "high"] = "auto"


LLMContentPart = LLMTextContentPart | LLMImageURLContentPart
~~~

约束：

- text 不得为空或只包含空白；
- image URL 只是内部引用，不在 Phase 9 下载或读取；
- 拒绝 `file://`、本地 Windows 路径和相对文件路径；
- 当前 Adapter 均声明不支持 image input，因此合法 image part 也会在网络前被拒绝。

### 7.2 Phase 9 的 text parts 行为

- system message 恰好一个 text part；
- tool message 恰好一个 text part；
- 纯文本 user message 恰好一个 text part；
- 纯文本 assistant message 最多一个 text part；
- 同一消息包含多个纯 text parts 时返回 `LLM_REQUEST_INVALID`；
- 不自动拼接，不插入换行，不静默删除 part；
- 只有真正启用多模态后，包含 image 的多个有序 parts 才序列化为 content array；
- Phase 9 的文本 transport 只序列化单一 text part 为字符串 `content`。

### 7.3 Tool 类型

~~~python
@dataclass(frozen=True, slots=True)
class LLMFunctionCall:
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    id: str
    type: Literal["function"]
    function: LLMFunctionCall


class LLMFunctionTool:
    name: str
    description: str | None
    parameters: Mapping[str, JSONValue]
~~~

`arguments` 保留为字符串。Provider 不解析、修复或执行工具参数。

`LLMFunctionTool.parameters` 必须：

1. 顶层是 JSON object；
2. 通过 `json.dumps(parameters, allow_nan=False)`；
3. 拒绝 list、scalar、null、NaN、Infinity 和不可序列化对象；
4. 构造时执行 JSON round-trip 防御性深拷贝；
5. 不保留调用方可变 dict 引用；
6. 暴露参数时返回新的反序列化 object，外部修改不改变内部状态。

### 7.4 Message

~~~python
@dataclass(frozen=True, slots=True)
class LLMMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: tuple[LLMContentPart, ...] = ()
    tool_calls: tuple[LLMToolCall, ...] = ()
    tool_call_id: str | None = None
~~~

角色规则：

| Role | Content | tool_calls | tool_call_id |
|---|---|---|---|
| system | 一个 text | 禁止 | 禁止 |
| user | 一个 text，或未来 image content array | 禁止 | 禁止 |
| assistant | 一个 text、tool calls 或二者 | 允许 | 禁止 |
| tool | 一个 text | 禁止 | 必填 |

## 8. Request-level tool linkage

`LLMGenerateRequest` 构造时按消息顺序执行线性校验：

~~~text
seen_tool_call_ids: tool_call_id → assistant_message_index
answered_tool_call_ids: set[tool_call_id]
~~~

规则：

- assistant tool call ID 在完整消息序列中全局唯一；
- tool message 必须引用此前 assistant message 声明的 tool call；
- tool message 不能引用后续或不存在的 call；
- 同一 tool call 只允许一个 tool response；
- tool call 可以暂时没有 response，以允许 transcript 停在 assistant tool call；
- 单条 assistant message 有多个 tool calls 时需要 parallel capability；
- 结构错误是 `LLM_REQUEST_INVALID`；
- 结构合法但 Provider 不支持 tools 时是 `LLM_PARAMETER_UNSUPPORTED`；
- capability error 必须发生在 SDK/HTTP 调用前。

## 9. Provider 契约

### 9.1 Capabilities

~~~python
@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    supports_json_mode: bool
    supports_think: bool
    supports_tools: bool
    supports_parallel_tool_calls: bool
    supports_image_input: bool
~~~

不变量：

~~~text
supports_parallel_tool_calls = true
→ supports_tools 必须为 true
~~~

Phase 9 capability：

| 能力 | Local/Ollama | Generic API |
|---|---:|---:|
| 文本 system/user/assistant | true | true |
| JSON mode | true | 由配置决定，默认 false |
| think | true | false |
| tools | false | false |
| parallel tool calls | false | false |
| image input | false | false |

Generic API 不因协议名称而假定支持 JSON mode、tools 或 vision。

### 9.2 Generate request

~~~python
@dataclass(frozen=True, slots=True)
class LLMGenerateRequest:
    messages: tuple[LLMMessage, ...]
    temperature: float | None = None
    max_tokens: int | None = None
    json_mode: bool = False
    think: bool | None = None
    think_required: bool = False
    timeout_seconds: float | None = None
    stop: tuple[str, ...] | None = None
    tools: tuple[LLMFunctionTool, ...] = ()
    parallel_tool_calls: bool | None = None
~~~

`messages` 是唯一正式输入，至少包含一条消息。Provider 不得重排、合并或删除消息。

兼容构造器：

~~~python
@classmethod
def from_prompt(
    cls,
    prompt: str,
    system_prompt: str | None = None,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    think: bool | None = None,
    think_required: bool = False,
    timeout_seconds: float | None = None,
    stop: tuple[str, ...] | None = None,
) -> "LLMGenerateRequest":
    ...
~~~

有 system prompt 时生成 `system → user`；否则只生成 user。兼容构造器不形成第二套 Provider 路径。

### 9.3 Generate result

~~~python
@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class LLMGenerateResult:
    message: LLMMessage
    provider: Literal["local", "api"]
    model: str
    usage: LLMUsage | None = None
    request_id: str | None = None

    @property
    def text(self) -> str:
        ...
~~~

- result message 必须是 assistant；
- Phase 9 文本结果必须恰好包含一个 text part；
- `text` 返回该唯一 part，不拼接多个 parts；
- 不返回 `raw`；
- 意外 tool calls 或不支持的多模态 response 返回 `LLM_RESPONSE_INVALID`。

## 10. Capability preflight

Provider 在创建 payload 和调用 `chat.completions.create()` 之前检查：

| Request 内容 | 所需 capability |
|---|---|
| `json_mode=True` | `supports_json_mode` |
| `think_required=True` | `supports_think` |
| 非空 `tools` | `supports_tools` |
| assistant 历史包含 tool calls | `supports_tools` |
| tool role message | `supports_tools` |
| `parallel_tool_calls=True` | tools + parallel |
| 单 assistant message 多个 calls | parallel |
| 任意 image part | `supports_image_input` |

`think` 行为：

- Local/Ollama 支持，通过 `extra_body` 发送；
- API 默认不支持；
- advisory `think=False` 且 Provider 不支持时省略，不阻断；
- `think_required=True` 且不支持时返回 `LLM_PARAMETER_UNSUPPORTED`；
- 不得把 `think` 作为 OpenAI SDK 顶层参数。

不支持 capability 时的安全 detail 只包含：

~~~json
{
  "provider": "api",
  "parameter": "json_mode",
  "required_capability": "supports_json_mode"
}
~~~

不得包含消息、图片 URL、tool arguments 或完整 tool schema。

## 11. 配置

### 11.1 完整环境变量

~~~dotenv
# Active channel: local | api
LLM_PROVIDER=local

# Shared generation defaults
LLM_TEMPERATURE=0.2
LLM_MAX_TOKENS=2048

# Local/Ollama
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=120

# Generic remote API
LLM_REMOTE_BASE_URL=
LLM_REMOTE_API_KEY=
LLM_REMOTE_MODEL=
LLM_REMOTE_TIMEOUT_SECONDS=60
LLM_REMOTE_SUPPORTS_JSON_MODE=false
LLM_REMOTE_ALLOW_INSECURE_HTTP=false
~~~

两个 `.env.example` 中 key 均为空，不写入真实密钥。

### 11.2 Provider 解析

| 配置值 | 规范化 Provider | 使用配置 |
|---|---|---|
| `local` | `local` | `LLM_BASE_URL/MODEL/API_KEY/TIMEOUT` |
| `api` | `api` | `LLM_REMOTE_*` |
| `openai_compatible` | `local` | Local 配置，并警告一次 |
| 其他 | error | `LLM_PROVIDER_INVALID` |

`openai_compatible` 至少保留一个阶段作为兼容别名。新文档和示例只使用 `local/api`。

### 11.3 Active-only 校验

Local 只校验 Local base URL、model、timeout 和 shared temperature/max tokens。Local key 为空时 Adapter 内部使用非秘密占位值 `ollama`。Local 不校验任何 remote key。

API 只校验 remote base URL、API key、model、timeout、JSON capability、insecure HTTP flag 和 shared temperature/max tokens。API 不得回退读取 Local `LLM_API_KEY=ollama`。

## 12. 远程 URL 安全策略

`LLM_REMOTE_BASE_URL` 解析后必须满足：

- `https://` 默认允许；
- `http://localhost` 允许；
- `http://127.0.0.1` 允许；
- `http://[::1]` 允许；
- 其他 HTTP 仅在 `LLM_REMOTE_ALLOW_INSECURE_HTTP=true` 时允许；
- 允许非 loopback HTTP 时每进程记录一次脱敏警告；
- 拒绝 URL userinfo，例如 `https://user:pass@example.com`；
- 拒绝 query；
- 拒绝 fragment；
- 拒绝 ftp、file、ws、data 和其他非 HTTP(S) scheme；
- host 必须存在；
- 不在错误或日志中输出 query、userinfo、API key 或 Authorization。

Insecure HTTP 警告只记录 Provider、`scheme=http`、hostname 和 `insecure_remote_transport_enabled`，不记录完整 URL。

## 13. 启动校验

新增：

~~~python
validate_active_llm_configuration(settings) -> ActiveLLMMetadata
resolve_active_llm_metadata(settings) -> ActiveLLMMetadata
~~~

`backend/app/main.py` 的实际顺序：

~~~python
def create_app(*, settings: Settings | None = None) -> FastAPI:
    configure_logging()
    active_settings = settings or get_settings()
    validate_active_llm_configuration(active_settings)

    from app.api.v1.router import api_router

    app = FastAPI(..., lifespan=...)
    ...
~~~

模块底部继续 `app = create_app()`。这保证 Uvicorn 导入应用时，在路由和数据库相关 route imports 之前发现无效 LLM 配置。

Factory 仍防御性复验：

~~~text
build_llm_provider(settings, client=None)
→ validate_active_llm_configuration(settings)
→ create Local/API Adapter
~~~

脚本、worker 和单元测试即使绕过 FastAPI，也不能构造无效 Provider。

## 14. 干净测试和 CI 边界

自动测试不得依赖开发者真实 `backend/.env`。

测试使用显式最小 local Settings：

~~~dotenv
LLM_PROVIDER=local
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=test-local-model
LLM_API_KEY=
LLM_TIMEOUT_SECONDS=1
LLM_TEMPERATURE=0
LLM_MAX_TOKENS=32
~~~

规则：

- 测试通过 `create_app(settings=...)` 注入 Settings；
- local 模式没有 remote key 时仍可导入和启动；
- CI 不需要真实 API key；
- `backend/.env` 不属于测试前置条件；
- 测试清除或隔离 `get_settings()` cache；
- 干净环境启动测试屏蔽开发者 `backend/.env`，使用显式 Settings 或临时空 env source；
- API 启动失败测试显式提供 `LLM_PROVIDER=api` 和缺失 remote key；
- 不修改真实 `backend/.env`；
- 不在测试快照中保存 key。

## 15. Active metadata 与 RAG no-context

~~~python
@dataclass(frozen=True, slots=True)
class ActiveLLMMetadata:
    provider: Literal["local", "api"]
    model: str


def resolve_active_llm_metadata(settings) -> ActiveLLMMetadata:
    ...
~~~

解析结果：

| Input Provider | Output Provider | Model |
|---|---|---|
| local | local | `LLM_MODEL` |
| api | api | `LLM_REMOTE_MODEL` |
| openai_compatible | local | `LLM_MODEL` |

该函数不得构造 Provider/OpenAI client、调用 factory、读取 API key、访问网络或填充 Provider cache。RAG no-context 分支使用该函数返回现有 response 中的 Provider/model，并继续保持 HTTP 200 和零 LLM 调用。

## 16. Provider 和 SDK client 生命周期

提供：

~~~python
build_llm_provider(settings, *, client=None) -> LLMProvider
get_llm_provider() -> LLMProvider
clear_llm_provider_cache() -> None
~~~

生命周期：

- `build_llm_provider()` 不缓存，用于测试和显式 client 注入；
- `get_llm_provider()` 使用进程内惰性单例；
- 一个 Provider 持有一个同步 OpenAI SDK client；
- 多次 LLM 请求复用该 client 和连接池；
- Provider 构造与 cache 摘除由同一把锁保护；
- 不在持锁状态执行可能阻塞的 client close；
- clear 先在锁内原子摘除，再在锁外 close；
- 尚未创建时 clear 成功返回；
- 连续 clear 幂等；
- Provider `close()` 幂等；
- 旧 SDK client 最多关闭一次；
- clear 后下一次 get 构造新实例；
- FastAPI shutdown 调用 clear；
- shutdown 多次触发不抛异常；
- 测试之间同时清除 Provider cache 和 Settings cache；
- 更改 `.env` 后必须重启进程。

应用启动只校验配置，不提前构造 Provider。Health、search 和 RAG no-context 不会创建 SDK client。

## 17. OpenAI Chat transport

`OpenAIChatTransport` 只负责：

- OpenAI-compatible Chat Completions；
- 文本 system/user/assistant 消息；
- 保持消息顺序、role 和唯一 text；
- model、temperature、max_tokens、stop、timeout；
- JSON response format；
- Local `think` extra body；
- assistant message、usage 和 request ID 解析；
- SDK exception 转换；
- `max_retries=0`。

第一版不实现 Responses API、streaming、tools serializer 或 image content array serializer。

## 18. 请求和响应规则

请求：

~~~text
POST {base_url}/chat/completions
Authorization: Bearer <secret>
Content-Type: application/json
~~~

文本多轮示例：

~~~json
[
  {"role": "system", "content": "system rules"},
  {"role": "user", "content": "turn 1"},
  {"role": "assistant", "content": "answer 1"},
  {"role": "user", "content": "turn 2"}
]
~~~

Transport 不重排、拼接或裁剪历史。

响应依次校验 ChatCompletion、非空 choices、assistant message、非空文本、意外 tool calls、JSON object、usage 和 request ID。原始 response 不保留。

## 19. 错误码和 HTTP 映射

| 错误码 | HTTP | 自动重试 | 说明 |
|---|---:|---:|---|
| `LLM_CONFIG_INVALID` | 400/启动失败 | 否 | active 配置非法 |
| `LLM_PROVIDER_INVALID` | 400/启动失败 | 否 | 未知 Provider |
| `LLM_REQUEST_INVALID` | 400 | 否 | message/tool/request 结构非法 |
| `LLM_PARAMETER_UNSUPPORTED` | 400 | 否 | capability 不支持 |
| `LLM_AUTHENTICATION_FAILED` | 502 | 否 | 上游 401 |
| `LLM_PERMISSION_DENIED` | 502 | 否 | 上游 403 |
| `LLM_MODEL_NOT_FOUND` | 502 | 否 | 上游模型不存在 |
| `LLM_TIMEOUT` | 504 | 否 | 请求超时 |
| `LLM_UNAVAILABLE` | 503 | 否 | DNS/连接失败 |
| `LLM_RATE_LIMITED` | 429 | 否 | 上游 429 |
| `LLM_UPSTREAM_FAILED` | 502 | 否 | 上游 5xx |
| `LLM_REQUEST_REJECTED` | 502 | 否 | 其他上游 4xx |
| `LLM_RESPONSE_INVALID` | 502 | 否 | 非法响应或意外能力 |
| `LLM_EMPTY_CONTENT` | 502 | 否 | 文本响应为空 |
| `LLM_JSON_INVALID` | 502 | 否 | JSON mode 返回非法 JSON/object |
| `LLM_GENERATION_FAILED` | 500 | 否 | 已脱敏的未知错误 |

第一版显式 `max_retries=0`。Timeout、连接失败、429 和 5xx 可标记为瞬时错误，但不自动重试，也不跨 Provider fallback。

## 20. 安全、错误响应和日志

允许记录：

- 规范化 Provider；
- model；
- error code；
- 安全 upstream status；
- latency；
- token usage 数量；
- request ID；
- retryable 分类；
- capability 名称；
- 脱敏 hostname。

禁止记录：

- API key 或 Authorization；
- 完整 headers；
- URL query/userinfo；
- 完整 remote error body；
- `str(exc)` 原文；
- prompt、messages 或 response 全文；
- image URL；
- tool arguments 或 parameters；
- 数据库中的密钥。

## 21. 业务接线

RAG 使用 `LLMGenerateRequest.from_prompt()`，继续读取 `result.text`，`/api/v1/rag/ask` schema 不变。No-context 使用 `resolve_active_llm_metadata()`。

Knowledge extraction 使用 `from_prompt()`、`json_mode=True` 和 advisory `think=False`。Local 发送 `think:false`；API 不发送 think；API JSON capability 为 false 时在网络前明确拒绝；领域 parser 继续校验业务结构。

## 22. 依赖和数据库

不新增依赖，复用 `openai>=1.0,<3.0`、`httpx>=0.27,<1.0`、pytest fake/monkeypatch 和 HTTPX MockTransport。

不新增 migration。Provider/model、usage、request ID 和 latency 使用现有响应元数据与结构化日志，不持久化 API key 或调用账单。

## 23. 自动化测试范围

覆盖：

- 现有 think fake/wire contract；
- message role/content 和多 text parts；
- tool linkage 和 parameters 防御性复制；
- Local/API 文本多轮 wire 顺序；
- JSON capability 默认 false/显式 true；
- capability error 零网络调用；
- URL scheme/userinfo/query/fragment/HTTP 安全；
- local/api/legacy metadata；
- create_app 主动校验和 factory 防御性校验；
- 干净环境 local 启动；
- Provider cache 并发、clear、close、重建和 shutdown；
- RAG no-context 零 Provider 构造；
- 401/403/model/timeout/connection/429/5xx；
- 非法 response、空 choices/content、非法 JSON；
- 敏感信息不进入日志和响应；
- Provider 切换不写 embedding 或 OpenSearch。

自动化测试不调用真实远程 API。

## 24. 人工验收

Local 模式验证后端启动、RAG、knowledge extraction、JSON mode、`think:false`、Provider/model 和内部文本多轮。Remote key 为空不得影响 Local 启动。

API 模式验证 HTTPS endpoint、RAG 远程生成、本地 embedding/retrieval 不变、JSON capability false/true 两条路径、密钥脱敏以及 timeout/认证/限流分类。

切换前后比较 PostgreSQL embedding 数量和值、OpenSearch count/alias/mapping、MinerU 和 parse run 状态；所有值必须不变。

## 25. 风险

- OpenAI-compatible 服务能力不一致，因此远程 JSON mode 默认 false；
- Ollama `think` 不是通用标准，必须保持 capability 边界；
- SDK 默认重试可能产生隐藏费用，必须显式设为零；
- Provider cache 必须避免双 client 和双 close；
- 宽版本 SDK 通过 SDK-level 和 wire-level 双层测试控制漂移；
- tool/image 类型只是未来协议边界，不能宣称已可实际调用。

## 26. 验收标准

- Local/API 可通过重启选择；
- active-only 配置和远程 URL 安全生效；
- create_app 和 factory 双层校验通过；
- message-first 契约和 assistant 历史通过 wire 测试；
- API JSON capability 默认 false；
- tool/image unsupported 时零网络调用；
- no-context 不构造 Provider；
- cache/clear/close/shutdown 幂等；
- 全部相关自动化和人工验收完成；
- embedding、OpenSearch、MinerU 和 schema 无变化；
- 无真实密钥进入 Git。

## 27. 后续阶段

以下分别规划，不能视为 Phase 9 已完成：

1. 独立无状态 Chat API；
2. 多轮 RAG、查询重写和历史裁剪；
3. 会话持久化；
4. 前端聊天功能；
5. tool calling、注册和执行循环；
6. 多模态上传、对象存储和 URL 转发。
