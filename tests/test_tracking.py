from bot.fees import estimate_crypto_taker_fee_usdc, estimate_fee_shares_on_buy
from bot.touchlog import summarize_touches
from bot.tracking import build_summary


def test_fee_estimate_decreases_toward_extremes():
    fee_80 = estimate_crypto_taker_fee_usdc(100, 0.80)
    fee_95 = estimate_crypto_taker_fee_usdc(100, 0.95)
    assert fee_80 > fee_95


def test_fee_shares_positive():
    fee_shares = estimate_fee_shares_on_buy(10, 0.80)
    assert fee_shares > 0


def test_build_summary_counts_correctly():
    trades = [
        {'settled_at': '2026-01-01T00:00:00Z', 'net_pnl_usdc': 1.0, 'gross_pnl_usdc': 1.2, 'entry_price': 0.8},
        {'settled_at': '2026-01-01T00:01:00Z', 'net_pnl_usdc': -1.0, 'gross_pnl_usdc': -0.8, 'entry_price': 0.9},
        {'settled_at': None, 'entry_price': 0.85},
    ]
    summary = build_summary(trades)
    assert summary['total_trades'] == 3
    assert summary['settled_trades'] == 2
    assert summary['wins'] == 1
    assert summary['losses'] == 1
    assert summary['net_pnl_usdc'] == 0.0


def test_touch_summary_counts_threshold_crossings():
    touches = [
        {'market_slug': 'a', 'best_price': 0.79, 'crossed_threshold': False},
        {'market_slug': 'a', 'best_price': 0.81, 'crossed_threshold': True},
        {'market_slug': 'b', 'best_price': 0.95, 'crossed_threshold': True},
    ]
    summary = summarize_touches(touches, 0.80)
    assert summary['total_samples'] == 3
    assert summary['crossed_samples'] == 2
    assert summary['crossed_markets'] == 2
    assert summary['max_best_price_seen'] == 0.95
