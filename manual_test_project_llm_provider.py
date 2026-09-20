from __future__ import annotations

import sys
import time
from pathlib import Path


# ------------------------------------------------------------
# 让脚本可以从 D:\rag_system 直接导入 backend/app
# ------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


from app.core.config import get_settings
from app.llm.provider import (
    LLMGenerateRequest,
    clear_llm_provider_cache,
    get_llm_provider,
)


def safe_print_exception(exc: BaseException) -> None:
    print()
    print("=" * 70)
    print("EXCEPTION")
    print("=" * 70)

    current: BaseException | None = exc
    level = 0

    while current is not None and level < 6:
        print(f"[{level}] type = {type(current).__name__}")

        # 项目 BusinessError 一类通常有这些安全字段
        for name in (
            "code",
            "status_code",
            "detail",
            "retryable",
        ):
            if hasattr(current, name):
                try:
                    print(
                        f"    {name} = "
                        f"{getattr(current, name)!r}"
                    )
                except Exception:
                    pass

        # 不打印 SecretStr / headers / request 对象
        current = current.__cause__
        level += 1


def main() -> None:
    # --------------------------------------------------------
    # 确保这里拿的是当前 .env，而不是先前进程缓存
    # --------------------------------------------------------

    clear_llm_provider_cache()

    try:
        get_settings.cache_clear()
    except AttributeError:
        pass

    settings = get_settings()

    print("=" * 70)
    print("PROJECT LLM PROVIDER TEST")
    print("=" * 70)

    print(
        "provider      =",
        getattr(settings, "llm_provider", None),
    )

    print(
        "remote_base   =",
        getattr(settings, "llm_remote_base_url", None),
    )

    print(
        "remote_model  =",
        getattr(settings, "llm_remote_model", None),
    )

    print(
        "remote_timeout=",
        getattr(
            settings,
            "llm_remote_timeout_seconds",
            None,
        ),
    )

    print(
        "json_support  =",
        getattr(
            settings,
            "llm_remote_supports_json_mode",
            None,
        ),
    )

    key = getattr(
        settings,
        "llm_remote_api_key",
        None,
    )

    if key is None:
        key_state = "EMPTY"
    elif hasattr(key, "get_secret_value"):
        key_state = (
            "SET"
            if key.get_secret_value()
            else "EMPTY"
        )
    else:
        key_state = "SET" if str(key) else "EMPTY"

    print("remote_key    =", key_state)

    # --------------------------------------------------------
    # 真正使用项目 factory/provider
    # --------------------------------------------------------

    print()
    print("-" * 70)
    print("[1] build/get cached project provider")
    print("-" * 70)

    try:
        provider = get_llm_provider()

        print(
            "provider_class =",
            provider.__class__.__module__
            + "."
            + provider.__class__.__name__,
        )

        capabilities = getattr(
            provider,
            "capabilities",
            None,
        )

        if capabilities is not None:
            print(
                "capabilities =",
                capabilities,
            )

    except Exception as exc:
        print("FAIL while creating provider")
        safe_print_exception(exc)
        return

    # --------------------------------------------------------
    # 构造和 RAG 同类型的文本请求
    # --------------------------------------------------------

    request = LLMGenerateRequest.from_prompt(
        prompt=(
            "这是项目 LLM Provider 连通性测试。"
            "只回复 PROJECT_API_OK，"
            "不要输出其他内容。"
        ),
        system_prompt=(
            "Follow the user's instruction exactly."
        ),
        temperature=0,
        max_tokens=32,
        timeout_seconds=60,
    )

    print()
    print("-" * 70)
    print("[2] project provider.generate()")
    print("-" * 70)

    start = time.perf_counter()

    try:
        result = provider.generate(request)

        elapsed_ms = (
            time.perf_counter() - start
        ) * 1000

        print()
        print("=" * 70)
        print("PROJECT PROVIDER TEST PASSED")
        print("=" * 70)

        print(
            f"latency    = {elapsed_ms:.2f} ms"
        )

        print(
            "provider   =",
            getattr(result, "provider", None),
        )

        print(
            "model      =",
            getattr(result, "model", None),
        )

        # 新契约兼容 result.text
        text = getattr(
            result,
            "text",
            None,
        )

        if text is None:
            text = getattr(
                result,
                "content",
                None,
            )

        print(
            "content    =",
            repr(text),
        )

        print(
            "request_id =",
            getattr(
                result,
                "request_id",
                None,
            ),
        )

        usage = getattr(
            result,
            "usage",
            None,
        )

        if usage is not None:
            print(
                "usage      =",
                usage,
            )

        print()
        print(
            "结论：项目自己的 "
            "get_llm_provider() → API Provider → "
            "OpenAIChatTransport 链路可以正常调用远程模型。"
        )

    except Exception as exc:
        elapsed_ms = (
            time.perf_counter() - start
        ) * 1000

        print()
        print("=" * 70)
        print("PROJECT PROVIDER TEST FAILED")
        print("=" * 70)

        print(
            f"elapsed = {elapsed_ms:.2f} ms"
        )

        safe_print_exception(exc)

    finally:
        clear_llm_provider_cache()


if __name__ == "__main__":
    main()