from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
from threading import Event, Thread

import pytest
from PySide6.QtCore import QObject, QSettings, Signal

from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.backend.muse_client import MuseClient
from muse_gui.catalog.package_archive import build_archive
from muse_gui.catalog.resources_view import ResourcesView
from muse_gui.experiments.experiment_draft import ExperimentDraft
from muse_gui.experiments.submission_controller import SubmissionController


class OrderedConnection(QObject):
    result_received = Signal(int, str, dict)
    state_changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.state = {"generation": 1, "state": "connected", "catalog_id": "catalog", "instance_id": "server"}
        self.requests = []
        self.has_pending_requests = False

    def request(self, request_id, method, path, **kwargs):
        self.requests.append((request_id, method, path, kwargs))

    def upload(self, request_id, path, archive, resource_id=None):
        assert Path(archive).is_file()
        self.requests.append((request_id, "POST", path, {"archive": archive, "resource_id": resource_id}))

    def answer(self, path, data=None, error=None):
        request = next(request for request in self.requests if request[2] == path)
        self.requests.remove(request)
        self.result_received.emit(1, request[0], {"ok": error is None, **({"data": data} if error is None else {"error": error})})


@pytest.fixture
def components(qapp, tmp_path):
    connection = OrderedConnection()
    draft = ExperimentDraft()
    draft.set_value(("catalog_id",), "catalog")
    draft.set_value(("name",), "captured label")
    draft.set_value(("request",), {"environment_id": "env", "environment_revision": 1,
                                   "training_package_id": "package", "training_package_revision": 1,
                                   "root_seed": 7, "explicit": {"value": 8}})
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    resources = ResourcesView(connection, settings, archive_dir=tmp_path / "archives")
    submission = SubmissionController(draft, connection, resources, flush=lambda: None)
    yield draft, connection, resources, submission
    submission.cancel()
    assert submission.close()
    assert resources.close_operations()
    resources.close()


def test_submission_keeps_captured_revision(components):
    draft, connection, resources, submission = components
    draft.set_value(("environment_edit",), {"base": {"resource_id": "env", "revision": 1, "sha256": "old"}, "upserts": {}, "removed": ["box"]})
    revision = draft.revision
    completed = []
    submission.completed.connect(lambda run, captured: completed.append((run, captured)))
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "registered")
    draft.set_value(("request", "root_seed"), 99)
    edited_after_capture = draft.document
    connection.answer("/catalog/environments/derive", {"resource_id": "derived", "revision": 1, "sha256": "derived-hash", "kind": "environment"})
    frozen = next(request for request in connection.requests if request[2] == "/experiments")
    assert frozen[3]["json"]["root_seed"] == 7
    assert frozen[3]["json"]["environment_id"] == "derived"
    assert frozen[3]["params"] == {"label": "captured label"}
    connection.answer("/experiments", {"experiment_id": "experiment"})
    run_request = next(request for request in connection.requests if request[2] == "/runs")
    assert run_request[3]["json"] == {"experiment_id": "experiment", "mode": "TRAIN", "max_decision_steps": 10}
    connection.answer("/runs", {"run_id": "r", "state": "QUEUED"})
    assert completed == [({"run_id": "r", "state": "QUEUED"}, revision)]
    assert draft.document == edited_after_capture


def test_cancel_during_derive_does_not_freeze_or_create_run(components):
    draft, connection, resources, submission = components
    draft.set_value(("environment_edit",), {"base": {"resource_id": "env", "revision": 1, "sha256": "old"}, "upserts": {}, "removed": ["box"]})
    before = draft.document
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "registered")
    submission.cancel()
    connection.answer("/catalog/environments/derive", {"resource_id": "derived", "revision": 1, "sha256": "hash", "kind": "environment"})
    assert not any(request[2] in ("/experiments", "/runs") for request in connection.requests)
    assert draft.document == before
    assert not submission.busy


