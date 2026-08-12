"""
validate_report.py

생성된 Daily Morning Brief(.md)를 검증한다 (명세 20번 항목).

검증 항목:
- 기준일 존재
- 제목 존재
- Executive Summary 존재
- 주요 뉴스 존재
- 종합 분석 존재
- Executive Check Point 존재
- 리스크관리 체크포인트 존재
- 핵심 결론 존재
- 보고서 기준일과 파일명 일치
- 빈 문서 아님
- 최소 문자 수 충족

검증 실패 시 자동화는 git commit을 수행하지 않는다 (명세 20번, 18번).
이 스크립트는 CLI로도, 모듈로도 사용 가능하다.

사용 예:
    python validate_report.py reports/2026/08/2026-08-11.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MIN_LENGTH_CHARS = 2500  # A4 2~3페이지 + 6개 뉴스 하위 카테고리 + 12개 리스크
                          # 체크포인트 항목을 모두 다루면 자연히 이보다 훨씬 길어진다.
                          # 뉴스가 2건 수준으로 부실했던 문제(주요 뉴스 밀도 개선
                          # 요청) 재발을 막기 위한 최소 하한선이며, 실제 뉴스가
                          # 정말 없는 극단적인 날에도 12개 리스크 체크포인트
                          # 항목과 6개 뉴스 하위 카테고리 표시("중요 신규 이슈
                          # 없음")만으로도 이 하한선은 충족 가능하다.

REQUIRED_SECTION_PATTERNS = [
    ("제목", re.compile(r"^#\s*저축은행\s*Daily Morning Brief", re.MULTILINE)),
    ("기준일", re.compile(r"\*\*기준일\s*:\s*(\d{4}-\d{2}-\d{2})\*\*")),
    ("Executive Summary", re.compile(r"^##\s*Executive Summary", re.MULTILINE)),
    ("주요 뉴스", re.compile(r"^##\s*주요 뉴스", re.MULTILINE)),
    ("종합 분석", re.compile(r"^##\s*종합 분석", re.MULTILINE)),
    ("Executive Check Point", re.compile(r"^##\s*Executive Check Point", re.MULTILINE)),
    ("리스크관리 체크포인트", re.compile(r"^##\s*리스크관리 체크포인트", re.MULTILINE)),
    ("핵심 결론", re.compile(r"^##\s*핵심 결론", re.MULTILINE)),
]

FILENAME_DATE_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


class ValidationError(RuntimeError):
    """보고서 검증 실패를 나타내는 예외. 메시지에 실패 사유 목록을 담는다."""


def validate_report_text(text: str, expected_date: str) -> list[str]:
    """
    보고서 텍스트를 검증하고, 실패 사유 목록을 반환한다.
    빈 리스트가 반환되면 검증 통과.
    """
    errors: list[str] = []

    if not text or not text.strip():
        errors.append("보고서 본문이 비어 있습니다.")
        return errors  # 빈 문서면 이후 검사는 의미가 없으므로 즉시 반환.

    if len(text.strip()) < MIN_LENGTH_CHARS:
        errors.append(
            f"보고서 최소 문자 수({MIN_LENGTH_CHARS}자)를 충족하지 못했습니다. "
            f"(현재 {len(text.strip())}자)"
        )

    for name, pattern in REQUIRED_SECTION_PATTERNS:
        if not pattern.search(text):
            errors.append(f"필수 섹션 누락: {name}")

    date_match = re.search(r"\*\*기준일\s*:\s*(\d{4}-\d{2}-\d{2})\*\*", text)
    if date_match:
        report_date = date_match.group(1)
        if report_date != expected_date:
            errors.append(
                f"보고서 기준일({report_date})과 파일명 날짜({expected_date})가 일치하지 않습니다."
            )
    # 기준일 자체가 없는 경우는 위 REQUIRED_SECTION_PATTERNS 검사에서 이미 잡힌다.

    # 명세 15번: AI 출력에는 마크다운 외 JSON, 코드블록이 섞이면 안 된다.
    if "```" in text:
        errors.append("보고서에 코드블록(```)이 포함되어 있어 형식 규칙을 위반합니다.")

    return errors


def validate_report_file(path: Path) -> list[str]:
    """
    파일 경로를 받아 검증한다. 파일명에서 기대 날짜를 추출하고,
    본문의 기준일과 대조한다.
    """
    filename_match = FILENAME_DATE_PATTERN.search(path.name)
    if not filename_match:
        return [f"파일명이 YYYY-MM-DD.md 형식이 아닙니다: {path.name}"]

    expected_date = filename_match.group(1)

    if not path.exists():
        return [f"파일이 존재하지 않습니다: {path}"]

    text = path.read_text(encoding="utf-8")
    return validate_report_text(text, expected_date)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("사용법: python validate_report.py <report.md 경로>", file=sys.stderr)
        return 2

    report_path = Path(argv[1])
    errors = validate_report_file(report_path)

    if errors:
        print(f"[검증 실패] {report_path}", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(f"[검증 통과] {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
