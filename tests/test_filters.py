from strategy.filters import sector_concentration_ok, filter_value_smallcap


def test_sector_ok_when_below_limit():
    counts = {"Technology": 2, "Financials": 1}
    assert sector_concentration_ok("Technology", counts, max_per_sector=3) is True


def test_sector_blocked_at_limit():
    counts = {"Technology": 3}
    assert sector_concentration_ok("Technology", counts, max_per_sector=3) is False


def test_empty_sector_always_passes():
    counts = {"Technology": 10}
    assert sector_concentration_ok("", counts, max_per_sector=3) is True


def test_unknown_sector_treated_as_zero():
    counts = {}
    assert sector_concentration_ok("Healthcare", counts, max_per_sector=3) is True


def test_filter_value_smallcap():
    # group median PE = median(7, 18, 20) = 18
    # 0.7 * 18 = 12.6  → A(PE=7) passes, C(PE=18) doesn't (18 >= 12.6)
    info_list = [
        {"symbol": "A", "marketCap": 1e9, "trailingPE": 7.0,  "epsGrowth": 0.15},  # passes
        {"symbol": "B", "marketCap": 6e9, "trailingPE": 18.0, "epsGrowth": 0.15},  # too large mcap
        {"symbol": "C", "marketCap": 2e9, "trailingPE": 20.0, "epsGrowth": 0.15},  # PE too high
    ]
    result = filter_value_smallcap(info_list, max_mcap=5e9, max_per_vs_group=0.7, min_eps_growth=0.10)
    assert "A" in result
    assert "B" not in result
    assert "C" not in result