def test_flush_precedes_capture_and_pending_text_blocks_start(components):
    draft, connection, resources, submission = components
    submission.flush = lambda: draft.set_pending("root_seed", "invalid text")
    failed = []
    submission.failed.connect(lambda stage, error: failed.append((stage, error)))
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "registered")
    assert failed[-1][0] == "pending_inputs"
    assert not connection.requests


def test_changed_source_requires_explicit_version_choice(components, tmp_path, wait_until):
    draft, connection, resources, submission = components
    source = tmp_path / "package"
    source.mkdir()
    entry = source / "entry.py"
    entry.write_text("version = 1\n", encoding="utf-8")
    old = build_archive(source, tmp_path / "old.zip")
    record = {"resource_id": "package", "revision": 1, "sha256": old["sha256"], "kind": "training_package"}
    resources.remember_source(record, source)
    entry.write_text("version = 2\n", encoding="utf-8")
    failed = []
    submission.failed.connect(lambda stage, error: failed.append((stage, error)))
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, None)
    wait_until(lambda: bool(failed))
    assert failed[-1][0] == "source_choice_required"
    assert failed[-1][1]["registered_sha256"] == old["sha256"]
    assert not connection.requests
    assert not submission.busy


def test_update_upload_is_once_and_edited_draft_is_not_overwritten(components, tmp_path, wait_until):
    draft, connection, resources, submission = components
    source = tmp_path / "package"
    source.mkdir()
    (source / "entry.py").write_text("new source", encoding="utf-8")
    current = build_archive(source, tmp_path / "current.zip")
    resources.remember_source({"resource_id": "package", "revision": 1, "sha256": "old", "kind": "training_package"}, source)
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "update")
    wait_until(lambda: any(request[2] == "/catalog/training-packages" for request in connection.requests))
    draft.set_value(("request", "root_seed"), 123)
    edited = draft.document
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "update")
    assert sum(request[2] == "/catalog/training-packages" for request in connection.requests) == 1
    connection.answer("/catalog/training-packages", {"resource_id": "package", "revision": 2, "sha256": current["sha256"], "kind": "training_package", "metadata": {"descriptor": "new"}})
    frozen = next(request for request in connection.requests if request[2] == "/experiments")
    assert frozen[3]["json"]["training_package_revision"] == 2
    assert frozen[3]["json"]["root_seed"] == 7
    assert draft.document == edited
    submission.cancel()
    connection.answer("/experiments", {"experiment_id": "already-saved"})
    assert not any(request[2] == "/runs" for request in connection.requests)
    assert submission.last_experiment_id == "already-saved"


def test_template_import_exposes_real_request_without_overwriting_draft(components):
    draft, connection, resources, submission = components
    ready = []
    resources.example_ready.connect(ready.append)
    before = draft.document
    resources.new_from_example("basic_flight")
    result = {"request": {"environment_id": "real-env", "environment_revision": 1}, "run_options": {"mode": "TRAIN"}, "resources": [], "research_status": {"learning": "not_evaluated"}}
    connection.answer("/catalog/examples/basic_flight/import", result)
    assert ready == [result]
    assert draft.document == before


def test_catalog_refresh_clears_stale_selection_before_async_response(components):
    draft, connection, resources, submission = components
    resources.accept_registered({"resource_id": "package", "revision": 1, "sha256": "hash", "kind": "training_package"})
    resources.table.selectRow(0)
    resources.refresh()
    assert resources.table.rowCount() == 0
    assert resources._selected() is None


@pytest.mark.parametrize("kind", ["training_package", "environment"])
def test_use_selected_resource_emits_independent_record_without_editing_draft(components, kind):
    draft, connection, resources, submission = components
    record = {"resource_id": "real-id", "revision": 3, "sha256": "hash", "kind": kind,
              "metadata": {"label": "registered source"}}
    resources.accept_registered(record)
    resources.kind_selector.setCurrentIndex(resources.kind_selector.findData(kind))
    resources.table.selectRow(0)
    before = draft.document
    selected = []
    resources.resource_selected.connect(selected.append)
    resources.use_selected_button.click()
    assert selected == [record]
    selected[0]["metadata"]["label"] = "receiver edit"
    assert resources._selected()["metadata"]["label"] == "registered source"
    assert record["metadata"]["label"] == "registered source"
    assert draft.document == before
    assert not connection.requests


