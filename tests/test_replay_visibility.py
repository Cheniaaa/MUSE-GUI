"""真实Qt可见性边界；受控网页宿主和连接，不启动WebEngine或研究Run。"""
from copy import deepcopy

import pytest
from PySide6.QtWidgets import QStackedWidget, QVBoxLayout, QWidget

from test_replay import Host, snapshot, source
from test_run_view import OrderedConnection


@pytest.fixture
def results(qapp, monkeypatch):
    from muse_gui.runs import replay_view
    from muse_gui.runs.results_view import ResultsView

    monkeypatch.setattr(replay_view, 'ViewerHost', Host)
    connection = OrderedConnection()
    view = ResultsView(connection, source_url='http://server')
    view.resize(1100, 650)
    yield connection, view
    view.close()


def test_switching_results_tab_unloads_page_but_export_and_cursor_continue(results, qapp):
    connection, view = results
    view.show()
    view.tabs.setCurrentWidget(view.replay)
    qapp.processEvents()
    view.replay.open(source())
    connection.answer('/replays', snapshot(camera_ready=True), method='POST')
    controller, host = view.replay.controller, view.replay.viewer
    controller.export({'world_id': 0, 'episode': 0, 'fps': 25., 'width': 640, 'height': 480})
    connection.answer('/replays/A/recordings', {'export_id': 'video', 'status': 'preparing'}, method='POST')
    session, subscriptions = controller.state, connection.subscriptions.copy()
    before = list(connection.requests)
    assert host.opened == [('http://viewer:1234', 'A')]

    view.tabs.setCurrentIndex(0)
    qapp.processEvents()
    assert not view.replay.isVisible() and host.unloaded == 1
    assert controller.active and controller.state is session
    assert connection.subscriptions == subscriptions and not connection.unsubscribed
    assert connection.requests == before
    export = {'export_id': 'video', 'status': 'rendering', 'frames_written': 1,
              'total_frames': 5, 'artifact_id': None, 'error': None}
    connection.push('/replays/A', {'kind': 'state', **snapshot(3, 11, camera_ready=False, export=export)})
    assert controller.state.cursor['sample_index'] == 3
    assert controller.state.data['export'] == export and controller.can_cancel_export
    assert len(host.opened) == 1 and host.unloaded == 1
    controller.playback('pause')
    assert connection.sent[-1] == ('/streams/replays/A', {
        'kind': 'playback', 'command': {'command_seq': 1, 'kind': 'pause'}})

    view.tabs.setCurrentWidget(view.replay)
    qapp.processEvents()
    assert host.opened == [('http://viewer:1234', 'A'), ('http://viewer:1234', 'A')]
    assert controller.state is session and controller.state.cursor['sample_index'] == 3
    assert controller.state.data['export'] == export
    assert connection.subscriptions == subscriptions and connection.requests == before


def test_nested_output_and_global_page_hiding_waits_for_all_ancestors(results, qapp):
    connection, view = results
    pages = QStackedWidget()
    workbench, other = QWidget(), QWidget()
    pages.addWidget(workbench)
    pages.addWidget(other)
    output = QWidget(workbench)
    QVBoxLayout(workbench).addWidget(output)
    QVBoxLayout(output).addWidget(view)
    view.tabs.setCurrentWidget(view.replay)
    pages.show()
    qapp.processEvents()
    try:
        view.replay.open(source())
        connection.answer('/replays', snapshot(), method='POST')
        host = view.replay.viewer
        before, subscriptions = list(connection.requests), connection.subscriptions.copy()
        output.hide()
        qapp.processEvents()
        assert not view.replay.isVisible() and host.unloaded == 1

        pages.setCurrentWidget(other)
        output.show()
        qapp.processEvents()
        connection.push('/replays/A', {'kind': 'state', **snapshot(2, 11)})
        assert not view.replay.isVisible() and len(host.opened) == 1
        assert host.unloaded == 1 and connection.requests == before
        assert connection.subscriptions == subscriptions and not connection.unsubscribed

        pages.setCurrentWidget(workbench)
        qapp.processEvents()
        assert view.replay.isVisible() and len(host.opened) == 2
        assert host.opened[-1] == ('http://viewer:1234', 'A')
        assert view.replay.controller.state.cursor['sample_index'] == 2
        pages.setCurrentWidget(other)
        qapp.processEvents()
        assert host.unloaded == 2
        pages.setCurrentWidget(workbench)
        qapp.processEvents()
        assert len(host.opened) == 3 and connection.requests == before
    finally:
        view.close()
        view.setParent(None)
        pages.close()


@pytest.mark.parametrize('delivery', ['create', 'stream'])
def test_ready_arriving_after_hide_waits_for_visibility(results, qapp, delivery):
    connection, view = results
    view.show()
    view.tabs.setCurrentWidget(view.replay)
    qapp.processEvents()
    view.replay.open(source())
    if delivery == 'stream':
        connection.answer('/replays', snapshot(status='preparing', viewer_url=None), method='POST')
    view.hide()
    qapp.processEvents()
    if delivery == 'create':
        connection.answer('/replays', snapshot(), method='POST')
    else:
        connection.push('/replays/A', {'kind': 'state', **snapshot(revision=11)})
    host, controller = view.replay.viewer, view.replay.controller
    assert controller.active and controller.ready
    assert host.opened == [] and host.unloaded == 0
    assert connection.subscriptions and not connection.unsubscribed
    assert not any(request[1] == 'DELETE' for request in connection.requests)
    view.show()
    qapp.processEvents()
    assert host.opened == [('http://viewer:1234', 'A')]


def test_initially_hidden_replay_defers_page_and_publishes_export_while_hidden(results, qapp):
    connection, view = results
    view.tabs.setCurrentWidget(view.replay)
    view.replay.open(source())
    export = {'export_id': 'video', 'status': 'publishing', 'frames_written': 5,
              'total_frames': 5, 'artifact_id': None, 'error': None}
    connection.answer('/replays', snapshot(export=export), method='POST')
    host, controller = view.replay.viewer, view.replay.controller
    assert not view.replay.isVisible() and not host.opened
    published = []
    controller.artifact_published.connect(published.append)
    completed = deepcopy(export) | {'status': 'completed', 'artifact_id': 'mp4'}
    connection.push('/replays/A', {'kind': 'state', **snapshot(2, 11, export=completed)})
    assert [item['artifact_id'] for item in published] == ['mp4']
    assert controller.state.cursor['sample_index'] == 2 and controller.state.data['export'] == completed
    assert not host.opened and connection.subscriptions and not connection.unsubscribed
    assert not any(request[1] == 'DELETE' for request in connection.requests)
    view.show()
    qapp.processEvents()
    assert host.opened == [('http://viewer:1234', 'A')]


def test_explicit_close_cannot_be_reopened_by_visibility_or_late_state(results, qapp):
    connection, view = results
    view.show()
    view.tabs.setCurrentWidget(view.replay)
    qapp.processEvents()
    view.replay.open(source())
    connection.answer('/replays', snapshot(), method='POST')
    stream = next(iter(connection.subscriptions))
    controller, host = view.replay.controller, view.replay.viewer
    view.replay.close_replay()
    before = list(connection.requests)
    view.hide()
    view.show()
    qapp.processEvents()
    connection.result_received.emit(1, stream, {'ok': True, 'data': {'kind': 'state', **snapshot(2, 11)}})
    assert not controller.active and not connection.subscriptions
    assert host.opened == [('http://viewer:1234', 'A')] and host.unloaded == 1
    assert connection.requests == before
    assert sum(request[1:3] == ('DELETE', '/replays/A') for request in connection.requests) == 1
