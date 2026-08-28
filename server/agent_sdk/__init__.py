"""AI agent SDK: minimal client to connect an AI agent to the world."""
from __future__ import annotations
import json
import time
from typing import Any
import urllib.request
import urllib.error


class WowAgent:
    """3-line connect: WowAgent(url, name, cls).register().loop()
    Or use connect() for an existing token."""

    def __init__(self, base_url: str, name: str | None = None, cls: str | None = None,
                 token: str | None = None, player_id: str | None = None, lang: str = "zh"):
        self.base = base_url.rstrip("/")
        self.lang = lang
        self.name = name
        self.cls = cls
        self.token = token
        self.player_id = player_id

    # ---- low-level http ----------------------------------------------------

    def _req(self, method: str, path: str, body: dict | None = None,
             auth: bool = True) -> dict:
        url = f"{self.base}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body_text)
            except Exception:
                err = {"detail": body_text}
            raise RuntimeError(f"{e.code}: {err}") from e

    # ---- lifecycle --------------------------------------------------------

    def register(self, name: str | None = None, cls: str | None = None) -> dict:
        if name:
            self.name = name
        if cls:
            self.cls = cls
        if not self.name or not self.cls:
            raise ValueError("name and cls required")
        r = self._req("POST", "/api/v1/register",
                      {"name": self.name, "cls": self.cls}, auth=False)
        self.token = r["token"]
        self.player_id = r["player_id"]
        return r

    def state(self) -> dict:
        return self._req("GET", f"/api/v1/state?lang={self.lang}")

    def action(self, act: str, payload: dict | None = None) -> dict:
        return self._req("POST", "/api/v1/action",
                         {"action": act, "payload": payload or {}})

    def observer(self) -> dict:
        return self._req("GET", f"/api/v1/observer/state?lang={self.lang}", auth=False)


# ---- Convenience: 3-line connect helper ----------------------------------

def connect(base_url: str, name: str, cls: str) -> WowAgent:
    """3-line connect:
        a = connect('http://127.0.0.1:8787', 'MyBot', 'warrior')
        s = a.state()
        a.action('attack', {'target_id': 'mob_xxx', 'skill_id': 'heroic_strike'})
    """
    a = WowAgent(base_url, name=name, cls=cls)
    a.register()
    return a


if __name__ == "__main__":
    # Smoke test: register a bot and print state.
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
    name = sys.argv[2] if len(sys.argv) > 2 else "sdk_smoke"
    cls = sys.argv[3] if len(sys.argv) > 3 else "warrior"
    a = connect(base, name, cls)
    print("registered:", a.name, a.cls, a.player_id)
    s = a.state()
    print("zone:", s["zone"], "hp:", s["you"]["hp"], "/", s["you"]["hp_max"])
