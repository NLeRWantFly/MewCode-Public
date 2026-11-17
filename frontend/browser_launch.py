"""Short-lived, single-use handoffs from an authenticated desktop to a browser."""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path


class BrowserLaunches:
    def __init__(self, *, ttl=90, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self.pending = {}

    def issue(self):
        now = self.clock()
        self.pending = {key: expiry for key, expiry in self.pending.items() if expiry > now}
        if len(self.pending) >= 32:
            self.pending.pop(next(iter(self.pending)))
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = now + self.ttl
        return "/desktop-boot?ticket=" + ticket

    def consume(self, ticket):
        expiry = self.pending.pop(ticket, 0)
        return expiry > self.clock()

    def publish_startup(self, path, port):
        """Opt-in handoff for local launchers; never export the desktop credential."""
        target = Path(path)
        # Fail rather than replace an unrelated file supplied by a launcher.
        with target.open("x", encoding="utf-8") as stream:
            json.dump({"url": f"http://127.0.0.1:{int(port)}{self.issue()}",
                       "expires_in": self.ttl}, stream)
