from __future__ import annotations

import time
from datetime import datetime, timezone

from rich.console import Console

from config import settings
from bot.execution import LiveExecutor, PaperExecutor
from bot.live_btc_feed import LiveBTCFeed
from bot.market_discovery import GammaMarketDiscovery
from bot.state import Journal
from bot.strategy import Strategy
from bot.touchlog import TouchEvent, TouchLogger
from bot.tracking import SettlementTracker, build_summary, write_summary_report


console = Console()


def build_executor(journal: Journal):
    if settings.live_trading:
        if not settings.private_key or not settings.funder_address:
            raise ValueError('LIVE_TRADING=true requires PRIVATE_KEY and FUNDER_ADDRESS')
        return LiveExecutor(
            journal=journal,
            trade_size_usd=settings.trade_size_usd,
            max_worst_price=settings.max_worst_price,
            min_liquidity_on_best_level=settings.min_liquidity_on_best_level,
            host=settings.polymarket_host,
            chain_id=settings.chain_id,
            private_key=settings.private_key,
            funder_address=settings.funder_address,
            signature_type=settings.signature_type,
        )
    return PaperExecutor(
        journal=journal,
        trade_size_usd=settings.trade_size_usd,
        max_worst_price=settings.max_worst_price,
        min_liquidity_on_best_level=settings.min_liquidity_on_best_level,
    )


def _seconds_left(end_date_iso: str) -> float:
    end_dt = datetime.fromisoformat(end_date_iso.replace('Z', '+00:00'))
    return (end_dt - datetime.now(timezone.utc)).total_seconds()


