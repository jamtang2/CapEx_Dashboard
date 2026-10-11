"""
투자금액 단위 검증 & 보정 (M5.5 — merger 이후, builder 이전)

문제: 공시마다 투자금액 표기 단위(원 / 천원 / 백만원)가 제각각이라
extractor의 단위 추정이 틀리면 금액이 1,000배 크거나 작게 저장된다.
(예: 제이에스링크 248.7억 = 24,866,512천원 → 백만원으로 오인 → 24.9조)

검증 근거:
  1) 공시 원문의 '자기자본대비(%)' (ratio_to_equity_pct) — 단위와 무관한 값
  2) DART fnlttSinglAcnt의 자본총계 (equity_mn, 백만원)
  → invest / equity × 100 이 공시 비율과 맞는 배율(1, 1/1000, 1000)을 선택.
  공시 비율이 없으면, 신규시설투자 공시는 통상 자기자본의 수 % 이상이므로
  자기자본 대비 비율이 비정상(>3,000% 또는 <0.05%)일 때 1,000배 보정.

capex.json 전 항목을 매 실행마다 검증한다(멱등). 자본총계는 항목별로
한 번만 조회해 equity_mn 에 캐시한다.

Usage:
    python -m src.amount_check                # DART 자본총계 조회 포함
    python -m src.amount_check --offline      # API 없이 공시 비율·매출 기준으로만 검증
"""

import os
import json
import math
import time
import logging

from .financials import fetch_financial, parse_amount

CALL_SLEEP = 0.3
FACTORS = [1, 1 / 1000, 1000]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 자본총계 조회
# ---------------------------------------------------------------------------

def extract_equity(records: list[dict]) -> int | None:
    """fnlttSinglAcnt 레코드에서 자본총계(원). CFS 우선 → OFS."""
    for fs_div in ("CFS", "OFS"):
        for r in records:
            if r.get("fs_div") == fs_div and r.get("account_nm", "").replace(" ", "") == "자본총계":
                amt = parse_amount(r.get("thstrm_amount"))
                if amt and amt > 0:
                    return amt
    return None


def fetch_equity_mn(api_key: str, corp_code: str, year: int) -> int | None:
    for y in (year, year - 1):
        try:
            records = fetch_financial(api_key, corp_code, y)
        except Exception as e:
            logger.warning(f"  자본총계 조회 오류 {corp_code}/{y}: {e}")
            records = []
        finally:
            time.sleep(CALL_SLEEP)
        eq = extract_equity(records)
        if eq:
            return round(eq / 1_000_000)
    return None


# ---------------------------------------------------------------------------
# 검증 로직
# ---------------------------------------------------------------------------

def _log_gap(a: float, b: float) -> float:
    return abs(math.log10(a / b))


def decide_factor(invest_mn: float, equity_mn: float | None,
                  equity_pct: float | None, revenue_mn: float | None) -> tuple[float, str, bool]:
    """
    반환: (적용 배율, 사유, suspect여부)
    배율 1 = 변경 없음.
    """
    if not invest_mn or invest_mn <= 0:
        return 1, "", False

    # 1) 자본총계 + 공시 자기자본대비(%) — 가장 신뢰도 높음
    if equity_mn and equity_pct and equity_pct > 0:
        gaps = {f: _log_gap(invest_mn * f / equity_mn * 100, equity_pct) for f in FACTORS}
        best = min(gaps, key=gaps.get)
        if best != 1 and gaps[best] < 0.7 and gaps[1] > 2:
            label = "천원→백만원" if best < 1 else "원 단위 과소→×1000"
            return best, f"{label} (공시 자기자본대비 {equity_pct}%와 대조)", False
        return 1, "", False

    # 2) 자본총계만 있는 경우 — 자기자본 대비 비율의 상식 범위로 판단
    if equity_mn:
        pct = invest_mn / equity_mn * 100
        if pct > 3000 and invest_mn / 1000 / equity_mn * 100 >= 0.5:
            return 1 / 1000, f"천원→백만원 (자기자본 대비 {pct:,.0f}% 비정상, 추정 보정)", False
        if pct < 0.05:
            return 1000, f"원 단위 과소→×1000 (자기자본 대비 {pct:.3f}% 비정상, 추정 보정)", False
        return 1, "", False

    # 3) 공시 비율만 있는 경우(오프라인) — 역산 자기자본이 매출의 수백 배면 보정
    if equity_pct and equity_pct > 0 and revenue_mn:
        implied_equity = invest_mn / (equity_pct / 100)
        if implied_equity / revenue_mn > 200:
            return 1 / 1000, (f"천원→백만원 (공시 자기자본대비 {equity_pct}% 기준 역산 자기자본이 "
                              f"매출의 {implied_equity / revenue_mn:,.0f}배)"), False
        return 1, "", False

    # 4) 근거 부족 — 매출 대비 50배 초과면 표시만
    if revenue_mn and invest_mn / revenue_mn > 50:
        return 1, "", True
    return 1, "", False


