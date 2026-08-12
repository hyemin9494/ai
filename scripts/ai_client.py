"""
ai_client.py

Claude API 호출 전용 모듈.

역할 (명세 27번 항목):
- Claude API 호출
- 모델 설정 (환경변수/설정으로 관리, 하드코딩 금지)
- timeout
- retry
- 오류 처리

이 모듈은 뉴스 수집(news_fetcher.py)이나 보고서 검증(validate_report.py)
로직을 알지 못한다. 오직 "프롬프트 문자열을 주면 완성된 텍스트를 돌려준다"는
역할만 수행한다.
"""

from __future__ import annotations

import os
import time
import urllib.request
import urllib.error
import json


class AIClientError(RuntimeError):
    """Claude API 호출 관련 오류를 나타내는 예외."""


# 모델명은 코드에 하드코딩하지 않고 환경변수로 관리한다 (명세 27번).
# ANTHROPIC_MODEL이 설정되지 않은 경우에만 아래 기본값을 사용한다.
DEFAULT_MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 5
DEFAULT_MAX_TOKENS = 4096


def _get_config():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # API Key가 없으면 자동화는 명세 18번에 따라 실패 처리되어야 한다.
        raise AIClientError("ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다.")

    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    timeout = int(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    max_retries = int(os.environ.get("ANTHROPIC_MAX_RETRIES", DEFAULT_MAX_RETRIES))
    max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", DEFAULT_MAX_TOKENS))

    return {
        "api_key": api_key,
        "model": model,
        "timeout": timeout,
        "max_retries": max_retries,
        "max_tokens": max_tokens,
    }


def generate_text(system_prompt: str, user_prompt: str) -> str:
    """
    Claude API의 /v1/messages 엔드포인트를 호출하여 완성된 텍스트를 반환한다.

    실패 시 재시도(retry)를 수행하고, 최종 실패 시 AIClientError를 발생시켜
    상위 스크립트(generate_brief.py)가 자동화를 실패 처리할 수 있도록 한다.
    빈 문자열이나 예외를 삼키지 않는다 (명세 18번: 실패를 성공으로 처리 금지).
    """
    config = _get_config()

    payload = {
        "model": config["model"],
        "max_tokens": config["max_tokens"],
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }

    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "x-api-key": config["api_key"],
        "anthropic-version": ANTHROPIC_VERSION,
    }

    last_error = None

    for attempt in range(1, config["max_retries"] + 1):
        try:
            request = urllib.request.Request(
                ANTHROPIC_API_URL, data=body, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
                response_body = response.read().decode("utf-8")
                data = json.loads(response_body)
                return _extract_text(data)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            last_error = AIClientError(
                f"Claude API HTTP 오류 (시도 {attempt}/{config['max_retries']}): "
                f"{exc.code} {exc.reason} - {error_body}"
            )
            # 4xx(요청 오류, 인증 오류 등)는 재시도해도 성공 가능성이 낮으므로 즉시 중단.
            if 400 <= exc.code < 500:
                raise last_error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = AIClientError(
                f"Claude API 연결 오류 (시도 {attempt}/{config['max_retries']}): {exc}"
            )
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = AIClientError(
                f"Claude API 응답 파싱 오류 (시도 {attempt}/{config['max_retries']}): {exc}"
            )

        if attempt < config["max_retries"]:
            time.sleep(DEFAULT_RETRY_BACKOFF_SECONDS * attempt)

    raise last_error or AIClientError("Claude API 호출에 실패했습니다 (알 수 없는 오류).")


def _extract_text(data: dict) -> str:
    content_blocks = data.get("content")
    if not content_blocks:
        raise AIClientError("Claude API 응답에 content가 없습니다 (AI 응답 없음).")

    text_parts = [
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(text_parts).strip()

    if not text:
        raise AIClientError("Claude API 응답에 텍스트가 없습니다 (AI 응답 없음).")

    return text
