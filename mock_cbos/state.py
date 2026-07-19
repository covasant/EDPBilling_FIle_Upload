"""In-memory state for the mock CBOS v4 server.

Deliberately models the real CBOS invariants that bite in practice:
  - an uploaded chunk lands in a GUID folder that is *orphaned* until a Step-7
    register call associates it with a PROCESSID + UPLOADID;
  - the FILEUPLOAD GTG check only turns TRUE once every *mandatory* upload step
    (non-zero UPLOADID) either has a registered file or has been marked optional
    via Step 8.

Reproducing those two rules is the whole point: code that passes here won't be
surprised by the real server.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from mock_cbos import data


@dataclass
class Step:
    stepno: int
    name: str
    uploadid: int
    status: str = "PENDING"
    is_optional: bool = False
    has_file: bool = False

    @property
    def expects_file(self) -> bool:
        return self.uploadid != 0

    @property
    def satisfied(self) -> bool:
        """A mandatory upload step is satisfied when it has a file or is optional;
        non-upload steps (uploadid 0) are always satisfied."""
        return (not self.expects_file) or self.has_file or self.is_optional


@dataclass
class GuidFolder:
    guid: str
    files: dict[str, int] = field(default_factory=dict)  # filename -> total bytes received
    registered: bool = False
    upload_id: str | None = None
    process_id: str | None = None


@dataclass
class Process:
    process_id: str
    segment: str
    login_id: str
    trade_date: str
    steps: list[Step]
    triggered: bool = False
    fileupload_polls: int = 0

    def step_by_uploadid(self, upload_id: str) -> Step | None:
        for s in self.steps:
            if str(s.uploadid) == str(upload_id):
                return s
        return None

    def step_by_stepno(self, stepno: int) -> Step | None:
        for s in self.steps:
            if s.stepno == int(stepno):
                return s
        return None

    def unsatisfied_upload_steps(self) -> list[Step]:
        return [s for s in self.steps if s.expects_file and not s.satisfied]

    def gtg_ready(self) -> bool:
        return not self.unsatisfied_upload_steps()


class MockState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._next_pid = 17658
            self.processes: dict[str, Process] = {}
            self.guids: dict[str, GuidFolder] = {}
            self.latest_pid_by_segment: dict[str, str] = {}

    # --- process lifecycle ----------------------------------------------------
    def reserve_process(self, segment: str, login_id: str, trade_date: str) -> Process:
        with self._lock:
            pid = str(self._next_pid)
            self._next_pid += 1
            steps = [
                Step(stepno=r["STEPNO"], name=r["NAME"], uploadid=r["UPLOADID"], status=r["STATUS"])
                for r in data.table2_for(segment)
            ]
            proc = Process(process_id=pid, segment=segment.upper(), login_id=login_id,
                           trade_date=trade_date, steps=steps)
            self.processes[pid] = proc
            self.latest_pid_by_segment[segment.upper()] = pid
            return proc

    def get_process(self, process_id: str) -> Process | None:
        return self.processes.get(str(process_id))

    def latest_process(self, segment: str) -> Process | None:
        pid = self.latest_pid_by_segment.get(segment.upper())
        return self.processes.get(pid) if pid else None

    # --- uploads --------------------------------------------------------------
    def add_chunk(self, guid: str, filename: str, nbytes: int) -> GuidFolder:
        with self._lock:
            folder = self.guids.setdefault(guid, GuidFolder(guid=guid))
            folder.files[filename] = folder.files.get(filename, 0) + nbytes
            return folder

    def register_file(self, guid: str, upload_id: str, process_id: str) -> tuple[bool, str]:
        """Associate an uploaded GUID folder with a process step. Returns
        (ok, message). Fails if the GUID was never uploaded (orphaned) or the
        PROCESSID/UPLOADID don't line up - the real failure surfaces."""
        with self._lock:
            folder = self.guids.get(guid)
            if folder is None:
                return False, f"uploadfoldername '{guid}' not found - no chunk uploaded under this GUID"
            proc = self.processes.get(str(process_id))
            if proc is None:
                return False, f"PROCESSID {process_id} not found"
            step = proc.step_by_uploadid(upload_id)
            if step is None:
                return False, f"UPLOADID {upload_id} is not a step in PROCESSID {process_id} (segment {proc.segment})"
            folder.registered = True
            folder.upload_id = str(upload_id)
            folder.process_id = str(process_id)
            step.has_file = True
            step.status = "UPLOADED"
            return True, "File entry saved successfully"

    def mark_optional(self, process_id: str, stepno: int, is_optional: bool) -> tuple[bool, str]:
        with self._lock:
            proc = self.processes.get(str(process_id))
            if proc is None:
                return False, f"PROCESSID {process_id} not found"
            step = proc.step_by_stepno(stepno)
            if step is None:
                return False, f"STEPNO {stepno} not found in PROCESSID {process_id}"
            step.is_optional = is_optional
            return True, "Updated Successfully"

    def trigger(self, process_id: str) -> Process | None:
        with self._lock:
            proc = self.processes.get(str(process_id))
            if proc is None:
                return None
            proc.triggered = True
            for s in proc.steps:
                if s.satisfied:
                    s.status = "COMPLETED" if not s.expects_file else "PROCESSED"
            return proc


STATE = MockState()
