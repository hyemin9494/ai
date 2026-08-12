"""
generate_brief.py

Daily Morning Brief 생성 오케스트레이션 스크립트.

흐름 (명세 12번 GitHub Actions 실행 흐름과 대응):
  1. 실행 시각(Asia/Seoul) 기준으로 보고서 기준일 계산 (= 실행일 - 1일, 명세 13번)
  2. 해당 날짜의 보고서가 이미 존재하면 기본적으로 skip (명세 19번)
  3. 전일 뉴스 수집 (news_fetcher.py)
  4. 뉴스가 있으면 Claude API 호출하여 보고서 생성 (ai_client.py + prompts/daily_morning_brief.txt)
  5. 생성된 보고서를 reports/YYYY/MM/YYYY-MM-DD.md 에 저장
  6. validate_report.py로 검증. 검증 실패 시 파일을 남기지 않고 비정상 종료
     (기존 보고서는 절대 덮어쓰지 않음, 빈 보고서를 생성하지 않음 - 명세 18번)

이 스크립트는 GitHub Actions에서 호출되지만, 로컬에서도 동일하게 동작한다.

환경변수:
  ANTHROPIC_API_KEY   (필수) - ai_client.py에서 사용
  ANTHROPIC_MODEL     (선택) - 기본값은 ai_client.py 참고
  SEARCH_API_URL       (필수) - news_fetcher.py에서 사용
  SEARCH_API_KEY / NEWS_API_KEY (필수) - news_fetcher.py에서 사용
  FORCE_REGENERATE    (선택, "true"/"false") - 이미 존재하는 보고서를 강제로 재생성할지 여부.
                       기본값 false. true인 경우 기존 파일을 백업(.bak) 후 덮어쓴다.
  TARGET_DATE          (선택, YYYY-MM-DD) - 수동 실행 시 기준일을 직접 지정하고 싶을 때 사용.
                       지정하지 않으면 "실행 시각 - 1일"을 사용한다 (명세 13번 기본 원칙).
"""

from __future__ import annotations

import os
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
    if not articles:
        return "(수집된 뉴스가 없습니다. 각 분야는 '중요 신규 이슈 없음'으로 작성하세요.)"

    lines = []
    for i, article in enumerate(articles, start=1):
        lines.append(
            f"{i}. 제목: {article['title']}\n"
            f"   출처: {article['source']}\n"
            f"   날짜: {article['published_at']}\n"
            f"   URL: {article['url']}\n"
            f"   요약: {article['summary']}"
        )
    return "\n".join(lines)


def build_user_prompt(target_date: datetime, articles: list[dict]) -> str:
    date_str = target_date.strftime("%Y-%m-%d")
    news_block = format_news_for_prompt(articles)
    return (
        f"오늘 작성해야 할 보고서의 기준일은 {date_str} 입니다 (Asia/Seoul, 전일 00:00~24:00 뉴스 기준).\n\n"
        f"아래는 수집된 전일 뉴스 목록입니다 (출처 우선순위 및 중복 제거가 이미 적용됨):\n\n"
        f"{news_block}\n\n"
        f"위 뉴스만 근거로 사용하여, 시스템 프롬프트의 작성 요구사항을 그대로 따르는 "
        f"Daily Morning Brief를 Markdown으로 작성하세요. "
        f"문서 최상단은 반드시 다음 두 줄로 시작해야 합니다:\n"
        f"{REPORT_TITLE_LINE}\n\n**기준일: {date_str}**"
    )


def generate_report_markdown(target_date: datetime, system_prompt: str) -> str:
    try:
        articles = news_fetcher.fetch_news_for_date(target_date)
    except news_fetcher.NewsFetchError as exc:
        raise GenerationFailure(f"뉴스 수집 실패: {exc}") from exc

    user_prompt = build_user_prompt(target_date, articles)

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
        raise GenerationFailure(
            "보고서 검증 실패로 파일을 저장하지 않습니다:\n" + "\n".join(f"  - {e}" for e in errors)
        )

    tmp_path.replace(path)
    print(f"[generate_brief] 보고서 저장 완료: {path}")


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
