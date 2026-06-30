"""
M2 — DART 공시 본문 데이터 추출기

규칙 기반 파싱 우선, 핵심 필드 누락 시 LLM(Claude) fallback.

Usage:
    python -m src.extractor
    python -m src.extractor --input data/candidates.json --output data/extracted.json --limit 10
"""

import os
import io
import json
import time
import logging
import re
import zipfile
from datetime import date

import requests
import anthropic

DOCUMENT_URL = "https://opendart.fss.or.kr/api/document.xml"
CALL_SLEEP = 0.3
LLM_MODEL = "claude-haiku-4-5-20251001"

# 핵심 필드: 이 3개가 모두 있으면 LLM 호출 생략
REQUIRED_FIELDS = ["invest_amount_mn", "purpose", "end_date"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Document download
# ---------------------------------------------------------------------------

def download_document(api_key: str, rcept_no: str) -> str:
    """DART document.xml API → ZIP 다운로드 → 텍스트 반환."""
    params = {"crtfc_key": api_key, "rcept_no": rcept_no}
    resp = requests.get(DOCUMENT_URL, params=params, timeout=30)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        xml_files = [n for n in names if n.lower().endswith(".xml")]
        target = max(xml_files or names, key=lambda n: zf.getinfo(n).file_size)
        raw = zf.read(target)

    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;|&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Rule-based parsers
# ---------------------------------------------------------------------------

def parse_amount_mn(raw: str):
    """원 단위 숫자 → 백만원 정수. 이미 백만원 단위인 경우 그대로."""
    raw = raw.replace(",", "").replace(" ", "")
    m = re.search(r"(\d+)", raw)
    if not m:
        return None
    val = int(m.group(1))
    # 1억 이상 → 원 단위로 간주
    return round(val / 1_000_000) if val >= 100_000_000 else val


def parse_date(raw: str):
    """다양한 날짜 형식 → YYYY-MM-DD."""
    raw = raw.strip()
    m = re.search(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{4})(\d{2})(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _clean_purpose(raw: str) -> str:
    """목적 텍스트에서 다음 항목(숫자. 또는 숫자. 로 시작하는 부분) 이전까지만 추출."""
    # "4. 투자기간" 같은 다음 항목 번호 앞에서 자름
    cut = re.split(r"\s+\d+\s*[.．]\s*[가-힣A-Za-z]", raw)
    return cut[0].strip()[:200]


def rule_extract(text: str) -> dict:
    result = {}

    # 투자금액
    for pat in [
        r"투자\s*금액\s*[（(]?원[）)]?\s*[：:\s]\s*([\d,]+)",
        r"취득\s*예정\s*금액\s*[：:\s]\s*([\d,]+)",
        r"투자\s*금액\s*[：:\s]\s*([\d,]+)",
        r"금\s+([\d,]+)\s*원",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_amount_mn(m.group(1))
            if val and val > 0:
                result["invest_amount_mn"] = val
                break

    # 투자목적 — 다음 번호 항목 앞에서 잘라냄
    for pat in [
        r"투자\s*목적\s*[：:\s]\s*([^\n]{5,})",
        r"취득\s*목적\s*[：:\s]\s*([^\n]{5,})",
        r"사업\s*목적\s*[：:\s]\s*([^\n]{5,})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            result["purpose"] = _clean_purpose(m.group(1))
            break

    # 시작일
    for pat in [
        r"시\s*작\s*일\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        r"착\s*공\s*(?:예정)?\s*일\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        r"투자\s*기간\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d = parse_date(m.group(1))
            if d:
                result["start_date"] = d
                break

    # 종료일 — 정정공시는 "정정 후" 섹션 값이 마지막에 등장하므로 findall로 마지막 매칭 사용
    for pat in [
        r"종\s*료\s*일\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        r"완\s*공\s*(?:예정)?\s*일\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        r"준\s*공\s*(?:예정)?\s*일\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        r"취득\s*예정\s*일\s*[：:\s]?\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
    ]:
        matches = re.findall(pat, text, re.IGNORECASE)
        if matches:
            d = parse_date(matches[-1])   # 마지막 = 정정 후 값
            if d:
                result["end_date"] = d
                break

    # 이사회결의일 — "(결정일)" 괄호 형태도 처리, 마지막 매칭 사용
    matches = re.findall(
        r"이\s*사\s*회\s*결\s*의\s*일\s*(?:[（(][^）)]*[）)])?\s*[：:\s]\s*(\d{4}[\s.\-/년]\s*\d{1,2}[\s.\-/월]\s*\d{1,2})",
        text, re.IGNORECASE,
    )
    if matches:
        d = parse_date(matches[-1])
        if d:
            result["board_resolution_date"] = d

    # 자기자본 대비(%) — 마지막 매칭 사용
    matches = re.findall(r"자기\s*자본\s*대비\s*[（(%]?\s*([\d.]+)", text, re.IGNORECASE)
    if matches:
        try:
            result["ratio_to_equity_pct"] = float(matches[-1])
        except ValueError:
            pass

    return result


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------

_LLM_PROMPT = """\
다음은 DART에 제출된 신규시설투자(또는 유형자산취득) 공시 본문입니다.
아래 JSON 스키마로 필드를 추출하세요. 찾을 수 없는 값은 null로 하세요.

{
  "invest_amount_mn": <투자금액. 원 단위이면 /1000000 반올림, 이미 백만원이면 그대로. 정수>,
  "purpose": <투자목적 텍스트, 200자 이내 문자열>,
  "start_date": <투자기간 시작일, YYYY-MM-DD 또는 null>,
  "end_date": <투자기간 종료일 또는 취득예정일, YYYY-MM-DD>,
  "board_resolution_date": <이사회결의일, YYYY-MM-DD 또는 null>,
  "ratio_to_equity_pct": <자기자본대비(%) 숫자 또는 null>
}

JSON만 출력하세요. 설명 불필요.

--- 공시 본문 ---
"""


def llm_extract(text: str, client: anthropic.Anthropic) -> dict:
    truncated = text[:4000]
    msg = client.messages.create(
        model=LLM_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": _LLM_PROMPT + truncated}],
    )
    raw = msg.content[0].text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Per-item extraction
# ---------------------------------------------------------------------------

def is_complete(result: dict) -> bool:
    return all(result.get(k) for k in REQUIRED_FIELDS)


def extract_one(item: dict, dart_key: str, anthropic_client: anthropic.Anthropic) -> dict:
    """단일 공시 추출. 실패해도 예외를 밖으로 던지지 않음."""
    rcept_no = item["rcept_no"]
    out = {"_parse_method": None, "_error": None}

    try:
        content = download_document(dart_key, rcept_no)
        text = strip_tags(content)

        extracted = rule_extract(text)

        if is_complete(extracted):
            out["_parse_method"] = "rule"
        else:
            logger.info(f"    {rcept_no} ({item['corp_name']}): LLM fallback")
            llm_result = llm_extract(text, anthropic_client)
            # LLM 결과로 누락 필드만 보완
            for k, v in llm_result.items():
                if v is not None and extracted.get(k) is None:
                    extracted[k] = v
            out["_parse_method"] = "llm" if not is_complete(extracted) or llm_result else "rule+llm"

        out.update(extracted)

    except Exception as e:
        logger.error(f"    {rcept_no} 추출 오류: {e}")
        out["_error"] = str(e)

    return out


# ---------------------------------------------------------------------------
# Batch extract
# ---------------------------------------------------------------------------

def extract_all(
    candidates: list[dict],
    dart_key: str,
    anthropic_client: anthropic.Anthropic,
) -> list[dict]:
    results = []
    total = len(candidates)

    for idx, item in enumerate(candidates, 1):
        logger.info(f"[{idx}/{total}] {item['corp_name']} {item['rcept_no']}")
        extracted = extract_one(item, dart_key, anthropic_client)

        merged = {**item, **extracted}
        results.append(merged)

        time.sleep(CALL_SLEEP)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DART 공시 본문 추출 (M2)")
    parser.add_argument("--input",  default="data/candidates.json")
    parser.add_argument("--output", default="data/extracted.json")
    parser.add_argument("--limit",  type=int, default=0, help="테스트용: 처리 건수 제한")
    args = parser.parse_args()

    dart_key = os.environ.get("DART_API_KEY", "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not dart_key:
        raise SystemExit("환경변수 DART_API_KEY 가 없습니다.")
    if not anthropic_key:
        raise SystemExit("환경변수 ANTHROPIC_API_KEY 가 없습니다.")

    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    candidates = data["items"]
    if args.limit:
        candidates = candidates[: args.limit]
        logger.info(f"테스트 모드: 상위 {args.limit}건만 처리")

    client = anthropic.Anthropic(api_key=anthropic_key)

    logger.info(f"추출 시작: {len(candidates)}건")
    results = extract_all(candidates, dart_key, client)

    success = sum(1 for r in results if not r.get("_error") and is_complete(r))
    llm_used = sum(1 for r in results if r.get("_parse_method") and "llm" in r.get("_parse_method", ""))
    errors = sum(1 for r in results if r.get("_error"))

    logger.info(f"완료: 성공 {success}건 / LLM사용 {llm_used}건 / 오류 {errors}건")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    output = {
        "extracted_at": date.today().isoformat(),
        "total": len(results),
        "success": success,
        "llm_used": llm_used,
        "errors": errors,
        "items": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"저장: {args.output}")
