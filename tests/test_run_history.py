import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest
from PySide6.QtCore import QItemSelectionModel, QObject, Qt, Signal

from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.runs.history_view import HistoryView
from muse_gui.runs.results_view import ResultsView
from muse_gui.runs.results_controller import ResultsController


class OrderedConnection(QObject):
    result_received = Signal(int, str, dict)
    state_changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.state = {"generation": 1, "state": "connected", "catalog_id": "catalog", "instance_id": "server"}
        self.requests = []

    def request(self, request_id, method, path, **kwargs):
        self.requests.append((request_id, method, path, kwargs))

    def answer(self, path, data):
        request = next(r for r in self.requests if r[2] == path)
        self.requests.remove(request)
        self.result_received.emit(1, request[0], {"ok": True, "data": data})


def test_artifacts_remain_bound_to_original_catalog_and_generation(qapp, tmp_path):
    connection = OrderedConnection()
    controller = ResultsController(connection, source_url="http://original")
    controller.show_runs(["r"])
    artifact = {"id": "t", "run": "r", "kind": "trajectory", "path": "trajectories/t.zip", "size": 2, "sha256": "x"}
    connection.answer("/runs/r/artifacts", [artifact])
    origin = controller.artifact_source(artifact)
    assert origin["catalog_id"] == "catalog" and origin["generation"] == 1
    connection.state.update(catalog_id="other", generation=2)
    before = list(connection.requests)
    with pytest.raises(ValueError):
        controller.artifact_source(artifact)
    with pytest.raises(ValueError):
        controller.download_artifact(artifact, tmp_path / "old.zip")
    controller.refresh_artifacts()
    assert connection.requests == before
    controller.stop()


def test_history_copy_preserves_explicit_overrides(qapp):
    connection = OrderedConnection()
    view = HistoryView(connection)
    copied = []
    view.copy_requested.connect(copied.append)
    view.set_page({"items": [{"run_id": "r", "experiment_id": "e", "mode": "TRAIN", "state": "COMPLETED"}],
                   "next_offset": 50})
    view.table.selectRow(0)
    source = {"environment": {"resource_id": "env", "revision": 1}, "overrides": {"seed": 7}}
    view.copy_selected()
    connection.answer("/experiments/e/source", source)
    assert copied == [source]
    copied[0]["overrides"]["seed"] = 10
    assert source["overrides"]["seed"] == 7
    assert not any(request[2] == "/experiments/e" for request in connection.requests)
    view.close()


def test_history_filters_and_pagination_are_server_queries(qapp):
    connection = OrderedConnection()
    view = HistoryView(connection)
    view.mode_filter.setCurrentText("EVALUATE")
    view.state_filter.setCurrentText("FAILED")
    view.experiment_filter.setText("exp-id")
    view.refresh()
    request = connection.requests[-1]
    assert request[2] == "/runs"
    assert request[3]["params"] == {"mode": "EVALUATE", "state": "FAILED", "experiment_id": "exp-id", "offset": 0, "limit": 50}
    connection.answer("/runs", {"items": [], "next_offset": 50})
    view.next_page()
    assert connection.requests[-1][3]["params"]["offset"] == 50
    view.close()


