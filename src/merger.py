"""
M5 — 정정공시 머징 & capex.json 빌드 (SSOT)

처리 흐름:
  1. financials.json 시간순 정렬
  2. 원공시(is_revised=False) → capex 딕셔너리에 신규 등재 + dedup 인덱스 등록
  3. 정정공시(is_revised=True) → dedup 키로 원공시 탐색
       매칭 성공: 필드 업데이트 + revisions[] append
       매칭 실패: orphan_revision=True 로 신규 등재

dedup 키 우선순위:
  1차: corp_code + 이사회결의일 (board_resolution_date)
  2차: corp_code + 정규화된 투자목적 앞 50자

Usage:
    python -m src.merger
    python -m src.merger --input data/financials.json --output data/capex.json
"""

import os
import json
import re
import logging
from datetime import datetime, timezone

UPDATABLE_FIELDS = [
    "invest_amount_mn",
    "purpose",
    "start_date",
    "end_date",
    "ratio_to_equity_pct",
    "annual_revenue_mn",
    "revenue_year",
    "revenue_account",
    "revenue_fs_div",
    "ratio_to_revenue_pct",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_purpose(purpose: str) -> str:
    """소문자 + 한글/영문/숫자만 남기고 앞 50자."""
    if not purpose:
        return ""
    cleaned = re.sub(r"[^\w가-힣]", "", purpose.lower())
    return cleaned[:50]


def dedup_keys(item: dict) -> list[str]:
    """우선순위 순으로 dedup 키 목록 반환."""
    corp = item.get("corp_code", "")
    keys = []

    brd = item.get("board_resolution_date", "")
    if brd:
        keys.append(f"{corp}__brd__{brd}")

    purpose_norm = normalize_purpose(item.get("purpose", ""))
    if purpose_norm:
        keys.append(f"{corp}__pur__{purpose_norm}")

    return keys


def build_capex_item(item: dict) -> dict:
    """financials 아이템에서 capex.json 스키마에 맞는 딕셔너리 생성."""
    rcept_no = item.get("rcept_no", "")
    return {
        "corp_code":              item.get("corp_code"),
        "corp_name":              item.get("corp_name"),
        "stock_code":             item.get("stock_code"),
        "market":                 item.get("market"),
        "rcept_no":               rcept_no,
        "rcept_dt":               item.get("rcept_dt"),
        "report_nm":              item.get("report_nm"),
        "invest_amount_mn":       item.get("invest_amount_mn"),
        "annual_revenue_mn":      item.get("annual_revenue_mn"),
        "revenue_year":           item.get("revenue_year"),
        "revenue_account":        item.get("revenue_account"),
        "revenue_fs_div":         item.get("revenue_fs_div"),
        "ratio_to_revenue_pct":   item.get("ratio_to_revenue_pct"),
        "ratio_to_equity_pct":    item.get("ratio_to_equity_pct"),
        "purpose":                item.get("purpose"),
        "start_date":             item.get("start_date"),
        "end_date":               item.get("end_date"),
        "board_resolution_date":  item.get("board_resolution_date"),
        "category":               item.get("category"),
        "classify_reason":        item.get("classify_reason"),
        "classify_confidence":    item.get("classify_confidence"),
        "dart_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
        "last_updated":           now_iso(),
    }


# ---------------------------------------------------------------------------
# Core merger
# ---------------------------------------------------------------------------

def merge(items: list[dict]) -> tuple[list[dict], dict]:
    """
    시간순으로 정렬된 아이템 목록을 머징해 capex 리스트로 변환.
    반환: (capex_items, stats)
    """
    items_sorted = sorted(items, key=lambda x: x.get("rcept_dt", ""))

    capex: dict[str, dict] = {}       # id → capex_item
    dedup_idx: dict[str, str] = {}    # dedup_key → id

    stats = {"new": 0, "updated": 0, "orphan": 0}

    for item in items_sorted:
        is_rev = item.get("is_revised", False) or "[기재정정]" in item.get("report_nm", "")
        corp   = item.get("corp_code", "")
        rno    = item.get("rcept_no", "")

        if not is_rev:
            # ── 원공시 신규 등재 ──────────────────────────────────
            item_id = f"{corp}__{rno}"
            entry = {
                **build_capex_item(item),
                "id":               item_id,
                "is_revised":       False,
                "orphan_revision":  False,
                "revisions":        [],
            }
            capex[item_id] = entry
            for k in dedup_keys(item):
                dedup_idx.setdefault(k, item_id)   # 먼저 등록된 원공시 우선
            stats["new"] += 1

        else:
            # ── 정정공시 매칭 시도 ────────────────────────────────
            matched_id = None
            for k in dedup_keys(item):
                if k in dedup_idx:
                    matched_id = dedup_idx[k]
                    break

            if matched_id and matched_id in capex:
                original = capex[matched_id]

                # 변경 전 스냅샷 보존
                snapshot = {f: original.get(f) for f in UPDATABLE_FIELDS}
                original["revisions"].append({
                    "rcept_no":  rno,
                    "rcept_dt":  item.get("rcept_dt"),
                    "snapshot":  snapshot,
                })

                # 필드 업데이트 (None이 아닌 값만)
                for f in UPDATABLE_FIELDS:
                    if item.get(f) is not None:
                        original[f] = item[f]

                original["is_revised"]   = True
                original["rcept_no"]     = rno          # 최신 접수번호로 갱신
                original["last_updated"] = now_iso()
                original["dart_url"]     = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"
                stats["updated"] += 1
                logger.debug(f"  정정 매칭: {item.get('corp_name')} {rno} → {matched_id}")

            else:
                # 매칭 실패 → orphan 신규 등재
                item_id = f"{corp}__orphan__{rno}"
                entry = {
                    **build_capex_item(item),
                    "id":               item_id,
                    "is_revised":       True,
                    "orphan_revision":  True,
                    "revisions":        [],
                }
                capex[item_id] = entry
                for k in dedup_keys(item):
                    dedup_idx.setdefault(k, item_id)
                stats["orphan"] += 1
                logger.debug(f"  orphan: {item.get('corp_name')} {rno}")

    return list(capex.values()), stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="정정공시 머징 & capex.json 빌드 (M5)")
    parser.add_argument("--input",  default="data/financials.json")
    parser.add_argument("--output", default="data/capex.json")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]
    logger.info(f"입력: {len(items)}건 (include={data.get('include', '?')})")

    capex_items, stats = merge(items)

    include_items = [i for i in capex_items if i.get("category") == "include"]
    revised       = sum(1 for i in capex_items if i.get("is_revised"))
    orphans       = sum(1 for i in capex_items if i.get("orphan_revision"))

    logger.info(
        f"완료: 총 {len(capex_items)}건 → "
        f"신규={stats['new']} / 정정흡수={stats['updated']} / orphan={stats['orphan']}"
    )
    include_orphans = sum(1 for i in include_items if i.get("orphan_revision"))
    logger.info(f"  include {len(include_items)}건 (원공시={len(include_items)-include_orphans}건, orphan정정={include_orphans}건)")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "schema_version": "1.0",
        "last_run": now_iso(),
        "total": len(capex_items),
        "include": len(include_items),
        "stats": stats,
        "items": capex_items,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"저장: {args.output}")
