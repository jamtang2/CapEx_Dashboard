"""
M1 — DART 신규시설투자 공시 수집기

Usage:
    # 최초 실행 (12개월 backfill)
    python -m src.collector

    # 특정 기간 지정
    python -m src.collector --start 20250101 --end 20251231

Output: data/candidates.json
"""

import os
import json
import time
import logging
from datetime import date, timedelta, datetime

import requests
from dateutil.relativedelta import relativedelta

LIST_URL = "https://opendart.fss.or.kr/api/list.json"

KEYWORDS = ["신규시설투자", "유형자산 취득", "유형자산취득"]

PAGE_COUNT = 100
CHUNK_MONTHS = 3      # DART는 corp_code 미지정 시 3개월 초과 검색 불가
BACKFILL_MONTHS = 12
CALL_SLEEP = 0.5      # API 호출 간격 (초)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def split_windows(start: date, end: date) -> list[tuple[date, date]]:
    """start~end 구간을 CHUNK_MONTHS 단위로 분할."""
    windows = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + relativedelta(months=CHUNK_MONTHS) - timedelta(days=1), end)
        windows.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return windows


def determine_range(capex_path: str) -> tuple[date, date]:
    """capex.json last_run 기준으로 수집 기간 결정. 없으면 12개월 backfill."""
    today = date.today()
    if os.path.exists(capex_path):
        with open(capex_path, encoding="utf-8") as f:
            data = json.load(f)
        last_run_str = data.get("last_run", "")
        if last_run_str:
            last_run = datetime.fromisoformat(last_run_str.replace("Z", "+00:00")).date()
            logger.info(f"증분 수집: {last_run} ~ {today}")
            return last_run, today

    start = today - relativedelta(months=BACKFILL_MONTHS)
    logger.info(f"최초 backfill: {start} ~ {today} ({BACKFILL_MONTHS}개월)")
    return start, today


# ---------------------------------------------------------------------------
# DART API
# ---------------------------------------------------------------------------

def _fetch_page(api_key: str, bgn_de: str, end_de: str, page_no: int) -> dict:
    params = {
        "crtfc_key": api_key,
        "bgn_de": bgn_de,
        "end_de": end_de,
        "pblntf_ty": "I",
        "page_no": page_no,
        "page_count": PAGE_COUNT,
        "last_reprt_at": "Y",
    }
    resp = requests.get(LIST_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_window(api_key: str, bgn: date, end: date) -> list[dict]:
    """한 날짜 구간의 전체 페이지를 수집하고 키워드 필터를 적용."""
    bgn_str = bgn.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    results = []
    page_no = 1

    while True:
        try:
            data = _fetch_page(api_key, bgn_str, end_str, page_no)
        except requests.RequestException as e:
            logger.error(f"API 오류 (page {page_no}): {e}")
            break

        status = data.get("status")
        if status == "013":   # 조회된 데이터 없음
            break
        if status != "000":
            logger.warning(f"예상치 못한 status={status}: {data.get('message')}")
            break

        for item in data.get("list", []):
            report_nm = item.get("report_nm", "")
            if any(kw in report_nm for kw in KEYWORDS):
                results.append({
                    "rcept_no":   item.get("rcept_no"),
                    "corp_code":  item.get("corp_code"),
                    "corp_name":  item.get("corp_name"),
                    "stock_code": item.get("stock_code"),
                    "rcept_dt":   item.get("rcept_dt"),
                    "report_nm":  report_nm,
                    "market":     item.get("rm", ""),   # 유=KOSPI, 코=KOSDAQ, 넥=코넥스
                    "is_revised": "[기재정정]" in report_nm,
                })

        total_count = int(data.get("total_count", 0))
        if page_no * PAGE_COUNT >= total_count:
            break

        page_no += 1
        time.sleep(CALL_SLEEP)

    return results


# ---------------------------------------------------------------------------
# Main collect
# ---------------------------------------------------------------------------

def collect(api_key: str, start: date, end: date) -> list[dict]:
    """전체 기간을 CHUNK_MONTHS 단위로 나눠 수집. rcept_no 기준 중복 제거."""
    windows = split_windows(start, end)
    logger.info(f"{len(windows)}개 구간으로 분할하여 수집")

    seen: set[str] = set()
    candidates: list[dict] = []

    for bgn, fin in windows:
        logger.info(f"  구간: {bgn.strftime('%Y%m%d')} ~ {fin.strftime('%Y%m%d')}")
        try:
            items = fetch_window(api_key, bgn, fin)
            new = [i for i in items if i["rcept_no"] not in seen]
            for i in new:
                seen.add(i["rcept_no"])
            candidates.extend(new)
            logger.info(f"    → {len(new)}건 (누계 {len(candidates)}건)")
        except Exception as e:
            logger.error(f"    구간 실패: {e}")

        time.sleep(CALL_SLEEP)

    return candidates


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DART 신규시설투자 공시 수집 (M1)")
    parser.add_argument("--start", help="시작일 YYYYMMDD")
    parser.add_argument("--end",   help="종료일 YYYYMMDD (기본: 오늘)")
    parser.add_argument("--output",  default="data/candidates.json")
    parser.add_argument("--capex",   default="data/capex.json")
    args = parser.parse_args()

    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("환경변수 DART_API_KEY 가 설정되지 않았습니다.")

    today = date.today()

    if args.start:
        start = _parse_date(args.start)
    else:
        start, _ = determine_range(args.capex)

    end = _parse_date(args.end) if args.end else today

    logger.info(f"수집 기간: {start} ~ {end}")
    candidates = collect(api_key, start, end)
    logger.info(f"최종 수집: {len(candidates)}건")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "collected_at": today.isoformat(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "count": len(candidates),
        "items": candidates,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"저장 완료: {args.output}")
