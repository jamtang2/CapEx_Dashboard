# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**CapEx Scanner Dashboard** — a static dashboard that automatically collects Korean listed companies' new facility investment (신규시설투자) disclosures from DART, filters for pure production-capacity investments, calculates revenue ratios, and renders a table + calendar view on GitHub Pages. Refreshes every Saturday via GitHub Actions cron.

The PRD is in `PRD/PRD_CapEx_Dashboard.pdf` (written in Korean). Read it for full functional requirements.

## Architecture

```
GitHub Actions (cron: 0 0 * * 6, = Sat 09:00 KST)
 │
 ├─ [M1] Collector  → DART list.json  →  keyword filter  →  candidate disclosures
 ├─ [M2] Extractor  → document.xml parsing  →  LLM fallback  →  amount/purpose/dates
 ├─ [M3] Classifier → rule-based keywords  →  LLM for ambiguous  →  include/exclude
 ├─ [M4] Financials → fnlttSinglAcnt API  →  annual revenue  →  ratio calculation
 ├─ [M5] Merger     → dedup key matching  →  update existing  →  revisions history
 ├─ [M6] Builder    → /data/capex.json (SSOT) + /data/calendar.json
 └─ [M7] Renderer   → index.html  →  git commit & push  →  Pages deploy
```

**Data flow**: All pipeline output accumulates in `/data/capex.json` as the single source of truth. `calendar.json` is derived from it. `index.html` is built from both and committed to the repo for Pages.

## Development Milestones (ordered)

| Milestone | Deliverable | Done When |
|-----------|-------------|-----------|
| M1 | `collector.py` — list.json pagination + keyword filter + 12-month backfill (split into ≤3-month windows) | Produces candidate JSON for past 12 months |
| M2 | `extractor.py` — document.xml → structured fields, LLM fallback | 10 sample disclosures extracted correctly |
| M3 | `classifier.py` — keyword rules + LLM for ambiguous cases | Research labs / dormitories correctly excluded |
| M4 | `financials.py` — fnlttSinglAcnt revenue lookup + ratio | Revenue ratio % calculated; 영업수익 fallback works |
| M5 | `merger.py` — dedup key logic verified on 5–10 real amendment samples | No duplicate rows; revisions[] appended correctly |
| M6 | `index.html` — table + calendar + KPI summary | Renders correctly on GitHub Pages |
| M7 | `.github/workflows/update.yml` — Saturday cron + workflow_dispatch | Unattended run commits & deploys |

## Key Implementation Rules

### Dedup / Amendment Matching (FR-6) — most critical logic
- **Primary key**: original `rcept_no` referenced in the amendment body
- **Fallback key**: `corp_code` + `이사회결의일` + normalized-purpose hash
- On match: overwrite fields, append to `revisions[]`, set `is_revised=true`
- On no match: add as new row with `orphan_revision=true`
- Verify key priority against 5–10 real amendment pairs before finalising (M5 task)

### Classification rules (FR-2)
- **Include keywords**: 생산라인, 증설, 신설(공장/라인), capacity, 생산능력, 제조설비, 양산설비, 신공장, 증축(공장)
- **Exclude keywords**: 연구소, R&D센터, 기숙사, 사택, 사옥, 본사, 오피스, 물류창고, 토지(단순), 지분취득, 출자, 노후교체
- Ambiguous → LLM returns `{include: bool, reason: str, confidence: float}`
- Call LLM only on ambiguous cases (cost control)