@pytest.mark.parametrize("blocked", ["no_selection", "disconnected", "example"])
def test_use_selected_resource_rejects_missing_offline_and_template(components, blocked):
    draft, connection, resources, submission = components
    selected = []
    resources.resource_selected.connect(selected.append)
    if blocked == "disconnected":
        resources.accept_registered({"resource_id": "id", "revision": 1, "sha256": "hash", "kind": "training_package"})
        resources.table.selectRow(0)
        connection.state["state"] = "disconnected"
        expected = "连接"
    elif blocked == "example":
        resources.kind_selector.setCurrentIndex(resources.kind_selector.findData("example"))
        resources.refresh()
        connection.answer("/catalog/examples", {"items": [{"id": "basic_flight", "label": "真实模板", "research_status": {"learning": "not_evaluated"}}], "next_offset": None})
        resources.table.selectRow(0)
        expected = "模板"
    else:
        expected = "选择"
    resources.use_selected_button.click()
    assert selected == []
    assert expected in resources.status.text()


def test_template_import_unknown_error_is_not_lost_to_connection_signal(components):
    draft, connection, resources, submission = components
    resources.new_from_example("basic_flight")
    connection.state["state"] = "disconnected"
    connection.state_changed.emit(connection.state)
    connection.answer("/catalog/examples/basic_flight/import", error={"kind": "connection", "outcome": "unknown", "message": "response lost"})
    assert "unknown" in resources.status.text() and "response lost" in resources.status.text()


def test_registration_unknown_error_keeps_detail_and_does_not_retry(components, tmp_path, wait_until):
    draft, connection, resources, submission = components
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_bytes(b"content")
    resources.register_source("training_package", source)
    wait_until(lambda: any(request[2] == "/catalog/training-packages" for request in connection.requests))
    connection.state["state"] = "disconnected"
    connection.state_changed.emit(connection.state)
    connection.answer("/catalog/training-packages", error={"kind": "connection", "outcome": "unknown", "message": "registration response lost"})
    assert "registration response lost" in resources.status.text()
    assert "unknown" in resources.status.text()
    assert not connection.requests and not list(resources.archive_dir.iterdir())


def test_cancel_after_run_post_waits_for_actual_result(components):
    draft, connection, resources, submission = components
    completed = []
    submission.completed.connect(lambda run, revision: completed.append(run))
    submission.start({"mode": "INTERACTIVE", "max_decision_steps": 10}, "registered")
    connection.answer("/experiments", {"experiment_id": "experiment"})
    submission.cancel()
    assert submission.stage == "awaiting_run_result" and submission.busy
    connection.answer("/runs", {"run_id": "accepted-before-cancel", "state": "QUEUED"})
    assert completed == [{"run_id": "accepted-before-cancel", "state": "QUEUED"}]
    assert not connection.requests


def test_cancel_from_stage_signal_prevents_the_next_post(components):
    draft, connection, resources, submission = components
    submission.stage_changed.connect(lambda stage: submission.cancel() if stage == "creating_run" else None)
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "registered")
    connection.answer("/experiments", {"experiment_id": "saved-before-cancel"})
    assert not any(request[2] == "/runs" for request in connection.requests)
    assert not submission.busy
    assert submission.last_experiment_id == "saved-before-cancel"


def test_unchanged_associated_source_is_not_reregistered(components, tmp_path, wait_until):
    draft, connection, resources, submission = components
    source = tmp_path / "package"
    source.mkdir()
    (source / "entry.py").write_bytes(b"same source")
    current = build_archive(source, tmp_path / "current.zip")
    resources.remember_source({"resource_id": "package", "revision": 1, "sha256": current["sha256"], "kind": "training_package"}, source)
    captured = draft.document["request"]
    submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "update")
    wait_until(lambda: any(request[2] == "/experiments" for request in connection.requests))
    assert not any(request[2] == "/catalog/training-packages" for request in connection.requests)
    assert next(request for request in connection.requests if request[2] == "/experiments")[3]["json"] == captured
    submission.cancel()
    connection.answer("/experiments", {"experiment_id": "saved"})


