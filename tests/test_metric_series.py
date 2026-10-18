import base64
import json

import numpy as np
import pytest

from muse_gui.runs.metric_series import LogBuffer, MetricSelection, MetricSeries


def event(step, value=None):
    return {"run_id": "r", "event_seq": step, "kind": "metric", "sim_time": step / 10,
            "wall_time": 1700000000 + step, "payload": {"steps": step, "updates": step // 4,
                                                         "loss": step if value is None else value}}


def test_paginated_metrics_keep_all_requested_points(tmp_path):
    series = MetricSeries()
    artifact = tmp_path / "metrics.jsonl"
    with artifact.open("wb") as output:
        offset = 0
        for start in range(0, 12000, 2000):
            records = [event(i) for i in range(start + 1, start + 2001)]
            data = b"".join(json.dumps(record).encode() + b"\n" for record in records)
            output.write(data)
            offset += len(data)
            series.append_page({"records": records, "next_offset": offset, "eof": start == 10000,
                                "terminal": True, "availability": "available"})
    x, display_y = series.select(("payload", "loss"), "steps", None, None)
    downloaded = [json.loads(line) for line in artifact.read_bytes().splitlines()]
    assert len(display_y) == len(series.records) == 10000
    assert series.total_records == len(downloaded) == 12000
    assert downloaded[-1]["payload"]["steps"] == 12000
    assert (x[0], x[-1]) == (2001, 12000)
    assert series.next_offset == artifact.stat().st_size


def test_arrays_require_explicit_selection_and_missing_values_are_not_zero():
    series = MetricSeries()
    records = [event(1, [[1, 3], [5, 7]]), event(2), event(3, [[2, 4], [6, 8]])]
    del records[1]["payload"]["loss"]
    series.append_page({"records": records, "next_offset": 100})
    with pytest.raises(ValueError, match="数组"):
        series.select(("payload", "loss"), "steps", None, None)
    x, y = series.select(("payload", "loss"), "steps", (1, 0), None)
    np.testing.assert_equal(x, [1, 2, 3])
    np.testing.assert_equal(y, [5, np.nan, 6])
    _, mean = series.select(("payload", "loss"), "sim_time", None, "mean")
    np.testing.assert_equal(mean, [4, np.nan, 5])
    with pytest.raises(ValueError):
        series.select(("payload", "invented"), "steps", None, None)
    with pytest.raises(ValueError):
        series.select(("payload", "loss"), "steps", (0, 0), "mean")


def test_axes_are_full_event_fields_and_not_synthesized():
    series = MetricSeries()
    series.append_page({"records": [event(1), event(2)], "next_offset": 200})
    assert ("payload", "loss") in series.paths
    x, _ = series.select(("payload", "loss"), "wall_time", None, None)
    np.testing.assert_equal(x, [1700000001, 1700000002])
    series.append_page({"records": [], "next_offset": 200})
    assert series.total_records == 2


def test_compare_requires_matching_units_and_axes():
    selection = MetricSelection(("payload", "loss"), "steps", unit="m")
    assert selection.can_overlay(MetricSelection(("payload", "loss"), "steps", unit="m"))
    assert not selection.can_overlay(MetricSelection(("payload", "loss"), "steps", unit="s"))
    assert not selection.can_overlay(MetricSelection(("payload", "loss"), "updates", unit="m"))
    assert not selection.can_overlay(MetricSelection(("payload", "loss"), "steps"))
    assert not MetricSelection(("payload", "loss"), "steps").can_overlay(
        MetricSelection(("payload", "loss"), "steps"))
    assert not selection.can_overlay(MetricSelection(("payload", "loss"), "steps", (0,), unit="m"))


def log_page(data, offset, **extra):
    return {"data_base64": base64.b64encode(data).decode(), "next_offset": offset,
            "eof": False, "terminal": False, "availability": "available", **extra}


def test_log_gap_and_utf8_are_visible():
    log = LogBuffer()
    raw = "中文\n".encode()
    log.append_page(log_page(raw[:2], 2))
    log.append_page(log_page(raw[2:], len(raw)))
    assert log.text == "中文\n"
    log.append_page(log_page(b"after\xff\n", 30, gap={"previous_offset": len(raw), "next_offset": 23}))
    assert "after�" in log.text
    assert any("23" in detail and "缺口" in detail for detail in log.diagnostics)
    assert any("UTF-8" in detail and "ff" in detail for detail in log.diagnostics)


def test_log_window_is_bounded_and_terminal_partial_has_diagnostic():
    log = LogBuffer()
    raw = ("line\n" * 6000).encode() + b"\xe4"
    log.append_page(log_page(raw, len(raw), eof=True, terminal=True))
    assert len(log.text.splitlines()) <= 5000
    assert log.truncated
    assert any("UTF-8" in detail for detail in log.diagnostics)


def test_log_gap_does_not_join_unrelated_utf8_fragments():
    log = LogBuffer()
    log.append_page(log_page(b"\xe4", 1))
    log.append_page(log_page(b"\xb8\xad", 12, gap={"previous_offset": 1, "next_offset": 10}))
    assert "中" not in log.text
    assert log.diagnostics


@pytest.mark.parametrize("separator", ["", "\n"])
def test_many_long_log_pages_keep_only_bounded_unicode_tail(separator):
    log = LogBuffer()
    text = separator.join(["前" * 70001] * 9 + ["后" * 70001] * 9)
    raw = text.encode("utf-8") + b"\xff" + "结束".encode("utf-8")
    for start in range(0, len(raw), 65536):
        end = min(start + 65536, len(raw))
        log.append_page(log_page(raw[start:end], end, eof=end == len(raw), terminal=True))
        assert len(log.text) <= 1_048_576
        assert len(log.text.splitlines()) <= 5000
    assert log.text == (text + "�结束")[-1_048_576:]
    assert log.truncated
    assert log.next_offset == len(raw)
    assert any("UTF-8" in detail and "ff" in detail and str(len(text.encode("utf-8"))) in detail
               for detail in log.diagnostics)
