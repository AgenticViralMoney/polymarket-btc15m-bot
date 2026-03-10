from __future__ import annotations

import time
import traceback
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
            stop_loss_price=settings.stop_loss_price,
            take_profit_price=settings.take_profit_price,
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
        stop_loss_price=settings.stop_loss_price,
        take_profit_price=settings.take_profit_price,
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
    strategy = Strategy(
        settings.min_confidence_price,
        settings.max_worst_price,
        settings.seconds_before_resolution,
        skip_seconds_delayed_markets=settings.skip_seconds_delayed_markets,
    )
    btc_feed = LiveBTCFeed(
        history_seconds=1800,
        volatility_lookback_seconds=settings.binance_volatility_lookback_seconds,
        stale_after_seconds=settings.binance_stale_after_seconds,
    )
    btc_feed.start()

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
    pre_window_seconds = max(900 - settings.seconds_before_resolution, 0)
    pre_window_minutes = pre_window_seconds / 60
    console.print(
        f"BTC 15m loop: sleep first ~{pre_window_minutes:.0f} minutes, monitor final {settings.seconds_before_resolution} seconds, buy if UP or DOWN >= {settings.min_confidence_price:.2f}"
    )

    while True:
        try:
            markets = discovery.find_current_btc_15m_markets()
            if not markets:
                markets = discovery.list_recent_btc_15m_markets_via_search()

            current = _pick_current_market(markets) if markets else None
            if not current:
                console.print('[yellow]No current BTC 15-minute market found[/yellow]')
                time.sleep(max(settings.poll_interval_seconds, 5))
                continue

            slug = current['slug']
            secs = _seconds_left(current['endDate'])

            existing_open_trade = next(
                (t for t in journal.unsettled_trades() if t.get('market_slug') == slug),
                None,
            )
            if slug in seen_markets and existing_open_trade is None:
                sleep_for = min(max(secs, 0.0), 5.0)
                console.print(f"[{slug}] already handled, waiting for next market | secs_left={secs:.1f} | sleep={sleep_for:.1f}s")
                time.sleep(max(min(sleep_for, 5.0), 0.5))
                continue

            if secs > settings.seconds_before_resolution:
                sleep_for = min(max(secs - settings.seconds_before_resolution, 0.0), 30.0)
                console.print(f"[{slug}] waiting for entry window | secs_left={secs:.1f} | sleep={sleep_for:.1f}s")
                if settings.run_once:
                    console.print('Run-once mode complete')
                    break
                time.sleep(max(min(sleep_for, 5.0), 0.5))
                continue

            console.print(f"[{slug}] ENTRY WINDOW | waiting for websocket updates")
            market = discovery.prepare_market(current)
            signal = btc_feed.build_market_signal(market)
            market['_signal_context'] = signal
            last_update_id = discovery.market_feed.current_update_id()


            while True:
                if not market:
                    break

                market['_decision_ts'] = datetime.now(timezone.utc).isoformat()
                decision = strategy.evaluate(market)
                parsed = market.get('_parsed_outcomes') or []
                up_price = parsed[0]['price'] if len(parsed) > 0 else None
                down_price = parsed[1]['price'] if len(parsed) > 1 else None
                best_price = max([x['price'] for x in parsed]) if parsed else None

                open_trade = next((t for t in journal.unsettled_trades() if t.get('market_slug') == slug), None)
                if open_trade is not None:
                    trade_idx = int(open_trade.get('outcome_index', -1))
                    trade_price = parsed[trade_idx]['price'] if 0 <= trade_idx < len(parsed) else None
                    if trade_price is not None and float(trade_price) <= float((open_trade.get('details') or {}).get('stop_loss_price', settings.stop_loss_price)):
                        stop_result = executor.stop_loss_exit(open_trade, float(trade_price))
                        console.print(f"Stop loss result for {slug}: {stop_result.status} | price={float(trade_price):.3f}")
                        journal.add_note(
                            'stop_loss_exit',
                            {'slug': slug, 'result': stop_result.status, 'trade_id': stop_result.trade_id, 'details': stop_result.details},
                        )
                        if stop_result.ok:
                            break
                        else:
                            console.print(f"[yellow][{slug}] Early stop loss failed ({stop_result.status}), retrying in 1s[/yellow]")
                            time.sleep(1)

                secs_left = decision.seconds_to_resolution
                secs_text = 'n/a' if secs_left is None else f"{secs_left:.1f}"
                source = market.get('_live_price_source')
                ws_status = market.get('_ws_status') or {}
                sync_gap = ws_status.get('sync_gap_seconds')
                sync_text = 'n/a' if sync_gap is None else f"{sync_gap:.3f}"
                console.print(
                    f"[{slug}] {decision.reason} | up={up_price} | down={down_price} | best={best_price} | source={source} | sync_gap={sync_text} | secs_left={secs_text}"
                )

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

                open_trade = next(
                    (
                        t for t in journal.unsettled_trades()
                        if t.get('market_slug') == slug and t.get('status') not in {'stopped_out', 'take_profit'}
                    ),
                    None,
                )
                if open_trade and parsed:
                    selected_price = None
                    try:
                        selected_idx = int(open_trade['outcome_index'])
                        if 0 <= selected_idx < len(parsed):
                            selected_price = float(parsed[selected_idx]['price'])
                    except Exception:
                        selected_price = None

                    details = dict(open_trade.get('details') or {})
                    profit_protect_armed = bool(details.get('profit_protect_armed'))

                    if selected_price is not None and not profit_protect_armed and float(open_trade.get('entry_price', 0)) < 0.89 and selected_price >= settings.profit_protect_arm_price:
                        details['profit_protect_armed'] = True
                        details['profit_protect_armed_at'] = float(selected_price)
                        journal.update_trade(open_trade['trade_id'], {'details': details})
                        open_trade['details'] = details
                        profit_protect_armed = True
                        console.print(f"Profit protect armed for {slug} at {selected_price:.3f}")

                    if selected_price is not None and selected_price >= settings.take_profit_price:
                        result = executor.take_profit_exit(open_trade, selected_price)
                        console.print(f"Take profit result for {slug}: {result.status}")
                        journal.add_note(
                            'take_profit_exit',
                            {'slug': slug, 'result': result.status, 'trade_id': result.trade_id, 'details': result.details},
                        )
                        if result.ok:
                            break
                        else:
                            console.print(f"[yellow][{slug}] Take profit failed ({result.status}), retrying in 1s[/yellow]")
                            time.sleep(1)
                    elif selected_price is not None and profit_protect_armed and selected_price <= settings.profit_protect_exit_price:
                        result = executor.take_profit_exit(open_trade, selected_price)
                        console.print(f"Profit protect exit for {slug}: {result.status}")
                        journal.add_note(
                            'profit_protect_exit',
                            {'slug': slug, 'result': result.status, 'trade_id': result.trade_id, 'details': result.details},
                        )
                        if result.ok:
                            break
                        else:
                            console.print(f"[yellow][{slug}] Profit protect exit failed ({result.status}), retrying in 1s[/yellow]")
                            time.sleep(1)
                    elif selected_price is not None and selected_price <= float((open_trade.get('details') or {}).get('stop_loss_price', settings.stop_loss_price)):
                        result = executor.stop_loss_exit(open_trade, selected_price)
                        console.print(f"Stop loss result for {slug}: {result.status}")
                        journal.add_note(
                            'stop_loss_exit',
                            {'slug': slug, 'result': result.status, 'trade_id': result.trade_id, 'details': result.details},
                        )
                        if result.ok:
                            break
                        else:
                            console.print(f"[yellow][{slug}] Stop loss failed ({result.status}), retrying in 1s[/yellow]")
                            time.sleep(1)

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
                    if result.ok:
                        seen_markets.add(slug)
                    else:
                        # Entry failed (API error, liquidity, etc.) — wait briefly then retry
                        console.print(f"[yellow][{slug}] Entry failed ({result.status}), will retry in 2s[/yellow]")
                        time.sleep(2)
                if secs_left is None or secs_left <= 0 or market.get('closed') or not market.get('acceptingOrders'):
                    break

                wait_timeout = min(max(settings.poll_interval_seconds, 0.05), max(secs_left, 0.05))
                new_update_id = discovery.market_feed.wait_for_update(last_update_id, timeout_seconds=wait_timeout)

                if new_update_id == last_update_id:
                    # No websocket update — use fast WS refresh if available, slow CLOB fetch as fallback
                    market = discovery.refresh_active_market(market)
                else:
                    last_update_id = new_update_id
                    market = discovery.refresh_active_market(market)

                if market:
                    signal = btc_feed.build_market_signal(market)
                    market['_signal_context'] = signal

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
                console.print(f'Report: {report_path}')

            if settings.run_once:
                console.print('Run-once mode complete')
                break

        except KeyboardInterrupt:
            console.print('Stopped by user')
            break
        except Exception as exc:
            journal.add_note('loop_error', {'error': repr(exc)})
            console.print(f'[red]Loop error:[/red] {repr(exc)}')
            traceback.print_exc()
            time.sleep(max(settings.poll_interval_seconds, 5))


if __name__ == '__main__':
    main()
