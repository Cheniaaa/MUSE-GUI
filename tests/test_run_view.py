"""真实 Qt 控件、可控 HTTP/WS 顺序；不启动研究 Run 或伪称浏览器验收。"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest

from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.runs.run_controller import RunController
from muse_gui.runs.run_state import RunState
from muse_gui.runs.run_view import RunView


class OrderedConnection(QObject):
    result_received = Signal(int, str, dict)
    state_changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.state = {"generation": 1, "state": "connected", "catalog_id": "catalog", "instance_id": "server"}
        self.requests, self.subscriptions, self.unsubscribed, self.sent = [], {}, [], []

    def request(self, token, method, path, **kwargs):
        self.requests.append((token, method, path, kwargs))

    def answer(self, path, data, *, method="GET", ok=True):
        request = next(r for r in self.requests if r[1:3] == (method, path))
        self.requests.remove(request)
        self.result_received.emit(self.state["generation"], request[0],
                                  {"ok": ok, "data" if ok else "error": deepcopy(data)})

    def subscribe(self, token, path):
        self.subscriptions[token] = path

    def unsubscribe(self, token):
        self.unsubscribed.append(token)
        self.subscriptions.pop(token, None)

    def send_stream(self, token, message):
        self.sent.append((self.subscriptions[token], deepcopy(message)))

    def push(self, suffix, data):
        token = next(key for key, path in self.subscriptions.items() if suffix in path)
        self.result_received.emit(self.state["generation"], token, {"ok": True, "data": deepcopy(data)})


class Host(QObject):
    page_ready = Signal(str, str)
    page_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.opened, self.unloaded = [], 0

    def open(self, url, view_id):
        self.opened.append((url, view_id))

    def unload(self):
        self.unloaded += 1


def record(run="r", mode="INTERACTIVE", state="RUNNING", interaction=None):
    return {"run_id": run, "experiment_id": "e", "mode": mode, "state": state,
            "event_seq": 2, "metrics": {}, "checkpoint": {"artifact_id": "input-cp", "sha256": "a" * 64},
            "max_decision_steps": 100, "interaction": interaction or {"command": None, "result": None}}


def display(world=1, seq=3, *, enabled=True, effective_seq=None, url="http://viewer:1234"):
    settings = {"enabled": enabled, "world_id": world, "rate_hz": 17.}
    return {"requested": settings.copy(), "effective": settings.copy(), "selection_seq": seq,
            "effective_seq": seq if effective_seq is None else effective_seq, "status": "ready",
            "viewer_url": url if enabled else None, "error": None, "frame": None,
            "interaction": {"command": None, "result": None}}


def frame(world=1, seq=3, episode=2, decision=9):
    return {"run_id": "r", "world_id": world, "selection_seq": seq, "episode": episode,
            "decision": decision, "tick": decision * 20, "sim_time": decision * .04,
            "scene_hash": "a" * 64, "nbody": 3}


def entities(identity=None, available=True):
    obj = {"object_id": "scout_0", "label": "无人机", "kind": "entity", "group_id": "scout",
           "group_slot": 0, "role_id": "pilot", "model_id": "cf2"}
    if available:
        obj["pose"] = {"position": [1., 2., 3.], "orientation_matrix": [1., 0., 0., 0., 1., 0., 0., 0., 1.]}
    return {"objects": [obj], "frame": identity or frame(), "available": available,
            "reason": None if available else "No live preview"}


def open_run(connection, controller, *, mode="INTERACTIVE", phase="RUNNING", new=False):
    controller.open("r", new_run=new)
    connection.answer("/runs/r", record(mode=mode, state=phase))
    connection.answer("/experiments/e", {"topology": {"world_count": 4}, "root_seed": 7})
    connection.answer("/experiments/e/source", {"overrides": {"seed": 7}})
    connection.answer("/runs/r/artifacts", [{"id": "output-cp", "run": "r", "kind": "checkpoint"}])
    connection.answer("/runs/r/display", display())
    connection.answer("/runs/r/entities", entities())


@pytest.fixture
def running(qapp):
    connection, host = OrderedConnection(), Host()
    controller = RunController(connection, host)
    view = RunView(controller)
    view.show()
    open_run(connection, controller)
    yield connection, host, controller, view
    controller.close_tab("r")
    view.close()


def test_commands_wait_for_result(running):
    connection, host, controller, view = running
    QTest.mouseClick(view.pause_button, Qt.MouseButton.LeftButton)
    command = next(r for r in connection.requests if r[2] == "/runs/r/commands")
    assert command[3]["json"] == {"command_seq": 1, "command": "pause"}
    assert controller.state.paused is None
    assert not view.pause_button.isEnabled()
    interaction = {"command": {"command_seq": 1, "command": "pause", "world_id": None}, "result": None}
    connection.answer("/runs/r/commands", interaction, method="POST")
    assert controller.state.command_status == "pending" and controller.state.paused is None
    assert "pending" in view.command_status.text()
    applied = {**interaction["command"], "status": "applied", "paused": True, "steps": 4, "updates": 0, "error": None}
    connection.push("/events", {"run_id": "r", "event_seq": 3, "kind": "command_result", "payload": applied})
    assert controller.state.command_status == "applied" and controller.state.paused is True
    QTest.mouseClick(view.step_button, Qt.MouseButton.LeftButton)
    rejected = {"command": {"command_seq": 2, "command": "step", "world_id": None},
                "result": {"command_seq": 2, "command": "step", "world_id": None, "status": "rejected",
                           "paused": True, "steps": 4, "updates": 0, "error": "not allowed"}}
    connection.answer("/runs/r/commands", rejected, method="POST")
    assert controller.state.command_status == "rejected"
    assert "not allowed" in view.command_status.text()


def test_command_conflict_reads_latest_without_automatic_retry(running):
    connection, _, controller, view = running
    controller.command("pause")
    connection.answer("/runs/r/commands", {"kind": "http", "status_code": 422, "message": "Expected command_seq 8"},
                      method="POST", ok=False)
    assert not view.pause_button.isEnabled()
    latest = {"command": {"command_seq": 7, "command": "resume", "world_id": None},
              "result": {"command_seq": 7, "command": "resume", "world_id": None, "status": "applied",
                         "paused": False, "steps": 5, "updates": 0}}
    connection.answer("/runs/r", record(interaction=latest))
    assert not any(r[1] == "POST" and r[2].endswith("/commands") for r in connection.requests)
    controller.command("pause")
    assert next(r for r in connection.requests if r[2].endswith("/commands"))[3]["json"]["command_seq"] == 8


def test_tab_close_preserves_run_and_discards_late_registration(running):
    connection, host, controller, view = running
    pending = next(r for r in connection.requests if r[2] == "/runs/r/display/clients")
    assert pending[3] == {}  # POST 不携带 GUI 编造的 view_id/body。
    before = list(connection.requests)
    controller.close_tab("r")
    assert not host.opened and not connection.subscriptions  # 登记尚在途，尚没有本 Run 网页可卸载。
    connection.answer("/runs/r/display/clients", {"view_id": "late-private", "connected": False}, method="POST")
    assert any(r[1:3] == ("DELETE", "/runs/r/display/clients/late-private") for r in connection.requests)
    assert not host.opened
    assert not any(r[2].endswith("/cancel") or r[1] == "PATCH" for r in before + connection.requests)
    assert controller.state.record["state"] == "RUNNING"


def test_reopen_uses_effective_display_and_private_intents(running):
    connection, host, controller, view = running
    connection.answer("/runs/r/display/clients", {"view_id": "private", "connected": False}, method="POST")
    assert host.opened == [("http://viewer:1234", "private")]
    host.page_ready.emit("http://viewer:1234", "private")
    assert not controller.state.pairing.get("paired")
    connection.push("/view?", {"kind": "pairing", "view_id": "private", "paired": True,
                                "connected": True, "client_id": 12})
    view.objects_table.selectRow(0)
    QTest.mouseClick(view.focus_button, Qt.MouseButton.LeftButton)
    assert connection.sent[-1] == ("/streams/runs/r/view?view_id=private", {"kind": "focus", "object_id": "scout_0"})
    controller.close_tab("r")
    assert host.unloaded == 1
    assert any(r[1:3] == ("DELETE", "/runs/r/display/clients/private") for r in connection.requests)
    view.world_spin.setValue(3)
    view.rate_spin.setValue(29.)
    open_run(connection, controller)
    assert controller.state.display["effective"]["world_id"] == 1
    assert controller.state.display["effective"]["rate_hz"] == 17.
    assert view.world_spin.value() == 1 and view.rate_spin.value() == 17.
    assert not any(r[1] == "PATCH" for r in connection.requests)


def test_new_run_enables_only_after_worker_stage_without_overriding_world_rate(qapp):
    connection, host = OrderedConnection(), Host()
    controller = RunController(connection, host)
    try:
        open_run(connection, controller, phase="QUEUED", new=True)
        assert not any(r[1] == "PATCH" for r in connection.requests)
        connection.push("/events", {"run_id": "r", "event_seq": 3, "kind": "preparing", "payload": {}})
        patches = [r for r in connection.requests if r[1] == "PATCH"]
        assert len(patches) == 1 and patches[0][3]["json"] == {"enabled": True}
        connection.push("/events", {"run_id": "r", "event_seq": 4, "kind": "compiling", "payload": {}})
        assert len([r for r in connection.requests if r[1] == "PATCH"]) == 1
    finally:
        controller.close_tab("r")


@pytest.mark.parametrize("mode", ["TRAIN", "EVALUATE"])
def test_frame_identity_and_readonly_modes(qapp, mode):
    connection, host = OrderedConnection(), Host()
    controller = RunController(connection, host)
    view = RunView(controller)
    try:
        open_run(connection, controller, mode=mode)
        assert not view.pause_button.isEnabled() and not view.reset_button.isEnabled()
        assert view.objects_table.editTriggers() == view.objects_table.EditTrigger.NoEditTriggers
        assert view.object_details.isReadOnly()
        controller.command("pause")
        assert not any(r[2].endswith("/commands") for r in connection.requests)
        controller.set_display({"world_id": 2})
        assert not controller.state.entities["available"]
        assert "position" not in view.object_details.toPlainText()
        changed = display(2, 4)
        connection.answer("/runs/r/display", changed, method="PATCH")
        connection.push("/entities", entities(frame(world=1, seq=3, episode=99)))
        assert not controller.state.entities["available"]
        connection.push("/entities", entities(frame(world=2, seq=4)))
        assert controller.state.entities["frame"]["world_id"] == 2
        connection.push("/entities", entities(frame(world=2, seq=4, episode=1, decision=100)))
        assert controller.state.entities["frame"]["episode"] == 2
        controller.view_intent({"kind": "edit", "object_id": "scout_0", "pose": {}})
        assert connection.sent == []
    finally:
        controller.close_tab("r")
        view.close()


def test_terminal_retains_identity_navigation_source_and_checkpoint(running):
    connection, host, controller, view = running
    connection.answer("/runs/r/display/clients", {"view_id": "private", "connected": False}, method="POST")
    requests = []
    view.continue_requested.connect(requests.append)
    view.checkpoint_selector.setCurrentIndex(1)
    QTest.mouseClick(view.continue_button, Qt.MouseButton.LeftButton)
    assert requests[0]["source"] == {"overrides": {"seed": 7}}
    assert requests[0]["checkpoint_artifact_id"] == "output-cp"
    assert requests[0]["record"]["checkpoint"]["artifact_id"] == "input-cp"
    connection.push("/events", {"run_id": "r", "event_seq": 3, "kind": "terminal", "payload": {"state": "COMPLETED"}})
    assert controller.state.record["state"] == "COMPLETED"
    assert not controller.state.entities["available"]
    assert controller.state.entities["objects"][0]["object_id"] == "scout_0"
    assert host.unloaded and not view.pause_button.isEnabled()
    assert view.results_button.isEnabled()
    assert not any(r[1] == "PATCH" or r[2].endswith("/cancel") for r in connection.requests)


def test_generation_and_page_failure_never_change_server_run_state(running):
    connection, host, controller, view = running
    connection.answer("/runs/r/display/clients", {"view_id": "private", "connected": False}, method="POST")
    host.page_failed.emit("page unavailable")
    assert "page unavailable" in view.viewer_status.text()
    assert controller.state.record["state"] == "RUNNING"
    old = next(token for token, path in connection.subscriptions.items() if path.endswith("/entities"))
    connection.state.update(state="disconnected", generation=2)
    connection.state_changed.emit(connection.state.copy())
    connection.result_received.emit(1, old, {"ok": True, "data": entities(frame(episode=99))})
    assert not controller.state.entities["available"]
    assert controller.state.record["state"] == "RUNNING"
    assert "断开" in view.connection_status.text()


def test_cancel_does_not_invent_terminal_state(running):
    connection, _, controller, view = running
    QTest.mouseClick(view.cancel_button, Qt.MouseButton.LeftButton)
    assert controller.state.record["state"] == "RUNNING"
    connection.answer("/runs/r/cancel", record(state="STOP_REQUESTED"), method="POST")
    assert controller.state.record["state"] == "STOP_REQUESTED"


def test_display_inflight_and_old_http_read_cannot_restore_previous_world(running):
    connection, _, controller, view = running
    view.objects_table.selectRow(0)
    controller.set_display({"world_id": 2})
    connection.push("/entities", entities(frame(world=1, seq=3, episode=99)))
    assert not controller.state.entities["available"]
    assert "position" not in view.object_details.toPlainText()
    connection.answer("/runs/r/display", display(2, 4), method="PATCH")
    connection.push("/entities", entities(frame(world=2, seq=4)))
    controller.refresh()
    connection.answer("/runs/r/display", display(1, 3))
    assert controller.state.display["requested"]["world_id"] == 2
    assert controller.state.entities["frame"]["world_id"] == 2


def test_ack_event_before_http_pending_does_not_lose_applied_state(running):
    connection, _, controller, view = running
    controller.command("pause")
    command = {"command_seq": 1, "command": "pause", "world_id": None}
    applied = {**command, "status": "applied", "paused": True, "steps": 1, "updates": 0}
    connection.push("/events", {"run_id": "r", "event_seq": 3, "kind": "command_result", "payload": applied})
    connection.answer("/runs/r/commands", {"command": command, "result": None}, method="POST")
    assert controller.state.paused is True and controller.state.command_status == "applied"
    assert view.pause_button.isEnabled()


def test_private_selection_rejects_old_world_and_episode(running):
    connection, _, controller, view = running
    connection.answer("/runs/r/display/clients", {"view_id": "private", "connected": False}, method="POST")
    connection.push("/view?", {"kind": "selection", "view_id": "private", "object_id": "scout_0", "frame": frame()})
    assert controller.state.selected_object == "scout_0"
    current = deepcopy(controller.state.selection_frame)
    for old in [frame(world=0), frame(episode=1, decision=99)]:
        connection.push("/view?", {"kind": "selection", "view_id": "private", "object_id": None, "frame": old})
        assert controller.state.selected_object == "scout_0" and controller.state.selection_frame == current


def test_unavailable_objects_never_expose_historical_pose_as_current(running):
    connection, _, controller, view = running
    view.objects_table.selectRow(0)
    connection.push("/entities", {**entities(), "available": False, "reason": "Last snapshot"})
    assert not controller.state.entities["available"]
    assert "position" not in view.object_details.toPlainText()


def test_conflict_read_supersedes_preexisting_poll_instead_of_using_stale_sequence(running):
    connection, _, controller, _ = running
    controller.refresh()
    old_token = next(r[0] for r in connection.requests if r[2] == "/runs/r")
    controller.command("pause")
    connection.answer("/runs/r/commands", {"kind": "http", "status_code": 422, "message": "Expected command_seq 8"},
                      method="POST", ok=False)
    reads = [r for r in connection.requests if r[2] == "/runs/r"]
    assert len(reads) == 2
    connection.result_received.emit(1, old_token, {"ok": True, "data": record()})
    assert controller.state.command_sync_required
    fresh = next(r for r in reads if r[0] != old_token)
    latest = {"command": {"command_seq": 7, "command": "pause", "world_id": None},
              "result": {"command_seq": 7, "command": "pause", "world_id": None, "status": "applied",
                         "paused": True, "steps": 5, "updates": 0}}
    connection.result_received.emit(1, fresh[0], {"ok": True, "data": record(interaction=latest)})
    controller.command("step")
    assert next(r for r in connection.requests if r[2].endswith("/commands"))[3]["json"]["command_seq"] == 8


def test_unavailable_transition_keeps_reset_frame_watermark(running):
    connection, _, controller, _ = running
    connection.push("/entities", {**entities(), "available": False, "reason": "Last snapshot"})
    connection.push("/entities", entities(frame(episode=1, decision=99)))
    assert not controller.state.entities["available"]


def test_display_frame_watermark_rejects_late_pre_reset_entities(running):
    connection, _, controller, _ = running
    controller.refresh()
    connection.answer("/runs/r/display", {**display(), "frame": frame(episode=3, decision=1)})
    connection.push("/entities", entities(frame(episode=2, decision=99)))
    assert not controller.state.entities["available"]


def test_cancel_response_supersedes_pre_cancel_run_poll(running):
    connection, _, controller, _ = running
    controller.refresh()
    old = next(r[0] for r in connection.requests if r[2] == "/runs/r")
    controller.cancel()
    connection.answer("/runs/r/cancel", record(state="STOP_REQUESTED"), method="POST")
    connection.result_received.emit(1, old, {"ok": True, "data": record()})
    assert controller.state.record["state"] == "STOP_REQUESTED"


def test_terminal_fetches_published_checkpoint_instead_of_initial_artifact_page(running):
    connection, _, controller, view = running
    connection.push("/events", {"run_id": "r", "event_seq": 3, "kind": "terminal", "payload": {"state": "COMPLETED"}})
    connection.answer("/runs/r/artifacts", [{"id": "final-cp", "run": "r", "kind": "checkpoint"}])
    view.checkpoint_selector.setCurrentIndex(1)
    assert view.checkpoint_selector.currentData() == "final-cp"


def test_open_other_run_clears_previous_pose_before_async_record_arrives(running):
    connection, _, controller, view = running
    view.objects_table.selectRow(0)
    assert "position" in view.object_details.toPlainText()
    controller.open("other")
    assert view.objects_table.rowCount() == 0
    assert "position" not in view.object_details.toPlainText()
    assert "other" in view.run_status.text()


def test_remote_selection_clear_clears_real_qt_table_selection(running):
    connection, _, controller, view = running
    connection.answer("/runs/r/display/clients", {"view_id": "private", "connected": False}, method="POST")
    view.objects_table.selectRow(0)
    connection.push("/view?", {"kind": "selection", "view_id": "private", "object_id": None, "frame": frame()})
    assert controller.state.selected_object is None
    assert not view.objects_table.selectionModel().selectedRows()


def test_late_patch_response_cannot_roll_back_applied_display_sequence(running):
    connection, _, controller, _ = running
    controller.set_display({"world_id": 2})
    controller.refresh()
    connection.answer("/runs/r/display", display(2, 4))
    connection.answer("/runs/r/display", display(2, 4, effective_seq=3), method="PATCH")
    assert controller.state.display["effective_seq"] == 4


def test_g1_disconnect_before_post_error_keeps_unknown_outcome_visible(running):
    connection, _, controller, view = running
    controller.command("pause")
    # G1 的真实顺序：先 state_changed(disconnected)，再 result_received(POST error)。
    connection.state["state"] = "disconnected"
    connection.state_changed.emit(connection.state.copy())
    connection.answer("/runs/r/commands", {"kind": "connection", "outcome": "unknown", "message": "connection lost"},
                      method="POST", ok=False)
    assert "unknown" in view.message.text()
    assert controller.state.record["state"] == "RUNNING"
    assert not controller.can_command
    assert not any(r[2] in {"/runs/r/commands", "/runs/r"} for r in connection.requests)


def test_real_ack_after_stream_disconnect_resolves_local_unknown_outcome(running):
    connection, _, controller, view = running
    controller.command("pause")
    connection.state["state"] = "disconnected"
    connection.state_changed.emit(connection.state.copy())
    command = {"command_seq": 1, "command": "pause", "world_id": None}
    applied = {**command, "status": "applied", "paused": True, "steps": 1, "updates": 0}
    connection.answer("/runs/r/commands", {"command": command, "result": applied}, method="POST")
    assert controller.state.command_status == "applied"
    assert controller.state.paused is True and not controller.state.connected
    assert "unknown" not in view.message.text()


def test_real_g1_http_boundary_keeps_close_local(qapp, wait_until, tmp_path):
    """真实 G1 HTTP 线程/序列化；流订阅另由确定性顺序用例覆盖。"""
    requests = []
    interaction = {"command": None, "result": None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path, None))
            if self.path == "/health":
                payload = {"api_revision": 2, "catalog_id": "catalog", "instance_id": "server"}
            elif self.path == "/runs/r":
                payload = record(interaction=interaction)
            elif self.path == "/runs/r/display":
                payload = display(enabled=False)
            elif self.path == "/runs/r/entities":
                payload = {"objects": [], "frame": None, "available": False, "reason": "Not started"}
            elif self.path == "/experiments/e":
                payload = {"topology": {"world_count": 4}}
            elif self.path == "/experiments/e/source":
                payload = {"seed": 7}
            else:
                payload = [] if self.path.endswith("/artifacts") else {"items": []}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, body))
            assert self.path == "/runs/r/commands"
            interaction.update(command={**body, "world_id": None}, result=None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(interaction).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = ConnectionController(cache_dir=tmp_path)
    # 此用例只核对 HTTP 真边界；并未声明它做过真实 Server/双浏览器。
    connection.subscribe = lambda *_: None
    host = Host()
    controller = RunController(connection, host)
    try:
        connection.connect({"base_url": f"http://127.0.0.1:{server.server_port}"})
        wait_until(lambda: connection.state["state"] == "connected")
        controller.open("r")
        wait_until(lambda: controller.state.display is not None and controller.state.source is not None)
        controller.command("pause")
        wait_until(lambda: any(method == "POST" for method, _, _ in requests) and not controller.state.submitting_command)
        assert controller.state.command_status == "pending" and controller.state.paused is None
        interaction["result"] = {**interaction["command"], "status": "applied", "paused": True, "steps": 2, "updates": 0}
        controller.refresh()
        wait_until(lambda: controller.state.paused is True)
        controller.close_tab("r")
        assert controller.state.record["state"] == "RUNNING"
        assert [body for method, path, body in requests if method == "POST"] == [{"command_seq": 1, "command": "pause"}]
        assert not any(method == "PATCH" or path.endswith("/cancel") for method, path, _ in requests)
    finally:
        controller.close_tab("r")
        wait_until(lambda: connection.close(30))
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_real_g1_stream_pairs_server_id_and_sends_direct_view_intent(qapp, wait_until, websocket_server, tmp_path):
    import httpx2
    from test_connection import controller_for

    received, requests = [], []
    def http_handler(request):
        path = request.url.path
        requests.append((request.method, path, request.content))
        if request.method == "DELETE":
            return httpx2.Response(204)
        if path.endswith("/display/clients"):
            assert request.content == b""
            data = {"view_id": "server-private", "connected": False}
        elif path == "/health":
            data = {"api_revision": 2, "catalog_id": "catalog", "instance_id": "server"}
        elif path == "/runs/r":
            data = record()
        elif path == "/runs/r/display":
            data = display()
        elif path == "/runs/r/entities":
            data = entities()
        elif path == "/runs/r/artifacts":
            data = []
        elif path == "/experiments/e":
            data = {"topology": {"world_count": 4}}
        elif path == "/experiments/e/source":
            data = {"seed": 7}
        else:
            data = {"items": []}
        return httpx2.Response(200, json=data)

    def socket_handler(ws):
        path = ws.request.path
        if path.endswith("/entities"):
            ws.send(json.dumps(entities()))
        elif "/view?" in path:
            assert path == "/streams/runs/r/view?view_id=server-private"
            ws.send(json.dumps({"kind": "pairing", "view_id": "server-private", "paired": True,
                                "connected": True, "client_id": 19}))
            received.append(json.loads(ws.recv(timeout=5)))
            ws.send(json.dumps({"kind": "selection", "view_id": "server-private", "object_id": "scout_0", "frame": frame()}))
        ws.wait_closed()

    with websocket_server(socket_handler) as port:
        with controller_for(tmp_path, wait_until, http_handler) as connection:
            connection.connect({"base_url": f"http://127.0.0.1:{port}"})
            wait_until(lambda: connection.state["state"] == "connected")
            host = Host()
            controller = RunController(connection, host)
            view = RunView(controller)
            try:
                controller.open("r")
                wait_until(lambda: controller.state.pairing.get("paired") and view.objects_table.rowCount() == 1)
                view.objects_table.selectRow(0)
                wait_until(lambda: controller.state.selection_frame is not None)
                assert received == [{"kind": "select", "object_id": "scout_0"}]
                assert host.opened == [("http://viewer:1234", "server-private")]
                controller.close_tab("r")
                wait_until(lambda: any(method == "DELETE" for method, _, _ in requests))
                assert not any(method == "PATCH" or path.endswith("/cancel") for method, path, _ in requests)
            finally:
                controller.close_tab("r")
                view.close()
