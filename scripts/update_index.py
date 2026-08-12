"""
update_index.py

reports/ 디렉터리를 스캔하여 data/reports.json을 재생성한다 (명세 6번, 21번).

원칙:
- 메인 페이지 날짜 목록은 수동 입력하지 않는다. reports/ 폴더가 유일한 진실 소스(source of truth)다.
- 최신 날짜가 배열의 맨 앞에 오도록 정렬한다.
- 중복 date를 만들지 않는다.
- 존재하지 않는 날짜(파일이 없는 날짜)는 목록에 포함하지 않는다.
- 기존 파일을 삭제하거나 손상시키지 않는다 (추가/갱신만 수행, 명세 25번).

사용 예:
    python update_index.py
    (repo 루트에서 실행한다고 가정. reports/ 및 data/reports.json 경로는
     REPO_ROOT 기준 상대 경로로 처리한다.)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPORT_FILENAME_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\.md$")
REPORT_TITLE = "저축은행 Daily Morning Brief"


def find_repo_root(start: Path) -> Path:
    """reports/ 및 data/ 디렉터리를 포함하는 상위 경로를 리포지토리 루트로 간주한다."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "reports").is_dir() and (candidate / "data").is_dir():
            return candidate
    # 못 찾으면 스크립트의 조부모 디렉터리(scripts/ 상위)를 기본값으로 사용.
    return start.resolve().parent.parent


def scan_reports(repo_root: Path) -> list[dict]:
    reports_dir = repo_root / "reports"
    entries: dict[str, dict] = {}

    if not reports_dir.is_dir():
        return []

    for md_path in reports_dir.glob("*/*/*.md"):
        match = REPORT_FILENAME_PATTERN.match(md_path.name)
        if not match:
            # 형식에 맞지 않는 파일은 무시한다 (명세 6번: 존재하지 않는/유효하지 않은 날짜 표시 금지).
            continue

        year, month, day = match.groups()
        date_str = f"{year}-{month}-{day}"

        # 폴더 구조(reports/YYYY/MM/)와 파일명의 연/월이 일치하는지 확인.
        expected_relative = Path(year) / month / md_path.name
        actual_relative = md_path.relative_to(reports_dir)
        if actual_relative != expected_relative:
            print(
                f"[update_index] 경고: 폴더 구조와 파일명이 불일치하여 건너뜁니다: {md_path}",
                file=sys.stderr,
            )
            continue

        # 중복 date 방지: 이미 등록된 날짜면 스킵 (동일 날짜 파일이 두 곳에 있을 수 없지만 방어적으로 처리).
        if date_str in entries:
            print(f"[update_index] 경고: 중복 날짜 발견, 첫 번째 파일을 유지합니다: {date_str}", file=sys.stderr)
            continue

        relative_path = md_path.relative_to(repo_root).as_posix()
        entries[date_str] = {
            "date": date_str,
            "path": relative_path,
            "title": REPORT_TITLE,
        }

    # 최신 날짜가 맨 앞에 오도록 내림차순 정렬.
    sorted_entries = sorted(entries.values(), key=lambda e: e["date"], reverse=True)
    return sorted_entries


def write_reports_json(repo_root: Path, entries: list[dict]) -> Path:
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "reports.json"

    output_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def main(argv: list[str]) -> int:
    script_dir = Path(__file__).parent
    repo_root = find_repo_root(script_dir)

    entries = scan_reports(repo_root)

    if not entries:
        print("[update_index] 경고: reports/ 디렉터리에서 유효한 보고서를 찾지 못했습니다.", file=sys.stderr)

    output_path = write_reports_json(repo_root, entries)
    print(f"[update_index] {output_path} 갱신 완료 ({len(entries)}개 보고서).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