def _pick_current_market(markets: list[dict]) -> dict | None:
    candidates = []
    for m in markets:
        try:
            secs = _seconds_left(m['endDate'])
        except Exception:
            continue
        if secs > 0:
            candidates.append((secs, m))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def main() -> None:
    journal = Journal(settings.journal_path)
    discovery = GammaMarketDiscovery(settings.polymarket_gamma_url, settings.polymarket_host)
    btc_feed = LiveBTCFeed(
        volatility_lookback_seconds=settings.binance_volatility_lookback_seconds,
        stale_after_seconds=settings.binance_stale_after_seconds,
    )
    btc_feed.start()
    if btc_feed.wait_until_ready(timeout_seconds=20):
        status = btc_feed.get_status()
        console.print(f"Live BTC stream ready | source={status.get('active_source')} | price={status.get('latest_price')}")
    else:
        status = btc_feed.get_status()
        console.print(f"[yellow]Live BTC stream not ready yet[/yellow] | source={status.get('active_source')} | last_error={status.get('last_error')}")

    strategy = Strategy(
        settings.min_confidence_price,
        settings.seconds_before_resolution,
        skip_seconds_delayed_markets=settings.skip_seconds_delayed_markets,
    )
    executor = build_executor(journal)
    tracker = SettlementTracker(
        gamma_url=settings.polymarket_gamma_url,
        data_url=settings.polymarket_data_url,
        journal=journal,
        user_address=settings.effective_tracking_address,
    )
    touch_logger = TouchLogger(settings.touchlog_path) if settings.touchlog_enabled else None

    seen_markets: set[str] = set()

    console.print(f"Mode: {'LIVE' if settings.live_trading else 'PAPER'}")
    console.print('BTC 5m loop: sleep first ~4 minutes, monitor last minute, buy if >= 0.80')

    while True:
        try:
            markets = discovery.find_current_btc_5m_markets()
            if not markets:
                markets = discovery.list_recent_btc_5m_markets_via_search()

            current = _pick_current_market(markets) if markets else None
            if not current:
                console.print('[yellow]No current BTC 5-minute market found[/yellow]')
                time.sleep(max(settings.poll_interval_seconds, 5))
                continue

            slug = current['slug']
            secs = _seconds_left(current['endDate'])

            if secs > settings.seconds_before_resolution:
                sleep_for = min(max(secs - settings.seconds_before_resolution, 0.0), 30.0)
                console.print(f"[{slug}] waiting for last minute | secs_left={secs:.1f} | sleep={sleep_for:.1f}s")
                if settings.run_once:
                    console.print('Run-once mode complete')
                    break
                time.sleep(max(sleep_for, 1.0))
                continue

            console.print(f"[{slug}] LAST MINUTE | monitoring every {settings.poll_interval_seconds}s")
            while True:
                market = discovery.get_market_by_slug(slug)
                if not market:
                    break

                signal = btc_feed.build_market_signal(market)
                if signal.get('ready'):
                    market = btc_feed.apply_signal_to_market(market, signal)
                else:
                    market['_signal_context'] = signal
                    market['_live_price_source'] = signal.get('price_source')

                market['_decision_ts'] = datetime.now(timezone.utc).isoformat()
                decision = strategy.evaluate(market)
                signal_ctx = market.get('_signal_context') or {}
                parsed = market.get('_parsed_outcomes') or []
                up_price = parsed[0]['price'] if len(parsed) > 0 else None
                down_price = parsed[1]['price'] if len(parsed) > 1 else None
                best_price = max([x['price'] for x in parsed]) if parsed else None

                secs_left = decision.seconds_to_resolution
                secs_text = 'n/a' if secs_left is None else f"{secs_left:.1f}"
                btc_now = signal_ctx.get('current_btc_price')
                btc_open = signal_ctx.get('market_open_price')
                if btc_now is not None and btc_open is not None:
                    console.print(
                        f"[{slug}] {decision.reason} | best={best_price} | btc={btc_now:.2f} vs open={btc_open:.2f} | secs_left={secs_text}"
                    )
                else:
                    console.print(f"[{slug}] {decision.reason} | best={best_price} | secs_left={secs_text}")

                if touch_logger and best_price is not None and secs_left is not None and secs_left <= settings.seconds_before_resolution and secs_left > 0:
                    touch_logger.append(
                        TouchEvent(
                            ts=datetime.now(timezone.utc).isoformat(),
                            market_slug=slug,
                            seconds_left=float(secs_left),
                            best_price=float(best_price),
                            up_price=float(up_price) if up_price is not None else None,
                            down_price=float(down_price) if down_price is not None else None,
                            crossed_threshold=bool(best_price >= settings.min_confidence_price),
                        )
                    )

                if decision.should_trade and (not settings.only_one_trade_per_market or slug not in seen_markets):
                    result = executor.execute(
                        market,
                        token_id=decision.chosen_token_id,
                        outcome=decision.chosen_outcome,
                        outcome_index=decision.chosen_outcome_index,
                        ref_price=decision.chosen_price,
                    )
                    console.print(f"Trade result for {slug}: {result.status}")
                    journal.add_note(
                        'trade_attempt',
                        {'slug': slug, 'result': result.status, 'trade_id': result.trade_id, 'details': result.details},
                    )
                    seen_markets.add(slug)

                if secs_left is None or secs_left <= 0 or market.get('closed') or not market.get('acceptingOrders'):
                    break

                time.sleep(settings.poll_interval_seconds)

            should_settle = settings.auto_settle_live if settings.live_trading else settings.auto_settle_paper
            if should_settle:
                updates = tracker.settle_all(live_mode=settings.live_trading)
                if updates:
                    console.print(f'Settled {len(updates)} trade(s)')

            report_path = write_summary_report(
                journal.trades(),
                settings.reports_dir,
                touches_path=settings.touchlog_path if settings.touchlog_enabled else None,
                threshold=settings.min_confidence_price,
            )
            if settings.report_every_loop:
                summary = build_summary(journal.trades())
                console.print(
                    f"Summary | total={summary['total_trades']} settled={summary['settled_trades']} wins={summary['wins']} losses={summary['losses']} net_pnl={summary['net_pnl_usdc']}"
                )
                console.print(f"Report: {report_path}")

            if settings.run_once:
                console.print('Run-once mode complete')
                break

        except KeyboardInterrupt:
            console.print('Stopped by user')
            break
        except Exception as exc:
            journal.add_note('loop_error', {'error': repr(exc)})
            console.print(f'[red]Loop error:[/red] {exc}')
            time.sleep(max(settings.poll_interval_seconds, 5))

    btc_feed.stop()


if __name__ == '__main__':
    main()
