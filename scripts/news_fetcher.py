"""
news_fetcher.py

Tavily Search API 기반 뉴스 수집 모듈.

역할:
- Tavily 뉴스 검색
- 날짜 필터 (전일 00:00~24:00, Asia/Seoul 기준)
- 출처 우선순위 적용
- 중복 제거
- 제목/URL/날짜/출처/요약 정규화

GitHub 환경변수:
- SEARCH_API_URL: https://api.tavily.com/search
- SEARCH_API_KEY: Tavily API Key
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SEOUL_TZ = ZoneInfo("Asia/Seoul")

OFFICIAL_SOURCES = [
    "금융위원회", "금융감독원", "한국은행", "기획재정부", "통계청",
    "BIS", "IMF", "OECD", "Federal Reserve", "ECB",
]

MAJOR_PRESS_SOURCES = [
    "연합뉴스", "한국경제", "매일경제", "서울경제", "이데일리",
    "조선비즈", "더벨", "연합인포맥스",
]

SEARCH_TOPICS = [
    "저축은행 PF",
    "저축은행 연체율",
    "저축은행 BIS 유동성",
    "저축은행 감독 금융감독원",
    "저축은행 M&A",
    "저축은행 예금금리 대출금리",
    "금융위원회 저축은행",
    "한국은행 기준금리 금융시장",
    "원달러 환율",
    "국고채 금리 회사채 금리",
    "코스피 은행주 금융주",
    "가계부채 부동산 PF",
]


class NewsFetchError(RuntimeError):
    """뉴스 수집 실패를 나타내는 예외."""


def _source_priority(source_name: str) -> int:
    if any(name.lower() in source_name.lower() for name in OFFICIAL_SOURCES):
        return 0
    if any(name.lower() in source_name.lower() for name in MAJOR_PRESS_SOURCES):
        return 1
    return 2


def _source_from_url(url: str) -> str:
    """Tavily 결과에 별도 source 필드가 없으므로 URL 도메인을 출처로 사용한다."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host or "출처 미상"
    except Exception:
        return "출처 미상"


def _normalize_published_at(value: str) -> str:
    """Tavily published_date를 기존 파이프라인이 이해할 ISO 문자열로 정규화."""
    if not value:
        return ""

    text = str(value).strip()

    # 날짜만 제공되는 경우 한국시간 자정으로 취급한다.
    if len(text) == 10:
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return f"{text}T00:00:00+09:00"
        except ValueError:
            return ""

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""

    if dt.tzinfo is None:
        return ""
    return dt.astimezone(SEOUL_TZ).isoformat()


def _normalize_article(raw: dict) -> dict:
    url = (raw.get("url") or "").strip()
    published = raw.get("published_date") or raw.get("publishedDate") or ""
    content = raw.get("content") or raw.get("raw_content") or raw.get("rawContent") or ""

    return {
        "title": (raw.get("title") or "").strip(),
        "url": url,
        "source": (raw.get("source") or _source_from_url(url)).strip(),
        "published_at": _normalize_published_at(published),
        "summary": str(content).strip(),
    }


def _within_target_day(published_at: str, target_date: datetime) -> bool:
    if not published_at:
        return False

    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False

    if dt.tzinfo is None:
        return False

    dt_seoul = dt.astimezone(SEOUL_TZ)
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start <= dt_seoul < day_end