def test_cancel_and_close_keep_archive_thread_until_it_finishes(components, tmp_path, wait_until, monkeypatch):
    from muse_gui.catalog import package_archive

    draft, connection, resources, submission = components
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_bytes(b"real contents")
    resources.remember_source({"resource_id": "package", "revision": 1, "sha256": "old", "kind": "training_package"}, source)
    entered, release = Event(), Event()
    original = package_archive.build_archive

    def delayed_archive(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(package_archive, "build_archive", delayed_archive)
    try:
        submission.start({"mode": "TRAIN", "max_decision_steps": 10}, "update")
        wait_until(entered.is_set)
        assert not submission.close()
        draft.set_value(("request", "root_seed"), 101)
        edited = draft.document
    finally:
        release.set()
        wait_until(lambda: not submission.busy)
    assert draft.document == edited
    assert not connection.requests
    assert not list(resources.archive_dir.iterdir())


@pytest.mark.parametrize("run_response", ["disconnect", "invalid_json"])
def test_unknown_submission_is_not_retried(qapp, tmp_path, wait_until, run_response):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = {"api_revision": 2, "catalog_id": "catalog", "instance_id": "server"} if self.path == "/health" else {"items": [], "next_offset": None}
            self.reply(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path == "/runs":
                if run_response == "disconnect":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(b"{")
            else:
                self.reply({"experiment_id": "saved-before-disconnect"})

        def reply(self, data):
            raw = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = ConnectionController(cache_dir=tmp_path / "cache")
    resources = ResourcesView(connection, QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat), archive_dir=tmp_path / "archives")
    draft = ExperimentDraft()
    draft.set_value(("catalog_id",), "catalog")
    draft.set_value(("request",), {"root_seed": 7})
    submission = SubmissionController(draft, connection, resources, flush=lambda: None)
    stages, failed = [], []
    submission.stage_changed.connect(stages.append)
    submission.failed.connect(lambda stage, error: failed.append((stage, error)))
    try:
        connection.connect({"base_url": f"http://127.0.0.1:{server.server_port}"})
        wait_until(lambda: connection.state["state"] == "connected" and not connection.has_pending_requests)
        submission.start({"mode": "TRAIN", "max_decision_steps": 1}, "registered")
        draft.set_value(("request", "root_seed"), 8)
        edited_after_capture = draft.document
        wait_until(lambda: bool(failed))
        assert sum(path == "/runs" for path, _ in requests) == 1
        assert stages[-1] == "submission_unknown"
        assert submission.last_experiment_id == "saved-before-disconnect"
        assert failed[-1][1]["experiment_id"] == "saved-before-disconnect"
        assert draft.document == edited_after_capture
    finally:
        submission.cancel()
        wait_until(submission.close)
        wait_until(lambda: connection.close(20))
        resources.close()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.skipif(not os.environ.get("MUSE_G4_URL"), reason="需显式指定真实资源/Experiment 验证服务；绝不创建 Run")
def test_real_templates_derive_inspect_validate_and_frozen_sources():
    client = MuseClient(os.environ["MUSE_G4_URL"], timeout=120)
    evidence = {"server": os.environ["MUSE_G4_URL"], "requests": [], "examples": {}}

    def send(method, path, **kwargs):
        assert not (method == "POST" and path == "/runs"), "真实服务禁止创建 Run"
        evidence["requests"].append({"method": method, "path": path, "params": kwargs.get("params")})
        return client.request(method, path, **kwargs)

    try:
        for example_id in ("basic_flight", "isac_cost", "hybrid_history_markov", "empty_environment"):
            imported = send("POST", f"/catalog/examples/{example_id}/import")
            if example_id == "empty_environment":
                assert "environment_id" in imported["request"] and "training_package_id" not in imported["request"]
                evidence["examples"][example_id] = imported
                continue
            request = deepcopy(imported["request"])
            if example_id == "basic_flight":
                editing = send("GET", f"/catalog/environments/{request['environment_id']}/{request['environment_revision']}/editing")
                edit = {"base": editing["base"], "upserts": {"gui_g4_box": {
                    "label": "GUI G4 independent derive", "pose": {"position": [4., 4., .5], "orientation_wxyz": [1., 0., 0., 0.]},
                    "size_m": [1., 1., 1.], "collision": True, "rf_blocking": True}}, "removed": []}
                derived = send("POST", "/catalog/environments/derive", json=edit)
                assert derived["metadata"]["derived_from"] == editing["base"]
                assert imported["request"]["environment_id"] == editing["base"]["resource_id"]
                request.update(environment_id=derived["resource_id"], environment_revision=derived["revision"])
                evidence["derived"] = derived
            inspected = send("POST", "/experiments/inspect", json=request)
            validated = send("POST", "/experiments/validate", json=request)
            assert validated["valid"]
            saved = send("POST", "/experiments", json=request, params={"label": f"GUI G4 {example_id}"})
            source = send("GET", f"/experiments/{saved['experiment_id']}/source")
            assert source == request
            evidence["examples"][example_id] = {"imported": imported, "inspection": inspected, "experiment": saved, "source": source}
        assert evidence["examples"]["isac_cost"]["imported"]["research_status"]["learning"] == "not_met"
        expected_labels = {value["experiment"]["experiment_id"]: f"GUI G4 {key}"
                           for key, value in evidence["examples"].items() if "experiment" in value}
        labels = {}
        offset = 0
        while True:
            page = send("GET", "/experiments", params={"offset": offset, "limit": 50})
            labels.update({item["id"]: item["label"] for item in page["items"] if item["id"] in expected_labels})
            if page["next_offset"] is None or labels.keys() == expected_labels.keys():
                break
            offset = page["next_offset"]
        assert labels == expected_labels
        evidence["saved_labels"] = labels
        evidence["post_runs"] = sum(item["method"] == "POST" and item["path"] == "/runs" for item in evidence["requests"])
        Path(".superpowers/integration/2026-10-18/g4-real-experiments.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        client.close()


@pytest.mark.skipif(not os.environ.get("MUSE_G4_URL"), reason="需显式指定真实资源验证服务")
def test_resources_view_real_async_registration_and_local_source_binding(qapp, tmp_path, wait_until):
    source = tmp_path / "environment"
    source.mkdir()
    (source / "environment.yaml").write_text('{"scene":"scene.mjcf"}', encoding="utf-8")
    (source / "scene.mjcf").write_text('<mujoco><worldbody><geom name="ground" type="plane" size="5 5 .1"/></worldbody></mujoco>', encoding="utf-8")
    expected = build_archive(source, tmp_path / "expected.zip")
    connection = ConnectionController(cache_dir=tmp_path / "cache")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    resources = ResourcesView(connection, settings, archive_dir=tmp_path / "archives")
    registered = []
    resources.resource_registered.connect(registered.append)
    try:
        connection.connect({"base_url": os.environ["MUSE_G4_URL"]})
        wait_until(lambda: connection.state["state"] == "connected" and not connection.has_pending_requests)
        resources.register_source("environment", source)
        wait_until(lambda: bool(registered))
        record = registered[0]
        assert record["sha256"] == expected["sha256"]
        association = resources.source_for("environment", record["resource_id"], record["revision"])
        assert association["path"] == str(source.absolute())
        assert association["reference"]["sha256"] == expected["sha256"]
        checked = []
        resources.source_checked.connect(checked.append)
        resources.check_source(record)
        wait_until(lambda: bool(checked))
        assert checked[0]["state"] == "unchanged"
        Path(".superpowers/integration/2026-10-18/g4-real-resource-view.json").write_text(json.dumps({"registered": record, "association": association, "checked": checked[0]}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        wait_until(resources.close_operations)
        wait_until(lambda: connection.close(20))
        resources.close()
