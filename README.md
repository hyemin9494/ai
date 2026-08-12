# 저축은행 Daily Morning Brief Archive

저축은행 리스크관리부서 임원 보고용 **Daily Morning Brief**를 날짜별로 저장·조회하는
정적 아카이브 사이트입니다. 매일 오전 8시(Asia/Seoul)에 전일 뉴스를 기준으로
새 보고서를 자동 생성합니다.

이 프로젝트는 "날짜별 브리핑 아카이브 + 매일 자동 업데이트" 두 가지에만 집중합니다.
로그인, 댓글, 검색, 통계, 대시보드, 별도 DB 등은 의도적으로 포함하지 않았습니다.

---

## 1. 프로젝트 구조

```
/
├─ index.html                 # 메인 페이지 (연/월/날짜 목록)
├─ report.html                 # 보고서 상세 페이지 (?date=YYYY-MM-DD)
├─ css/style.css               # 스타일 (Navy/White/Gray, 내부 보고서 아카이브 톤)
├─ js/main.js                  # Vanilla JS (reports.json 로드 + 렌더링, 빌드 불필요)
├─ reports/YYYY/MM/YYYY-MM-DD.md   # 날짜별 보고서 원본 (Markdown)
├─ data/reports.json           # 사이트 날짜 목록 생성용 인덱스 (자동 갱신)
├─ prompts/daily_morning_brief.txt # 브리핑 작성 프롬프트 (자동화와 분리 관리)
├─ scripts/
│  ├─ ai_client.py             # Gemini API 호출 전용 모듈
│  ├─ news_fetcher.py          # 뉴스 수집 전용 모듈
│  ├─ generate_brief.py        # 생성 오케스트레이션 (뉴스 수집 → AI 호출 → 저장 → 검증)
│  ├─ validate_report.py       # 보고서 검증 (필수 섹션/날짜 일치/최소 길이)
│  └─ update_index.py          # reports/ 스캔 → data/reports.json 재생성
└─ .github/workflows/daily-brief.yml  # 매일 08:00 KST 자동 실행 워크플로우
```

정적 사이트이므로 별도 서버나 빌드 과정 없이 GitHub Pages에서 바로 서비스됩니다.
보고서 목록 렌더링과 Markdown → HTML 변환은 브라우저에서 수행합니다
(`report.html`이 `marked.js`를 CDN에서 로드하여 Markdown을 렌더링합니다).

---

## 2. 로컬 실행 방법

빌드 도구가 필요 없습니다. 정적 파일 서버만 있으면 됩니다.

```bash
# 프로젝트 루트에서
python3 -m http.server 8000
```

브라우저에서 `http://localhost:8000/index.html` 접속.

> `file://`로 직접 열면 `fetch()`가 `data/reports.json`과 `.md` 파일을
> 불러오지 못할 수 있습니다(브라우저 보안 정책). 반드시 로컬 서버를 통해 접속하세요.

### 자동화 스크립트 로컬 테스트

```bash
export GEMINI_API_KEY="AIza..."
export GEMINI_MODEL="gemini-3.6-flash"
export SEARCH_API_URL="https://your-search-api.example.com/search"
export SEARCH_API_KEY="..."

# 특정 날짜를 기준일로 강제 지정하여 테스트 (지정하지 않으면 "오늘-1일" 사용)
export TARGET_DATE="2026-08-12"

pip install -r requirements.txt
python3 scripts/generate_brief.py
python3 scripts/update_index.py
python3 scripts/validate_report.py reports/2026/08/2026-08-12.md
```

---

## 3. GitHub Pages 설정 방법

1. GitHub 저장소 **Settings → Pages**로 이동합니다.
2. **Source**를 `Deploy from a branch`로 설정합니다.
3. **Branch**를 `main` (또는 기본 브랜치), 폴더는 `/ (root)`로 선택합니다.
4. 저장 후 몇 분 내로 `https://<사용자명>.github.io/<저장소명>/` 에서 사이트가 열립니다.

이 프로젝트는 정적 HTML/CSS/JS만으로 구성되어 있어 별도 빌드 단계 없이
GitHub Pages가 바로 서비스할 수 있습니다.

---

## 4. GitHub Secrets 설정 방법 (필수)

저장소 **Settings → Secrets and variables → Actions**에서 아래 Secret을 등록합니다.

| 이름 | 필수 여부 | 설명 |
|---|---|---|
| `GEMINI_API_KEY` | 필수 | Google Gemini API 호출용 키 |
| `TAVILY_API_KEY` (또는 `SEARCH_API_KEY`/`NEWS_API_KEY`) | 필수 | 뉴스 검색 API 키 (기본값: Tavily) |

