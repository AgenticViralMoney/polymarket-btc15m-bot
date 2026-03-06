from datetime import datetime, timedelta, timezone

from bot.market_discovery import GammaMarketDiscovery
from bot.strategy import Strategy
from main import _pick_current_market, _seconds_left


def market_template(seconds_left: int, prices=(0.82, 0.18), outcomes=('Up', 'Down')):
    end_dt = datetime.now(timezone.utc) + timedelta(seconds=seconds_left)
    return {
        'endDate': end_dt.isoformat().replace('+00:00', 'Z'),
        'acceptingOrders': True,
        'enableOrderBook': True,
        'closed': False,
        'secondsDelay': None,
        '_parsed_outcomes': [
            {'index': 0, 'label': outcomes[0], 'price': prices[0]},
            {'index': 1, 'label': outcomes[1], 'price': prices[1]},
        ],
        '_parsed_token_ids': ['token-up', 'token-down'],
    }


def test_triggers_when_threshold_and_time_window_match():
    strategy = Strategy(min_confidence_price=0.80, seconds_before_resolution=59)
    decision = strategy.evaluate(market_template(45))
    assert decision.should_trade is True
    assert decision.chosen_outcome == 'Up'
    assert decision.chosen_token_id == 'token-up'
    assert decision.chosen_outcome_index == 0


def test_rejects_if_too_early():
    strategy = Strategy(min_confidence_price=0.80, seconds_before_resolution=59)
    decision = strategy.evaluate(market_template(75))
    assert decision.should_trade is False
    assert 'too early' in decision.reason


def test_rejects_if_price_below_threshold():
    strategy = Strategy(min_confidence_price=0.80, seconds_before_resolution=59)
    decision = strategy.evaluate(market_template(30, prices=(0.79, 0.21)))
    assert decision.should_trade is False
    assert 'below threshold' in decision.reason


def test_rejects_seconds_delay_market():
    strategy = Strategy(min_confidence_price=0.80, seconds_before_resolution=59)
    market = market_template(30)
    market['secondsDelay'] = 3
    decision = strategy.evaluate(market)
    assert decision.should_trade is False
    assert 'execution delay' in decision.reason


def test_direct_slug_generation_for_current_5m_market():
    discovery = GammaMarketDiscovery('https://gamma-api.polymarket.com')
    markets = discovery.find_current_btc_5m_markets(horizon_steps=3)
    assert isinstance(markets, list)


def test_pick_current_market_chooses_nearest_future_end():
    m1 = market_template(250)
    m2 = market_template(40)
    picked = _pick_current_market([m1, m2])
    assert picked is m2


def test_seconds_left_returns_future_value():
    market = market_template(25)
    assert _seconds_left(market['endDate']) > 0
