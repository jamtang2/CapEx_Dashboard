"""
M6 Builder — capex.json → calendar.json

include 항목의 end_date 기준 월별 그룹핑.

Usage:
    python -m src.builder
    python -m src.builder --input data/capex.json --output data/calendar.json
"""

import os
import json
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_calendar(items: list[dict]) -> dict:
    """include 항목을 end_date 기준 월별로 그룹핑."""
    calendar: dict[str, list] = {}
    for item in items:
        if item.get("category") != "include":
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

    parser = argparse.ArgumentParser(description="calendar.json 빌드 (M6)")
    parser.add_argument("--input",  default="data/capex.json")
    parser.add_argument("--output", default="data/calendar.json")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    calendar = build_calendar(data["items"])

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(calendar, f, ensure_ascii=False, indent=2)

    total_items = sum(len(v) for v in calendar.values())
    logger.info(f"calendar.json 저장: {len(calendar)}개월 / {total_items}건")