**Variables**(같은 화면의 "Variables" 탭, 비밀값이 아닌 설정값)에는 아래를 등록할 수 있습니다.

| 이름 | 필수 여부 | 설명 |
|---|---|---|
| `GEMINI_MODEL` | 선택 | 사용할 Gemini 모델명. 미설정 시 `scripts/ai_client.py`의 기본값(`gemini-3.6-flash`) 사용 |
| `SEARCH_API_URL` | 필수 | 실제 사용할 뉴스/검색 API의 엔드포인트 URL |

> **API Key는 절대 소스코드나 README에 직접 입력하지 않습니다.**
> 모든 코드는 `os.environ[...]`로 환경변수를 통해서만 접근합니다.

### ⚠️ 필수 설정 (구현 시 실제 서비스 선택 필요)

기본 구현은 **Tavily Search API**(`https://api.tavily.com/search`, `topic="news"`)를
사용합니다. `TAVILY_API_KEY`(또는 `SEARCH_API_KEY`/`NEWS_API_KEY`) Secret만
등록하면 별도 코드 수정 없이 바로 동작합니다.

다른 검색 API로 교체하고 싶다면 `scripts/news_fetcher.py`의
`_call_search_api()` 함수 하나만 수정하면 됩니다(이후 정규화/날짜 필터/
중복 제거/우선순위 정렬 파이프라인은 그대로 재사용됩니다). 배포 전:

1. Tavily를 그대로 쓸지, 다른 검색 API로 교체할지 결정합니다.
2. `SEARCH_API_URL`(선택, 기본값은 Tavily 엔드포인트), `TAVILY_API_KEY`(또는
   `SEARCH_API_KEY`)를 설정합니다.
3. 응답 스키마가 다르면 `_call_search_api()`의 반환값 파싱 부분만 수정합니다.

### 뉴스 수집 방식 (v2 — 밀도 개선)

과거 버전은 토픽 12개를 각각 1회씩만 검색하여 최종 뉴스가 2건 수준으로
지나치게 적었습니다. 현재 버전은 **7개 대분류(저축은행/국내금융정책/경제/
금리/환율/증시/국제) x 대분류별 다수의 세부 검색어(총 55개 검색어)**로
넓게 검색한 뒤, 다음 순서로 처리합니다.

```
검색 후보 수집 (검색어당 최대 6건, 총 최대 300여 건)
  → 날짜 필터 (Asia/Seoul 전일 00:00~23:59:59, Tavily의 days 파라미터 +
     파이썬 내부 필터 이중 검증. 날짜 확인 불가 기사는 제외)
  → 정규화 (제목/URL/출처/날짜/요약)
  → 중복 제거 (URL 기준 1차 → 정규화된 제목 기준 2차, 동일 이슈 다중 출처는 병기)
  → 출처 우선순위 정렬 (공식기관 > 주요 언론 > 기타)
  → 카테고리별 상한 적용 (카테고리당 최대 15건)
  → AI에게 카테고리별로 정리된 후보 전달 (목표 20건 이상, 권장 30~50건)
  → AI가 우선순위 기준에 따라 최종 6~10건 선정 및 심층 분석 작성
```

후보가 목표치(20건)에 못 미치면 로그에 경고를 남기지만 자동화를 실패
처리하지는 않습니다 — 실제로 뉴스가 적은 날일 수 있기 때문입니다. 이 경우
프롬프트가 AI에게 해당 분야를 "중요 신규 이슈 없음"으로 작성하도록
안내합니다. 뉴스를 지어내서 분량을 채우는 것은 프롬프트에서 명시적으로
금지되어 있습니다.

---

## 5. GitHub Actions 설명

워크플로우 파일: `.github/workflows/daily-brief.yml`

- **자동 실행**: 매일 UTC 23:00 (= Asia/Seoul 08:00) `schedule` 트리거로 실행됩니다.
  한국은 서머타임을 사용하지 않으므로 연중 동일한 UTC 오프셋(+9시간)을 사용합니다.
- **수동 실행**: `workflow_dispatch`로 Actions 탭에서 수동 실행할 수 있습니다.
  - `force_regenerate`: 이미 존재하는 날짜의 보고서를 강제로 재생성 (기존 파일은 `.bak`으로 백업)
  - `target_date`: 기준일을 직접 지정 (`YYYY-MM-DD`). 비워두면 "실행일 - 1일"을 자동 사용합니다.

### 실행 흐름

