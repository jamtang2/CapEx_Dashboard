"""
M3 — 순수 설비투자 분류기

규칙 기반 1차 분류 → 애매한 건만 LLM 판정.
결과: category = "include" | "exclude" | "ambiguous"

Usage:
    python -m src.classifier
    python -m src.classifier --input data/extracted.json --output data/classified.json --limit 20
"""

import os
import json
import time
import logging
import re
from datetime import date

import anthropic

CALL_SLEEP = 0.2

INCLUDE_KEYWORDS = [
    "생산라인", "생산 라인", "증설", "신설", "신공장", "신축",
    "생산능력", "생산 능력", "생산설비", "제조설비", "양산설비",
    "생산시설", "증축", "capacity", "capa", "캐파",
    "공장 건설", "공장건설", "생산거점", "양산라인", "양산 라인",
]

EXCLUDE_KEYWORDS = [
    "연구소", "r&d", "기숙사", "사택", "사옥", "본사",
    "오피스", "물류창고", "창고", "데이터센터",
    "토지 매입", "토지매입", "지분취득", "지분 취득", "출자",
    "노후", "교체", "임대", "리츠",
]

LLM_MODEL = "claude-haiku-4-5-20251001"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rule-based classifier
# ---------------------------------------------------------------------------

def rule_classify(purpose: str) -> str | None:
    """
    명확한 경우만 반환. 애매하면 None.
    반환값: "include" | "exclude" | None
    """
    if not purpose:
        return None

    text = purpose.lower()

    exclude_hits = [kw for kw in EXCLUDE_KEYWORDS if kw.lower() in text]
    include_hits = [kw for kw in INCLUDE_KEYWORDS if kw.lower() in text]

    # 배제 키워드만 있으면 → exclude
    if exclude_hits and not include_hits:
        return "exclude"

    # 포함 키워드만 있으면 → include
    if include_hits and not exclude_hits:
        return "include"

    # 둘 다 없거나 둘 다 있으면 → 애매 → LLM
    return None


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

_LLM_PROMPT = """\
다음은 한국 상장사가 DART에 제출한 신규시설투자 공시의 투자목적 텍스트입니다.
이 투자가 "순수 생산설비 증설(생산 capacity 확대)"에 해당하는지 판단하세요.

[포함] 생산라인 신·증설, 공장 신축/증축, 신규 생산설비 도입, 제조 capacity 확대
[배제] 연구소/R&D센터, 기숙사/사택, 본사/사옥, 물류창고, 토지 단순 매입, 지분투자, 노후설비 단순 교체(capacity 무변동)

투자목적: {purpose}

아래 JSON만 출력하세요 (설명 없이):
{{"include": true/false, "reason": "판단 근거 한 문장", "confidence": 0.0~1.0}}
"""


def llm_classify(purpose: str, client: anthropic.Anthropic) -> dict:
    msg = client.messages.create(
        model=LLM_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": _LLM_PROMPT.format(purpose=purpose)}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {"include": None, "reason": "LLM 파싱 실패", "confidence": 0.0}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"include": None, "reason": "LLM JSON 오류", "confidence": 0.0}


# ---------------------------------------------------------------------------
# Per-item classify
# ---------------------------------------------------------------------------

def classify_one(item: dict, client: anthropic.Anthropic) -> dict:
    purpose = item.get("purpose", "")
    result = {}

    rule_result = rule_classify(purpose)

    if rule_result == "include":
        result["category"] = "include"
        result["classify_reason"] = "규칙: 생산설비 관련 키워드 포함"
        result["classify_confidence"] = 1.0
        result["_classify_method"] = "rule"

    elif rule_result == "exclude":
        result["category"] = "exclude"
        result["classify_reason"] = "규칙: 비생산 투자 키워드 포함"
        result["classify_confidence"] = 1.0
        result["_classify_method"] = "rule"

    else:
        # LLM 판정
        llm = llm_classify(purpose, client)
        if llm.get("include") is True:
            result["category"] = "include"
        elif llm.get("include") is False:
            result["category"] = "exclude"
        else:
            result["category"] = "ambiguous"

        result["classify_reason"] = llm.get("reason", "")
        result["classify_confidence"] = llm.get("confidence", 0.0)
        result["_classify_method"] = "llm"

    return result


def classify_all(items: list[dict], client: anthropic.Anthropic) -> list[dict]:
    results = []
    total = len(items)
    llm_count = 0

    for idx, item in enumerate(items, 1):
        try:
            clf = classify_one(item, client)
        except Exception as e:
            logger.error(f"[{idx}/{total}] {item.get('corp_name')} 분류 오류: {e}")
            clf = {"category": "ambiguous", "classify_reason": f"오류: {e}", "classify_confidence": 0.0, "_classify_method": "error"}

        if clf.get("_classify_method") == "llm":
            llm_count += 1
            time.sleep(CALL_SLEEP)

        results.append({**item, **clf})

        if idx % 50 == 0:
            inc = sum(1 for r in results if r.get("category") == "include")
            exc = sum(1 for r in results if r.get("category") == "exclude")
            logger.info(f"  [{idx}/{total}] include={inc} exclude={exc} llm={llm_count}")

    return results, llm_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="신규시설투자 포함/배제 분류 (M3)")
    parser.add_argument("--input",  default="data/extracted.json")
    parser.add_argument("--output", default="data/classified.json")
    parser.add_argument("--limit",  type=int, default=0)
    args = parser.parse_args()

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not anthropic_key:
        raise SystemExit("환경변수 ANTHROPIC_API_KEY 가 없습니다.")

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    items = data["items"]
    if args.limit:
        items = items[: args.limit]
        logger.info(f"테스트 모드: 상위 {args.limit}건")

    client = anthropic.Anthropic(api_key=anthropic_key)

    logger.info(f"분류 시작: {len(items)}건")
    results, llm_count = classify_all(items, client)

    inc   = sum(1 for r in results if r["category"] == "include")
    exc   = sum(1 for r in results if r["category"] == "exclude")
    amb   = sum(1 for r in results if r["category"] == "ambiguous")

    logger.info(f"완료: include={inc} / exclude={exc} / ambiguous={amb} / LLM사용={llm_count}건")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "classified_at": date.today().isoformat(),
        "total": len(results),
        "include": inc,
        "exclude": exc,
        "ambiguous": amb,
        "llm_used": llm_count,
        "items": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"저장: {args.output}")
