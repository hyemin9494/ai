"""
news_fetcher.py (v4 — Claude 입력 토큰 절감을 위한 Python 후보 축소 추가)

v4 변경 사항 (문제: 최종 후보 79건 전체가 그대로 Claude 프롬프트에 들어가
입력 토큰이 과도해지는 문제 수정):
- 기존에는 카테고리별 상한(MAX_CANDIDATES_PER_CATEGORY=15, 최대 7개 카테고리
  x 15 = 105건까지도 가능)만 있어 "충분한 후보"를 넘어 "지나치게 많은 후보"가
  그대로 Claude에 전달되는 문제가 있었다.
- CATEGORY_CANDIDATE_CAPS(저축은행 8 / 국내금융정책 5 / 경제 4 / 금리 3 /
  환율 2 / 증시 4 / 국제 4, 합계 30)로 카테고리별 캡을 명확히 정하고,
  `_candidate_score()`로 (1)공식기관 출처 (2)저축은행 직접 관련성
  (3)금융시장 영향도 (4)최신성 (5)구체성 (6)중복 가능성으로 점수를 매겨
  카테고리별 상위 N건만 Claude에 전달하도록 변경했다.
- "날짜필터 통과 → 중복제거 → Python 후보 축소" 각 단계의 건수를 모두
  로그로 남겨 진단 가능하게 했다 (API Key 등 민감정보는 출력하지 않음).

v3 변경 사항 (문제: "검색결과 40건 → 날짜필터 0건" 재발 이슈 수정):

뉴스 수집 전용 모듈 (명세 28번 항목, 그리고 "뉴스 2건만 수집되는 문제" 개선 요청 반영).

v3 변경 사항 (문제: "검색결과 40건 → 날짜필터 0건" 재발 이슈 수정):
- 근본 원인: 기존 `_parse_published_at`는 timezone 정보가 없는(naive)
  published_date를 파싱에 성공해도 무조건 버렸다. 그런데 Tavily 검색 결과의
  published_date는 소스에 따라 "2026-08-11 09:00:00"처럼 timezone 정보가
  전혀 없는 형태로 오는 경우가 흔하고(특히 국내 언론사 메타데이터),
  이런 값이 검색 결과의 상당 부분(이번 사례는 전부)을 차지하면 날짜 필터
  이후 후보가 0건이 되는 문제가 발생했다.
- 수정: timezone 정보가 없는 날짜는 Asia/Seoul(KST) 로컬 시각으로 간주하는
  정책을 명시적으로 도입했다 (자세한 근거는 `_parse_published_at` 위쪽 주석
  참고). 아울러 RFC822/ISO8601(공백·T 구분 모두)/날짜만 있는 값(".", "-", "/"
  구분 모두)/Unix epoch까지 폭넓게 인식하도록 파서를 보강했다.
- 디버그: 날짜 필터링 전/후 건수와, 제외된 기사가 "published_date 없음 /
  날짜 파싱 실패 / 날짜 범위 밖" 중 어떤 이유로 제외됐는지 로그에 남긴다
  (API Key 등 민감정보는 절대 출력하지 않는다).

개선 핵심:
- 기존에는 카테고리 구분 없이 단일 토픽 리스트(12개) 각각 1회 검색만 수행하여
  최종 결과가 2건 수준으로 지나치게 적었다.
- v2는 7개 대분류(저축은행/국내금융정책/경제/금리/환율/증시/국제) x
  대분류별 다수의 세부 검색어로 "넓게" 검색한 뒤, 날짜 필터 → 출처 필터 →
  중복 제거 → 우선순위 정렬을 거쳐 "충분한 후보(목표 20건 이상, 가능하면
  30~50건)"를 AI에게 전달한다. 최종 6~10건 선정은 AI(generate_brief.py가
  호출하는 프롬프트)가 담당한다 — 이 모듈은 "선정"이 아니라 "후보 확보"까지만
  책임진다.

역할:
- 뉴스 검색 (Tavily Search API 기본 사용, 다른 검색 API로 교체 가능하도록 추상화)
- 날짜 필터 (Asia/Seoul 기준 전일 00:00~23:59:59) — API 자체 날짜 필터(Tavily의
  `days` 파라미터)와 파이썬 내부 날짜 필터를 모두 사용한다 (이중 검증).
- 출처 우선순위 적용 (공식기관 > 주요 언론 > 기타)
- 중복 제거 (URL 기준 1차, 정규화된 제목 기준 2차)
- 제목/URL/날짜/출처 정규화

이 모듈은 AI 작성 로직(ai_client.py)을 알지 못한다. 카테고리 태그가 붙은
정규화된 리스트(list[dict])를 반환하는 역할만 수행한다.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

SEOUL_TZ = ZoneInfo("Asia/Seoul")

# ---------------------------------------------------------------------------
# 출처 우선순위 (명세 3번: 공식기관 > 주요 언론 > 보조 언론)
# ---------------------------------------------------------------------------

OFFICIAL_SOURCES = [
    "금융위원회", "금융감독원", "한국은행", "기획재정부", "통계청",
    "BIS", "IMF", "OECD", "Federal Reserve", "연방준비제도", "ECB",
]

MAJOR_PRESS_SOURCES = [
    "연합뉴스", "한국경제", "매일경제", "서울경제", "이데일리",
    "조선비즈", "더벨", "연합인포맥스",
]

# 도메인 -> 표시용 출처명 매핑 (Tavily 등 검색 API가 언론사명을 직접 주지 않고
# URL만 주는 경우를 대비한 보정용. 필요에 따라 계속 추가 가능).
DOMAIN_SOURCE_MAP = {
    "yna.co.kr": "연합뉴스",
    "hankyung.com": "한국경제",
    "mk.co.kr": "매일경제",
    "sedaily.com": "서울경제",
    "edaily.co.kr": "이데일리",
    "chosun.com": "조선비즈",
    "biz.chosun.com": "조선비즈",
    "thebell.co.kr": "더벨",
    "yonhapinfomax.co.kr": "연합인포맥스",
    "fsc.go.kr": "금융위원회",
    "fss.or.kr": "금융감독원",
    "bok.or.kr": "한국은행",
    "moef.go.kr": "기획재정부",
    "kostat.go.kr": "통계청",
    "bis.org": "BIS",
    "imf.org": "IMF",
    "oecd.org": "OECD",
    "federalreserve.gov": "Federal Reserve",
    "ecb.europa.eu": "ECB",
}

# ---------------------------------------------------------------------------
# 카테고리별 검색어 (명세 2번 A~G 그대로 반영, D/E는 "금리"/"환율"로 분리 유지)
# ---------------------------------------------------------------------------

CATEGORY_QUERIES: dict[str, list[str]] = {
    "저축은행": [
        "저축은행", "저축은행 PF", "저축은행 브릿지론", "저축은행 토담대",
        "저축은행 연체율", "저축은행 고정이하여신", "저축은행 BIS",
        "저축은행 유동성", "저축은행 예금금리", "저축은행 대출금리",
        "저축은행 NPL", "저축은행 M&A", "저축은행 검사", "저축은행 제재",
    ],
    "국내금융정책": [
        "금융위원회", "금융감독원", "한국은행", "기획재정부",
        "금융정책", "금융규제", "가계대출", "부동산 PF",
    ],
    "경제": [
        "CPI", "PPI", "GDP", "고용", "소비", "수출입", "경기", "부동산",
    ],
    "금리": [
        "한국 기준금리", "미국 기준금리", "Fed", "국고채", "회사채",
        "금융채", "시장금리",
    ],
    "환율": [
        "원달러", "원엔", "원위안", "달러인덱스",
    ],
    "증시": [
        "코스피", "코스닥", "미국 증시", "금융주", "은행주",
        "외국인 수급", "기관 수급",
    ],
    "국제": [
        "미국 경제", "중국 경제", "일본 경제", "유럽 경제",
        "중동", "지정학", "글로벌 금융시장",
    ],
}

# "주요 뉴스 브리핑"의 6개 하위 카테고리와 위 CATEGORY_QUERIES 키의 매핑.
# (프롬프트 구성 단계 - generate_brief.py에서 이 매핑을 사용해 후보를 정리한다.)
CATEGORY_TO_REPORT_SECTION = {
    "저축은행": "저축은행 핵심 이슈",
    "국내금융정책": "국내 금융정책",
    "경제": "경제",
    "금리": "금리·환율",
    "환율": "금리·환율",
    "증시": "증시",
    "국제": "국제",
}

CATEGORY_PRIORITY_ORDER = ["저축은행", "국내금융정책", "경제", "금리", "환율", "증시", "국제"]

# 각 검색어당 요청할 결과 수.
RESULTS_PER_QUERY = 6

# ---------------------------------------------------------------------------
# Claude 입력 토큰 절감을 위한 "Python 단계 후보 축소" (v4 추가)
#
# 배경: 날짜 필터/중복 제거를 마친 후보 풀이 80건 전후로 형성되는데, 이 전부를
# Claude 프롬프트에 넣으면 입력 토큰이 과도하게 커진다. Claude가 6~10건을
# "선정"하는 역할은 유지하되, 그 전에 Python 쪽에서 카테고리별로 명확한
# 우선순위 점수를 매겨 "Claude에게 넘길 후보"를 카테고리별 캡 수준으로 먼저
# 추려낸다. 캡의 합(30)은 실제 검색 결과가 부족한 카테고리가 있어도 다른
# 카테고리 쪽으로 재분배하지 않는다 — 분야 간 균형(저축은행 최우선 비중 유지)이
# "총량 30건 채우기"보다 더 중요하기 때문이다.
# ---------------------------------------------------------------------------
CATEGORY_CANDIDATE_CAPS: dict[str, int] = {
    "저축은행": 8,
    "국내금융정책": 5,
    "경제": 4,
    "금리": 3,
    "환율": 2,
    "증시": 4,
    "국제": 4,
}
TOTAL_CANDIDATE_CAP = sum(CATEGORY_CANDIDATE_CAPS.values())  # = 30

# 후보 풀(날짜 필터 + 중복 제거 이후, Python 축소 이전) 규모에 대한 참고용 기준값.
# Claude에 넘기는 최종 건수가 아니라, "선택할 재료가 충분했는지"를 진단하기 위한
# 값이다 (혼동 방지를 위해 TOTAL_CANDIDATE_CAP과 분리해서 유지).
TARGET_MIN_POOL_SIZE = 20
TARGET_GOOD_POOL_SIZE = 30

DEFAULT_TAVILY_URL = "https://api.tavily.com/search"

# 금융시장/저축은행 영향도를 근사하기 위한 키워드 (Python 우선순위 평가 2/3단계에서 사용).
IMPACT_KEYWORDS = [
    "기준금리", "금융위", "금융감독원", "한국은행", "제재", "부실", "연체율",
    "PF", "M&A", "BIS", "규제", "정책", "충당금", "NPL", "유동성", "예대율",
]


class NewsFetchError(RuntimeError):
    """뉴스 수집 실패를 나타내는 예외."""


# ---------------------------------------------------------------------------
# 정규화 / 우선순위 유틸
# ---------------------------------------------------------------------------

def _source_priority(source_name: str) -> int:
    """숫자가 낮을수록 우선순위가 높다."""
    if any(name in source_name for name in OFFICIAL_SOURCES):
        return 0
    if any(name in source_name for name in MAJOR_PRESS_SOURCES):
        return 1
    return 2


def _guess_source_from_url(url: str) -> str | None:
    try:
        domain = urlparse(url).netloc.lower()
    except ValueError:
        return None
    domain = domain[4:] if domain.startswith("www.") else domain
    for known_domain, name in DOMAIN_SOURCE_MAP.items():
        if domain == known_domain or domain.endswith("." + known_domain):
            return name
    return None


def _normalize_title_key(title: str) -> str:
    """중복 제거용 제목 정규화: 공백/기호 제거 + 소문자화."""
    return re.sub(r"[\s\W]+", "", title).lower()


def _recency_hour(article: dict) -> int:
    """published_at("YYYY-MM-DD HH:MM")에서 시각(0~23)만 추출한다. 실패 시 0."""
    published_at = article.get("published_at", "")
    try:
        return int(published_at.split(" ")[1].split(":")[0])
    except (IndexError, ValueError):
        return 0


def _candidate_score(article: dict, category: str) -> float:
    """
    "Python 후보 축소" 단계에서 카테고리 내 우선순위를 매기는 점수 함수.
    점수가 높을수록 Claude에 먼저 전달될 후보다. 아래 6가지 기준(요청 명세
    2번의 우선순위 순서)을 각각 독립된 가산 요소로 반영한다.
    """
    text = f"{article['title']} {article['summary']}"
    score = 0.0

    # 1) 공식기관 출처 최우선 (공식기관=0, 주요언론=1, 기타=2 -> 점수는 역순 가중)
    priority = _source_priority(article["source"])
    score += {0: 300.0, 1: 150.0, 2: 0.0}[priority]

    # 2) 저축은행 직접 관련성: 카테고리 자체가 저축은행이거나, 제목/요약에
    #    "저축은행"이 실제로 언급된 경우 (카테고리가 달라도 저축은행이 직접
    #    언급되면 업권 관련 기사일 가능성이 높다).
    if "저축은행" in text:
        score += 120.0
    elif category == "저축은행":
        score += 60.0

    # 3) 금융시장 영향도 근사치: 정책/리스크 핵심 키워드 포함 개수만큼 가산.
    score += 15.0 * sum(1 for kw in IMPACT_KEYWORDS if kw in text)

    # 4) 최신성: 같은 "전일" 하루 안에서는 시각이 늦을수록(장 마감 후/저녁 발표 등
    #    최신 정보일 가능성) 소폭 가산. 하루 안의 미세 차이라 가중치는 작게 둔다.
    score += _recency_hour(article) * 1.0

    # 5) 제목/내용의 구체성: 구체적 수치·발표가 포함된 기사일수록 실제 브리핑에
    #    유용한 경우가 많다는 가정 하에, 숫자 포함 여부와 요약 길이를 반영한다.
    if re.search(r"\d", text):
        score += 20.0
    score += min(len(article["summary"]), 300) / 10.0

    # 6) 중복 가능성이 낮은 기사: 이 시점의 article은 이미 URL·제목 정규화
    #    기준 중복 제거를 마친 뒤이므로(아래 "중복 제거" 단계), 이 단계에서는
    #    추가 조치가 필요 없다. 다만 "N개 언론 종합"으로 병합된 기사(출처에
    #    " 등 종합"이 포함된 경우)는 여러 언론이 동시에 보도한 만큼 중요도가
    #    높은 이슈일 가능성이 크므로 소폭 가산한다.
    if "등 종합" in article["source"]:
        score += 25.0

    return score


# ---------------------------------------------------------------------------
# timezone 정보 없는(naive) published_date 처리 정책 (명세 5번)
#
# 근본 원인 분석: 기존 코드는 naive datetime(timezone 정보 없음)을 파싱해도
# 무조건 버렸다(바로 아래 "구 정책" 참고). 그런데 Tavily 뉴스 검색 결과의
# published_date는 소스마다 형식이 제각각이며, 특히 국내 언론사 페이지의
# 메타데이터를 그대로 긁어온 경우 다음과 같이 timezone 정보가 전혀 없는
# 형태(naive)로 오는 경우가 매우 흔하다:
#   - "2026-08-11 09:00:00"  (공백 구분 ISO 유사 포맷)
#   - "2026-08-11T09:00:00"  (T 구분이지만 오프셋 없음)
#   - "2026.08.11"           (국내식 점 구분 날짜)
# 이런 값들은 datetime.fromisoformat()으로는 "파싱"은 되지만 tzinfo가
# None이라서, 기존 코드의 "if dt.tzinfo is not None: return dt" 분기를
# 통과하지 못하고 이후 분기에서도 매칭되지 않아 결국 None으로 버려졌다.
# 이 프로젝트의 검색어 대부분이 국내(한국어) 뉴스를 대상으로 하기 때문에,
# 이런 naive 값들이 실제 검색 결과의 상당 부분(이번 이슈에서는 전부)을
# 차지할 수 있고, 그 결과 "검색 40건 → 날짜필터 0건"과 같은 증상이
# 발생한다.
#
# [정책] timezone 정보가 없는 published_date는 Asia/Seoul(KST) 로컬 시각으로
# 간주한다. 이 서비스가 한국 저축은행 대상 국내 금융/뉴스 브리핑이고 검색
# 대상도 대부분 국내 언론이므로, timezone이 누락된 값은 UTC보다 KST일
# 가능성이 훨씬 높다. UTC로 잘못 간주하면 야간 시간대 기사가 실제보다
# 9시간 이른 시각으로 밀려 날짜 경계에서 오탐(다음날로 인식)이 발생하므로,
# 국내 서비스 특성상 KST를 기본값으로 삼는 것이 더 안전하다.
# ---------------------------------------------------------------------------

_ISO_LIKE_DATE_ONLY_RE = re.compile(r"^(\d{4})[.\-/](\d{2})[.\-/](\d{2})$")
_UNIX_EPOCH_SECONDS_RE = re.compile(r"^\d{10}$")
_UNIX_EPOCH_MILLIS_RE = re.compile(r"^\d{13}$")


def _parse_published_at(raw: str) -> tuple[datetime | None, str]:
    """
    다양한 날짜 포맷을 timezone-aware datetime으로 파싱한다.

    반환값: (parsed_datetime 또는 None, reason)
      - reason == "ok"                : timezone 정보가 명시된 값을 정상 파싱.
      - reason == "ok_assumed_seoul"  : timezone 정보가 없어 위 정책에 따라
                                         Asia/Seoul로 간주하여 파싱.
      - reason == "no_raw"            : published_date 필드 자체가 없거나 빈 문자열.
      - reason == "parse_failed"      : 알려진 어떤 포맷으로도 파싱할 수 없음.

    이 함수는 절대 "임의로 오늘/내일 날짜"를 만들어내지 않는다 — 파싱 불가 시
    항상 None을 반환하고, 상위 로직이 그 사실을 로그로 남긴 뒤 해당 기사를
    후보에서 제외한다.
    """
    if not raw:
        return None, "no_raw"
    raw = raw.strip()
    if not raw:
        return None, "no_raw"

    # 0) Unix epoch 타임스탬프 (초/밀리초) - 일부 검색 API가 숫자 타임스탬프를
    #    문자열로 돌려주는 경우에 대비. epoch는 정의상 UTC이므로 그대로 UTC로 처리.
    if _UNIX_EPOCH_SECONDS_RE.fullmatch(raw):
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc), "ok"
        except (ValueError, OverflowError, OSError):
            pass
    if _UNIX_EPOCH_MILLIS_RE.fullmatch(raw):
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc), "ok"
        except (ValueError, OverflowError, OSError):
            pass

    # 1) RFC 822/1123 (예: "Tue, 11 Aug 2026 09:00:00 GMT") - Tavily 뉴스 API
    #    문서에 나온 기본 형식이며 RSS/뉴스 API에서도 흔하다.
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is not None:
        if dt.tzinfo is not None:
            return dt, "ok"
        return dt.replace(tzinfo=SEOUL_TZ), "ok_assumed_seoul"

    # 2) ISO 8601 계열. "Z"는 UTC 오프셋으로 치환하고, 공백/"T" 구분자를 모두
    #    허용한다 (예: "2026-08-11T09:00:00+09:00", "2026-08-11 09:00:00Z",
    #    "2026-08-11T09:00:00", "2026-08-11 09:00:00").
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
    except ValueError:
        dt = None
    if dt is not None:
        if dt.tzinfo is not None:
            return dt, "ok"
        return dt.replace(tzinfo=SEOUL_TZ), "ok_assumed_seoul"

    # 3) 날짜만 있는 경우 (예: "2026-08-11", "2026.08.11", "2026/08/11")
    #    -> 시각 정보가 전혀 없으므로 Asia/Seoul 자정으로 간주.
    m = _ISO_LIKE_DATE_ONLY_RE.match(raw)
    if m:
        year, month, day = (int(part) for part in m.groups())
        try:
            return datetime(year, month, day, tzinfo=SEOUL_TZ), "ok_assumed_seoul"
        except ValueError:
            pass

    return None, "parse_failed"


def _within_target_day(dt: datetime | None, target_date: datetime) -> bool:
    if dt is None:
        return False
    dt_seoul = dt.astimezone(SEOUL_TZ)
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return day_start <= dt_seoul < day_end


def _normalize_article(raw: dict, category: str) -> dict:
    title = (raw.get("title") or "").strip()
    url = (raw.get("url") or "").strip()
    source = (raw.get("source") or "").strip() or _guess_source_from_url(url) or "출처 미상"
    published_raw = (raw.get("published_date") or raw.get("published_at") or "").strip()
    summary = (raw.get("content") or raw.get("summary") or "").strip()

    return {
        "title": title,
        "url": url,
        "source": source,
        "published_at_raw": published_raw,
        "summary": summary[:600],  # 프롬프트 토큰 낭비 방지를 위한 상한.
        "category": category,
    }


# ---------------------------------------------------------------------------
# 검색 API 호출부 (Tavily 기본, 다른 서비스로 교체 가능하도록 함수만 분리)
# ---------------------------------------------------------------------------

def _call_search_api(query: str, timeout: int) -> list[dict]:
    """
    실제 검색 API 호출부.

    기본 구현은 Tavily Search API(https://tavily.com)를 사용한다. Tavily는
    `topic="news"` + `days=N` 파라미터로 최근 N일 이내 뉴스만 필터링해 주는
    자체 날짜 필터를 제공하며, 이 결과를 다시 `_parse_published_at` +
    `_within_target_day`로 한 번 더 검증한다(이중 날짜 필터 - 명세 4번).

    다른 검색 API로 교체하려면 이 함수 내부만 수정하면 되고, 이후 파이프라인
    (정규화/날짜 필터/중복 제거/우선순위 정렬)은 그대로 재사용할 수 있다.
    SEARCH_API_URL 환경변수로 엔드포인트를, SEARCH_API_KEY(or TAVILY_API_KEY,
    NEWS_API_KEY)로 키를 설정한다.
    """
    api_key = (
        os.environ.get("TAVILY_API_KEY")
        or os.environ.get("SEARCH_API_KEY")
        or os.environ.get("NEWS_API_KEY")
    )
    if not api_key:
        raise NewsFetchError(
            "TAVILY_API_KEY(또는 SEARCH_API_KEY/NEWS_API_KEY) 환경변수가 설정되어 있지 "
            "않습니다. README.md의 '필수 설정' 항목을 확인하세요."
        )

    api_url = os.environ.get("SEARCH_API_URL", DEFAULT_TAVILY_URL)

    payload = {
        "api_key": api_key,
        "query": query,
        "topic": "news",
        # 목표일이 전일이므로 여유를 두고 최근 3일을 요청한 뒤, 파이썬 쪽에서
        # Asia/Seoul 기준 정확한 하루로 다시 필터링한다 (API 날짜 필터의
        # 시간대/경계 오차를 보정하기 위함).
        "days": 3,
        "max_results": RESULTS_PER_QUERY,
        "include_answer": False,
        "include_raw_content": False,
    }
    body = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        api_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise NewsFetchError(f"검색 API HTTP 오류 (query='{query}'): {exc.code} {error_body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise NewsFetchError(f"검색 API 호출 실패 (query='{query}'): {exc}") from exc

    # Tavily 응답 스키마: {"results": [{"title","url","content","published_date","score"}, ...]}
    # 다른 서비스 사용 시 이 부분만 맞춰 수정하면 된다.
    results = data.get("results") or data.get("articles") or []
    return [r for r in results if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 메인 엔트리
# ---------------------------------------------------------------------------

def fetch_news_for_date(target_date: datetime, timeout: int = 20) -> list[dict]:
    """
    target_date(Asia/Seoul, tz-aware) 하루치(00:00~23:59:59) 뉴스를,
    7개 대분류 x 대분류별 다수 검색어로 폭넓게 수집한 뒤 다음 순서로 처리하여
    반환한다.

        검색 후보 수집 → 날짜 필터(이중 검증) → 정규화 →
        중복 제거(URL → 제목) → 출처/카테고리 우선순위 정렬 → 카테고리별 상한 적용

    반환값은 list[dict] (각 dict는 title/url/source/published_at/summary/category)이며,
    최종 6~10건 "선정"은 이 함수가 아니라 AI(프롬프트)가 담당한다 — 이 함수는
    선정 가능한 "충분한 후보"를 확보하는 것이 목표다.

    모든 카테고리 검색이 전면 실패한 경우 NewsFetchError를 발생시킨다
    (명세: 자동화 실패 조건 - 뉴스 수집 실패).
    """
    if target_date.tzinfo is None:
        raise NewsFetchError("target_date는 timezone-aware 이어야 합니다 (naive datetime 금지).")

    raw_by_category: dict[str, list[dict]] = {cat: [] for cat in CATEGORY_QUERIES}
    total_queries = 0
    failed_queries: list[str] = []

    for category, queries in CATEGORY_QUERIES.items():
        for query in queries:
            total_queries += 1
            try:
                results = _call_search_api(query, timeout=timeout)
                raw_by_category[category].extend(
                    _normalize_article(r, category) for r in results
                )
            except NewsFetchError as exc:
                failed_queries.append(query)
                print(f"[news_fetcher] 경고: '{query}' 검색 실패 - {exc}")

    if failed_queries and len(failed_queries) == total_queries:
        raise NewsFetchError(
            f"모든 뉴스 검색어({total_queries}개)가 실패했습니다. 검색 API 설정을 확인하세요."
        )

    # ---- 디버그: 날짜 필터링 전, 실제 검색 결과의 published_date 원본 값을
    #      그대로 로그에 남긴다 (명세 7, 8번 - API Key 등 민감정보는 절대
    #      포함하지 않고, title/published_date만 노출한다). ----
    raw_total = sum(len(articles) for articles in raw_by_category.values())
    print(f"[news_fetcher] 검색결과: {raw_total}건 (날짜필터 전, 전체 카테고리 합산)")
    print("[news_fetcher] 날짜필터 전 상세 (제목 / published_date 원본값):")
    for category, articles in raw_by_category.items():
        for article in articles:
            raw_value = article["published_at_raw"] or "(없음)"
            print(f"[news_fetcher]   - [{category}] {article['title'][:60]!r} / {raw_value!r}")

    # ---- 날짜 필터 (Asia/Seoul 기준 전일 00:00~23:59:59, 이중 검증 중 파이썬 측) ----
    # 제외 이유를 명확히 구분해서 집계한다: published_date 없음 / 날짜 파싱 실패 /
    # 날짜 범위 밖 (명세 10번: 왜 0건인지 진단 가능하게).
    exclusion_reasons: dict[str, int] = {
        "published_date 없음": 0,
        "날짜 파싱 실패": 0,
        "날짜 범위 밖": 0,
    }
    kept_total = 0

    for category in raw_by_category:
        filtered = []
        for article in raw_by_category[category]:
            parsed_dt, reason = _parse_published_at(article["published_at_raw"])

            if reason == "no_raw":
                exclusion_reasons["published_date 없음"] += 1
                continue
            if reason == "parse_failed":
                exclusion_reasons["날짜 파싱 실패"] += 1
                print(
                    f"[news_fetcher]   날짜 파싱 실패: [{category}] {article['title'][:60]!r} "
                    f"/ published_date={article['published_at_raw']!r}"
                )
                continue

            # reason == "ok" 또는 "ok_assumed_seoul" (parsed_dt는 not None)
            if not _within_target_day(parsed_dt, target_date):
                exclusion_reasons["날짜 범위 밖"] += 1
                continue

            if reason == "ok_assumed_seoul":
                # timezone 정보가 없어 정책에 따라 Asia/Seoul로 간주했음을
                # 후보 데이터에도 남겨, AI/사람이 재검증할 때 참고할 수 있게 한다.
                article["published_at_tz_assumed"] = True

            article["published_at"] = parsed_dt.astimezone(SEOUL_TZ).strftime("%Y-%m-%d %H:%M")
            filtered.append(article)
            kept_total += 1
        raw_by_category[category] = filtered

    print(
        f"[news_fetcher] 날짜필터 후: 실제 전일({target_date.strftime('%Y-%m-%d')}) 뉴스 "
        f"{kept_total}건 (검색결과 {raw_total}건 중)"
    )
    if kept_total == 0 and raw_total > 0:
        print(
            "[news_fetcher] 경고: 날짜필터 후 0건입니다. 제외 이유별 집계:\n"
            + "\n".join(f"[news_fetcher]   - {reason}: {count}건" for reason, count in exclusion_reasons.items())
        )
    elif any(exclusion_reasons.values()):
        print(
            "[news_fetcher] 날짜필터 제외 이유별 집계: "
            + ", ".join(f"{reason} {count}건" for reason, count in exclusion_reasons.items())
        )

    # ---- 중복 제거: URL 우선, 그 다음 정규화된 제목 기준 ----
    seen_urls: set[str] = set()
    seen_title_keys: dict[str, dict] = {}

    for category in raw_by_category:
        deduped = []
        for article in raw_by_category[category]:
            if not article["title"]:
                continue

            if article["url"] and article["url"] in seen_urls:
                continue

            title_key = _normalize_title_key(article["title"])
            if not title_key:
                continue

            if title_key in seen_title_keys:
                # 동일 이슈를 여러 언론이 보도한 경우: 우선순위가 더 높은 출처를 대표로 남기고,
                # 서로 다른 출처명은 병기한다.
                existing = seen_title_keys[title_key]
                if existing["source"] != article["source"]:
                    combined_sources = {existing["source"], article["source"]}
                    existing["source"] = ", ".join(sorted(combined_sources)) + " 등 종합"
                if _source_priority(article["source"]) < _source_priority(existing["source"]):
                    existing["source"] = article["source"]
                continue

            seen_title_keys[title_key] = article
            if article["url"]:
                seen_urls.add(article["url"])
            deduped.append(article)

        raw_by_category[category] = deduped

    deduped_total = sum(len(articles) for articles in raw_by_category.values())
    print(f"[news_fetcher] 중복제거 후: {deduped_total}건 (날짜필터 통과 {kept_total}건 중)")

    if deduped_total < TARGET_MIN_POOL_SIZE:
        print(
            f"[news_fetcher] 참고: 중복제거 후 후보 풀이 {deduped_total}건으로 참고 기준치"
            f"({TARGET_MIN_POOL_SIZE}건)보다 적습니다. 실제 전일 뉴스가 적은 날일 수 있습니다."
        )
    elif deduped_total < TARGET_GOOD_POOL_SIZE:
        print(f"[news_fetcher] 참고: 중복제거 후 후보 풀 {deduped_total}건 (권장 {TARGET_GOOD_POOL_SIZE}건).")
    else:
        print(f"[news_fetcher] 참고: 중복제거 후 후보 풀 {deduped_total}건 (양호).")

    # ---- Python 우선순위 평가 + 카테고리별 상한 적용 (Claude 입력 축소, 요청 명세 1~2번) ----
    # 후보 풀 전체를 Claude에 넘기지 않고, 카테고리별로 _candidate_score 기준
    # 상위 CATEGORY_CANDIDATE_CAPS[category]건만 선별한다. 카테고리 캡의 합은
    # TOTAL_CANDIDATE_CAP(=30)을 넘지 않도록 설계되어 있으므로, 카테고리별로만
    # 캡을 적용하면 전체 총량도 자동으로 30건 이하가 된다.
    final_candidates: list[dict] = []
    for category in CATEGORY_PRIORITY_ORDER:
        articles = raw_by_category.get(category, [])
        cap = CATEGORY_CANDIDATE_CAPS.get(category, 0)
        articles.sort(key=lambda a: _candidate_score(a, category), reverse=True)
        selected = articles[:cap]
        final_candidates.extend(selected)
        if articles:
            print(
                f"[news_fetcher]   Python 후보 축소 - {category}: "
                f"{len(articles)}건 중 상위 {len(selected)}건 선정 (카테고리 캡 {cap}건)"
            )

    total = len(final_candidates)
    assert total <= TOTAL_CANDIDATE_CAP, (
        f"카테고리별 캡의 합({TOTAL_CANDIDATE_CAP})을 초과했습니다 - 캡 설정을 확인하세요 (실제 {total}건)."
    )
    print(f"[news_fetcher] Claude 전달 후보: {total}건 (카테고리 캡 합계 {TOTAL_CANDIDATE_CAP}건 이내)")

    for cat in CATEGORY_PRIORITY_ORDER:
        count = sum(1 for a in final_candidates if a["category"] == cat)
        print(f"[news_fetcher]   - {cat}: {count}건")

    return final_candidates
