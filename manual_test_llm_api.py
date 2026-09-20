"""
单独测试项目配置中的远程 LLM API 是否可用。

作用：
1. 读取 backend/.env 中当前项目实际使用的 LLM 配置
2. 不经过 RAG / Hybrid / Reranker / Graph
3. 直接通过 OpenAI SDK 调用远程 OpenAI-compatible API
4. 区分：
   - 网络连接失败
   - 超时
   - API Key 错误
   - 权限错误
   - 模型不存在
   - 429
   - 400 请求错误
   - 5xx 上游错误
   - 正常响应

注意：
不会打印 API Key。
"""

from __future__ import annotations

import time

from openai import (
    OpenAI,
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    PermissionDeniedError,
    NotFoundError,
    RateLimitError,
    BadRequestError,
    APIStatusError,
)

from app.core.config import get_settings


def get_secret_value(value) -> str:
    """兼容普通 str 和 Pydantic SecretStr。"""
    if value is None:
        return ""

    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()

    return str(value)


def main() -> None:
    settings = get_settings()

    provider = str(getattr(settings, "llm_provider", "") or "")

    base_url = str(
        getattr(settings, "llm_remote_base_url", "") or ""
    ).strip()

    model = str(
        getattr(settings, "llm_remote_model", "") or ""
    ).strip()

    api_key = get_secret_value(
        getattr(settings, "llm_remote_api_key", None)
    ).strip()

    timeout = float(
        getattr(settings, "llm_remote_timeout_seconds", 60)
    )

    print("=" * 70)
    print("LLM API connectivity test")
    print("=" * 70)

    # 只打印非敏感配置
    print(f"provider : {provider}")
    print(f"base_url : {base_url}")
    print(f"model    : {model}")
    print(f"timeout  : {timeout}s")
    print(f"api_key  : {'SET' if api_key else 'EMPTY'}")
    print()

    # --------------------------------
    # 基础配置检查
    # --------------------------------

    if provider != "api":
        print(
            f"[WARNING] 当前 LLM_PROVIDER={provider!r}，"
            "并不是 'api'。"
        )

    if not base_url:
        raise RuntimeError(
            "LLM_REMOTE_BASE_URL 为空。"
        )

    if not model:
        raise RuntimeError(
            "LLM_REMOTE_MODEL 为空。"
        )

    if not api_key:
        raise RuntimeError(
            "LLM_REMOTE_API_KEY 为空。"
        )

    # --------------------------------
    # 建立与生产实现相同类型的 client
    # --------------------------------

    client = OpenAI(
        base_url=base_url.rstrip("/"),
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )

    # --------------------------------
    # 测试 1：models endpoint
    #
    # 注意：
    # 一些 OpenAI-compatible 服务并不实现 /models，
    # 所以这个失败不代表 chat 一定失败。
    # --------------------------------

    print("-" * 70)
    print("[TEST 1] GET /models")
    print("-" * 70)

    try:
        models = client.models.list()

        model_ids = [
            item.id
            for item in models.data[:20]
        ]

        print("[PASS] /models 可访问")

        if model_ids:
            print("返回模型（最多显示20个）：")
            for model_id in model_ids:
                marker = "  <-- configured" if model_id == model else ""
                print(f"  - {model_id}{marker}")
        else:
            print("模型列表为空。")

        if model_ids and model not in model_ids:
            print()
            print(
                "[WARNING] 当前配置的模型没有出现在 /models 返回列表中："
            )
            print(f"  configured model = {model}")

    except Exception as exc:
        print(
            "[WARNING] /models 调用失败。"
        )
        print(
            "这并不一定代表 Chat Completions 不可用。"
        )
        print(
            f"{type(exc).__name__}: {exc}"
        )

    print()

    # --------------------------------
    # 测试 2：真正进行一次最小生成
    # --------------------------------

    print("-" * 70)
    print("[TEST 2] POST /chat/completions")
    print("-" * 70)

    start = time.perf_counter()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are performing an API connectivity test. "
                        "Follow the user's instruction exactly."
                    ),
                },
                {
                    "role": "user",
                    "content": "只回复 API_OK，不要输出其他内容。",
                },
            ],
            temperature=0,
            max_tokens=32,
        )

        elapsed_ms = (
            time.perf_counter() - start
        ) * 1000

        if not response.choices:
            raise RuntimeError(
                "API 请求成功，但 choices 为空。"
            )

        content = response.choices[0].message.content

        print()
        print("=" * 70)
        print("API TEST PASSED")
        print("=" * 70)

        print(f"latency     : {elapsed_ms:.2f} ms")
        print(f"model       : {response.model}")
        print(f"content     : {content!r}")

        request_id = getattr(
            response,
            "_request_id",
            None,
        )

        if request_id:
            print(f"request_id  : {request_id}")

        usage = getattr(
            response,
            "usage",
            None,
        )

        if usage:
            print(
                f"prompt_tokens     : "
                f"{getattr(usage, 'prompt_tokens', None)}"
            )
            print(
                f"completion_tokens : "
                f"{getattr(usage, 'completion_tokens', None)}"
            )
            print(
                f"total_tokens      : "
                f"{getattr(usage, 'total_tokens', None)}"
            )

        print()
        print(
            "结论：远程 LLM API 可以正常完成 Chat Completions。"
        )

    # --------------------------------
    # 网络不可达
    # --------------------------------

    except APIConnectionError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: CONNECTION ERROR")
        print("=" * 70)

        print(
            "无法连接到远程 LLM API。"
        )
        print(
            "这类错误与你项目中的 LLM_UNAVAILABLE 最接近。"
        )
        print()

        print(f"type     : {type(exc).__name__}")
        print(f"base_url : {base_url}")

        print()
        print(
            "优先检查：Base URL、网络、DNS、VPN、代理、防火墙、"
            "API 服务是否在线。"
        )

    # --------------------------------
    # 超时
    # --------------------------------

    except APITimeoutError as exc:
        elapsed_ms = (
            time.perf_counter() - start
        ) * 1000

        print()
        print("=" * 70)
        print("API TEST FAILED: TIMEOUT")
        print("=" * 70)

        print(f"elapsed : {elapsed_ms:.2f} ms")
        print(f"timeout : {timeout}s")
        print(f"type    : {type(exc).__name__}")

    # --------------------------------
    # API Key 不正确
    # --------------------------------

    except AuthenticationError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: AUTHENTICATION")
        print("=" * 70)

        print(
            "远程服务器拒绝 API Key。"
        )

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

    # --------------------------------
    # Key 有效，但没权限
    # --------------------------------

    except PermissionDeniedError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: PERMISSION DENIED")
        print("=" * 70)

        print(
            "API Key 可被识别，但当前账号/Key没有调用权限。"
        )

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

    # --------------------------------
    # 模型或 endpoint 不存在
    # --------------------------------

    except NotFoundError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: NOT FOUND")
        print("=" * 70)

        print(
            "常见原因："
        )
        print(
            "1. LLM_REMOTE_MODEL 写错；"
        )
        print(
            "2. Base URL 路径写错；"
        )
        print(
            "3. 当前 API 服务没有这个模型。"
        )

        print(f"model : {model}")

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

    # --------------------------------
    # 429
    # --------------------------------

    except RateLimitError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: RATE LIMITED")
        print("=" * 70)

        print(
            "API 可以连接，但被限流/额度限制。"
        )

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

    # --------------------------------
    # 400
    # --------------------------------

    except BadRequestError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: BAD REQUEST")
        print("=" * 70)

        print(
            "网络和认证一般已经正常，"
            "但请求参数或模型配置不被 API 接受。"
        )

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

    # --------------------------------
    # 其他 HTTP error，例如 5xx
    # --------------------------------

    except APIStatusError as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: UPSTREAM HTTP ERROR")
        print("=" * 70)

        print(
            f"status_code : {getattr(exc, 'status_code', None)}"
        )

        request_id = getattr(
            exc,
            "request_id",
            None,
        )

        if request_id:
            print(f"request_id  : {request_id}")

    # --------------------------------
    # 未预期错误
    # --------------------------------

    except Exception as exc:
        print()
        print("=" * 70)
        print("API TEST FAILED: UNEXPECTED ERROR")
        print("=" * 70)

        print(f"type    : {type(exc).__name__}")
        print(f"message : {exc}")


if __name__ == "__main__":
    main()