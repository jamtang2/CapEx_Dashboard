"""
M4 — 연매출액 조회 & 투자 비율 계산

DART fnlttSinglAcnt API로 직전 사업연도 매출액을 조회하고
매출 대비 투자 비율(%)을 계산한다.

연결(CFS) 우선 → 없으면 별도(OFS)
매출액 우선 → 없으면 영업수익
직전연도 미제출 → 그 전년도 fallback

Usage:
    python -m src.financials
    python -m src.financials --input data/classified.json --output data/financials.json
"""

import os
import json
import time
import logging
from datetime import date, timedelta

import requests

FINANCIAL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
REPRT_CODE_ANNUAL = "11011"
CALL_SLEEP = 0.3
MAX_YEAR_FALLBACK = 2   # 최대 2년 전까지 fallback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DART 재무 API
# ---------------------------------------------------------------------------

def fetch_financial(api_key: str, corp_code: str, bsns_year: int) -> list[dict]:
    """단일 사업연도 재무데이터 조회. status != 000이면 빈 리스트."""
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(bsns_year),
        "reprt_code": REPRT_CODE_ANNUAL,
    }
    resp = requests.get(FINANCIAL_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "000":
        return []
    return data.get("list", [])


def parse_amount(raw: str | None) -> int | None:
    """쉼표 제거 후 정수 변환. 실패 시 None."""
    if not raw:
        return None
    try:
        return int(raw.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def extract_revenue(records: list[dict]) -> tuple[int | None, str, str]:
    """
    재무 레코드에서 매출액(또는 영업수익)을 추출.
    반환: (금액_원, 사용한_account_nm, fs_div)
    CFS 우선 → OFS 순서, 매출액 우선 → 영업수익 순서.
    """
    target_accounts = ["매출액", "영업수익"]
    fs_priority = ["CFS", "OFS"]

    for fs_div in fs_priority:
        fs_records = [r for r in records if r.get("fs_div") == fs_div]
        for account_nm in target_accounts:
            for r in fs_records:
                if r.get("account_nm") == account_nm:
                    amount = parse_amount(r.get("thstrm_amount"))
                    if amount and amount > 0:
                        return amount, account_nm, fs_div

    return None, "", ""


def extract_net_income(records: list[dict]) -> int | None:
    """
    재무 레코드에서 당기순이익을 추출. CFS 우선 → OFS.
    account_nm이 '당기순이익'으로 시작하는 항목을 사용.
    """
    for fs_div in ["CFS", "OFS"]:
        for r in records:
            if r.get("fs_div") == fs_div and r.get("account_nm", "").startswith("당기순이익"):
                return parse_amount(r.get("thstrm_amount"))
    return None


# ---------------------------------------------------------------------------
# 시가총액 (FinanceDataReader)
# ---------------------------------------------------------------------------

def _last_weekday() -> str:
    """가장 최근 평일을 YYYYMMDD 형식으로 반환 (주말 실행 대비)."""
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_marcap_dict() -> dict[str, int]:
    """FinanceDataReader로 KRX 전 종목 시가총액 배치 조회. {stock_code: 시가총액_원}"""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
        return {str(code): int(mc) for code, mc in zip(df["Code"], df["Marcap"]) if mc and mc > 0}
    except Exception as e:
        logger.warning(f"시가총액 배치 조회 오류: {e}")
        return {}


def calc_per(marcap: int | None, net_income_mn: int | None) -> float | None:
    """PER = 시가총액 / 당기순이익. 순이익 0 이하이면 None."""
    if not marcap or not net_income_mn or net_income_mn <= 0:
        return None
    return round(marcap / (net_income_mn * 1_000_000), 1)


# ---------------------------------------------------------------------------
# Per-item financials lookup
# ---------------------------------------------------------------------------

def get_revenue(api_key: str, corp_code: str, rcept_dt: str) -> dict:
    """
    공시일 기준 직전 사업연도부터 MAX_YEAR_FALLBACK년 전까지 순차 조회.
    반환: {annual_revenue_mn, revenue_year, revenue_account, revenue_fs_div, net_income_mn}
    """
    base_year = int(rcept_dt[:4]) - 1   # 직전 사업연도

    for offset in range(MAX_YEAR_FALLBACK + 1):
        year = base_year - offset
        try:
            records = fetch_financial(api_key, corp_code, year)
            amount, account_nm, fs_div = extract_revenue(records)
            if amount:
                net_income = extract_net_income(records)
                return {
                    "annual_revenue_mn": round(amount / 1_000_000),
                    "revenue_year": year,
                    "revenue_account": account_nm,
                    "revenue_fs_div": fs_div,
                    "net_income_mn": round(net_income / 1_000_000) if net_income is not None else None,
                }
        except Exception as e:
            logger.warning(f"    재무조회 오류 ({year}): {e}")
        time.sleep(CALL_SLEEP)

    return {
        "annual_revenue_mn": None,
        "revenue_year": None,
        "revenue_account": None,
        "revenue_fs_div": None,
        "net_income_mn": None,
    }


def calc_ratio(invest_amount_mn: int | None, annual_revenue_mn: int | None) -> float | None:
    """매출 대비 투자 비율(%). 소수 1자리. 0이거나 없으면 None."""
    if not invest_amount_mn or not annual_revenue_mn:
        return None
    return round(invest_amount_mn / annual_revenue_mn * 100, 1)


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def process_financials(items: list[dict], api_key: str) -> list[dict]:
    """include 건에만 재무조회 수행. exclude/ambiguous는 재무 필드 None으로."""
    results = []
    total_include = sum(1 for i in items if i.get("category") == "include")
    done = 0

    logger.info("시가총액 배치 조회 중 (FinanceDataReader)…")
    marcap_dict = fetch_marcap_dict()
    logger.info(f"시가총액 로드: {len(marcap_dict)}종목")

    for item in items:
        if item.get("category") != "include":
            results.append({
                **item,
                "annual_revenue_mn": None,
                "revenue_year": None,
                "revenue_account": None,
                "revenue_fs_div": None,
                "net_income_mn": None,
                "ratio_to_revenue_pct": None,
                "per": None,
            })
            continue

        done += 1
        corp_name = item.get("corp_name", "")
        corp_code = item.get("corp_code", "")
        rcept_dt  = item.get("rcept_dt", "")
        stock_code = item.get("stock_code", "")

        logger.info(f"[{done}/{total_include}] {corp_name} ({corp_code}) rcept_dt={rcept_dt}")

        rev   = get_revenue(api_key, corp_code, rcept_dt)
        ratio = calc_ratio(item.get("invest_amount_mn"), rev.get("annual_revenue_mn"))

        marcap = marcap_dict.get(stock_code)
        per    = calc_per(marcap, rev.get("net_income_mn"))

        if ratio is None:
            logger.warning("  → 매출 없음 또는 금액 미확인 (ratio=N/A)")
        else:
            logger.info(
                f"  → 매출 {rev['annual_revenue_mn']:,}백만원 / 비율 {ratio}% / PER {per}x"
            )

        results.append({
            **item,
            **rev,
            "ratio_to_revenue_pct": ratio,
            "per": per,
        })

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="연매출 조회 & 비율 계산 (M4)")
    parser.add_argument("--input",  default="data/classified.json")
    parser.add_argument("--output", default="data/financials.json")
    parser.add_argument("--limit",  type=int, default=0, help="테스트: include 건 기준 제한")
    args = parser.parse_args()

    dart_key = os.environ.get("DART_API_KEY", "").strip()
    if not dart_key:
        raise SystemExit("환경변수 DART_API_KEY 가 없습니다.")

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]

    if args.limit:
        # include 건만 limit개, 나머지는 그대로
        include_items = [i for i in items if i.get("category") == "include"][: args.limit]
        other_items   = [i for i in items if i.get("category") != "include"]
        items = include_items + other_items
        logger.info(f"테스트 모드: include {len(include_items)}건만 처리")

    logger.info(f"전체 {len(items)}건 중 include {sum(1 for i in items if i.get('category')=='include')}건 재무조회")
    results = process_financials(items, dart_key)

    include_done = [r for r in results if r.get("category") == "include"]
    with_ratio   = [r for r in include_done if r.get("ratio_to_revenue_pct") is not None]
    no_revenue   = [r for r in include_done if r.get("annual_revenue_mn") is None]
    with_per     = [r for r in include_done if r.get("per") is not None]

    logger.info(
        f"완료: include {len(include_done)}건 / 비율계산 {len(with_ratio)}건 / "
        f"PER계산 {len(with_per)}건 / 매출없음 {len(no_revenue)}건"
    )

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "financials_at": date.today().isoformat(),
        "total": len(results),
        "include": len(include_done),
        "with_ratio": len(with_ratio),
        "no_revenue": len(no_revenue),
        "items": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"저장: {args.output}")
