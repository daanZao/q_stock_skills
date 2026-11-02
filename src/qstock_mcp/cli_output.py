import json
import logging
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from time import monotonic
from typing import TextIO


class _CliProgress:
    def __init__(
        self,
        output: "CliOutput",
        task: str,
        total: int,
        every: int,
        interval: float,
    ):
        self._output = output
        self._task = task
        self._total = total
        self._every = every
        self._interval = interval
        self._last_logged = output._clock()

    def update(self, current: int, **context: object) -> None:
        now = self._output._clock()
        if (
            current % self._every == 0
            or current == self._total
            or now - self._last_logged >= self._interval
        ):
            self._output._write(self._task, f"{current}/{self._total}", **context)
            self._last_logged = now


class _CliLogHandler(logging.Handler):
    def __init__(self, output: "CliOutput", task: str):
        super().__init__()
        self._output = output
        self._task = task

    def emit(self, record: logging.LogRecord) -> None:
        self._output._write(
            self._task, record.levelname.lower(), message=record.getMessage()
        )


class CliOutput:
    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self._now = now or (lambda: datetime.now().astimezone())
        self._clock = clock

    def emit(self, payload: dict) -> None:
        print(
            json.dumps(payload, ensure_ascii=False),
            file=self._stdout if self._stdout is not None else sys.stdout,
        )

    def _write(self, task: str, event: str, **context: object) -> None:
        details = " ".join(f"{key}={value}" for key, value in context.items())
        print(
            f"{self._now().isoformat(timespec='seconds')} | {task} | {event} | {details}",
            file=self._stderr if self._stderr is not None else sys.stderr,
        )

    @contextmanager
    def logs(self, task: str) -> Iterator[None]:
        root = logging.getLogger()
        previous_handlers = root.handlers[:]
        previous_level = root.level
        root.handlers[:] = [_CliLogHandler(self, task)]
        root.setLevel(logging.INFO)
        try:
            yield
        finally:
            root.handlers[:] = previous_handlers
            root.setLevel(previous_level)

    @contextmanager
    def phase(
        self, task: str, *, total: int, every: int, interval: float
    ) -> Iterator[_CliProgress]:
        with self.logs(task):
            self._write(task, "start", total=total)
            progress = _CliProgress(self, task, total, every, interval)
            try:
                yield progress
            finally:
                self._write(task, "done", total=total)