### Trading-halt exclusion (`src/trading_status.py`)
- Every M6 build run refreshes `is_trading_halted` on **all** items in `capex.json` (not just this week's new items), since a company's halt status can change independently of new disclosures.
- Detection: `FinanceDataReader`'s KRX snapshot (`fdr.StockListing("KRX")`) — a stock with `Volume == Open == High == Low == 0` and `Close > 0` had no trades that session (long-term delisting-review halt like 금양, or a same-day circuit-breaker halt).
- `index.html` filters out `is_trading_halted` items client-side before KPI/table/calendar rendering; the row itself is never deleted from `capex.json` so it reappears automatically once trading resumes.

### Revenue lookup (FR-4)
- Use `fnlttSinglAcnt.json`, `reprt_code=11011` (annual), prior fiscal year
- Prefer 연결(CFS); fall back to 별도(OFS); fall back to 영업수익 when 매출액 absent
- If prior year missing, use year before; record `revenue_year` in item
- Ratio = `invest_amount_mn / annual_revenue_mn * 100`, 1 decimal place; `N/A` when revenue is 0/missing

### DART API constraints
- 공시검색 (`list.json`): no `corp_code` → 3-month search window limit → split 12-month backfill into 4× 3-month calls
- Rate limits: add `sleep` between calls; cache `CORPCODE.xml` locally
- Secrets: `DART_API_KEY`, `ANTHROPIC_API_KEY` in GitHub Secrets

### Pipeline resilience
- Each disclosure processed in `try/except`; failures go to `error_log`, never abort the run
- Idempotent: re-running same week must not create duplicate rows (dedup key enforces this)

## Data Models

### `/data/capex.json` (SSOT)
```json
{
  "schema_version": "1.0",
  "last_run": "2026-06-29T00:00:00Z",
  "items": [{
    "id": "00126380__20260612000123",
    "corp_code": "00126380",
    "corp_name": "...",
    "stock_code": "012345",
    "rcept_no": "20260612000123",
    "rcept_dt": "2026-06-12",
    "invest_amount_mn": 35000,
    "annual_revenue_mn": 280000,
    "revenue_year": 2025,
    "ratio_to_revenue_pct": 12.5,
    "ratio_to_equity_pct": 18.3,
    "purpose": "○○ 생산라인 증설",
    "start_date": "2026-07-01",
    "end_date": "2026-12-31",
    "board_resolution_date": "2026-06-10",
    "category": "include",
    "classify_reason": "...",
    "classify_confidence": 0.96,
    "is_revised": false,
    "orphan_revision": false,
    "is_trading_halted": false,
    "revisions": [],
    "dart_url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260612000123",
    "last_updated": "2026-06-29T00:00:00Z"
  }]
}
```

### `/data/calendar.json` (derived)
```json
{ "2026-09": [{"corp_name": "...", "id": "...", "purpose": "..."}] }
```

## DART API Endpoints

| Purpose | Endpoint | Key Params |
|---------|----------|------------|
| 공시검색 | `https://opendart.fss.or.kr/api/list.json` | `crtfc_key`, `bgn_de`, `end_de`, `pblntf_ty=I`, `page_no`, `page_count=100`, `last_reprt_at=Y` |
| 공시 원문 | `https://opendart.fss.or.kr/api/document.xml` | `crtfc_key`, `rcept_no` (returns ZIP→XML) |
| 연매출(재무) | `https://opendart.fss.or.kr/api/fnlttSinglAcnt.json` | `crtfc_key`, `corp_code`, `bsns_year`, `reprt_code=11011` |
| 기업코드 | `https://opendart.fss.or.kr/api/corpCode.xml` | `crtfc_key` (returns ZIP) |
| 공시 뷰어 | `https://dart.fss.or.kr/dsaf001/main.do` | `rcpNo={rcept_no}` |

`reprt_code`: `11011`=사업보고서(연간), `pblntf_ty=I`=거래소공시

## Tech Stack

- **Pipeline**: Python (`requests`, `xmltodict`); optionally `OpenDartReader`
- **LLM**: Claude API (`ANTHROPIC_API_KEY`) — used for classification of ambiguous cases and extraction fallback
- **Scheduler**: GitHub Actions (`schedule` cron + `workflow_dispatch`)
- **Storage**: `/data/capex.json` committed in repo
- **Frontend**: Static HTML/CSS/JS (`index.html` committed to repo)
- **Hosting**: GitHub Pages

## UI Requirements

- Dark-themed data dashboard, desktop + mobile responsive
- Table: sticky header, sort arrows, heatmap on ratio column (higher = more intense highlight)
- Calendar: monthly grid, company badges per cell, hover tooltip showing purpose + amount
- KPI strip: total count, new this week, revised count, top ratio company
- Table controls: sort, search by company name, filter by ratio/amount range
- Each row links to DART original disclosure (new tab)
