"""历史游标、来源及生命周期；网页加载用宿主替身，不运行真实WebEngine。"""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from test_run_view import OrderedConnection


class Host(QWidget):
    page_ready = Signal(str, str)
    page_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.opened, self.unloaded = [], 0
        self.releasing = False

    def open(self, url, view_id):
        self.opened.append((url, view_id))

    def unload(self):
        self.unloaded += 1


def source(**changes):
    return dict(catalog_id="catalog", run_id="r", trajectory_artifact_id="trajectory", generation=1, **changes)


def snapshot(sample=8, revision=10, session="A", **changes):
    return dict(session_id=session, status="ready", source={"run_id": "r", "trajectory_artifact_id": "trajectory"},
                viewer_url="http://viewer:1234", revision=revision, last_command_seq=0, playing=False, speed=1.,
                cursor={"world_id": 0, "sample_index": sample, "episode": 0},
                worlds={"0": {"sample_count": 9, "last_run_decision": 12, "last_episode": 0, "last_decision": 12}},
                episodes={"0": [{"episode": 0, "first_sample_index": 0, "last_sample_index": 8, "complete": False}]},
                objects=[dict(object_id="uav", label="无人机", kind="entity", group_id="aircraft", group_slot=2,
                              role_id="pilot", model_id="cf2"),
                         dict(object_id="box", label="箱体", kind="environment", group_id=None, group_slot=None,
                              role_id=None, model_id=None)],
                diagnostics=dict(world_id=0, sample_index=sample, run_decision=12, episode=0, decision=12,
                    tick=240, sim_time=.48, phase="step", reset_source="none", world={"world_reward": 4., "costs": {"danger": 1.}},
                    roles={"pilot": {"action": {"motion": [[.1, .2, .3]], "radio": {"power_w": [[.5]], "channel": [[2]]}},
                        "reward": [3.], "costs": {"energy": [7.]}, "velocity_world": [[1., 2., 3.]],
                        "angular_velocity_body": [[4., 5., 6.]], "present": [True], "alive": [True],
                        "controllable": [True], "crashed": [False], "learning_done": [False]},},
                    objects={"uav": {"position": [float(sample), 0., 1.], "orientation_matrix": [1., 0., 0., 0., 1., 0., 0., 0., 1.], "role_id": "pilot", "role_slot": 0},
                             "box": {"position": [0., 0., 0.], "orientation_matrix": [1., 0., 0., 0., 1., 0., 0., 0., 1.], "role_id": None, "role_slot": None}}),
                camera_ready=False, export=None, error=None) | changes


def opened(qapp):
    from muse_gui.runs.replay_controller import ReplayController
    connection, host = OrderedConnection(), Host()
    controller = ReplayController(connection, host)
    controller.open(source())
    connection.answer("/replays", snapshot(), method="POST")
    return connection, host, controller


def test_replay_accepts_backward_seek_but_rejects_old_generation_and_session():
    from muse_gui.runs.replay_state import ReplayState
    state = ReplayState(source(), 1)
    assert state.apply(snapshot(), generation=1)
    assert state.apply(snapshot(2, 11), generation=1)
    assert state.cursor["sample_index"] == 2
    assert not state.apply(snapshot(8, 12), generation=0)
    assert not state.apply(snapshot(8, 12, session="B"), generation=1)
    assert not state.apply(snapshot(8, 10), generation=1)
    assert state.cursor["sample_index"] == 2


def test_seek_waits_for_applied_state_and_rejects_late_receipt(qapp):
    connection, host, controller = opened(qapp)
    controller.playback("seek", sample_index=2)
    assert connection.sent[-1] == ("/streams/replays/A", {"kind": "playback", "command": {
        "command_seq": 1, "kind": "seek", "sample_index": 2}})
    assert controller.state.cursor["sample_index"] == 8
    assert controller.state.requested_command["sample_index"] == 2
    connection.push("/replays/A", {"kind": "command_result", "session_id": "A", "command_seq": 1,
                                  "status": "applied", "revision": 11, "error": None})
    assert controller.state.requested_command is not None
    connection.push("/replays/A", {"kind": "state", **snapshot(2, 11, last_command_seq=1)})
    assert controller.state.cursor["sample_index"] == 2 and controller.state.requested_command is None
    controller.playback("seek", sample_index=3)
    connection.push("/replays/A", {"kind": "command_result", "session_id": "A", "command_seq": 1,
                                  "status": "error", "revision": 10, "error": "old"})
    assert controller.state.requested_command["command_seq"] == 2
    controller.close()
    assert host.unloaded == 1 and not connection.subscriptions
    assert not any(r[2].endswith("/cancel") for r in connection.requests)


@pytest.mark.parametrize("changed_connection", [False, True])
def test_late_create_never_revives_closed_page(qapp, changed_connection):
    from muse_gui.runs.replay_controller import ReplayController
    connection, host = OrderedConnection(), Host()
    controller = ReplayController(connection, host)
    controller.open(source())
    create = next(r for r in connection.requests if r[2] == "/replays")
    controller.close()
    if changed_connection:
        connection.state.update(generation=2, catalog_id="other")
        connection.state_changed.emit(connection.state.copy())
    connection.result_received.emit(1, create[0], {"ok": True, "data": snapshot()})
    assert not host.opened and not connection.subscriptions
    assert any(r[1:3] == ("DELETE", "/replays/A") for r in connection.requests) is not changed_connection


