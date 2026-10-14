"""Per-client request limits for the public service.

A flood from one address gets "try again later" (429) instead of taking the only
instance's CPU or the day's Gemini runs. Best effort by design: the counts live in this
instance's memory (Cloud Run runs at most one, and a restart forgets them), and a client
is the address Google's front end reports. Many addresses at once (a distributed attack)
need a control in front of the service, such as Cloud Armor; the daily Gemini quota in
Cloud Storage and the billing caps still bound what anyone can spend.
"""

from __future__ import annotations

import ipaddress
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    name: str
    requests: int
    window_s: float


# Any API request: a person clicking through the page stays far below this.
API = Limit("api", 60, 60.0)
# Requests that spend the day's shared Gemini runs (one full run is up to three), per
# client in ten minutes and in a day, and for everyone together in an hour: no single
# client, and no crowd of them, can spend the whole day's runs in one sitting.
LIVE_AI = Limit("live_ai", 6, 600.0)
LIVE_AI_DAY = Limit("live_ai_day", 9, 86_400.0)
LIVE_AI_ALL = Limit("live_ai_all", 15, 3_600.0)
EVERYONE = "*"
MAX_CLIENTS = 5000  # tracked addresses per limit; older ones are dropped first
# A home line in Japan usually gets a /56 (256 /64s): it counts as one client.
IPV6_PREFIX = 56


def client_of(forwarded_for: str | None, peer: str | None, proxies: int = 0) -> str:
    """The client's address, IPv6 grouped by /56.

    Google's front end appends the address it saw to X-Forwarded-For, so the last entry
    is the one a client cannot choose; anything before it is the client's own claim.
    Each proxy of ours in front (a load balancer) appends one more: `proxies` skips them.
    """
    entries = [entry.strip() for entry in (forwarded_for or "").split(",") if entry.strip()]
    raw = entries[-1 - proxies] if len(entries) > proxies else (peer or "")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw[:64] or "unknown"
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/{IPV6_PREFIX}", strict=False))
    return str(address)


class SlidingWindow:
    """How many requests each client made in each limit's window."""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._windows: dict[str, float] = {}

    def wait(self, client: str, limit: Limit, now: float | None = None) -> float:
        """0 when the request may go ahead (it is then counted), else seconds to wait."""
        now = time.monotonic() if now is None else now
        self._windows[limit.name] = limit.window_s
        key = (limit.name, client)
        hits = self._hits.get(key)
        if hits is None:
            self._make_room(now)
            hits = self._hits[key] = deque()
        while hits and hits[0] <= now - limit.window_s:
            hits.popleft()
        if len(hits) >= limit.requests:
            return max(hits[0] + limit.window_s - now, 0.001)
        hits.append(now)
        return 0.0

    def _make_room(self, now: float) -> None:
        if len(self._hits) < MAX_CLIENTS:
            return
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= now - self._windows[k[0]]]:
            del self._hits[key]
        if len(self._hits) >= MAX_CLIENTS:
            # Still full of active addresses: forget the oldest half rather than grow.
            for key in list(self._hits)[: len(self._hits) - MAX_CLIENTS // 2]:
                del self._hits[key]

    def clear(self) -> None:
        self._hits.clear()
