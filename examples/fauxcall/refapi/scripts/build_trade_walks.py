"""Extract each symbol's deterministic trade walk from the recorded SSE sessions.

The real ``GET /v1/stream/trades`` emits the same price/size sequence for a symbol on every call
(shorter ``count`` values are prefixes), so the fake replays the longest recorded session of each
symbol. Run from the fake's folder: ``uv run python scripts/build_trade_walks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "data" / "recordings" / "get-v1-stream-trades"
OUTPUT = ROOT / "data" / "trade_walks.json"


def main() -> None:
    """Write ``data/trade_walks.json``: symbol -> [[price, size], ...] (longest session wins)."""
    walks: dict[str, list[list[str | int]]] = {}
    for path in sorted(RECORDINGS.glob("*.json")):
        exchange = json.loads(path.read_text(encoding="utf-8"))
        if exchange["response"]["status"] != 200:  # noqa: PLR2004
            continue
        query = dict(parse_qsl(urlsplit(exchange["request"]["url"]).query))
        symbol = query["symbol"].upper()
        events = [json.loads(e["data"]) for e in exchange.get("stream") or [] if e["kind"] == "sse"]
        walk: list[list[str | int]] = [[e["price"], e["size"]] for e in events]
        known = walks.get(symbol, [])
        if known[: len(walk)] != walk[: len(known)]:
            msg = f"{symbol}: recorded sessions disagree ({path.name})"
            raise SystemExit(msg)
        if len(walk) > len(known):
            walks[symbol] = walk
    OUTPUT.write_text(json.dumps(dict(sorted(walks.items())), indent=1) + "\n", encoding="utf-8")
    print(f"{len(walks)} walks -> {OUTPUT.relative_to(ROOT)}")  # noqa: T201


if __name__ == "__main__":
    main()