def test_catalog_change_prevents_replay_export_and_commands(qapp):
    connection, host, controller = opened(qapp)
    connection.state.update(generation=2, catalog_id="other")
    connection.state_changed.emit(connection.state.copy())
    before = len(connection.requests)
    controller.export({"world_id": 0, "episode": 0, "fps": 25., "width": 640, "height": 480})
    controller.playback("play")
    controller.open(source())
    assert len(connection.requests) == before and not connection.sent
    assert not controller.active and host.unloaded == 1


def test_export_requires_camera_and_completion_refreshes_original_artifacts(qapp):
    connection, host, controller = opened(qapp)
    published = []
    controller.artifact_published.connect(published.append)
    options = {"world_id": 0, "episode": 0, "fps": 25., "width": 640, "height": 480}
    controller.export(options)
    assert not any(r[2].endswith("/recordings") for r in connection.requests)
    connection.push("/replays/A", {"kind": "state", **snapshot(revision=11, camera_ready=True)})
    controller.export(options)
    request = next(r for r in connection.requests if r[2] == "/replays/A/recordings")
    assert request[3]["json"] == options
    connection.answer("/replays/A/recordings", {"export_id": "video", "status": "preparing"}, method="POST")
    controller.cancel_export()
    assert any(r[1:3] == ("DELETE", "/replays/A/recordings/video") for r in connection.requests)
    completed = {"kind": "state", **snapshot(revision=12, camera_ready=True, export={"export_id": "video",
        "status": "completed", "frames_written": 5, "total_frames": 5, "artifact_id": "mp4", "error": None})}
    connection.push("/replays/A", completed)
    connection.push("/replays/A", completed)
    assert len(published) == 1 and published[0]["run_id"] == "r" and published[0]["artifact_id"] == "mp4"
    controller.close()


def test_export_ack_keeps_slot_until_authoritative_terminal_state(qapp):
    connection, host, controller = opened(qapp)
    connection.push('/replays/A', {'kind': 'state', **snapshot(revision=11, camera_ready=True)})
    options = {'world_id': 0, 'episode': 0, 'fps': 25., 'width': 640, 'height': 480}
    controller.export(options)
    connection.answer('/replays/A/recordings', {'export_id': 'video', 'status': 'preparing'}, method='POST')
    assert not controller.can_export
    controller.export(options)
    assert not any(r[1:3] == ('POST', '/replays/A/recordings') for r in connection.requests)
    controller.close()


def test_command_error_after_applied_state_remains_visible(qapp):
    connection, host, controller = opened(qapp)
    controller.playback('seek', sample_index=99)
    connection.push('/replays/A', {'kind': 'state', **snapshot(revision=11, last_command_seq=1)})
    connection.push('/replays/A', {'kind': 'command_result', 'session_id': 'A', 'command_seq': 1,
                                  'status': 'error', 'revision': 11, 'error': 'Sample outside recorded range'})
    assert controller.state.cursor['sample_index'] == 8
    assert controller.state.error['message'] == 'Sample outside recorded range'
    controller.close()


def test_view_uses_same_sample_role_slot_and_nonstep_not_applicable(qapp, monkeypatch):
    from muse_gui.runs import replay_view
    monkeypatch.setattr(replay_view, "ViewerHost", Host)
    connection = OrderedConnection()
    view = replay_view.ReplayView(connection)
    view.open(source())
    connection.answer("/replays", snapshot(), method="POST")
    connection.answer("/runs/r", {"recording": {"config": {"every_n_decisions": 3}, "summary": {"reason": "capacity"}}})
    view.objects_table.selectRow(0)
    details = json.loads(view.object_details.toPlainText())
    assert details["sample_index"] == 8 and details["pose"]["position"] == [8., 0., 1.]
    assert details["role"]["action"]["radio"]["channel"] == [2]
    assert details["role"]["costs"] == {"energy": 7.}
    view.controller.playback("seek", sample_index=2)
    assert "定位中" in view.cursor_status.text() and view.controller.state.cursor["sample_index"] == 8
    state = snapshot(2, 11, last_command_seq=1)
    state["diagnostics"]["phase"] = "reset_after"
    state["diagnostics"]["roles"]["pilot"].update(action=None, reward=None, costs=None)
    connection.push("/replays/A", {"kind": "state", **state})
    details = json.loads(view.object_details.toPlainText())
    assert details["sample_index"] == 2 and details["pose"]["position"][0] == 2.
    assert details["role"]["action"] is None and "不适用" in view.diagnostic_note.text()
    view.objects_table.selectRow(1)
    assert json.loads(view.object_details.toPlainText())["role"] is None
    assert "无学习数据" in view.diagnostic_note.text()
    view.close_replay()


