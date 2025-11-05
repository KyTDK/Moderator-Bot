from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

__all__ = ["TaskMetadata", "TaskRuntimeDetail", "SlowTaskReporter"]


@dataclass(slots=True)
class TaskMetadata:
    display_name: str
    module: Optional[str]
    qualname: Optional[str]
    function: Optional[str]
    filename: Optional[str]
    first_lineno: Optional[int]

    @classmethod
    def from_coroutine(cls, coro) -> "TaskMetadata":
        """Extract identifying information for a coroutine."""
        code = getattr(coro, "cr_code", None)
        qualname = getattr(code, "co_qualname", None) if code is not None else None
        func_name = getattr(code, "co_name", None) if code is not None else None
        filename = getattr(code, "co_filename", None) if code is not None else None
        first_lineno = getattr(code, "co_firstlineno", None) if code is not None else None

        module = getattr(coro, "__module__", None)
        if module is None:
            frame = getattr(coro, "cr_frame", None)
            if frame is not None:
                module = frame.f_globals.get("__name__")

        fallback = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None)
        name = qualname or func_name or fallback
        if not name and module:
            name = f"{module}.<coroutine>"
        display_name = str(name) if name else repr(coro)

        return cls(
            display_name=display_name,
            module=module,
            qualname=qualname,
            function=func_name,
            filename=filename,
            first_lineno=first_lineno,
        )


@dataclass(slots=True)
class TaskRuntimeDetail:
    metadata: TaskMetadata
    wait: float
    runtime: float
    enqueued_at_monotonic: float
    started_at_monotonic: float
    completed_at_monotonic: float
    started_at_wall: float
    completed_at_wall: float
    backlog_at_enqueue: int
    backlog_at_start: int
    backlog_at_finish: int
    active_workers_start: int
    busy_workers_start: int
    max_workers: int
    autoscale_max: int

    def as_mapping(self) -> dict[str, object]:
        return {
            "display_name": self.metadata.display_name,
            "module": self.metadata.module,
            "qualname": self.metadata.qualname,
            "function": self.metadata.function,
            "filename": self.metadata.filename,
            "first_lineno": self.metadata.first_lineno,
            "wait": self.wait,
            "runtime": self.runtime,
            "enqueued_at_monotonic": self.enqueued_at_monotonic,
            "started_at_monotonic": self.started_at_monotonic,
            "completed_at_monotonic": self.completed_at_monotonic,
            "started_at_wall": self.started_at_wall,
            "completed_at_wall": self.completed_at_wall,
            "backlog_at_enqueue": self.backlog_at_enqueue,
            "backlog_at_start": self.backlog_at_start,
            "backlog_at_finish": self.backlog_at_finish,
            "active_workers_start": self.active_workers_start,
            "busy_workers_start": self.busy_workers_start,
            "max_workers": self.max_workers,
            "autoscale_max": self.autoscale_max,
        }


SlowTaskReporter = Callable[[TaskRuntimeDetail, str], Awaitable[None]]
