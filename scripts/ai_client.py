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
    - AIClientError (예외 클래스명도 동일)
따라서 generate_brief.py는 import 구문을 포함해 전혀 수정할 필요가 없다
(`import ai_client`, `ai_client.generate_text(...)`, `except ai_client.AIClientError`
모두 그대로 동작한다).

역할 (기존 명세 27번 항목과 동일한 책임 범위를 유지):
- Gemini API 호출 — 공식 Python SDK인 google-genai 사용
  (`pip install google-genai`, PyPI: https://pypi.org/project/google-genai/,
  GitHub: https://github.com/googleapis/python-genai)
- 모델 설정 (환경변수로 관리, 코드에 하드코딩하지 않음)
- timeout
- retry (최대 2~3회 — 무한 재시도하지 않음)
- 오류 처리 (API Key 없음/잘못됨, HTTP 오류, rate limit, quota 초과, timeout,
  빈 응답, 응답 파싱 실패)
- API Key는 어떤 경우에도 로그에 출력하지 않음

이 모듈은 뉴스 수집(news_fetcher.py)이나 보고서 검증(validate_report.py)
로직을 알지 못한다. 오직 "프롬프트 문자열을 주면 완성된 텍스트를 돌려준다"는
역할만 수행한다.
"""

from __future__ import annotations

import os
import time

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types


class AIClientError(RuntimeError):
    """Gemini API 호출 관련 오류를 나타내는 예외 (기존 클래스명 그대로 유지)."""


# 로그/사용자 안내에 사용할 공급자 표기. generate_brief.py의 로그(명세 16번:
# "AI provider: Gemini")에서 사용한다.
AI_PROVIDER_NAME = "Gemini"

# 모델명은 코드에 하드코딩하지 않고 환경변수(GEMINI_MODEL)로 관리한다.
# GEMINI_MODEL이 설정되지 않은 경우에만 아래 기본값을 사용한다.
#
# 기본값 gemini-3.6-flash는 2026-07-21에 정식 출시(GA, Generally Available)된
# 안정 버전 Flash 모델이다. Google 공식 문서("Using the latest Gemini models",
# ai.google.dev/gemini-api/docs/latest-model, 마지막 업데이트 2026-08-06 UTC 기준
# 확인)에 따르면 이 모델이 현재 신규 코드의 기본 권장 모델이다. 존재 여부를
# 추측하지 않고 공식 문서 기준으로 확정한 값이며, 모델이 추후 변경되어도
# GEMINI_MODEL 환경변수(GitHub Variables)만 바꾸면 코드 수정 없이 반영된다.
DEFAULT_MODEL = "gemini-3.6-flash"

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 5

# 기존 5000에서 8000으로 상향 (문제 진단 결과 반영).
#
# 실제 원인: gemini-3.x 계열 모델(gemini-3.6-flash 포함)은 기본적으로
# "thinking"(내부 추론) 토큰을 사용하며, 이 thinking 토큰이 max_output_tokens
# 예산을 실제 출력 텍스트와 "합산"해서 소비한다(Google 공식 문서 "Thinking |
# Gemini Enterprise Agent Platform"과 다수의 실사용 이슈 리포트에서 확인:
# usageMetadata.thoughtsTokenCount가 출력 쪽 과금/예산에 포함됨). 이번 프로젝트는
# 입력이 30건의 뉴스 후보(약 28,000토큰)로 크고 요구 출력 형식도 7개 섹션 +
# 6~10건의 구조화된 뉴스 분석이라 실제 텍스트 생성에 필요한 토큰이 상당한데,
# 기존 max_output_tokens=5000 중 상당 부분이 thinking 토큰으로 소진되면서
# 응답이 "주요 뉴스 브리핑"까지만 쓰이고 중간에 잘린 것으로 보인다(finish_reason
# 진단 로그로 다음 실행에서 확정 가능 - 아래 generate_text 참고).
#
# 대응: (1) thinking_level을 LOW로 낮춰 thinking 토큰 소비 자체를 줄이고,
# (2) max_output_tokens 여유치를 8000으로 상향해 이중으로 여유를 확보한다.
DEFAULT_MAX_TOKENS = 8000

# gemini-3.x 모델의 thinking 강도 설정. thinking_budget(구 파라미터, 2.5 계열)
# 대신 3.x 계열은 thinking_level(문자열 등급: MINIMAL/LOW/MEDIUM/HIGH)을 쓴다
# (Google 공식 문서 "Thinking | Gemini Enterprise Agent Platform" 및 SDK
# `google.genai.types.ThinkingLevel` enum에서 확인). 이 프로젝트는 창의적
# 추론이 아니라 "주어진 뉴스 후보를 정해진 형식으로 정리"하는 구조화된
# 요약/분류 작업이므로 깊은 추론이 크게 필요하지 않다고 보고 LOW로 낮춰
# thinking 토큰 소비를 줄이고 실제 출력(보고서 본문)에 더 많은 토큰 예산을
# 확보한다. 값 자체가 부담될 경우 GEMINI_THINKING_LEVEL 환경변수로 조정 가능.
DEFAULT_THINKING_LEVEL = "LOW"

# 재시도해도 성공 가능성이 낮은 HTTP 상태코드(인증 오류/잘못된 요청/모델 없음 등).
# 이 코드에 해당하면 즉시 실패 처리하고 재시도하지 않는다.
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}

# 디버그 로그에서 응답 본문 앞/뒤로 몇 글자를 미리보기로 출력할지. API Key나
# 개인정보는 애초에 이 응답 본문에 포함되지 않지만(뉴스 요약/분석 텍스트일 뿐),
# 로그가 과도하게 길어지는 것을 막기 위해 상한을 둔다.
DEBUG_PREVIEW_CHARS = 300


def _get_config() -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        # API Key가 없으면 자동화는 기존과 동일하게 실패 처리되어야 한다.
        raise AIClientError("GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다.")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    timeout = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES))
    max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", DEFAULT_MAX_TOKENS))
    thinking_level = os.environ.get("GEMINI_THINKING_LEVEL", DEFAULT_THINKING_LEVEL)

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
    Gemini 응답의 진단 정보를 로그로 남긴다 (명세: 원본 텍스트/빈 응답 여부/
    heading 형식을 확인할 수 있는 디버그 로그, 단 API Key나 개인정보는 출력하지
    않음). 이 함수가 다루는 정보는 모델 사용량 메타데이터와 보고서 본문(뉴스
    요약/분석 텍스트) 미리보기뿐이며, API Key나 개인정보는 애초에 이 응답에
    포함되지 않는다.
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        finish_reason = candidates[0].finish_reason if candidates else None
    except Exception:  # noqa: BLE001 - 진단 로그 자체가 실패해도 본 로직에 영향 주지 않음
        finish_reason = None

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", None) if usage else None
    output_tokens = getattr(usage, "candidates_token_count", None) if usage else None
    thinking_tokens = getattr(usage, "thoughts_token_count", None) if usage else None
    total_tokens = getattr(usage, "total_token_count", None) if usage else None

    print(
        f"[ai_client] Gemini 응답 메타데이터: finish_reason={finish_reason}, "
        f"prompt_tokens={prompt_tokens}, output_tokens={output_tokens}, "
        f"thinking_tokens={thinking_tokens}, total_tokens={total_tokens}"
    )

    # finish_reason == MAX_TOKENS는 "응답이 정상적으로 끝난 것이 아니라 토큰
    # 예산이 소진되어 중간에 잘렸다"는 뜻이다. 이번에 실제로 관찰된 "뒤쪽 4개
    # 섹션 누락" 증상과 정확히 일치하는 원인이므로 명확한 경고를 남긴다.
    if finish_reason is not None and str(finish_reason).endswith("MAX_TOKENS"):
        print(
            "[ai_client] 경고: finish_reason=MAX_TOKENS - 응답이 max_output_tokens "
            "예산(thinking 토큰 + 실제 출력 토큰 합산)을 모두 소진해 중간에 잘렸을 "
            "가능성이 매우 높습니다. GEMINI_MAX_TOKENS를 늘리거나 GEMINI_THINKING_LEVEL을 "
            "더 낮춰(MINIMAL) 재시도하는 것을 검토하세요."
        )

    try:
        full_text = response.text or ""
    except Exception as exc:  # noqa: BLE001 - 진단 로그 자체가 실패해도 본 로직에 영향 주지 않음
        print(f"[ai_client] 응답 텍스트 미리보기 생성 실패: {type(exc).__name__}: {exc}")
        return

    length = len(full_text)
    print(f"[ai_client] 응답 본문 길이: {length:,}자 (비어있음: {length == 0})")

    if length == 0:
        print("[ai_client] 응답 본문이 완전히 비어 있습니다 (미리보기 없음).")
        return

    head = full_text[:DEBUG_PREVIEW_CHARS]
    tail = full_text[-DEBUG_PREVIEW_CHARS:] if length > DEBUG_PREVIEW_CHARS else ""
    print(f"[ai_client] 응답 앞부분 미리보기 (최대 {DEBUG_PREVIEW_CHARS}자):\n{head}")
    if tail:
        print(f"[ai_client] 응답 뒷부분 미리보기 (마지막 {DEBUG_PREVIEW_CHARS}자):\n{tail}")


def generate_text(system_prompt: str, user_prompt: str) -> str:
    """
    Gemini API(공식 SDK google-genai)의 client.models.generate_content()를
    호출하여 완성된 텍스트를 반환한다.

    실패 시 재시도(최대 config['max_retries']회, 기본 3회, 무한 재시도 금지)를
    수행하고, 최종 실패 시 AIClientError를 발생시켜 상위 스크립트
    (generate_brief.py)가 자동화를 실패 처리할 수 있도록 한다. 빈 문자열이나
    예외를 삼키지 않는다.
    """
    config = _get_config()

    # google-genai SDK의 HttpOptions.timeout은 "밀리초" 단위다 (초 단위 아님 -
    # 공식 문서 및 SDK 이슈에서 확인된 사양).
    client = genai.Client(
        api_key=config["api_key"],
        http_options=genai_types.HttpOptions(timeout=config["timeout"] * 1000),
    )

    generation_config = genai_types.GenerateContentConfig(
        system_instruction=system_prompt,
        max_output_tokens=config["max_tokens"],
        thinking_config=genai_types.ThinkingConfig(thinking_level=config["thinking_level"]),
    )

    print(
        f"[ai_client] Gemini 호출 설정: model={config['model']}, "
        f"max_output_tokens={config['max_tokens']}, thinking_level={config['thinking_level']}"
    )

    last_error: AIClientError | None = None

    for attempt in range(1, config["max_retries"] + 1):
        try:
            response = client.models.generate_content(
                model=config["model"],
                contents=user_prompt,
                config=generation_config,
            )
            _log_response_debug_info(response)
            return _extract_text(response)

        except genai_errors.APIError as exc:
            # google-genai SDK는 4xx를 ClientError, 5xx를 ServerError로 구분해
            # 발생시키며, 둘 다 공통 상위 클래스 APIError에 .code/.message를 갖는다.
            status_code = getattr(exc, "code", None)
            message = getattr(exc, "message", str(exc))
            last_error = AIClientError(
                f"Gemini API 오류 (시도 {attempt}/{config['max_retries']}): "
                f"{status_code} {message}"
            )
            if status_code in NON_RETRYABLE_STATUS_CODES:
                # 인증 오류(401/403) / 잘못된 요청(400) / 모델 없음(404)은
                # 재시도해도 결과가 바뀌지 않으므로 즉시 중단한다.
                raise last_error
            # 그 외(429 rate limit/quota 초과, 5xx 서버 오류 등)는 재시도 대상.

        except AIClientError:
            # _extract_text에서 올라온 오류(빈 응답/파싱 실패)는 그대로 전달한다.
            raise

        except Exception as exc:  # noqa: BLE001 - SDK/네트워크 계층(timeout, 연결 오류 등) 포괄 처리
            last_error = AIClientError(
                f"Gemini API 연결/처리 오류 (시도 {attempt}/{config['max_retries']}): "
                f"{type(exc).__name__}: {exc}"
            )

        if attempt < config["max_retries"]:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * attempt)

    raise last_error or AIClientError("Gemini API 호출에 실패했습니다 (알 수 없는 오류).")


def _extract_text(response) -> str:
    """
    Gemini 응답에서 텍스트를 추출한다. `response.text`는 공식 SDK가 제공하는
    편의 접근자이며, 안전 필터에 의해 후보(candidates)가 비어 있는 등의 경우
    접근 자체가 예외를 던질 수 있으므로 별도로 감싸서 처리한다.
    """
    try:
        text = response.text
    except Exception as exc:  # noqa: BLE001 - SDK 응답 구조 관련 예외 포괄 처리
        raise AIClientError(
            f"Gemini API 응답 파싱 실패: {type(exc).__name__}: {exc}"
        ) from exc

    if not text or not text.strip():
        raise AIClientError(
            "Gemini API 응답에 텍스트가 없습니다 (AI 응답 없음 - 안전 필터에 의해 "
            "차단되었거나 빈 응답일 수 있습니다)."
        )

    return text.strip()
