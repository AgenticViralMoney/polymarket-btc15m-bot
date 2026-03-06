from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class TradeRecord:
    mode: str
    market_slug: str
    market_question: str
    condition_id: str
    token_id: str
    outcome: str
    outcome_index: int
    entry_price: float
    amount_usd: float
    shares_gross: float
    shares_net: float
    entry_fee_usdc_est: float
    entry_fee_shares_est: float
    end_date: str
    fees_enabled: bool
    status: str
    trade_id: str = field(default_factory=lambda: uuid4().hex)
    placed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    order_id: str | None = None
    response_status: str | None = None
    settled_at: str | None = None
    settlement_source: str | None = None
    winner_outcome: str | None = None
    payout_usdc: float | None = None
    gross_pnl_usdc: float | None = None
    net_pnl_usdc: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


class Journal:
    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.exists():
            self._write({'trades': [], 'notes': []})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text())

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def add_trade(self, record: TradeRecord) -> str:
        data = self._read()
        data.setdefault('trades', []).append(asdict(record))
        self._write(data)
        return record.trade_id

    def update_trade(self, trade_id: str, updates: dict[str, Any]) -> None:
        data = self._read()
        for trade in data.get('trades', []):
            if trade.get('trade_id') == trade_id:
                trade.update(updates)
                break
        self._write(data)

    def add_note(self, note: str, extra: dict[str, Any] | None = None) -> None:
        data = self._read()
        data.setdefault('notes', []).append(
            {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'note': note,
                'extra': extra or {},
            }
        )
        self._write(data)

    def trades(self) -> list[dict[str, Any]]:
        return self._read().get('trades', [])

    def unsettled_trades(self) -> list[dict[str, Any]]:
        return [t for t in self.trades() if not t.get('settled_at')]