@pytest.mark.parametrize('refresh_outcome', ['pending', 'error'])
def test_history_catalog_switch_blocks_old_selection_until_current_page(qapp, refresh_outcome):
    connection = OrderedConnection()
    view = HistoryView(connection)
    opened, compared, copied = [], [], []
    view.open_requested.connect(opened.append)
    view.compare_requested.connect(compared.append)
    view.copy_requested.connect(copied.append)
    old_page = {'items': [{'run_id': 'a1', 'experiment_id': 'ea1', 'mode': 'TRAIN', 'state': 'COMPLETED'},
                          {'run_id': 'a2', 'experiment_id': 'ea2', 'mode': 'TRAIN', 'state': 'COMPLETED'}],
                'next_offset': 50}
    view.set_page(old_page)
    view.table.selectRow(0)
    view.next_page()
    late = connection.requests[-1][0]
    connection.state.update(state='disconnected', generation=2)
    connection.state_changed.emit(connection.state.copy())
    connection.state.update(state='connected', generation=3, catalog_id='catalog-b')
    connection.state_changed.emit(connection.state.copy())
    view.refresh()
    current = connection.requests[-1][0]
    connection.result_received.emit(1, late, {'ok': True, 'data': old_page})
    if refresh_outcome == 'error':
        connection.result_received.emit(3, current, {'ok': False, 'error': {'message': 'query failed'}})
    before = list(connection.requests)
    try:
        view.table.selectRow(0)
        view.open_selected()
        view.copy_selected()
        view.table.selectionModel().select(view.table.model().index(1, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        view.compare_selected()
        assert not opened and not compared and not copied
        assert connection.requests == before
        assert view.table.rowCount() == 0 and not view.next_button.isEnabled()

        view.refresh()
        current = connection.requests[-1][0]
        new_page = {'items': [{'run_id': 'b1', 'experiment_id': 'eb1', 'mode': 'TRAIN', 'state': 'COMPLETED'},
                              {'run_id': 'b2', 'experiment_id': 'eb2', 'mode': 'TRAIN', 'state': 'COMPLETED'}],
                    'next_offset': None}
        connection.result_received.emit(3, current, {'ok': True, 'data': new_page})
        view.table.selectRow(0)
        view.open_selected()
        view.copy_selected()
        assert opened == ['b1'] and connection.requests[-1][2] == '/experiments/eb1/source'
        connection.result_received.emit(3, connection.requests[-1][0], {'ok': True, 'data': {'root_seed': 17}})
        assert copied == [{'root_seed': 17}]
        view.table.selectionModel().select(view.table.model().index(1, 0),
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        view.compare_selected()
        assert compared == [['b1', 'b2']]
    finally:
        view.close()


def test_results_ignore_stale_runs_and_close_is_local(qapp):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["old"])
    old_id = next(r[0] for r in connection.requests if r[2] == "/runs/old/metrics")
    view.show_runs(["new"])
    connection.result_received.emit(1, old_id, {"ok": True, "data": {"records": [{"payload": {"steps": 99}}], "next_offset": 7}})
    assert list(view.controller.series) == ["new"]
    assert view.controller.series["new"].total_records == 0
    with pytest.raises(ValueError):
        view.show_runs(["a", "b", "c"])
    view.close()
    assert not view.controller.active
    assert all(r[1] == "GET" for r in connection.requests)


def test_history_cursor_is_per_run_and_manual_pages_do_not_race_to_tail(qapp):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["a", "b"])
    controller = view.controller
    old_token = next(r[0] for r in connection.requests if r[2] == "/runs/a/metrics")
    connection.requests = [r for r in connection.requests if r[2] != "/runs/a/metrics"]
    controller.load_metric_history(123, run_id="a")
    request = next(r for r in connection.requests if r[2] == "/runs/a/metrics")
    assert request[3]["params"]["offset"] == 123
    connection.result_received.emit(1, old_token, {"ok": True, "data": {"records": [], "next_offset": 900}})
    connection.answer("/runs/a/metrics", {"records": [{"payload": {"steps": 2, "loss": 4}}],
                                         "next_offset": 300, "eof": False, "terminal": True, "availability": "available"})
    controller.poll()
    assert not any(r[2] == "/runs/a/metrics" for r in connection.requests)
    assert controller.series["a"].next_offset == 300
    assert controller.series["b"].next_offset == 0
    controller.next_metric_page("a")
    assert next(r for r in connection.requests if r[2] == "/runs/a/metrics")[3]["params"]["offset"] == 300
    view.close()


def test_failed_read_stops_polling_instead_of_silent_retry(qapp):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["r"])
    token = next(r[0] for r in connection.requests if r[2] == "/runs/r/metrics")
    connection.result_received.emit(1, token, {"ok": False, "error": {"kind": "http", "status_code": 404, "message": "missing"}})
    before = len(connection.requests)
    view.controller.poll()
    assert len(connection.requests) == before
    assert not view.controller.active
    assert "missing" in view.message.text()
    view.close()


