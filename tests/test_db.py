import pytest
from storage.db import PositionDB


@pytest.fixture
def db():
    return PositionDB(path=":memory:")


def test_open_and_get_position(db):
    db.open_position("AAPL", "momentum", 150.0, 10, "Technology")
    pos = db.get_open_position("AAPL")
    assert pos is not None
    assert pos["symbol"] == "AAPL"
    assert pos["entry_price"] == 150.0
    assert pos["peak_price"] == 150.0
    assert pos["status"] == "open"


def test_update_peak_increases(db):
    db.open_position("MSFT", "momentum", 300.0, 5, "Technology")
    db.update_peak("MSFT", 320.0)
    pos = db.get_open_position("MSFT")
    assert pos["peak_price"] == 320.0


def test_update_peak_does_not_decrease(db):
    db.open_position("TSLA", "momentum", 200.0, 3)
    db.update_peak("TSLA", 180.0)  # lower — should be ignored
    pos = db.get_open_position("TSLA")
    assert pos["peak_price"] == 200.0


def test_close_position(db):
    db.open_position("AMD", "value", 100.0, 20)
    db.close_position("AMD")
    assert db.get_open_position("AMD") is None


def test_list_open_positions(db):
    db.open_position("NVDA", "momentum", 400.0, 2, "Technology")
    db.open_position("META", "value",    200.0, 5, "Communication Services")
    rows = db.list_open_positions()
    syms = [r["symbol"] for r in rows]
    assert "NVDA" in syms and "META" in syms


def test_count_open_by_sector(db):
    db.open_position("AAPL", "momentum", 150.0, 10, "Technology")
    db.open_position("MSFT", "momentum", 300.0, 5,  "Technology")
    db.open_position("JPM",  "value",    120.0, 8,  "Financials")
    counts = db.count_open_by_sector()
    assert counts.get("Technology") == 2
    assert counts.get("Financials") == 1


def test_record_and_get_trades(db):
    db.record_trade("AAPL", "buy", 10, 150.0, "momentum", "signal")
    db.record_trade("AAPL", "sell", 10, 165.0, "momentum", "take_profit")
    trades = db.get_trades("AAPL")
    assert len(trades) == 2
    assert trades[0]["side"] == "sell"  # DESC order
