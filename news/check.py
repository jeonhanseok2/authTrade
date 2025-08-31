# news/check.py
from __future__ import annotations
from typing import List
import os

def is_positive_news(symbol: str, keywords: List[str]) -> bool:
    """
    간단/보수적으로 처리: 키워드 기반.
    실제 운용 시 Alpaca News API나 별도 뉴스 API 붙이세요.
    여기서는 항상 False 반환하거나, 나중에 훅을 연결.
    """
    # TODO: Alpaca News API 연동 시 구현
    return False