def test_results_batch_log_refresh_and_real_plot_data(qapp, wait_until):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["r"])
    records = [{"payload": {"steps": 1, "loss": 2}, "sim_time": .1, "wall_time": 1},
               {"payload": {"steps": 2, "loss": 3}, "sim_time": .2, "wall_time": 2}]
    connection.answer("/runs/r/metrics", {"records": records, "next_offset": 500, "eof": True,
                                           "terminal": True, "availability": "available"})
    raw = "中文\n\n\n".encode()
    connection.answer("/runs/r/logs", {"data_base64": base64.b64encode(raw).decode(), "next_offset": len(raw),
                                       "eof": True, "terminal": True, "availability": "available"})
    view.path_selector.setCurrentText("payload.loss")
    wait_until(lambda: "中文" in view.log_output.toPlainText() and bool(view.curves))
    x, y = view.curves["r"].getOriginalDataset()
    assert list(x) == [1, 2] and list(y) == [2, 3]
    assert "未知" in view.metric_status.text()
    assert view.log_output.toPlainText() == "中文\n\n"
    assert view.refresh_timer.interval() == 100
    assert view.refresh_timer.timerType() == Qt.TimerType.PreciseTimer
    assert view.log_output.maximumBlockCount() == 5000
    assert "5000" in view.log_status.text() and "1,048,576" in view.log_status.text()
    view.close()


def test_results_compare_diffs_read_server_source_and_definition(qapp, wait_until):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["a", "b"])
    for run_id, experiment_id, seed in [("a", "e1", 1), ("b", "e2", 2)]:
        connection.answer(f"/runs/{run_id}", {"run_id": run_id, "experiment_id": experiment_id,
                                             "mode": "TRAIN", "max_decision_steps": 100, "checkpoint": None})
        connection.answer(f"/experiments/{experiment_id}/source", {"overrides": {"seed": seed}})
        connection.answer(f"/experiments/{experiment_id}", {"seed": seed, "default_value": 9})
    wait_until(lambda: "seed" in view.diff_output.toPlainText())
    assert "source" in view.diff_output.toPlainText()
    assert "definition" in view.diff_output.toPlainText()
    view.close()


def test_two_run_plot_uses_parallel_unknown_units_then_overlay_for_declared_semantics(qapp, wait_until):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["a", "b"])
    for run in ("a", "b"):
        connection.answer(f"/runs/{run}/metrics", {"records": [{"payload": {"steps": 1, "loss": 4}}],
                                                   "next_offset": 100, "eof": True, "terminal": True,
                                                   "availability": "available"})
    wait_until(lambda: all(selector[0].count() for selector in view._selectors.values()))
    for selector in view._selectors.values():
        selector[0].setCurrentText("payload.loss")
    wait_until(lambda: "并列" in view.metric_status.text())
    assert len(view._plots) == 2
    for selector in view._selectors.values():
        selector[0].setCurrentText("payload.steps")
    wait_until(lambda: "叠加" in view.metric_status.text())
    assert len(view._plots) == 1 and set(view.curves) == {"a", "b"}
    view._selectors["b"][1].setCurrentText("updates")
    wait_until(lambda: "并列" in view.metric_status.text())
    assert len(view._plots) == 2
    view.close()


