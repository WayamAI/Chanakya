"""Background execution for the UI: ONE job at a time (the LinkedIn account and Clay spend controls assume a single
active pipeline). A job runs `orchestrator.run(..., live=True)` on its own SQLite connection; the UI polls
workflow_stages / workflow_events through the API. Pause and cancel are cooperative: checked between stages."""
from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass, field
from typing import Callable

from chanakya.api.db import open_db
from chanakya.storage.db import now


@dataclass
class Job:
    workflow_id: int
    kind: str  # run | stage
    started: str = field(default_factory=now)
    finished: str | None = None
    control: str | None = None  # pause | cancel
    result: dict | None = None
    error: str | None = None


class JobRunner:
    def __init__(self, services_factory: Callable):
        self.services_factory = services_factory
        self._lock = threading.Lock()
        self.current: Job | None = None
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, workflow_id: int, *, stage: str | None = None) -> Job:
        from chanakya.workflow import orchestrator as W
        with self._lock:
            if self.busy:
                raise RuntimeError("A run step is working; wait for it to finish")
            job = Job(workflow_id, "stage" if stage else "run")
            self.current = job

            def body():
                con = open_db()
                try:
                    svc = self.services_factory(True)
                    if stage:
                        job.result = W.run_stage(con, workflow_id, stage, svc, live=True)
                    else:
                        job.result = W.run(con, workflow_id, svc, live=True, should_stop=lambda: (
                            {"pause": "Paused by you", "cancel": "Cancelled by you"}.get(job.control or "")))
                    if job.control:
                        status = "paused" if job.control == "pause" else "cancelled"
                        con.execute("UPDATE workflows SET status=?, updated_at=? WHERE id=?", (status, now(),
                                                                                              workflow_id))
                        con.execute("INSERT INTO workflow_events(workflow_id,stage,level,message,at) VALUES(?,?,?,?,?)",
                                    (workflow_id, None, "info", f"{status} by user", now()))
                        con.commit()
                except Exception as e:  # stage failures are already recorded by the orchestrator
                    job.error = f"{type(e).__name__}: {e}"
                    con.execute("INSERT INTO workflow_events(workflow_id,stage,level,message,detail_json,at) "
                                "VALUES(?,?,?,?,?,?)", (workflow_id, None, "error", job.error,
                                                        traceback.format_exc()[-2000:], now()))
                    con.commit()
                finally:
                    job.finished = now()
                    con.close()
            self._thread = threading.Thread(target=body, name=f"chanakya-wf-{workflow_id}", daemon=True)
            self._thread.start()
            return job

    def request(self, workflow_id: int, control: str) -> bool:
        """Ask the running job to stop after its current stage. False if that workflow is not running."""
        if self.busy and self.current and self.current.workflow_id == workflow_id:
            self.current.control = control
            return True
        return False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)
