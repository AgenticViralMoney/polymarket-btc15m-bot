from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from bot.fees import estimate_crypto_taker_fee_usdc, estimate_fee_shares_on_buy
from bot.state import Journal, TradeRecord


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    details: dict[str, Any]
    trade_id: str | None = None


class BaseExecutor:
    def __init__(self, journal: Journal, trade_size_usd: float, max_worst_price: float, min_liquidity_on_best_level: float):
        self.journal = journal
        self.trade_size_usd = trade_size_usd
        self.max_worst_price = max_worst_price
        self.min_liquidity_on_best_level = min_liquidity_on_best_level

    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        raise NotImplementedError

    def _base_trade_record(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, price: float, status: str, details: dict[str, Any]) -> TradeRecord:
        fee_usdc = estimate_crypto_taker_fee_usdc(self.trade_size_usd, price) if market.get('feesEnabled') else 0.0
        fee_shares = estimate_fee_shares_on_buy(self.trade_size_usd, price) if market.get('feesEnabled') else 0.0
        gross_shares = self.trade_size_usd / max(price, 1e-9)
        net_shares = max(gross_shares - fee_shares, 0.0)
        return TradeRecord(
            mode='live' if details.get('live') else 'paper',
            market_slug=market['slug'],
            market_question=market['question'],
            condition_id=market['conditionId'],
            token_id=token_id,
            outcome=outcome,
            outcome_index=outcome_index,
            entry_price=price,
            amount_usd=self.trade_size_usd,
            shares_gross=gross_shares,
            shares_net=net_shares,
            entry_fee_usdc_est=fee_usdc,
            entry_fee_shares_est=fee_shares,
            end_date=market['endDate'],
            fees_enabled=bool(market.get('feesEnabled')),
            status=status,
            response_status=details.get('response_status'),
            order_id=details.get('order_id'),
            details=details,
        )


class PaperExecutor(BaseExecutor):
    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        details = {
            'ref_price': ref_price,
            'worst_price_cap': min(self.max_worst_price, max(ref_price, 0.01)),
            'simulated': True,
            'live': False,
        }
        record = self._base_trade_record(market, token_id, outcome, outcome_index, ref_price, 'simulated', details)
        trade_id = self.journal.add_trade(record)
        return ExecutionResult(True, 'simulated', details, trade_id=trade_id)


class LiveExecutor(BaseExecutor):
    def __init__(
        self,
        journal: Journal,
        trade_size_usd: float,
        max_worst_price: float,
        min_liquidity_on_best_level: float,
        host: str,
        chain_id: int,
        private_key: str,
        funder_address: str,
        signature_type: int,
    ):
        super().__init__(journal, trade_size_usd, max_worst_price, min_liquidity_on_best_level)
        self.client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=signature_type,
            funder=funder_address,
        )
        creds = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(creds)

    def preflight(self, token_id: str, ref_price: float) -> dict[str, Any]:
        tick_size = self.client.get_tick_size(token_id)
        neg_risk = self.client.get_neg_risk(token_id)
        book = self.client.get_order_book(token_id)
        asks = getattr(book, 'asks', []) or []
        best_ask = float(asks[0].price) if asks else None
        best_ask_size = float(asks[0].size) if asks else 0.0
        return {
            'tick_size': tick_size,
            'neg_risk': neg_risk,
            'best_ask': best_ask,
            'best_ask_size': best_ask_size,
            'book_summary': {
                'market': getattr(book, 'market', None),
                'asset_id': getattr(book, 'asset_id', None),
                'tick_size': getattr(book, 'tick_size', None),
                'min_order_size': getattr(book, 'min_order_size', None),
            },
            'ref_price': ref_price,
        }

    def execute(self, market: dict[str, Any], token_id: str, outcome: str, outcome_index: int, ref_price: float) -> ExecutionResult:
        pre = self.preflight(token_id, ref_price)
        best_ask = pre['best_ask']
        if best_ask is None:
            return ExecutionResult(False, 'no_ask_liquidity', pre)
        if best_ask > self.max_worst_price:
            return ExecutionResult(False, 'ask_above_worst_price_cap', pre)
        if pre['best_ask_size'] < self.min_liquidity_on_best_level:
            return ExecutionResult(False, 'insufficient_best_ask_liquidity', pre)

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=self.trade_size_usd,
            side=BUY,
            price=float(best_ask),
            order_type=OrderType.FOK,
        )
        signed = self.client.create_market_order(
            mo,
            {
                'tick_size': pre['tick_size'],
                'neg_risk': pre['neg_risk'],
            },
        )
        resp = self.client.post_order(signed, OrderType.FOK)
        details = {
            'response': resp,
            'ref_price': ref_price,
            'best_ask': best_ask,
            'best_ask_size': pre['best_ask_size'],
            'tick_size': pre['tick_size'],
            'neg_risk': pre['neg_risk'],
            'response_status': resp.get('status') if isinstance(resp, dict) else None,
            'order_id': resp.get('orderID') if isinstance(resp, dict) else None,
            'live': True,
        }
        record = self._base_trade_record(market, token_id, outcome, outcome_index, best_ask, str(resp.get('status', 'submitted')) if isinstance(resp, dict) else 'submitted', details)
        trade_id = self.journal.add_trade(record)
        return ExecutionResult(True, 'submitted', details, trade_id=trade_id)
