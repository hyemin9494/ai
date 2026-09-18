/**
 * 저축은행 Daily Morning Brief Archive
 * Vanilla JS. No build step. No framework.
 *
 * Data source: data/reports.json
 *   [{ "date": "YYYY-MM-DD", "path": "reports/YYYY/MM/YYYY-MM-DD.md", "title": "..." }, ...]
 *   Must be sorted newest-first by the automation (update_index.py), but this
 *   file also defensively re-sorts on the client so a malformed json can't
 *   break "최신 날짜가 상단" requirement.
 */
(function () {
  "use strict";

  var REPORTS_JSON_PATH = "data/reports.json";

  function qs(name) {
    var params = new URLSearchParams(window.location.search);
    return params.get(name);
  }

  function setStatus(el, message, isError) {
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("is-error", !!isError);
  }

  function fetchReports() {
    return fetch(REPORTS_JSON_PATH, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("reports.json 요청 실패 (HTTP " + res.status + ")");
        }
        return res.json();
      })
      .then(function (list) {
        if (!Array.isArray(list)) {
          throw new Error("reports.json 형식이 올바르지 않습니다.");
        }
        // Deduplicate by date (defensive) and sort descending by date.
        var seen = {};
        var deduped = [];
        list.forEach(function (item) {
          if (item && item.date && !seen[item.date]) {
            seen[item.date] = true;
            deduped.push(item);
          }
        });
        deduped.sort(function (a, b) {
          return a.date < b.date ? 1 : a.date > b.date ? -1 : 0;
        });
        return deduped;
      });
  }

  function formatDateLabel(dateStr) {
    // "2026-08-11" -> "08월 11일"
    var parts = dateStr.split("-");
    if (parts.length !== 3) return dateStr;
    return parts[1] + "월 " + parts[2] + "일";
  }

  function formatYearMonth(dateStr) {
    var parts = dateStr.split("-");
    return { year: parts[0], month: parts[1] };
  }

  /* -------------------- Index page -------------------- */

  function renderIndexPage() {
    var root = document.getElementById("archive-root");
    var statusEl = document.getElementById("status-message");

    fetchReports()
      .then(function (reports) {
        if (reports.length === 0) {
          setStatus(statusEl, "아직 등록된 보고서가 없습니다.", false);
          return;
        }

        // Group by year -> month, preserving descending order.
        var years = [];
        var yearMap = {};

        reports.forEach(function (item, index) {
          var ym = formatYearMonth(item.date);
          if (!yearMap[ym.year]) {
            yearMap[ym.year] = { year: ym.year, months: [], monthMap: {} };
            years.push(yearMap[ym.year]);
          }
          var yearEntry = yearMap[ym.year];
          if (!yearEntry.monthMap[ym.month]) {
            yearEntry.monthMap[ym.month] = { month: ym.month, dates: [] };
            yearEntry.months.push(yearEntry.monthMap[ym.month]);
          }
          yearEntry.monthMap[ym.month].dates.push({
            date: item.date,
            isLatest: index === 0
          });
        });

        var frag = document.createDocumentFragment();

        years.forEach(function (yearEntry) {
          var yearSection = document.createElement("section");
          yearSection.className = "year-group";

          var yearHeading = document.createElement("h2");
          yearHeading.className = "year-heading";
          yearHeading.textContent = yearEntry.year + "년";
          yearSection.appendChild(yearHeading);

          yearEntry.months.forEach(function (monthEntry) {
            var monthDiv = document.createElement("div");
            monthDiv.className = "month-group";

            var monthHeading = document.createElement("h3");
            monthHeading.className = "month-heading";
            monthHeading.textContent = parseInt(monthEntry.month, 10) + "월";
            monthDiv.appendChild(monthHeading);

            var list = document.createElement("ul");
            list.className = "date-list";

            monthEntry.dates.forEach(function (d) {
              var li = document.createElement("li");
              li.className = "date-item" + (d.isLatest ? " date-item--latest" : "");

              var a = document.createElement("a");
              a.href = "report.html?date=" + encodeURIComponent(d.date);

              var label = document.createElement("span");
              label.textContent = formatDateLabel(d.date);
              a.appendChild(label);

              if (d.isLatest) {
                var badge = document.createElement("span");
                badge.className = "latest-badge";
                badge.textContent = "최신";
                a.appendChild(badge);
              }

              li.appendChild(a);
              list.appendChild(li);
            });

            monthDiv.appendChild(list);
            yearSection.appendChild(monthDiv);
          });

          frag.appendChild(yearSection);
        });

        statusEl.remove();
        root.appendChild(frag);
      })
      .catch(function (err) {
        setStatus(statusEl, "보고서 목록을 불러오지 못했습니다: " + err.message, true);
      });
  }

  /* -------------------- Report detail page -------------------- */

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // 이 보고서는 임원 보고용 금융 문서이며, 취소선(GFM의 ~~텍스트~~ → <del>)이
  // 의도적으로 쓰일 일이 없다.
  //
  // [중요] 처음에는 "이중 물결(~~)만 위험하고 단일 물결(~)은 범위 표시라
  // 안전하다"고 판단해 marked의 renderer.del만 재정의했었다. 그런데 실제
  // marked.js 동작을 다시 검증한 결과, 이 라이브러리는 물결표 1개만으로도
  // 취소선 구분자로 인식하며, 문서 안에 물결표가 있는 범위 표현이 2번 이상
  // 나오면(예: "4~7건"과 "1~2문장"이 같은 문단에 함께 있는 경우) 서로 다른
  // 두 표현의 물결표끼리 짝지어져 그 "사이"에 있는 모든 텍스트가 사라지는
  // 심각한 문제가 있음을 재현으로 확인했다. renderer.del 재정의만으로는
  // 이 손실을 막을 수 없다(물결표가 이미 구분자로 소비된 뒤이기 때문).
  //
  // 그래서 근본적인 해결책으로, marked에 넘기기 "직전"에 원문 텍스트의
  // 물결표를 전부 백슬래시로 이스케이프(~  ->  \~)해서 marked의 인라인
  // 토크나이저가 물결표를 아예 구분자로 인식하지 못하게 만든다. 백슬래시
  // 이스케이프는 CommonMark 표준 문법이며, 렌더링 결과에서는 원래의 "~"
  // 문자 그대로 보이고 텍스트 손실도 없다(실제 재현 검증 완료). 이 프로젝트의
  // 출력 형식 규칙상 AI가 코드블록을 쓰지 않으므로(물결표 코드펜스 ~~~ 같은
  // 다른 용도로 쓰일 일이 없음), 물결표를 전부 이스케이프해도 안전하다.
  //
  // renderer.del 재정의는 혹시 모를 예외 상황에 대비해 그대로 남겨둔다
  // (물결표 이스케이프 후에는 실행될 일이 없지만, 있어도 해가 없다).
  function escapeTildesForMarked(mdText) {
    return mdText.replace(/~/g, "\\~");
  }

  var markedDelRendererConfigured = false;
  function configureMarkedRendererOnce() {
    if (markedDelRendererConfigured) return;
    if (!window.marked || typeof window.marked.use !== "function") return;
    window.marked.use({
      renderer: {
        del: function (text) {
          return text; // <del> 태그로 감싸지 않고 텍스트만 그대로 반환
        }
      }
    });
    markedDelRendererConfigured = true;
  }

  function renderMarkdown(mdText) {
    if (window.marked && typeof window.marked.parse === "function") {
      configureMarkedRendererOnce();
      return window.marked.parse(escapeTildesForMarked(mdText), { headerIds: false, mangle: false });
    }
    // Fallback: no markdown library available (e.g. offline / CDN blocked).
    // Show as preformatted text so content is never lost.
    return "<pre class=\"report-fallback\">" + escapeHtml(mdText) + "</pre>";
  }

  function updateNavLink(idPrimary, idBottom, targetDate) {
    [idPrimary, idBottom].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      if (targetDate) {
        el.href = "report.html?date=" + encodeURIComponent(targetDate);
        el.removeAttribute("aria-disabled");
      } else {
        el.href = "#";
        el.setAttribute("aria-disabled", "true");
      }
    });
  }

  /**
   * 아래 두 함수는 렌더링된 DOM을 "재배치·재포장"만 할 뿐, 텍스트 노드의
   * 내용을 새로 만들거나 수정하지 않는다 (보고서 데이터/문구는 100% 그대로).
   * 목적은 PDF 스타일 디자인(강조 박스, 중요도 색상)을 순수 표시 계층에서
   * 구현하는 것이며, marked.js가 생성한 일반 HTML(h1~h4, p, ul, ol)만으로는
   * CSS만으로 표현할 수 없는 두 가지(① 여러 형제 요소를 하나의 박스로 감싸기,
   * ② 텍스트 중 "★" 부분만 색상 강조)를 보완한다.
   */

  // Executive Summary / 종합 분석 / 핵심 결론 섹션을, 텍스트 변경 없이
  // 강조 박스(div)로 감싼다. 다음 h2가 나올 때까지의 형제 요소들을 그대로
  // 옮겨 담을 뿐, 요소 자체나 텍스트를 새로 만들지 않는다.
  var HIGHLIGHT_PANEL_HEADINGS = {
    "Executive Summary": "report-highlight-panel",
    "종합 분석": "report-highlight-panel",
    "핵심 결론": "report-conclusion-panel"
  };

  function wrapHighlightSections(root) {
    var headings = root.querySelectorAll("h2");
    headings.forEach(function (h2) {
      var panelClass = HIGHLIGHT_PANEL_HEADINGS[h2.textContent.trim()];
      if (!panelClass) return;

      var toMove = [];
      var node = h2.nextElementSibling;
      while (node && node.tagName !== "H2") {
        toMove.push(node);
        node = node.nextElementSibling;
      }
      if (toMove.length === 0) return;

      var wrapper = document.createElement("div");
      wrapper.className = panelClass;
      toMove.forEach(function (el) {
        wrapper.appendChild(el); // 기존 요소를 그대로 이동 (복제/재작성 아님)
      });
      h2.insertAdjacentElement("afterend", wrapper);
    });
  }

  // "중요도: ★★★" 같은 기존 텍스트에서 "★" 연속 구간만 색상 span으로
  // 감싼다. 별표 개수나 문자 자체는 절대 바꾸지 않고, 이미 존재하는 값을
  // 시각적으로만 강조한다 (새로운 판단값을 만들지 않음).
  function colorizeStarRatings(root) {
    var items = root.querySelectorAll("li");
    items.forEach(function (li) {
      if (li.querySelector(".stars-1, .stars-2, .stars-3")) return;
      if (li.textContent.indexOf("★") === -1) return;
      li.innerHTML = li.innerHTML.replace(/★+/, function (stars) {
        var cls = stars.length >= 3 ? "stars-3" : stars.length === 2 ? "stars-2" : "stars-1";
        return '<span class="' + cls + '">' + stars + "</span>";
      });
    });
  }

  function enhanceReportPresentation(root) {
    wrapHighlightSections(root);
    colorizeStarRatings(root);
  }

  function renderReportPage() {
    var root = document.getElementById("report-root");
    var statusEl = document.getElementById("status-message");
    var requestedDate = qs("date");

    if (!requestedDate) {
      setStatus(statusEl, "조회할 날짜가 지정되지 않았습니다. 메인 화면에서 날짜를 선택해 주세요.", true);
      return;
    }

    fetchReports()
      .then(function (reports) {
        var index = -1;
        for (var i = 0; i < reports.length; i++) {
          if (reports[i].date === requestedDate) {
            index = i;
            break;
          }
        }

        if (index === -1) {
          setStatus(statusEl, "요청하신 날짜(" + requestedDate + ")의 보고서를 찾을 수 없습니다.", true);
          return;
        }

        var current = reports[index];
        // reports is sorted descending (newest first):
        // "다음 날짜" (more recent) is at index-1, "이전 날짜" (older) is at index+1.
        var nextDate = index > 0 ? reports[index - 1].date : null;
        var prevDate = index < reports.length - 1 ? reports[index + 1].date : null;

        updateNavLink("nav-prev", "nav-prev-bottom", prevDate);
        updateNavLink("nav-next", "nav-next-bottom", nextDate);

        return fetch(current.path, { cache: "no-store" }).then(function (res) {
          if (!res.ok) {
            throw new Error("보고서 파일 요청 실패 (HTTP " + res.status + ")");
          }
          return res.text();
        }).then(function (mdText) {
          document.title = requestedDate + " 저축은행 Daily Morning Brief | 저축은행 Daily Morning Brief";

          var wrapper = document.createElement("div");

          var dateTag = document.createElement("div");
          dateTag.className = "report-doc__date";
          dateTag.textContent = "기준일 " + requestedDate;

          var docDiv = document.createElement("div");
          docDiv.className = "report-doc";
          docDiv.appendChild(dateTag);

          var bodyDiv = document.createElement("div");
          bodyDiv.className = "report-body";
          bodyDiv.innerHTML = renderMarkdown(mdText);
          enhanceReportPresentation(bodyDiv); // 표시 전용 재포장 (데이터/텍스트 변경 없음)
          docDiv.appendChild(bodyDiv);

          wrapper.appendChild(docDiv);

          statusEl.remove();
          root.appendChild(wrapper);
        });
      })
      .catch(function (err) {
        setStatus(statusEl, "보고서를 불러오지 못했습니다: " + err.message, true);
      });
  }

  window.Archive = {
    renderIndexPage: renderIndexPage,
    renderReportPage: renderReportPage
  };
})();
