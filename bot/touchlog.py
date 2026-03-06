from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


@dataclass
class TouchEvent:
    ts: str
    market_slug: str
    seconds_left: float
    best_price: float
    up_price: float | None
    down_price: float | None
    crossed_threshold: bool


class TouchLogger:
    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({'touches': []}, indent=2))

    def append(self, event: TouchEvent) -> None:
        data = json.loads(self.path.read_text())
        data.setdefault('touches', []).append(event.__dict__)
        self.path.write_text(json.dumps(data, indent=2))


def summarize_touches(touches: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    total = len(touches)
    crossed = [t for t in touches if t.get('crossed_threshold')]
    by_market: dict[str, int] = {}
    for t in crossed:
        by_market[t['market_slug']] = by_market.get(t['market_slug'], 0) + 1

    max_best = max([float(t.get('best_price') or 0) for t in touches], default=0.0)
    return {
        'total_samples': total,
        'threshold': threshold,
        'crossed_samples': len(crossed),
        'crossed_markets': len(by_market),
        'max_best_price_seen': round(max_best, 6),
    }
