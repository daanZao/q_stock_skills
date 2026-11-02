import io
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from qstock_mcp.cli_output import CliOutput


def test_emit_writes_only_json_to_stdout():
    stdout = io.StringIO()
    stderr = io.StringIO()
    output = CliOutput(stdout=stdout, stderr=stderr)

    output.emit({"status": "ok", "note": "中文"})

    assert json.loads(stdout.getvalue()) == {"status": "ok", "note": "中文"}
    assert stdout.getvalue().endswith("\n")
    assert stderr.getvalue() == ""


def test_phase_reports_four_columns_and_always_finishes():
    stdout = io.StringIO()
    stderr = io.StringIO()
    now = datetime(2026, 10, 29, 15, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    output = CliOutput(
        stdout=stdout,
        stderr=stderr,
        now=lambda: now,
        clock=lambda: 0.0,
    )

    with output.phase("lhb.staging", total=2, every=20, interval=60) as progress:
        progress.update(1, date="20260417")
        progress.update(2, date="20260420")

    assert stderr.getvalue().splitlines() == [
        "2026-10-29T15:04:05+08:00 | lhb.staging | start | total=2",
        "2026-10-29T15:04:05+08:00 | lhb.staging | 2/2 | date=20260420",
        "2026-10-29T15:04:05+08:00 | lhb.staging | done | total=2",
    ]
    assert stdout.getvalue() == ""


def test_logs_capture_standard_warnings_in_four_columns():
    stderr = io.StringIO()
    now = datetime(2026, 10, 29, 15, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    output = CliOutput(stderr=stderr, now=lambda: now)

    with output.logs("lhb.backfill"):
        logging.getLogger("tests.cli_output").warning("seat %s", "missing")

    assert stderr.getvalue().splitlines() == [
        "2026-10-29T15:04:05+08:00 | lhb.backfill | warning | message=seat missing"
    ]


def test_phase_owns_warnings_emitted_inside_it():
    stderr = io.StringIO()
    now = datetime(2026, 10, 29, 15, 4, 5, tzinfo=timezone(timedelta(hours=8)))
    output = CliOutput(stderr=stderr, now=lambda: now, clock=lambda: 0.0)

    with output.phase("lhb.staging", total=1, every=20, interval=60):
        logging.getLogger("tests.cli_output").warning("bad row")

    assert stderr.getvalue().splitlines() == [
        "2026-10-29T15:04:05+08:00 | lhb.staging | start | total=1",
        "2026-10-29T15:04:05+08:00 | lhb.staging | warning | message=bad row",
        "2026-10-29T15:04:05+08:00 | lhb.staging | done | total=1",
    ]


def test_phase_reports_when_time_threshold_elapses():
    stderr = io.StringIO()
    ticks = iter([0.0, 0.0, 59.0, 60.0])
    output = CliOutput(stderr=stderr, clock=lambda: next(ticks))

    with output.phase("lhb.backfill", total=4, every=20, interval=60) as progress:
        progress.update(1)
        progress.update(2)
        progress.update(3, date="20260624")

    events = [line.split(" | ")[2] for line in stderr.getvalue().splitlines()]
    assert events == ["start", "3/4", "done"]


def test_phase_reports_done_when_work_raises():
    stderr = io.StringIO()
    output = CliOutput(stderr=stderr)

    with pytest.raises(RuntimeError, match="boom"):
        with output.phase("lhb.staging", total=1, every=20, interval=60):
            raise RuntimeError("boom")

    events = [line.split(" | ")[2] for line in stderr.getvalue().splitlines()]
    assert events == ["start", "done"]


def test_phase_reports_every_count_threshold_before_final_item():
    stderr = io.StringIO()
    output = CliOutput(stderr=stderr, clock=lambda: 0.0)

    with output.phase("lhb.backfill", total=21, every=20, interval=60) as progress:
        for current in range(1, 22):
            progress.update(current)

    events = [line.split(" | ")[2] for line in stderr.getvalue().splitlines()]
    assert events == ["start", "20/21", "21/21", "done"]
