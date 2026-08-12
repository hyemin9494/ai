"""
news_fetcher.py

뉴스 수집 전용 모듈 (명세 28번 항목).

역할:
- 뉴스 검색
- 날짜 필터 (전일 00:00~24:00, Asia/Seoul 기준)
- 출처 우선순위 적용 (공식기관 > 주요 언론 > 보조 언론)
- 중복 제거
- 제목/URL/날짜/출처 정규화

이 모듈은 AI 작성 로직(ai_client.py)을 알지 못한다. 뉴스 수집 결과를
정규화된 리스트(list[dict])로 반환하는 역할만 수행한다.

실제 뉴스 검색 API는 배포 환경에 따라 교체될 수 있도록 추상화되어 있다.
기본 구현은 SEARCH_API_URL / SEARCH_API_KEY 환경변수를 사용하는
범용 뉴스/웹 검색 API(JSON 응답)를 호출한다고 가정한다.
실제 사용할 서비스는 README.md의 "필수 설정" 항목을 참고하여
아래 `_call_search_api` 함수만 교체하면 된다.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SEOUL_TZ = ZoneInfo("Asia/Seoul")

# 출처 우선순위 (명세 14번, 16번).
OFFICIAL_SOURCES = [
    "금융위원회", "금융감독원", "한국은행", "기획재정부", "통계청",
    "BIS", "IMF", "OECD", "Federal Reserve", "ECB",
]

MAJOR_PRESS_SOURCES = [
    "연합뉴스", "한국경제", "매일경제", "서울경제", "이데일리",
    "조선비즈", "더벨", "연합인포맥스",
]

SEARCH_TOPICS = [
    "저축은행 PF", "저축은행 연체율", "저축은행 감독", "저축은행 M&A",
    "금융위원회", "금융감독원", "한국은행 기준금리", "코스피 은행주",
    "원달러 환율", "미국 기준금리", "국고채 금리", "가계부채",
]


class NewsFetchError(RuntimeError):
    """뉴스 수집 실패를 나타내는 예외."""


def _source_priority(source_name: str) -> int:
    """숫자가 낮을수록 우선순위가 높다."""
    if any(name in source_name for name in OFFICIAL_SOURCES):
        return 0
    if any(name in source_name for name in MAJOR_PRESS_SOURCES):
        return 1
    return 2


def _normalize_article(raw: dict) -> dict:
    return {
        "title": (raw.get("title") or "").strip(),
        "url": (raw.get("url") or "").strip(),
        "source": (raw.get("source") or "출처 미상").strip(),
        "published_at": (raw.get("published_at") or "").strip(),
        "summary": (raw.get("summary") or "").strip(),
    }


def _within_target_day(published_at: str, target_date: datetime) -> bool:
    """
    published_at(ISO 8601 문자열)이 target_date(Asia/Seoul 기준 하루) 범위
    00:00:00 ~ 23:59:59 안에 있는지 확인한다. 파싱 불가능한 경우 제외한다
    (명세 16번: 전일 00:00~24:00 기준이 아닌 뉴스는 기본적으로 제외).
    """
    if not published_at:
        return False
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False

    if dt.tzinfo is None:
        # naive datetime은 사용하지 않는다 (명세 24번). 파싱 실패로 간주하여 제외.
        return False

    dt_seoul = dt.astimezone(SEOUL_TZ)
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start <= dt_seoul < day_end


def _call_search_api(query: str, timeout: int) -> list[dict]:
    """
    실제 검색 API 호출부. SEARCH_API_URL / SEARCH_API_KEY 환경변수로
    설정된 뉴스/웹 검색 API를 호출한다고 가정한다.

    API 응답 스키마는 서비스마다 다르므로, 실제 배포 시 이 함수 내부만
    교체하면 나머지 파이프라인(중복 제거, 우선순위, 날짜 필터)은
    그대로 재사용할 수 있다.
    """
    api_url = os.environ.get("SEARCH_API_URL")
    api_key = os.environ.get("SEARCH_API_KEY") or os.environ.get("NEWS_API_KEY")

    if not api_url or not api_key:
        raise NewsFetchError(
            "SEARCH_API_URL 및 SEARCH_API_KEY(또는 NEWS_API_KEY) 환경변수가 "
            "설정되어 있지 않습니다. README.md의 '필수 설정' 항목을 확인하세요."
        )

    request_url = f"{api_url}?q={urllib.parse.quote(query)}"
    request = urllib.request.Request(
        request_url,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NewsFetchError(f"뉴스 검색 API 호출 실패 (query='{query}'): {exc}") from exc

    articles = data.get("articles") or data.get("results") or []
    return [_normalize_article(a) for a in articles if isinstance(a, dict)]


def fetch_news_for_date(target_date: datetime, timeout: int = 30) -> list[dict]:
    """
    target_date(Asia/Seoul, tz-aware) 하루치(00:00~24:00) 뉴스를 수집하여
    정규화 + 중복 제거 + 출처 우선순위 정렬된 리스트로 반환한다.

    뉴스 수집이 전면 실패한 경우 NewsFetchError를 발생시킨다
    (명세 18번: 자동화 실패 조건 - 뉴스 수집 실패).
    """
    if target_date.tzinfo is None:
        raise NewsFetchError("target_date는 timezone-aware 이어야 합니다 (naive datetime 금지).")

    all_articles: list[dict] = []
    failed_topics: list[str] = []

    for topic in SEARCH_TOPICS:
        try:
            all_articles.extend(_call_search_api(topic, timeout=timeout))
        except NewsFetchError as exc:
            failed_topics.append(topic)
            # 개별 주제 검색 실패는 기록만 하고 계속 진행한다.
            print(f"[news_fetcher] 경고: '{topic}' 검색 실패 - {exc}")

    if not all_articles and failed_topics:
        raise NewsFetchError(
            f"모든 뉴스 검색 주제({len(failed_topics)}개)가 실패했습니다: {failed_topics}"
        )

    # 날짜 필터: 전일 00:00~24:00 (Asia/Seoul) 기준만 유지.
    filtered = [a for a in all_articles if _within_target_day(a["published_at"], target_date)]

    # 중복 제거: (정규화된 제목) 기준. 동일 이슈의 다중 보도는 하나로 통합하고,
    # 우선순위가 가장 높은 출처의 기사를 대표로 남긴다.
    deduped: dict[str, dict] = {}
    for article in filtered:
        key = article["title"].replace(" ", "").lower()
        if not key:
            continue
        if key not in deduped:
            deduped[key] = article
        else:
            existing_priority = _source_priority(deduped[key]["source"])
            new_priority = _source_priority(article["source"])
            if new_priority < existing_priority:
                deduped[key] = article

    result = list(deduped.values())
    result.sort(key=lambda a: _source_priority(a["source"]))

    return result