1. 저장소 checkout
2. Python 3.12 설치
3. 의존성 설치 (`pip install -r requirements.txt` — Gemini 공식 SDK `google-genai`)
4. 필수 Secret 존재 확인 (없으면 즉시 실패)
5. `scripts/generate_brief.py` 실행
   - 기준일 계산 (기본: 실행일 - 1일, Asia/Seoul)
   - 해당 날짜 보고서가 이미 있으면 **skip** (중복 생성 방지)
   - 전일 뉴스 수집 (`news_fetcher.py`)
   - Gemini API 호출하여 보고서 생성 (`ai_client.py` + `prompts/daily_morning_brief.txt`)
   - 생성 결과를 **검증 통과한 경우에만** `reports/YYYY/MM/YYYY-MM-DD.md`로 저장
6. `scripts/update_index.py` 실행 → `data/reports.json` 재생성
7. 변경사항이 있을 때만 commit & push
8. GitHub Pages가 자동으로 재배포

### 실패 처리

다음 상황에서는 워크플로우가 **실패 상태**로 종료되며, **커밋이 수행되지 않습니다**.
기존 보고서는 그대로 보존됩니다.

- API Key(Secret) 없음
- 뉴스 수집 전면 실패
- Gemini API 호출 실패 / 응답 없음
- 보고서 검증 실패 (필수 섹션 누락, 날짜 불일치, 빈 문서, 최소 길이 미달, 코드블록 혼입 등)

---

## 6. 브리핑 작성 프롬프트 위치

`prompts/daily_morning_brief.txt`

- 브리핑 작성 규칙(포함 분야, 출처 우선순위, 중요도 기준, 출력 형식 등)은
  전부 이 파일에 있습니다.
- 코드에는 프롬프트를 하드코딩하지 않았습니다. 이 파일만 수정하면 다음 실행부터
  바로 반영됩니다.

---

## 7. 시간 기준 원칙 (매우 중요)

- 웹사이트 표시, 보고서 기준일: **Asia/Seoul**
- GitHub Actions cron: **UTC** (23:00 UTC = 08:00 KST 다음날)
- "실행일 = 보고서 기준일 + 1일" — 예: 2026-08-11 08:00(KST) 실행 → 2026-08-10 뉴스 분석 →
  파일명 `2026-08-10.md`
- 모든 Python 코드는 `zoneinfo.ZoneInfo("Asia/Seoul")`를 사용한 **timezone-aware**
  datetime만 사용합니다. naive datetime은 사용하지 않습니다.

---

## 8. 문제 발생 시 점검 방법

1. **Actions 탭에서 실패한 워크플로우 로그를 확인합니다.**
   각 단계(환경변수 확인 / 생성 / 검증 / 커밋)가 어디서 실패했는지 로그에 명시됩니다.
2. **"필수 환경변수 확인" 단계에서 실패한 경우**: Secrets 설정을 다시 확인합니다
   (`GEMINI_API_KEY`, `SEARCH_API_KEY`).
3. **"Daily Morning Brief 생성" 단계에서 실패한 경우**:
   - 뉴스 수집 실패: `SEARCH_API_URL`이 올바른지, 검색 API가 정상 응답하는지 확인합니다.
   - AI 호출 실패: Gemini API 키/모델명/요금 한도(quota)를 확인합니다.
   - 보고서 검증 실패: 로그에 출력된 누락 섹션/날짜 불일치 등의 사유를 확인합니다.
4. **사이트에 새 보고서가 안 보이는 경우**:
   - `data/reports.json`이 최신 커밋에 반영되었는지 확인합니다.
   - 브라우저 캐시 문제일 수 있으니 강력 새로고침을 시도합니다.
   - GitHub Pages 배포가 완료되었는지 저장소의 "Deployments"에서 확인합니다.
5. **특정 날짜만 다시 생성하고 싶은 경우**: Actions 탭에서 `workflow_dispatch`를 수동
   실행하며 `target_date`와 `force_regenerate=true`를 입력합니다. 기존 파일은
   자동으로 `.bak`으로 백업된 뒤 덮어써집니다.

---

## 9. 설계 원칙 요약

- 날짜별 보고서(`reports/`)와 자동화 프롬프트(`prompts/`)는 완전히 분리되어 있습니다.
- 사이트 날짜 목록은 수동 입력하지 않고 `reports/` 폴더를 기준으로 `update_index.py`가
  자동 생성합니다.
- 자동화는 추가(additive) 방식으로만 동작하며, 기존 보고서를 삭제하거나 임의로
  덮어쓰지 않습니다.
- 검증에 실패한 보고서는 절대 커밋되지 않습니다.
- API Key는 코드/README 어디에도 노출되지 않으며, GitHub Secrets를 통해서만 전달됩니다.
