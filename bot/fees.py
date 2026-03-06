from __future__ import annotations

from typing import Any


def estimate_crypto_taker_fee_usdc(trade_value_usdc: float, price: float) -> float:
    """Rough estimator based on the published crypto fee table and formula behavior.

    We keep this as an estimate only; the authoritative settlement for your account is the Data API.
    Fees are collected in shares on buys and USDC on sells.

    References: https://docs.polymarket.com/trading/fees
    """
    # The docs show effective rate of ~0.64% at p=0.80; ~0.41% at 0.85; ~0.20% at 0.90; ~0.06% at 0.95.
    # We do simple piecewise linear interpolation.
    p = max(0.0001, min(0.9999, float(price)))
    points = [
        (0.80, 0.0064),
        (0.85, 0.0041),
        (0.90, 0.0020),
        (0.95, 0.0006),
        (0.99, 0.0000),
    ]
    if p <= points[0][0]:
        rate = points[0][1]
    else:
        rate = points[-1][1]
        for (p0, r0), (p1, r1) in zip(points, points[1:]):
            if p0 <= p <= p1:
                t = (p - p0) / (p1 - p0)
                rate = r0 + t * (r1 - r0)
                break
    return float(trade_value_usdc) * rate


def estimate_fee_shares_on_buy(amount_usdc: float, price: float) -> float:
    fee_usdc = estimate_crypto_taker_fee_usdc(amount_usdc, price)
    return fee_usdc / max(float(price), 1e-9)
