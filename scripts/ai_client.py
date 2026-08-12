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
DEFAULT_MAX_TOKENS = 5000

# 재시도해도 성공 가능성이 낮은 HTTP 상태코드(인증 오류/잘못된 요청/모델 없음 등).
# 이 코드에 해당하면 즉시 실패 처리하고 재시도하지 않는다.
NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404}


def _get_config() -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        # API Key가 없으면 자동화는 기존과 동일하게 실패 처리되어야 한다.
        raise AIClientError("GEMINI_API_KEY 환경변수가 설정되어 있지 않습니다.")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    timeout = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    max_retries = int(os.environ.get("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES))
    max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", DEFAULT_MAX_TOKENS))

    return {
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "max_retries": max_retries,
        "max_tokens": max_tokens,
    }


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
    )

    last_error: AIClientError | None = None

    for attempt in range(1, config["max_retries"] + 1):
        try:
            response = client.models.generate_content(
                model=config["model"],
                contents=user_prompt,
                config=generation_config,
            )
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