def test_log_history_is_manual_and_never_reuses_the_other_channel_cursor(qapp):
    connection = OrderedConnection()
    view = ResultsView(connection, source_url="http://server")
    view.show_runs(["r"])
    connection.requests = [r for r in connection.requests if not (r[2] == "/runs/r/logs" and r[3]["params"]["channel"] == "worker")]
    view.controller.load_log_history("r", "worker", 50)
    request = next(r for r in connection.requests if r[2] == "/runs/r/logs" and r[3]["params"]["channel"] == "worker")
    assert request[3]["params"]["offset"] == 50
    connection.requests.remove(request)
    connection.result_received.emit(1, request[0], {"ok": True, "data": {
        "data_base64": base64.b64encode(b"old\n").decode(), "next_offset": 54, "eof": False,
        "terminal": True, "availability": "available"}})
    view.controller.poll()
    assert not any(r[2] == "/runs/r/logs" and r[3]["params"]["channel"] == "worker" for r in connection.requests)
    assert view.controller.logs[("r", "viser")].next_offset == 0
    view.controller.next_log_page("r", "worker")
    assert next(r for r in connection.requests if r[2] == "/runs/r/logs" and r[3]["params"]["channel"] == "worker")[3]["params"]["offset"] == 54
    view.close()


@pytest.mark.parametrize("correct_hash", [True, False])
def test_real_artifact_download_keeps_complete_bytes_and_provenance(qapp, wait_until, tmp_path, correct_hash):
    raw = b"".join(json.dumps({"payload": {"steps": i}}).encode() + b"\n" for i in range(1, 12001))
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {"id": "r:metrics", "run": "r", "kind": "metrics", "path": "metrics.jsonl", "size": len(raw),
                "sha256": digest if correct_hash else "0" * 64}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/runs/r/artifacts/r%3Ametrics":
                body = raw
                content_type = "application/octet-stream"
            else:
                data = {"/health": {"api_revision": 2, "catalog_id": "c", "instance_id": "s"},
                        "/catalog/schema": {}, "/catalog/environments": {"items": []},
                        "/catalog/training-packages": {"items": []}, "/runs": {"items": [], "next_offset": None},
                        "/runs/r": {"run_id": "r", "experiment_id": "e"},
                        "/runs/r/artifacts": [manifest], "/experiments/e/source": {}, "/experiments/e": {},
                        "/runs/r/metrics?offset=0&limit=2000": {"records": [], "next_offset": 0, "eof": True, "terminal": True, "availability": "available"},
                        "/runs/r/logs?channel=worker&offset=0&limit=65536": {"data_base64": "", "next_offset": 0, "eof": True, "terminal": True, "availability": "available"},
                        "/runs/r/logs?channel=viser&offset=0&limit=65536": {"data_base64": "", "next_offset": 0, "eof": True, "terminal": True, "availability": "available"}}[self.path]
                body = json.dumps(data).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = ConnectionController(cache_dir=tmp_path / "cache")
    view = ResultsView(connection, source_url=f"http://127.0.0.1:{server.server_port}")
    completed = []
    view.controller.download_finished.connect(completed.append)
    try:
        connection.connect({"base_url": f"http://127.0.0.1:{server.server_port}"})
        wait_until(lambda: connection.state["state"] == "connected" and not connection.has_pending_requests)
        view.show_runs(['r'])
        wait_until(lambda: view.controller.artifacts.get('r') == [manifest])
        destination = tmp_path / "full.jsonl"
        view.controller.download_artifact(manifest, destination)
        wait_until(lambda: bool(completed))
        assert completed[0]["verified"] is correct_hash
        assert destination.read_bytes() == raw
        downloaded = [json.loads(line) for line in destination.read_bytes().splitlines()]
        assert len(downloaded) == 12000 and downloaded[-1]["payload"]["steps"] == 12000
        provenance = json.loads(Path(str(destination) + ".source.json").read_text(encoding="utf-8"))
        assert provenance["artifact"] == manifest
        assert provenance["catalog_id"] == "c" and provenance["instance_id"] == "s"
        assert provenance["actual_sha256"] == digest
        assert provenance["actual_size"] == len(raw)
        assert provenance["url"] == f"http://127.0.0.1:{server.server_port}/runs/r/artifacts/r%3Ametrics"
        wait_until(lambda: not view.controller.has_pending_downloads)
    finally:
        view.close()
        wait_until(lambda: connection.close(20))
        server.shutdown()
        thread.join()
        server.server_close()
