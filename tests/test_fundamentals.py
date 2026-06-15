import pandas as pd
import pytest
from unittest.mock import MagicMock, patch

from data.fundamentals import _compute_eps_growth, _get_sector


def _mock_ticker_with_eps(eps_latest, eps_prior):
    ticker = MagicMock()
    stmt = pd.DataFrame(
        {"2023": [eps_latest], "2022": [eps_prior]},
        index=["BasicEPS"],
    )
    ticker.get_income_stmt.return_value = stmt
    return ticker


def test_eps_growth_positive():
    ticker = _mock_ticker_with_eps(2.0, 1.6)
    growth = _compute_eps_growth(ticker)
    assert abs(growth - 0.25) < 0.001  # (2.0-1.6)/1.6 = 0.25


def test_eps_growth_negative_prior_returns_zero():
    ticker = _mock_ticker_with_eps(2.0, -1.0)
    growth = _compute_eps_growth(ticker)
    assert growth == 0.0


def test_eps_growth_no_data_returns_zero():
    ticker = MagicMock()
    ticker.get_income_stmt.return_value = pd.DataFrame()
    growth = _compute_eps_growth(ticker)
    assert growth == 0.0


def test_eps_growth_exception_returns_zero():
    ticker = MagicMock()
    ticker.get_income_stmt.side_effect = Exception("network error")
    growth = _compute_eps_growth(ticker)
    assert growth == 0.0


def test_get_sector():
    ticker = MagicMock()
    ticker.info = {"sector": "Technology"}
    assert _get_sector(ticker) == "Technology"


def test_get_sector_missing():
    ticker = MagicMock()
    ticker.info = {}
    assert _get_sector(ticker) == ""
