"""Tiny HTTP client for live acceptance runs: every request and response is appended to an evidence file (UTC time,
duration, status, body), so a test report can cite exactly what the running system answered.

    from acceptance_client import Client
    c = Client("http://127.0.0.1:8040", "data/accept/evidence.jsonl")
    c.post("/api/runs", {"kind": "top_down", "params": {"product_id": "PRD-SETU"}}, label="A1 start top-down")

Secrets never pass through it: it only talks to the local Chanakya API.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import httpx


class Client:
    def __init__(self, base: str, evidence: str | Path, *, timeout: float = 600):
        self.base, self.path = base.rstrip("/"), Path(evidence)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.http = httpx.Client(timeout=timeout)

    def call(self, method: str, path: str, body=None, *, label: str = "") -> dict:
        t0, at = time.monotonic(), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        try:
            r = self.http.request(method, self.base + path, json=body)
            status = r.status_code
            try:
                data = r.json()
            except ValueError:
                data = {"_text": r.text[:2000], "_content_type": r.headers.get("content-type")}
        except httpx.HTTPError as e:
            status, data = 0, {"_error": f"{type(e).__name__}: {e}"}
        rec = {"at": at, "label": label, "method": method, "path": path, "request": body, "status": status,
               "ms": int((time.monotonic() - t0) * 1000), "response": data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return {"status": status, "data": data, "ms": rec["ms"]}

    def get(self, path: str, *, label: str = "") -> dict:
        return self.call("GET", path, label=label)

    def post(self, path: str, body=None, *, label: str = "") -> dict:
        return self.call("POST", path, body if body is not None else {}, label=label)

    def put(self, path: str, body=None, *, label: str = "") -> dict:
        return self.call("PUT", path, body if body is not None else {}, label=label)

    def wait_run(self, wid: int, *, every: float = 4, limit: float = 900) -> dict:
        """Poll a run until its background job stops; returns the final stage table."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            s = self.http.get(f"{self.base}/api/summary").json()
            if not s.get("busy"):
                break
            time.sleep(every)
        return self.get(f"/api/runs/{wid}/stages", label=f"stages of run {wid}")