def _call_search_api(query: str, target_date: datetime, timeout: int) -> list[dict]:
    """Tavily Search API를 POST 방식으로 호출한다."""
    api_url = os.environ.get("SEARCH_API_URL", "https://api.tavily.com/search").strip()
    api_key = os.environ.get("SEARCH_API_KEY", "").strip()

    if not api_url or not api_key:
        raise NewsFetchError(
            "SEARCH_API_URL 및 SEARCH_API_KEY 환경변수가 설정되어 있지 않습니다."
        )

    target_day = target_date.astimezone(SEOUL_TZ).date()
    next_day = target_day + timedelta(days=1)

    payload = {
        "query": query,
        "search_depth": "basic",
        "topic": "news",
        "start_date": target_day.isoformat(),
        "end_date": next_day.isoformat(),
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    last_error: Exception | None = None

    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                response_body = response.read().decode("utf-8")

            if status < 200 or status >= 300:
                raise NewsFetchError(f"Tavily HTTP 오류: {status}")

            data = json.loads(response_body)
            results = data.get("results") or []
            return [
                _normalize_article(result)
                for result in results
                if isinstance(result, dict)
            ]

        except urllib.error.HTTPError as exc:
            # 401/403은 인증 문제이므로 반복해도 해결되지 않는다.
            if exc.code in (400, 401, 403):
                detail = exc.read().decode("utf-8", errors="replace")
                raise NewsFetchError(
                    f"Tavily API 요청 오류 HTTP {exc.code}: {detail[:500]}"
                ) from exc
            last_error = exc

        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc

        if attempt < 2:
            time.sleep(2 ** attempt)

    raise NewsFetchError(
        f"Tavily 뉴스 검색 실패 (query='{query}'): {last_error}"
    ) from last_error


def _dedupe_key(article: dict) -> str:
    title = "".join((article.get("title") or "").lower().split())
    return title


def fetch_news_for_date(target_date: datetime, timeout: int = 30) -> list[dict]:
    """
    target_date(Asia/Seoul, tz-aware)의 하루치 뉴스를 수집하여
    날짜 필터 + 중복 제거 + 출처 우선순위 정렬 후 반환한다.
    """
    if target_date.tzinfo is None:
        raise NewsFetchError("target_date는 timezone-aware 이어야 합니다 (naive datetime 금지).")

    all_articles: list[dict] = []
    failed_topics: list[str] = []

    for topic in SEARCH_TOPICS:
        try:
            articles = _call_search_api(topic, target_date, timeout=timeout)
            all_articles.extend(articles)
            print(f"[news_fetcher] '{topic}' 검색 완료: {len(articles)}건")
        except NewsFetchError as exc:
            failed_topics.append(topic)
            print(f"[news_fetcher] 경고: '{topic}' 검색 실패 - {exc}")

    if not all_articles and failed_topics:
        raise NewsFetchError(
            f"모든 뉴스 검색 주제({len(failed_topics)}개)가 실패했습니다: {failed_topics}"
        )

    # Tavily의 서버측 날짜 필터와 별개로 Python에서 한 번 더 검증한다.
    filtered = [
        article
        for article in all_articles
        if article.get("title")
        and article.get("url")
        and _within_target_day(article.get("published_at", ""), target_date)
    ]

    # 동일 URL 제거
    by_url: dict[str, dict] = {}
    for article in filtered:
        url = article["url"].rstrip("/")
        existing = by_url.get(url)
        if existing is None or _source_priority(article["source"]) < _source_priority(existing["source"]):
            by_url[url] = article

    # 제목 중복 제거
    deduped: dict[str, dict] = {}
    for article in by_url.values():
        key = _dedupe_key(article)
        if not key:
            continue
        existing = deduped.get(key)
        if existing is None or _source_priority(article["source"]) < _source_priority(existing["source"]):
            deduped[key] = article

    result = list(deduped.values())
    result.sort(key=lambda a: (_source_priority(a["source"]), a["title"]))

    print(
        f"[news_fetcher] 최종 뉴스: {len(result)}건 "
        f"(검색결과 {len(all_articles)}건 → 날짜필터 {len(filtered)}건 → 중복제거 {len(result)}건)"
    )

    return result


if __name__ == "__main__":
    # GitHub Actions 밖에서 직접 연결 테스트할 때 사용.
    # API Key 자체는 절대 출력하지 않는다.
    test_target = datetime.now(SEOUL_TZ) - timedelta(days=1)
    test_target = test_target.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        results = fetch_news_for_date(test_target)
        print(f"\n테스트 기준일: {test_target.date()}")
        print(f"수집 건수: {len(results)}")
        for article in results[:10]:
            print(f"- {article['title']} | {article['source']} | {article['published_at']}")
            print(f"  {article['url']}")
    except NewsFetchError as exc:
        print(f"테스트 실패: {exc}")
        raise SystemExit(1)
