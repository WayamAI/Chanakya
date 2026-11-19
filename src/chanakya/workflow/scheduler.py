"""Recurring workflows: Chanakya runs discovery on its own, on a schedule, without anyone pressing Start.

A schedule = (kind, params, every_minutes). Each tick (default every 30 s, in a daemon thread of the API process):
  1. for each enabled schedule whose last workflow is waiting at a human gate, resume it, so a run continues by
     itself once a person has approved what it was waiting for (gates are re-checked; nothing is bypassed);
  2. for each enabled schedule that is due, create a NEW workflow with its params and start it.
It uses the API's JobRunner, so the one-job-at-a-time rule still holds: if a job is running, the tick waits.
Every action is written to schedule_events. Turn the thread off with CHANAKYA_SCHEDULER=0.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import traceback

from chanakya.storage.db import jdump, now

DDL = """
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, params_json TEXT NOT NULL,
  every_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, auto_resume INTEGER NOT NULL DEFAULT 1,
  last_run_at TEXT, next_run_at TEXT, last_workflow_id INTEGER, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedule_events (
  id INTEGER PRIMARY KEY, schedule_id INTEGER NOT NULL, workflow_id INTEGER, action TEXT NOT NULL, detail TEXT,
  at TEXT NOT NULL);
"""


def ensure(con) -> None:
    con.executescript(DDL)


def _ts(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def create(con, *, name: str, kind: str, params: dict, every_minutes: int, by: str, start_now: bool = True,
           auto_resume: bool = True) -> int:
    from chanakya.workflow.orchestrator import KINDS
    ensure(con)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if int(every_minutes) < 5:
        raise ValueError("every_minutes must be at least 5")
    nxt = _now() if start_now else _now() + dt.timedelta(minutes=int(every_minutes))
    sid = con.execute("INSERT INTO schedules(name,kind,params_json,every_minutes,enabled,auto_resume,next_run_at,"
                      "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (name, kind, jdump(params), int(every_minutes), 1, int(auto_resume), _ts(nxt), by, now())).lastrowid
    _event(con, sid, None, "created", {"by": by, "every_minutes": every_minutes, "kind": kind})
    return sid


def set_enabled(con, sid: int, enabled: bool, by: str) -> None:
    ensure(con)
    con.execute("UPDATE schedules SET enabled=? WHERE id=?", (int(enabled), sid))
    _event(con, sid, None, "enabled" if enabled else "disabled", {"by": by})


def _event(con, sid, wid, action, detail=None) -> None:
    con.execute("INSERT INTO schedule_events(schedule_id,workflow_id,action,detail,at) VALUES(?,?,?,?,?)",
                (sid, wid, action, jdump(detail), now()))
    con.commit()


def listing(con) -> list[dict]:
    ensure(con)
    out = []
    for r in con.execute("SELECT * FROM schedules ORDER BY id"):
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json") or "{}")
        w = con.execute("SELECT status, current_stage FROM workflows WHERE id=?", (r["last_workflow_id"],)).fetchone() \
            if r["last_workflow_id"] else None
        d["last_workflow"] = dict(w) | {"id": r["last_workflow_id"]} if w else None
        d["events"] = [dict(e) for e in con.execute("SELECT * FROM schedule_events WHERE schedule_id=? ORDER BY id "
                                                    "DESC LIMIT 10", (r["id"],))]
        out.append(d)
    return out


def next_run(con) -> str | None:
    ensure(con)
    r = con.execute("SELECT MIN(next_run_at) FROM schedules WHERE enabled=1").fetchone()
    return r[0] if r else None


def tick(con, jobs) -> list[dict]:
    """One scheduler pass. Returns what it did (also logged in schedule_events)."""
    from chanakya.workflow import orchestrator as W
    ensure(con)
    did: list[dict] = []
    if jobs.busy:
        return did
    t = _now()
    for s in con.execute("SELECT * FROM schedules WHERE enabled=1 ORDER BY id").fetchall():
        if jobs.busy:
            break
        wid = s["last_workflow_id"]
        if s["auto_resume"] and wid:
            w = con.execute("SELECT status FROM workflows WHERE id=?", (wid,)).fetchone()
            if w and w["status"] == "waiting_human" and _gate_cleared(con, wid):
                jobs.start(wid)
                _event(con, s["id"], wid, "resumed", {"reason": "the human gate it waited on is now satisfied"})
                did.append({"schedule_id": s["id"], "resumed": wid})
                continue
        if s["next_run_at"] and dt.datetime.fromisoformat(s["next_run_at"]) <= t:
            params = json.loads(s["params_json"] or "{}") | {"schedule_id": s["id"]}
            nw = W.create_workflow(con, s["kind"], params)
            con.execute("UPDATE schedules SET last_run_at=?, next_run_at=?, last_workflow_id=? WHERE id=?",
                        (_ts(t), _ts(t + dt.timedelta(minutes=s["every_minutes"])), nw, s["id"]))
            con.commit()
            jobs.start(nw)
            _event(con, s["id"], nw, "started", {"kind": s["kind"]})
            did.append({"schedule_id": s["id"], "started": nw})
    return did


def _gate_cleared(con, wid: int) -> bool:
    """Re-evaluate the waiting gate without side effects (gate stages only read the database)."""
    from chanakya.workflow import orchestrator as W
    wf = con.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
    st = con.execute("SELECT stage FROM workflow_stages WHERE workflow_id=? AND status='waiting_human' ORDER BY seq "
                     "LIMIT 1", (wid,)).fetchone()
    if wf is None or st is None or not st[0].endswith("_gate"):
        return False
    c = W.Ctx(con, dict(wf), W.Services(), live=False)
    return W.STAGES[st[0]](c).status == "done"


class SchedulerThread:
    def __init__(self, jobs, open_db, interval_s: float = 30.0):
        self.jobs, self.open_db, self.interval = jobs, open_db, interval_s
        self._stop = threading.Event()
        self.last_tick: str | None = None
        self.last_error: str | None = None
        self.thread = threading.Thread(target=self._loop, name="chanakya-scheduler", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.interval):
            con = self.open_db()
            try:
                tick(con, self.jobs)
                self.last_tick, self.last_error = now(), None
            except Exception as e:  # noqa: BLE001 - logged and kept; the loop goes on
                self.last_error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                con.close()


def enabled_by_env() -> bool:
    return os.getenv("CHANAKYA_SCHEDULER", "1") != "0"
