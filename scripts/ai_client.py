"""
ai_client.py

AI(Google Gemini) API 호출 전용 모듈.

이 모듈은 과거 Claude(Anthropic) API를 호출했으나, Anthropic API 크레딧/사용량
제한으로 테스트가 막힌 문제를 해결하기 위해 Google Gemini API로 교체되었다.
Tavily(뉴스 검색)와의 역할 분리는 그대로 유지된다:

    Tavily  → 뉴스 "검색" (news_fetcher.py, 변경 없음)
    Gemini  → 뉴스 "분석 및 보고서 작성" (이 모듈)

generate_brief.py가 사용하는 공개 인터페이스는 교체 전과 완전히 동일하다:
    - generate_text(system_prompt: str, user_prompt: str) -> str
    - AIClientError (예외 클래스명도 동일하다)

따라서 generate_brief.py는 import 구문을 포함해 전혀 수정할 필요가 없다
(`import ai_client`, `ai_client.generate_text(...)`, `except ai_client.AIClientError`
모두 그대로 동작한다).

역할:
- Gemini API 호출 — 공식 Python SDK인 google-genai 사용
- 모델 설정
- timeout
- retry
- 오류 처리
- 503/504 서버 오류 반복 시 fallback 모델로 자동 전환
- API Key는 어떤 경우에도 로그에 출력하지 않음
"""

from __future__ import annotations

import os
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types


class AIClientError(RuntimeError):
    """Gemini API 호출 관련 오류를 나타내는 예외."""


AI_PROVIDER_NAME = "Gemini"


# 환경변수 GEMINI_MODEL이 설정되지 않은 경우 사용할 기본 모델
DEFAULT_MODEL = "gemini-3.6-flash"

# 3.8 Flash에서 503/504가 반복될 경우 fallback할 모델
# 기존에 정상적으로 사용했던 3.6 Flash를 사용한다.
DEFAULT_FALLBACK_MODEL = "gemini-3.6-flash"

# fallback을 적용할 서버 오류
FALLBACK_STATUS_CODES = {503, 504}


DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 5

DEFAULT_MAX_TOKENS = 8000

# gemini-3.x 모델의 thinking 강도
DEFAULT_THINKING_LEVEL = "LOW"


# 재시도해도 성공 가능성이 낮은 HTTP 상태코드
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


DEBUG_PREVIEW_CHARS = 300


def _get_config() -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise AIClientError(
            "GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다."
        )

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    timeout = int(
        os.environ.get(
            "GEMINI_TIMEOUT_SECONDS",
            DEFAULT_TIMEOUT_SECONDS,
        )
    )
    max_retries = int(
        os.environ.get(
            "GEMINI_MAX_RETRIES",
            DEFAULT_MAX_RETRIES,
        )
    )
    max_tokens = int(
        os.environ.get(
            "GEMINI_MAX_TOKENS",
            DEFAULT_MAX_TOKENS,
        )
    )
    thinking_level = os.environ.get(
        "GEMINI_THINKING_LEVEL",
        DEFAULT_THINKING_LEVEL,
    )

    return {
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "max_retries": max_retries,
        "max_tokens": max_tokens,
        "thinking_level": thinking_level,
    }


