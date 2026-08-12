"""
generate_brief.py

Daily Morning Brief 생성 오케스트레이션 스크립트.

흐름 (명세 12번 GitHub Actions 실행 흐름과 대응):
  1. 실행 시각(Asia/Seoul) 기준으로 보고서 기준일 계산 (= 실행일 - 1일, 명세 13번)
  2. 해당 날짜의 보고서가 이미 존재하면 기본적으로 skip (명세 19번)
  3. 전일 뉴스 수집 (news_fetcher.py)
  4. 뉴스가 있으면 Gemini API 호출하여 보고서 생성 (ai_client.py + prompts/daily_morning_brief.txt)
  5. 생성된 보고서를 reports/YYYY/MM/YYYY-MM-DD.md 에 저장
  6. validate_report.py로 검증. 검증 실패 시 파일을 남기지 않고 비정상 종료
     (기존 보고서는 절대 덮어쓰지 않음, 빈 보고서를 생성하지 않음 - 명세 18번)

이 스크립트는 GitHub Actions에서 호출되지만, 로컬에서도 동일하게 동작한다.

환경변수:
  GEMINI_API_KEY       (필수) - ai_client.py에서 사용 (Google Gemini API 키)
  GEMINI_MODEL         (선택) - 기본값은 ai_client.py 참고
  SEARCH_API_URL       (필수) - news_fetcher.py에서 사용
  SEARCH_API_KEY / NEWS_API_KEY (필수) - news_fetcher.py에서 사용
  FORCE_REGENERATE    (선택, "true"/"false") - 이미 존재하는 보고서를 강제로 재생성할지 여부.
                       기본값 false. true인 경우 기존 파일을 백업(.bak) 후 덮어쓴다.
  TARGET_DATE          (선택, YYYY-MM-DD) - 수동 실행 시 기준일을 직접 지정하고 싶을 때 사용.
                       지정하지 않으면 "실행 시각 - 1일"을 사용한다 (명세 13번 기본 원칙).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import ai_client  # noqa: E402
import news_fetcher  # noqa: E402
import validate_report  # noqa: E402

SEOUL_TZ = ZoneInfo("Asia/Seoul")
REPORT_TITLE_LINE = "# 저축은행 Daily Morning Brief"

# 검증 실패 진단 로그에서 본문 미리보기로 보여줄 최대 글자 수
# (ai_client.py의 DEBUG_PREVIEW_CHARS와 동일한 값을 사용해 일관성 유지).
DEBUG_PREVIEW_CHARS = 300


class GenerationFailure(RuntimeError):
    """생성 파이프라인 실패. 이 예외가 발생하면 자동화는 실패 상태로 종료되어야 한다."""


def find_repo_root() -> Path:
    script_dir = Path(__file__).parent.resolve()
    for candidate in [script_dir, *script_dir.parents]:
        if (candidate / "prompts" / "daily_morning_brief.txt").is_file():
            return candidate
    raise GenerationFailure("리포지토리 루트를 찾을 수 없습니다 (prompts/daily_morning_brief.txt 없음).")


def resolve_target_date() -> datetime:
    """
    보고서 기준일을 계산한다.
    - TARGET_DATE 환경변수가 있으면 그 날짜를 그대로 사용 (수동 실행용).
    - 없으면 "지금(Asia/Seoul) - 1일"의 날짜를 사용한다 (명세 13번: 실행일 = 기준일 + 1일).
    """
    override = os.environ.get("TARGET_DATE")
    if override:
        try:
            return datetime.strptime(override, "%Y-%m-%d").replace(tzinfo=SEOUL_TZ)
        except ValueError as exc:
            raise GenerationFailure(f"TARGET_DATE 형식이 올바르지 않습니다 (YYYY-MM-DD): {override}") from exc

    now_seoul = datetime.now(SEOUL_TZ)
    target = now_seoul - timedelta(days=1)
    return target.replace(hour=0, minute=0, second=0, microsecond=0)


def report_path_for(repo_root: Path, target_date: datetime) -> Path:
    year = target_date.strftime("%Y")
    month = target_date.strftime("%m")
    day_str = target_date.strftime("%Y-%m-%d")
    return repo_root / "reports" / year / month / f"{day_str}.md"


def load_prompt(repo_root: Path) -> str:
    prompt_path = repo_root / "prompts" / "daily_morning_brief.txt"
    if not prompt_path.is_file():
        raise GenerationFailure(f"프롬프트 파일을 찾을 수 없습니다: {prompt_path}")
    text = prompt_path.read_text(encoding="utf-8")
    if not text.strip():
        raise GenerationFailure("프롬프트 파일이 비어 있습니다.")
    return text


def format_news_for_prompt(articles: list[dict]) -> str:
    """
    카테고리별로 후보 뉴스를 그룹핑하여 정리한다. 각 카테고리 헤더에는
    "주요 뉴스 브리핑"의 어느 하위 소제목에 대응하는지 함께 표기하여,
    AI가 최종 보고서의 6개 하위 카테고리(저축은행 핵심 이슈/국내 금융정책/
    경제/금리·환율/증시/국제)에 곧바로 매핑할 수 있도록 한다.

    후보가 전혀 없는 카테고리는 "(해당 분야 수집된 후보 없음 → 중요 신규
    이슈 없음으로 작성)"이라고 명시하여, AI가 뉴스를 지어내지 않도록 한다.
    """
    if not articles:
        return "(수집된 뉴스 후보가 전혀 없습니다. 모든 분야를 '중요 신규 이슈 없음'으로 작성하세요.)"

    by_category: dict[str, list[dict]] = {cat: [] for cat in news_fetcher.CATEGORY_PRIORITY_ORDER}
    for article in articles:
        by_category.setdefault(article.get("category", "기타"), []).append(article)

    blocks = []
    for category in news_fetcher.CATEGORY_PRIORITY_ORDER:
        section_name = news_fetcher.CATEGORY_TO_REPORT_SECTION.get(category, category)
        cat_articles = by_category.get(category, [])
        header = f"[{category} — 보고서 소제목: '{section_name}']"

        if not cat_articles:
            blocks.append(f"{header}\n(해당 분야 수집된 후보 없음 → 중요 신규 이슈 없음으로 작성)")
            continue

        lines = [header]
        for i, article in enumerate(cat_articles, start=1):
            lines.append(
                f"  {i}. 제목: {article['title']}\n"
                f"     출처: {article['source']}\n"
                f"     날짜: {article['published_at']}\n"
                f"     URL: {article['url']}\n"
                f"     요약: {article['summary']}"
            )
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_user_prompt(target_date: datetime, articles: list[dict]) -> str:
    date_str = target_date.strftime("%Y-%m-%d")
    news_block = format_news_for_prompt(articles)
    total_count = len(articles)
    return (
        f"오늘 작성해야 할 보고서의 기준일은 {date_str} 입니다 (Asia/Seoul, 전일 00:00~24:00 뉴스 기준).\n\n"
        f"아래는 뉴스 수집 파이프라인(7개 분야 x 다수 검색어 → 날짜 필터 → 중복 제거 → "
        f"Python 우선순위 평가로 카테고리별 상위 후보만 선별)을 거쳐 확보한 총 {total_count}건의 "
        f"후보 뉴스입니다. 분야별로 정리되어 있습니다:\n\n"
        f"{news_block}\n\n"
        f"위 후보 목록 안에 있는 뉴스와 사실만 근거로 사용하세요. 후보에 없는 뉴스, 확인되지 않은 "
        f"수치나 발표를 만들어내지 마세요. 이 후보들 중에서 시스템 프롬프트의 선정 기준과 우선순위에 "
        f"따라 최종 6~10건(실제로 중요한 뉴스가 부족하면 그 미만도 가능)을 선정하고, 시스템 프롬프트의 "
        f"'5. 보고서 구성'에 정의된 형식과 소제목을 정확히 따라 Daily Morning Brief를 Markdown으로 "
        f"작성하세요. 문서 최상단은 반드시 다음 두 줄로 시작해야 합니다:\n"
        f"{REPORT_TITLE_LINE}\n\n**기준일: {date_str}**"
    )


def generate_report_markdown(target_date: datetime, system_prompt: str) -> str:
    try:
        articles = news_fetcher.fetch_news_for_date(target_date)
    except news_fetcher.NewsFetchError as exc:
        raise GenerationFailure(f"뉴스 수집 실패: {exc}") from exc

    # news_fetcher.py가 이미 Python 단계에서 카테고리별 캡(합계
    # news_fetcher.TOTAL_CANDIDATE_CAP건)으로 후보를 축소해서 반환하므로,
    # 여기서는 그 결과를 그대로 프롬프트에 사용한다(중복 축소 로직을 두지 않음).
    ai_model = os.environ.get("GEMINI_MODEL", ai_client.DEFAULT_MODEL)
    print(f"[generate_brief] AI provider: {ai_client.AI_PROVIDER_NAME}")
    print(f"[generate_brief] AI model: {ai_model}")
    print(f"[generate_brief] AI 후보 뉴스: {len(articles)}건")

    user_prompt = build_user_prompt(target_date, articles)

    # ---- Gemini 호출 전 입력 크기 로깅 (API Key 등 민감정보는 절대 출력하지 않음) ----
    system_chars = len(system_prompt)
    user_chars = len(user_prompt)
    total_chars = system_chars + user_chars
    print(
        f"[generate_brief] 입력 문자 수: system={system_chars:,} / user={user_chars:,} "
        f"/ 합계={total_chars:,}"
    )

    # 후보를 최대 news_fetcher.TOTAL_CANDIDATE_CAP(=30)건으로 이미 줄였고,
    # 뉴스별 작성 분량도 프롬프트(daily_morning_brief.txt) 쪽에서 간결하게
    # 조정했지만, 실제 운영 로그에서 입력이 30건 기준 약 28,000토큰까지
    # 커지는 것이 확인되었고, gemini-3.6-flash가 기본적으로 사용하는 "thinking"
    # 토큰이 max_output_tokens 예산을 실제 출력 텍스트와 합산해서 소비하는
    # 문제로 보고서 뒷부분(종합 분석 이후 4개 섹션)이 잘리는 현상이 있었다.
    # ai_client.py의 기본값을 8000으로 올리고 thinking_level도 LOW로
    # 낮췄으므로, 여기 기본값도 그와 일치시킨다. ai_client.py 자체는 건드리지
    # 않고, 그 모듈이 읽는 환경변수만 여기서 지정한다(이미 외부에서
    # GEMINI_MAX_TOKENS가 지정된 경우는 그 값을 그대로 존중한다).
    os.environ.setdefault("GEMINI_MAX_TOKENS", "8000")

    print("[generate_brief] 최종 보고서 생성 시작")
    try:
        markdown_text = ai_client.generate_text(system_prompt, user_prompt)
    except ai_client.AIClientError as exc:
        raise GenerationFailure(f"AI 보고서 생성 실패: {exc}") from exc

    return markdown_text


def write_report_file(path: Path, content: str, force_regenerate: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        if not force_regenerate:
            raise GenerationFailure(
                f"이미 보고서가 존재합니다 (skip 대상, FORCE_REGENERATE=true로 강제 재생성 가능): {path}"
            )
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup_path)
        print(f"[generate_brief] 기존 파일 백업 생성: {backup_path}")

    # 검증 전에는 최종 경로에 바로 쓰지 않고, 임시 파일에 먼저 쓴 뒤 검증 통과 시에만 옮긴다.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")

    # 파일명(YYYY-MM-DD)을 기대 날짜로 사용하여 본문 검증 (validate_report.py 재사용).
    errors = validate_report.validate_report_text(content, path.stem)

    if errors:
        tmp_path.unlink(missing_ok=True)
        _log_validation_failure_debug_info(content, errors)
        raise GenerationFailure(
            "보고서 검증 실패로 파일을 저장하지 않습니다:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    tmp_path.replace(path)
    print(f"[generate_brief] 보고서 저장 완료: {path}")


def _log_validation_failure_debug_info(content: str, errors: list[str]) -> None:
    """
    보고서 검증 실패 시(특히 "필수 섹션 누락") 원인을 진단할 수 있도록,
    Gemini API 호출 자체는 성공했더라도 실제로 어떤 형식의 텍스트가
    돌아왔는지 로그에 남긴다. API Key나 개인정보는 포함되지 않는다(보고서
    본문은 뉴스 요약/분석 텍스트일 뿐이다).
    """
    section_errors = [e for e in errors if "필수 섹션 누락" in e]
    if not section_errors:
        return

    print("[generate_brief] 검증 실패 진단: 필수 섹션 누락이 감지되었습니다.")
    print(f"[generate_brief]   - 감지된 문제: {', '.join(section_errors)}")

    # 실제로 어떤 최상위(##) 소제목이 본문에 존재하는지 스캔해서, "아예 안 썼는지"
    # 와 "썼는데 문구/형식이 달라서 정규식이 못 잡았는지"를 구분할 수 있게 한다.
    found_headings = re.findall(r"^##(?!#)\s*(.+)$", content, flags=re.MULTILINE)
    print(f"[generate_brief]   - 본문에서 실제로 발견된 '##' 소제목 목록: {found_headings}")

    length = len(content)
    print(f"[generate_brief]   - 본문 전체 길이: {length:,}자")
    tail_preview = content[-DEBUG_PREVIEW_CHARS:] if length > DEBUG_PREVIEW_CHARS else content
    print(
        f"[generate_brief]   - 본문 마지막 {min(length, DEBUG_PREVIEW_CHARS)}자 미리보기 "
        f"(여기서 그대로 잘렸는지 확인):\n{tail_preview}"
    )


def main() -> int:
    try:
        repo_root = find_repo_root()
        target_date = resolve_target_date()
        target_path = report_path_for(repo_root, target_date)
        force_regenerate = os.environ.get("FORCE_REGENERATE", "false").strip().lower() == "true"

        if target_path.exists() and not force_regenerate:
            print(f"[generate_brief] {target_path} 가 이미 존재하여 생성을 건너뜁니다 (중복 생성 방지).")
            return 0

        system_prompt = load_prompt(repo_root)
        markdown_text = generate_report_markdown(target_date, system_prompt)
        write_report_file(target_path, markdown_text, force_regenerate)
        return 0

    except GenerationFailure as exc:
        print(f"[generate_brief] 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