def test_replaced_source_closes_old_session_and_ignores_old_stream(qapp):
    connection, host, controller = opened(qapp)
    old = next(iter(connection.subscriptions))
    controller.open(source())
    assert any(r[1:3] == ("DELETE", "/replays/A") for r in connection.requests)
    connection.result_received.emit(1, old, {"ok": True, "data": {"kind": "state", **snapshot(1, 100)}})
    assert controller.state.cursor is None and not connection.subscriptions
    controller.close()


def test_results_open_trajectory_refresh_export_and_wait_for_release(qapp, monkeypatch):
    from muse_gui.runs import replay_view
    from muse_gui.runs.results_view import ResultsView
    monkeypatch.setattr(replay_view, 'ViewerHost', Host)
    connection = OrderedConnection()
    view = ResultsView(connection, source_url='http://server')
    view.show_runs(['r'])
    artifact = {'id': 'trajectory', 'run': 'r', 'kind': 'trajectory', 'path': 'trajectories/t.zip', 'size': 1, 'sha256': 'x'}
    connection.answer('/runs/r/artifacts', [artifact])
    view._refresh()
    assert view.replay_button.isEnabled()
    view.replay_button.click()
    connection.answer('/replays', snapshot(), method='POST')
    assert view.replay.viewer.opened == [('http://viewer:1234', 'A')]
    view.replay.controller.artifact_published.emit({'catalog_id': 'catalog', 'run_id': 'r', 'artifact_id': 'mp4', 'generation': 1})
    assert any(r[2] == '/runs/r/artifacts' for r in connection.requests)
    view.close()
    view.replay.viewer.releasing = True
    assert view.releasing and not connection.subscriptions
    connection.answer('/replays/A', None, method='DELETE')
    assert view.releasing
    view.replay.viewer.releasing = False
    assert not view.releasing
    assert not any(r[2].endswith('/cancel') for r in connection.requests)


def test_refresh_artifacts_replaces_old_pending_page(qapp):
    from muse_gui.runs.results_controller import ResultsController
    connection = OrderedConnection()
    controller = ResultsController(connection, source_url='http://server')
    controller.show_runs(['r'])
    old = next(r for r in connection.requests if r[2] == '/runs/r/artifacts')
    controller.refresh_artifacts()
    fresh = [r for r in connection.requests if r[2] == '/runs/r/artifacts' and r[0] != old[0]]
    assert len(fresh) == 1
    connection.result_received.emit(1, fresh[0][0], {'ok': True, 'data': [{'run': 'r', 'id': 'new'}]})
    connection.result_received.emit(1, old[0], {'ok': True, 'data': []})
    assert controller.artifacts['r'] == [{'run': 'r', 'id': 'new'}]
    controller.stop()


def test_real_g1_replay_envelopes_and_local_close(qapp, wait_until, websocket_server, tmp_path):
    import httpx2
    from test_connection import controller_for
    from muse_gui.runs.replay_controller import ReplayController
    requests, commands = [], []

    def http_handler(request):
        path = request.url.path
        requests.append((request.method, path, request.content))
        if request.method == 'DELETE':
            return httpx2.Response(204)
        if path == '/health':
            data = {'api_revision': 2, 'catalog_id': 'catalog', 'instance_id': 'server'}
        elif path == '/replays':
            assert json.loads(request.content) == {'run_id': 'r', 'trajectory_artifact_id': 'trajectory'}
            data = snapshot()
        elif path == '/runs/r':
            data = {'recording': {'config': {'every_n_decisions': 3}, 'summary': {'reason': 'capacity'}}}
        else:
            data = {'items': []}
        return httpx2.Response(201 if path == '/replays' else 200, json=data)

    def socket_handler(ws):
        assert ws.request.path == '/streams/replays/A'
        ws.send(json.dumps({'kind': 'state', **snapshot()}))
        command = json.loads(ws.recv(timeout=5))
        commands.append(command)
        ws.send(json.dumps({'kind': 'state', **snapshot(2, 11, last_command_seq=1)}))
        ws.send(json.dumps({'kind': 'command_result', 'session_id': 'A', 'command_seq': 1,
                            'status': 'applied', 'revision': 11, 'error': None}))
        ws.wait_closed()

    with websocket_server(socket_handler) as port:
        with controller_for(tmp_path, wait_until, http_handler) as connection:
            connection.connect({'base_url': f'http://127.0.0.1:{port}'})
            wait_until(lambda: connection.state['state'] == 'connected')
            host = Host()
            controller = ReplayController(connection, host)
            try:
                controller.open(source() | {'generation': connection.state['generation']})
                wait_until(lambda: controller.ready)
                controller.playback('seek', sample_index=2)
                wait_until(lambda: controller.state.cursor['sample_index'] == 2)
                assert commands == [{'kind': 'playback', 'command': {'command_seq': 1, 'kind': 'seek', 'sample_index': 2}}]
                controller.close()
                wait_until(lambda: not controller.releasing)
                assert host.unloaded == 1
                assert any(method == 'DELETE' and path == '/replays/A' for method, path, _ in requests)
                assert not any(path.endswith('/cancel') or method == 'PATCH' for method, path, _ in requests)
            finally:
                controller.close()