def _log_response_debug_info(response) -> None:
    """
    Gemini 응답의 진단 정보를 로그로 남긴다.
    """

    try:
        candidates = getattr(response, "candidates", None) or []
        finish_reason = (
            candidates[0].finish_reason
            if candidates
            else None
        )
    except Exception:
        finish_reason = None

    usage = getattr(response, "usage_metadata", None)

    prompt_tokens = (
        getattr(usage, "prompt_token_count", None)
        if usage
        else None
    )

    output_tokens = (
        getattr(usage, "candidates_token_count", None)
        if usage
        else None
    )

    thinking_tokens = (
        getattr(usage, "thoughts_token_count", None)
        if usage
        else None
    )

    total_tokens = (
        getattr(usage, "total_token_count", None)
        if usage
        else None
    )

    print(
        f"[ai_client] Gemini 응답 메타데이터: "
        f"finish_reason={finish_reason}, "
        f"prompt_tokens={prompt_tokens}, "
        f"output_tokens={output_tokens}, "
        f"thinking_tokens={thinking_tokens}, "
        f"total_tokens={total_tokens}"
    )

    if (
        finish_reason is not None
        and str(finish_reason).endswith("MAX_TOKENS")
    ):
        print(
            "[ai_client] 경고: finish_reason=MAX_TOKENS - "
            "응답이 max_output_tokens 예산을 모두 소진해 "
            "중간에 잘렸을 가능성이 높습니다."
        )

    try:
        full_text = response.text or ""
    except Exception as exc:
        print(
            f"[ai_client] 응답 텍스트 미리보기 생성 실패: "
            f"{type(exc).__name__}: {exc}"
        )
        return

    length = len(full_text)

    print(
        f"[ai_client] 응답 본문 길이: "
        f"{length:,}자 (비어있음: {length == 0})"
    )

    if length == 0:
        print(
            "[ai_client] 응답 본문이 완전히 비어 있습니다 "
            "(미리보기 없음)."
        )
        return

    head = full_text[:DEBUG_PREVIEW_CHARS]

    tail = (
        full_text[-DEBUG_PREVIEW_CHARS:]
        if length > DEBUG_PREVIEW_CHARS
        else ""
    )

    print(
        f"[ai_client] 응답 앞부분 미리보기 "
        f"(최대 {DEBUG_PREVIEW_CHARS}자):\n{head}"
    )

    if tail:
        print(
            f"[ai_client] 응답 뒷부분 미리보기 "
            f"(마지막 {DEBUG_PREVIEW_CHARS}자):\n{tail}"
        )


