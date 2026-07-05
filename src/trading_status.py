"""
거래정지(추정) 종목 조회

FinanceDataReader의 KRX 시세 스냅샷에서 거래량/시가/고가/저가가
모두 0인 종목을 거래정지 상태로 간주한다. (금양처럼 상장폐지 심사 등으로
장기 매매정지된 종목, 단기과열 등으로 당일 매매정지된 종목 모두 해당)
"""

import logging

logger = logging.getLogger(__name__)


def fetch_halted_stock_codes() -> set[str]:
    """현재 거래정지(추정) 종목코드(6자리) 집합. 조회 실패 시 빈 집합."""
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
    except Exception as e:
        logger.warning(f"거래정지 종목 조회 오류: {e}")
        return set()

    halted = df[
        (df["Volume"] == 0)
        & (df["Open"] == 0)
        & (df["High"] == 0)
        & (df["Low"] == 0)
        & (df["Close"] > 0)
    ]
    return set(halted["Code"].astype(str).str.zfill(6))
