"""Build ``src/fauxcall_timeapi/ip_zones.json`` from the IP recordings (evidence, not invention).

Every successful ``/time/current/ip`` or ``/timezone/ip`` recording (v1 and legacy) maps the
address it asked about to the zone the real API answered. Run after recording more IPs:

    uv run python scripts/build_ip_table.py
"""

from __future__ import annotations

import ipaddress
import json
import socket
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("time-current-ip", "timezone-ip")


def canonical(text: str) -> str | None:
    """The address as the fake normalizes it (IPv4 dotted quad, IPv6 compressed)."""
    try:
        if ":" in text:
            address = ipaddress.IPv6Address(text)
            mapped = address.ipv4_mapped
            return str(mapped) if mapped else str(address)
        return str(ipaddress.IPv4Address(socket.inet_aton(text)))
    except (OSError, ValueError):
        return None


def main() -> None:
    """Scan the recordings and write the table."""
    table: dict[str, str] = {}
    for folder in sorted((ROOT / "data" / "recordings").iterdir()):
        if not any(folder.name.endswith(source) for source in SOURCES):
            continue
        for path in sorted(folder.glob("*.json")):
            exchange = json.loads(path.read_text(encoding="utf-8"))
            response = exchange["response"]
            body = (response.get("body") or {}).get("json")
            if response["status"] != 200 or not isinstance(body, dict):  # noqa: PLR2004
                continue
            query = dict(parse_qsl(urlsplit(exchange["request"]["url"]).query))
            raw = next((v for k, v in query.items() if k.lower() == "ipaddress"), None)
            zone = body.get("timezone") or body.get("timeZone")
            address = canonical(raw) if raw else None
            if address and isinstance(zone, str):
                table[address] = zone
    target = ROOT / "src" / "fauxcall_timeapi" / "ip_zones.json"
    target.write_text(json.dumps(dict(sorted(table.items())), indent=1) + "\n", encoding="utf-8")
    print(f"{len(table)} addresses -> {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