def generate_text(system_prompt: str, user_prompt: str) -> str:
    """
    Gemini API를 호출하여 완성된 텍스트를 반환한다.

    기본 모델에서 503 또는 504가 반복될 경우
    기존에 사용하던 fallback 모델로 자동 전환한다.
    """

    config = _get_config()

    # google-genai SDK의 HttpOptions.timeout은 밀리초 단위
    client = genai.Client(
        api_key=config["api_key"],
        http_options=genai_types.HttpOptions(
            timeout=config["timeout"] * 1000
        ),
    )

    generation_config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=config["max_tokens"],
        thinking_config=genai_types.ThinkingConfig(
            thinking_level=config["thinking_level"]
        ),
    )

    print(
        f"[ai_client] Gemini 호출 설정: "
        f"model={config['model']}, "
        f"max_output_tokens={config['max_tokens']}, "
        f"thinking_level={config['thinking_level']}"
    )

    last_error: AIClientError | None = None
    last_status_code = None

    # ---------------------------------------------------------
    # 1차: 현재 설정된 모델로 기존 retry 수행
    # ---------------------------------------------------------
    for attempt in range(
        1,
        config["max_retries"] + 1,
    ):
        try:
            response = client.models.generate_content(
                model=config["model"],
                contents=user_prompt,
                config=generation_config,
            )

            _log_response_debug_info(response)

            return _extract_text(response)

        except genai_errors.APIError as exc:
            status_code = getattr(exc, "code", None)
            message = getattr(
                exc,
                "message",
                str(exc),
            )

            last_status_code = status_code

            last_error = AIClientError(
                f"Gemini API 오류 "
                f"(시도 {attempt}/{config['max_retries']}): "
                f"{status_code} {message}"
            )

            print(f"[ai_client] {last_error}")

            if status_code in NON_RETRYABLE_STATUS_CODES:
                raise last_error

        except AIClientError:
            raise

        except Exception as exc:
            last_error = AIClientError(
                f"Gemini API 연결/처리 오류 "
                f"(시도 {attempt}/{config['max_retries']}): "
                f"{type(exc).__name__}: {exc}"
            )

            print(f"[ai_client] {last_error}")

            # 일반 예외가 발생한 경우에는
            # 503/504 fallback 조건으로 보지 않는다.
            last_status_code = None

        if attempt < config["max_retries"]:
            time.sleep(
                DEFAULT_RETRY_BACKOFF_SECONDS * attempt
            )

    # ---------------------------------------------------------
    # 2차: 503/504가 반복된 경우 fallback 모델 사용
    # ---------------------------------------------------------
    #
    # 현재 모델과 fallback 모델이 같으면 fallback하지 않는다.
    #
    # 예:
    #   3.8 → 503 → 503 → 503
    #   ↓
    #   3.6으로 전환
    #
    if (
        last_status_code in FALLBACK_STATUS_CODES
        and config["model"] != DEFAULT_FALLBACK_MODEL
    ):
        fallback_model = DEFAULT_FALLBACK_MODEL

        print(
            f"[ai_client] {config['model']}에서 "
            f"{last_status_code} 오류가 반복되어 "
            f"{fallback_model}로 fallback합니다."
        )

        fallback_generation_config = (
            genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=config["max_tokens"],
                thinking_config=genai_types.ThinkingConfig(
                    thinking_level=config["thinking_level"]
                ),
            )
        )

        fallback_last_error: AIClientError | None = None

        for fallback_attempt in range(
            1,
            config["max_retries"] + 1,
        ):
            try:
                print(
                    f"[ai_client] Fallback Gemini 호출: "
                    f"model={fallback_model}, "
                    f"시도={fallback_attempt}/"
                    f"{config['max_retries']}"
                )

                response = client.models.generate_content(
                    model=fallback_model,
                    contents=user_prompt,
                    config=fallback_generation_config,
                )

                _log_response_debug_info(response)

                print(
                    f"[ai_client] Fallback 성공: "
                    f"model={fallback_model}"
                )

                return _extract_text(response)

            except genai_errors.APIError as exc:
                status_code = getattr(
                    exc,
                    "code",
                    None,
                )

                message = getattr(
                    exc,
                    "message",
                    str(exc),
                )

                fallback_last_error = AIClientError(
                    f"Gemini fallback API 오류 "
                    f"(모델={fallback_model}, "
                    f"시도 {fallback_attempt}/"
                    f"{config['max_retries']}): "
                    f"{status_code} {message}"
                )

                print(
                    f"[ai_client] {fallback_last_error}"
                )

                if status_code in NON_RETRYABLE_STATUS_CODES:
                    raise fallback_last_error

            except AIClientError:
                raise

            except Exception as exc:
                fallback_last_error = AIClientError(
                    f"Gemini fallback 연결/처리 오류 "
                    f"(모델={fallback_model}, "
                    f"시도 {fallback_attempt}/"
                    f"{config['max_retries']}): "
                    f"{type(exc).__name__}: {exc}"
                )

                print(
                    f"[ai_client] {fallback_last_error}"
                )

            if fallback_attempt < config["max_retries"]:
                time.sleep(
                    DEFAULT_RETRY_BACKOFF_SECONDS
                    * fallback_attempt
                )

        raise (
            fallback_last_error
            or last_error
            or AIClientError(
                "Gemini API 호출에 실패했습니다 "
                "(알 수 없는 오류)."
            )
        )

    # fallback 조건이 아니면 기존처럼 최종 실패
    raise (
        last_error
        or AIClientError(
            "Gemini API 호출에 실패했습니다 "
            "(알 수 없는 오류)."
        )
    )


def _extract_text(response) -> str:
    """
    Gemini 응답에서 텍스트를 추출한다.
    """

    try:
        text = response.text

    except Exception as exc:
        raise AIClientError(
            f"Gemini API 응답 파싱 실패: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not text or not text.strip():
        raise AIClientError(
            "Gemini API 응답에 텍스트가 없습니다 "
            "(AI 응답 없음 - 안전 필터에 의해 차단되었거나 "
            "빈 응답일 수 있습니다)."
        )

    return text.strip()