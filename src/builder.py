"""
M6 Builder — capex.json 거래정지 상태 갱신 + calendar.json 빌드

매 실행마다 capex.json 전 항목의 거래정지(is_trading_halted) 여부를
최신 시세 기준으로 갱신한 뒤, include 항목을 end_date 기준 월별로 그룹핑한다.

Usage:
    python -m src.builder
    python -m src.builder --capex data/capex.json --output data/calendar.json
"""

import os
import json
import logging

from .trading_status import fetch_halted_stock_codes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def apply_trading_halt_status(items: list[dict], halted_codes: set[str]) -> int:
    """stock_code 기준 거래정지 여부를 각 항목에 갱신. 변경된 건수 반환."""
    changed = 0
    for item in items:
        raw_code = item.get("stock_code")
        stock_code = str(raw_code).zfill(6) if raw_code else ""
        is_halted = bool(stock_code) and stock_code in halted_codes
        if item.get("is_trading_halted") != is_halted:
            changed += 1
        item["is_trading_halted"] = is_halted
    return changed


def build_calendar(items: list[dict]) -> dict:
    """include 항목(거래정지 제외)을 end_date 기준 월별로 그룹핑."""
    calendar: dict[str, list] = {}
    for item in items:
        if item.get("category") != "include" or item.get("is_trading_halted"):
            continue
        end_date = item.get("end_date") or ""
        if len(end_date) < 7:
            continue
        month_key = end_date[:7]
        calendar.setdefault(month_key, []).append({
            "corp_name":            item.get("corp_name"),
            "id":                   item.get("id"),
            "purpose":              item.get("purpose"),
            "invest_amount_mn":     item.get("invest_amount_mn"),
            "ratio_to_revenue_pct": item.get("ratio_to_revenue_pct"),
            "dart_url":             item.get("dart_url"),
        })
    return calendar


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="거래정지 상태 갱신 & calendar.json 빌드 (M6)")
    parser.add_argument("--capex",  default="data/capex.json")
    parser.add_argument("--output", default="data/calendar.json")
    args = parser.parse_args()

    with open(args.capex, encoding="utf-8") as f:
        data = json.load(f)

    logger.info("거래정지(추정) 종목 조회 중 (FinanceDataReader)…")
    halted_codes = fetch_halted_stock_codes()
    logger.info(f"거래정지(추정) 종목 {len(halted_codes)}개 확인")

    changed = apply_trading_halt_status(data["items"], halted_codes)
    logger.info(f"거래정지 상태 갱신: {changed}건 변경")

    with open(args.capex, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    calendar = build_calendar(data["items"])

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(calendar, f, ensure_ascii=False, indent=2)

    total_items = sum(len(v) for v in calendar.values())
    logger.info(f"calendar.json 저장: {len(calendar)}개월 / {total_items}건")