def validate_item(item: dict) -> bool:
    """항목 하나를 검증·보정. 변경 여부 반환."""
    invest = item.get("invest_amount_mn")
    # 정정공시로 금액이 새로 덮어써졌으면 이전 보정 기록은 무효
    if item.get("amount_fixed_value") is not None and item["amount_fixed_value"] != invest:
        for k in ("amount_unit_fixed", "amount_fixed_value", "invest_amount_mn_raw"):
            item.pop(k, None)
    factor, reason, suspect = decide_factor(
        invest,
        item.get("equity_mn"),
        item.get("ratio_to_equity_pct"),
        item.get("annual_revenue_mn"),
    )
    changed = False

    if factor != 1:
        item.setdefault("invest_amount_mn_raw", invest)
        new_invest = round(invest * factor)
        item["invest_amount_mn"] = new_invest
        item["amount_unit_fixed"] = reason
        item["amount_fixed_value"] = new_invest
        rev = item.get("annual_revenue_mn")
        item["ratio_to_revenue_pct"] = round(new_invest / rev * 100, 1) if rev else None
        logger.info(f"  보정 {item.get('corp_name')} {item.get('rcept_no')}: "
                    f"{invest:,} → {new_invest:,} 백만원 | {reason}")
        changed = True

    if bool(item.get("amount_suspect")) != suspect:
        item["amount_suspect"] = suspect
        changed = True
        if suspect:
            logger.warning(f"  금액 의심(근거 부족) {item.get('corp_name')} {item.get('rcept_no')}: "
                           f"{invest:,} 백만원")
    return changed


def run(items: list[dict], api_key: str | None) -> int:
    fixed = 0
    for item in items:
        if item.get("category") != "include":
            continue
        try:
            if api_key and not item.get("equity_checked") and item.get("corp_code"):
                year = item.get("revenue_year") or int(str(item.get("rcept_dt", "2000"))[:4]) - 1
                item["equity_mn"] = fetch_equity_mn(api_key, item["corp_code"], int(year))
                item["equity_checked"] = True
            if validate_item(item):
                fixed += 1
        except Exception as e:
            logger.error(f"  검증 오류 {item.get('corp_name')} {item.get('rcept_no')}: {e}")
    return fixed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="투자금액 단위 검증 & 보정")
    parser.add_argument("--capex", default="data/capex.json")
    parser.add_argument("--offline", action="store_true", help="DART 조회 없이 검증")
    args = parser.parse_args()

    api_key = None if args.offline else os.environ.get("DART_API_KEY")
    if not args.offline and not api_key:
        logger.warning("DART_API_KEY 없음 → 오프라인 검증만 수행")

    with open(args.capex, encoding="utf-8") as f:
        data = json.load(f)

    n = run(data["items"], api_key)
    logger.info(f"금액 검증 완료: {n}건 변경")

    with open(args.capex, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
