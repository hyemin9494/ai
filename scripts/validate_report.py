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

MIN_LENGTH_CHARS = 1800  # (v10 프롬프트 개정에 맞춰 2500 → 1800으로 조정)
                          # 기존 2500자는 "뉴스가 2건 수준으로 부실했던 문제" 재발
                          # 방지용으로 설정됐었다. 그런데 프롬프트가 v10에서
                          # "사실에 없는 내용을 추가하지 않는 것이 분량보다 우선"
                          # "뉴스가 3건뿐이면 3건만 작성" 원칙을 명시적으로 채택하면서,
                          # 실제로 뉴스 3건짜리 정상적인 저밀도 보고서(모든 섹션을
                          # 충실히 작성했음에도)가 약 2,000~2,100자 수준으로 나오는
                          # 사례가 실측되었다(2026-08 확인, 2,063자). 2500자를 그대로
                          # 두면 "질을 위해 억지로 채우지 않는" 정상적인 보고서가
                          # 부당하게 검증 실패 처리된다.
                          # 1800자는 위 정상 사례(2,063자)에는 여유 있게 통과하면서도,
                          # 원래 막고자 했던 부실 보고서(비-뉴스 섹션까지 성의 없이
                          # 한두 문장으로 때운 경우, 실측 약 650자)는 여전히 확실히
                          # 걸러낸다. A4 2~3페이지 분량의 "정상적인" 날은 이 값과
                          # 무관하게 어차피 훨씬 길게 나온다 — 이 하한선은 오직
                          # "뉴스가 정말 적은 날에도 최소한의 성실한 분석은
                          # 있어야 한다"는 마지노선 역할만 한다.

REQUIRED_SECTION_PATTERNS = [
    ("제목", re.compile(r"^#\s*저축은행\s*Daily Morning Brief", re.MULTILINE)),
    ("기준일", re.compile(r"\*\*기준일\s*:\s*(\d{4}-\d{2}-\d{2})\*\*")),
    # 아래 6개 소제목은 "##" 다음에 Gemini가 종종 붙이는 번호(예: "4.", "IV)")나
    # 콜론을 허용하도록 관대하게 매칭한다. prompts/daily_morning_brief.txt에서
    # 번호를 붙이지 말라고 명시했지만, 모델이 그래도 번호를 붙이는 경우
    # 필수 섹션이 실제로는 존재함에도 "누락"으로 오판되는 것을 막기 위함이다
    # (문구 자체가 완전히 달라지는 경우까지 관대하게 허용하지는 않는다 -
    # 핵심 키워드는 정확히 일치해야 한다).
    ("Executive Summary", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?Executive Summary", re.MULTILINE | re.IGNORECASE)),
    ("주요 뉴스", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?주요\s*뉴스", re.MULTILINE)),
    ("종합 분석", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?종합\s*분석", re.MULTILINE)),
    ("Executive Check Point", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?Executive Check\s*Point", re.MULTILINE | re.IGNORECASE)),
    ("리스크관리 체크포인트", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?리스크\s*관리\s*체크\s*포인트", re.MULTILINE)),
    ("핵심 결론", re.compile(r"^##\s*(?:[\dIVX]+[.\)]\s*)?핵심\s*결론", re.MULTILINE)),
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
