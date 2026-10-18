from copy import deepcopy
import json
import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal, Qt
from PySide6.QtTest import QSignalSpy, QTest

from muse_gui.experiments.experiment_draft import ExperimentDraft


BASE = {'resource_id': 'env', 'revision': 1, 'sha256': 'a' * 64}
POSE = {'position': [1., 2., 3.], 'orientation_wxyz': [0.9238795325112867, 0., 0., 0.3826834323650898]}
BOX = {'label': 'box', 'pose': POSE, 'size_m': [2., 4., 6.], 'collision': True, 'rf_blocking': False}


@pytest.fixture(autouse=True)
def release_owned_scene_widgets(qapp):
    from muse_gui.scene.scene_editor_view import SceneEditorView
    before = set(qapp.allWidgets())
    yield
    owned = [widget for widget in set(qapp.allWidgets()) - before if isinstance(widget, SceneEditorView)]
    for widget in owned:
        widget.close()
        widget.deleteLater()
    for widget in owned:
        QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)


class Connection(QObject):
    result_received = Signal(int, str, dict)
    state_changed = Signal(dict)
    def __init__(self):
        super().__init__()
        self.state = {'state': 'connected', 'generation': 1}
        self.requests, self.intents, self.streams = [], [], []

    def request(self, token, method, path, **kwargs):
        self.requests.append((token, method, path, kwargs))

    def subscribe(self, token, path):
        self.streams.append((token, path))

    def unsubscribe(self, token):
        pass

    def send_stream(self, token, message):
        self.intents.append(deepcopy(message))


class Viewer(QObject):
    page_ready = Signal(str, str)
    page_failed = Signal(str)
    def __init__(self):
        super().__init__()
        self.opens, self.unloaded = [], False

    def open(self, url, view_id):
        self.opens.append((url, view_id))

    def unload(self):
        self.unloaded = True


def editor(qapp):
    from muse_gui.scene.scene_controller import SceneController
    from muse_gui.scene.scene_editor_view import SceneEditorView
    draft = ExperimentDraft()
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1, 'scenario': {
        'entity_groups': [{'group_id': 'uav', 'model_id': 'model', 'model_revision': 1, 'count': 3,
                           'placement_id': 'start', 'sensor_profile_id': 'none'}],
        'placements': {'start': [deepcopy(POSE) for _ in range(3)]},
        'command_targets_world': {'pilot': [[10., 0., 0.], [20., 0., 0.]]},
        'radio_geometry': {'endpoints': {'tx': {'group_id': 'uav', 'slots': [0, 2], 'device_id': 'rf'}},
                           'links': [], 'sensing': [{'sensing_id': 's', 'tx': 'tx', 'rx': 'tx',
                                                   'target': {'group_id': 'uav', 'slots': [1, 2]}}]}},
        'role_config': {'pilot': {'group_id': 'uav', 'slots': [2, 0]}}})
    draft.set_value(('environment_edit',), {'base': BASE, 'upserts': {'box-1': deepcopy(BOX)}, 'removed': []})
    view, connection, viewer = SceneEditorView(), Connection(), Viewer()
    controller = SceneController(draft, connection, view, viewer, parent=view)
    draft.setParent(view)
    connection.setParent(view)
    viewer.setParent(view)
    controller.set_environment_manifest({'base': BASE, 'objects': [
        {'object_id': 'ground', 'label': 'ground', 'editable': False, 'reason': 'plane', 'box': None}]})
    return draft, view, controller, connection, viewer


def test_text_focus_and_drag_undo(qapp, monkeypatch):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.session_id = 'session'
    controller.applied_revision = draft.revision
    controller.preview_status = 'ready'
    before = draft.document
    count = draft.undo_stack.count()
    for phase in ('start', 'update'):
        controller.accept_gesture({'kind': 'gesture', 'session_id': 'session', 'gesture': {
            'base_revision': draft.revision, 'object_id': 'box-1', 'gesture_id': 'g', 'phase': phase,
            'pose': {'position': [7., 8., 9.], 'orientation_wxyz': [1., 0., 0., 0.]}}})
    assert draft.document == before
    controller.accept_gesture({'kind': 'gesture', 'session_id': 'session', 'gesture': {
        'base_revision': draft.revision, 'object_id': 'box-1', 'gesture_id': 'g', 'phase': 'end',
        'pose': {'position': [7., 8., 9.], 'orientation_wxyz': [1., 0., 0., 0.]}}})
    assert draft.undo_stack.count() == count + 1
    assert draft.document['environment_edit']['upserts']['box-1']['size_m'] == [2., 4., 6.]
    assert '尚未' not in view.message.text()
    draft.undo_stack.undo()
    assert draft.document == before
    controller.select('box-1')
    view.show()
    view.position_fields[0].setFocus()
    view.position_fields[0].selectAll()
    QTest.keyClick(view.position_fields[0], Qt.Key.Key_Delete)
    assert 'box-1' in draft.document['environment_edit']['upserts']
    controller.close_preview()
    view.close()


def test_delete_slot_updates_references_once(qapp, monkeypatch):
    draft, view, controller, connection, viewer = editor(qapp)
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    original = draft.document
    count = draft.undo_stack.count()
    controller.remove_object('entity/uav/0')
    request = draft.document['request']
    assert draft.undo_stack.count() == count + 1
    assert request['role_config']['pilot']['slots'] == [1]
    assert request['scenario']['command_targets_world']['pilot'] == [[10., 0., 0.]]
    assert request['scenario']['radio_geometry']['endpoints']['tx']['slots'] == [1]
    assert request['scenario']['radio_geometry']['sensing'][0]['target']['slots'] == [0, 1]
    assert request['scenario']['entity_groups'][0]['count'] == 2
    draft.undo_stack.undo()
    assert draft.document == original
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: False)
    controller.remove_object('entity/uav/0')
    assert draft.document == original
    controller.close_preview()
    view.close()


def test_box_dimensions_and_pose_roundtrip(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select('box-1')
    original = deepcopy(POSE['orientation_wxyz'])
    view.position_fields[0].setText('12.3456789012345')
    view.position_fields[0].setModified(True)
    view.flush_inputs()
    box = draft.document['environment_edit']['upserts']['box-1']
    assert box['pose']['orientation_wxyz'] == original
    assert box['pose']['position'][0] == 12.3456789012345
    view.rotation_fields[2].setText('90')
    view.rotation_fields[2].setModified(True)
    view.flush_inputs()
    from scipy.spatial.transform import Rotation
    q = draft.document['environment_edit']['upserts']['box-1']['pose']['orientation_wxyz']
    assert abs(Rotation.from_quat([*q[1:], q[0]]).as_euler('xyz', degrees=True)[2] - 90) < 1e-9
    assert draft.document['environment_edit']['upserts']['box-1']['size_m'] == [2., 4., 6.]
    controller.close_preview()
    view.close()


def test_preview_old_revision_is_stale_and_viewer_url_reloads(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.session_id = 'session'
    controller.accept_state({'kind': 'state', 'session_id': 'session', 'status': 'ready',
        'applied_revision': draft.revision, 'viewer_url': 'http://localhost:1001', 'error': None,
        'objects': [{'object_id': 'box-1'}], 'sensor_omissions': []})
    assert viewer.opens[-1] == ('http://localhost:1001', 'session')
    previous = draft.revision
    controller.set_pose('box-1', {'position': [9., 2., 3.], 'orientation_wxyz': POSE['orientation_wxyz']})
    controller.accept_gesture({'kind': 'gesture', 'session_id': 'session', 'gesture': {
        'base_revision': previous, 'object_id': 'box-1', 'gesture_id': 'old', 'phase': 'end', 'pose': POSE}})
    assert draft.document['environment_edit']['upserts']['box-1']['pose']['position'][0] == 9.
    assert controller.stale
    controller.accept_state({'kind': 'state', 'session_id': 'session', 'status': 'ready',
        'applied_revision': draft.revision, 'viewer_url': 'http://localhost:1002', 'error': None,
        'objects': [{'object_id': 'box-1'}], 'sensor_omissions': []})
    assert viewer.opens[-1] == ('http://localhost:1002', 'session') and not controller.stale
    controller.close_preview()
    assert viewer.unloaded and any(item[1:3] == ('DELETE', '/previews/session') for item in connection.requests)
    view.close()


def test_unplaced_entities_are_not_previewed_and_pending_box_can_cancel(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.set_group_count('uav', 5)
    payload = controller.preview_payload()
    assert [item['slot'] for item in payload['entities']] == [0, 1, 2]
    assert all(item['pose'] == POSE for item in payload['entities'])
    count = draft.undo_stack.count()
    controller.add_box(deepcopy(BOX))
    assert draft.undo_stack.count() == count
    controller.cancel_placement()
    assert draft.undo_stack.count() == count
    controller.add_box(deepcopy(BOX))
    controller.confirm_placement(POSE)
    assert draft.undo_stack.count() == count + 1
    controller.close_preview()
    view.close()


@pytest.fixture
def empty_scene(qapp):
    from muse_gui.scene.scene_controller import SceneController
    from muse_gui.scene.scene_editor_view import SceneEditorView
    view = SceneEditorView()
    draft, connection, viewer = ExperimentDraft(), Connection(), Viewer()
    controller = SceneController(draft, connection, view, viewer, parent=view)
    for owned in (draft, connection, viewer):
        owned.setParent(view)
    yield draft, view, controller, connection, viewer
    controller.close_preview()


@pytest.mark.parametrize('previous_environment', [None, 'old-env', 'env'])
def test_explicit_preview_restore_loads_environment_selected_while_hidden(empty_scene, previous_environment):
    draft, view, controller, connection, viewer = empty_scene
    if previous_environment is not None:
        draft.set_value(('request',), {'environment_id': previous_environment, 'environment_revision': 1})
        token = connection.requests[-1][0]
        connection.result_received.emit(1, token, {'ok': True, 'data': {
            'base': {**BASE, 'resource_id': previous_environment}, 'objects': []}})
    controller.close_preview()
    connection.requests.clear()
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1})
    assert connection.requests == []

    controller.refresh_preview()
    if previous_environment != 'env':
        manifests = [item for item in connection.requests
                     if item[1:3] == ('GET', '/catalog/environments/env/1/editing')]
        assert manifests, '显式恢复应取得隐藏期间选择的环境manifest'
        connection.result_received.emit(1, manifests[-1][0], {'ok': True, 'data': {'base': BASE, 'objects': []}})
        controller.flush_preview()
    post = next(item for item in connection.requests if item[1:3] == ('POST', '/previews'))
    assert post[3]['json']['environment'] == BASE
    assert viewer.opens == []


def test_explicit_preview_rebuild_recovers_failed_manifest(empty_scene):
    draft, view, controller, connection, viewer = empty_scene
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1})
    token = connection.requests[-1][0]
    connection.result_received.emit(1, token, {'ok': False, 'error': {'message': 'manifest unavailable'}})
    assert 'manifest unavailable' in view.message.text()
    connection.requests.clear()

    controller.refresh_preview()
    manifests = [item for item in connection.requests
                 if item[1:3] == ('GET', '/catalog/environments/env/1/editing')]
    assert manifests, '失败后用户显式重建应重新取得manifest'
    connection.result_received.emit(1, manifests[-1][0], {'ok': True, 'data': {'base': BASE, 'objects': []}})
    controller.flush_preview()
    post = next(item for item in connection.requests if item[1:3] == ('POST', '/previews'))
    assert post[3]['json']['environment'] == BASE


@pytest.mark.parametrize('invalidation', ['hidden_epoch', 'connection_generation'])
def test_preview_restore_ignores_late_manifest_from_old_context(empty_scene, invalidation):
    draft, view, controller, connection, viewer = empty_scene
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1})
    old_token = connection.requests[-1][0]
    if invalidation == 'hidden_epoch':
        controller.close_preview()
        draft.set_value(('request', 'environment_id'), 'new-env')
        controller.refresh_preview()
        base = {**BASE, 'resource_id': 'new-env'}
    else:
        connection.state = {'state': 'disconnected', 'generation': 2}
        connection.state_changed.emit(connection.state)
        connection.state = {'state': 'connected', 'generation': 3}
        connection.state_changed.emit(connection.state)
        base = BASE
    current_token = connection.requests[-1][0]
    connection.requests.clear()

    connection.result_received.emit(1, old_token, {'ok': True, 'data': {'base': BASE, 'objects': []}})
    controller.flush_preview()
    assert connection.requests == []
    assert viewer.opens == []
    connection.result_received.emit(connection.state['generation'], current_token, {
        'ok': True, 'data': {'base': base, 'objects': []}})
    controller.flush_preview()
    post = next(item for item in connection.requests if item[1:3] == ('POST', '/previews'))
    assert post[3]['json']['environment'] == base


@pytest.mark.parametrize('action', ['duplicate', 'remove', 'tree_delete', 'page_delete'])
def test_pending_box_blocks_committed_object_intents(qapp, action):
    from qfluentwidgets import PushButton
    from muse_gui.scene.scene_editor_view import SceneEditorView
    view = SceneEditorView()
    item = dict(label=BOX['label'], editable=True, reason=None, kind='environment', box=BOX, pose=POSE)
    duplicates, removals = QSignalSpy(view.duplicate_requested), QSignalSpy(view.remove_requested)

    def invoke():
        if action in ('duplicate', 'remove'):
            label = '复制box' if action == 'duplicate' else '删除'
            button = next(button for button in view.findChildren(PushButton) if button.text() == label)
            button.click()
        else:
            QTest.keyClick(view.tree if action == 'tree_delete' else view, Qt.Key.Key_Delete)

    view.set_object('candidate', item, pending=True)
    invoke()
    assert duplicates.count() == removals.count() == 0
    assert view.confirm_button.isEnabled()
    if action in ('duplicate', 'remove'):
        label = '复制box' if action == 'duplicate' else '删除'
        assert not next(button for button in view.findChildren(PushButton) if button.text() == label).isEnabled()

    view.set_object('committed', item)
    invoke()
    signal = duplicates if action == 'duplicate' else removals
    assert signal.count() == 1 and signal.at(0) == ['committed']


def test_duplicate_box_confirm_and_delete_remain_single_undo_operations(qapp, monkeypatch):
    from qfluentwidgets import PushButton
    draft, view, controller, connection, viewer = editor(qapp)
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    controller.select('box-1')
    before, count = draft.document, draft.undo_stack.count()
    duplicate = next(button for button in view.findChildren(PushButton) if button.text() == '复制box')
    duplicate.click()
    assert draft.document == before and draft.undo_stack.count() == count
    view.confirm_button.click()
    identity = controller.selected
    assert identity != 'box-1'
    assert draft.document['environment_edit']['upserts'][identity]['label'] == 'box 副本'
    assert draft.undo_stack.count() == count + 1
    draft.undo_stack.undo()
    assert draft.document == before
    draft.undo_stack.redo()
    controller.select(identity)
    remove = next(button for button in view.findChildren(PushButton) if button.text() == '删除')
    remove.click()
    assert identity not in draft.document['environment_edit']['upserts']
    assert draft.undo_stack.count() == count + 2
    draft.undo_stack.undo()
    assert identity in draft.document['environment_edit']['upserts']
    controller.close_preview()


def test_preview_submission_is_throttled_to_latest(qapp, wait_until):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.set_pose('box-1', {'position': [2., 2., 3.], 'orientation_wxyz': POSE['orientation_wxyz']})
    controller.set_pose('box-1', {'position': [3., 2., 3.], 'orientation_wxyz': POSE['orientation_wxyz']})
    assert not any(item[1] == 'POST' for item in connection.requests)
    wait_until(lambda: any(item[1] == 'POST' for item in connection.requests))
    posts = [item for item in connection.requests if item[1] == 'POST']
    assert len(posts) == 1
    assert posts[0][3]['json']['revision'] == draft.revision
    assert posts[0][3]['json']['environment_edit']['upserts']['box-1']['pose']['position'][0] == 3.
    assert 'training_package_id' not in posts[0][3]['json']
    controller.close_preview()
    view.close()


def test_visibility_is_view_only_and_late_created_session_is_deleted(qapp, wait_until):
    draft, view, controller, connection, viewer = editor(qapp)
    before = draft.document
    controller.set_visibility('box-1', False)
    assert draft.document == before
    wait_until(lambda: any(item[1] == 'POST' for item in connection.requests))
    post = next(item for item in connection.requests if item[1] == 'POST')
    controller.close_preview()
    connection.result_received.emit(1, post[0], {'ok': True, 'data': {'session_id': 'late-created'}})
    assert any(item[1:3] == ('DELETE', '/previews/late-created') for item in connection.requests)
    assert controller.session_id is None and not connection.streams
    view.close()


def test_selected_group_pose_does_not_mutate_shared_placement(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    request = draft.document['request']
    other = deepcopy(request['scenario']['entity_groups'][0])
    other['group_id'] = 'mirror'
    request['scenario']['entity_groups'].append(other)
    draft.set_value(('request',), request)
    before = draft.document
    controller.set_pose('entity/uav/0', {'position': [44., 2., 3.], 'orientation_wxyz': POSE['orientation_wxyz']})
    changed = draft.document['request']['scenario']
    first, second = changed['entity_groups']
    assert first['placement_id'] != second['placement_id']
    assert changed['placements'][second['placement_id']][0] == POSE
    assert changed['placements'][first['placement_id']][0]['position'][0] == 44.
    draft.undo_stack.undo()
    assert draft.document == before
    controller.close_preview()
    view.close()


def test_readonly_object_and_nonfinite_editor_do_not_change_semantic_values(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    before = draft.document
    controller.set_pose('ground', POSE)
    assert draft.document == before
    controller.select('box-1')
    view.position_fields[0].setText('nan')
    view.position_fields[0].setModified(True)
    view.flush_inputs()
    expected = deepcopy(before)
    expected['pending_inputs']['scene.box-1'] = json.dumps({'position.0': 'nan'})
    assert draft.document == expected
    assert '有限' in view.message.text()
    controller.close_preview()
    view.close()


def test_initial_connection_check_is_not_reported_as_a_lost_preview(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    errors = QSignalSpy(controller.error)
    connection.state['state'] = 'checking'
    connection.state_changed.emit(connection.state)
    assert errors.count() == 0
    controller.close_preview()
    view.close()


def test_connected_last_seen_updates_do_not_restart_manifest_requests(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    before = list(connection.requests)
    connection.state['last_seen'] = '2026-10-18T00:00:00Z'
    connection.state_changed.emit(connection.state)
    assert connection.requests == before
    assert controller.preview_payload()['environment'] == BASE
    controller.close_preview()
    view.close()


def test_escape_cancels_pending_placement_even_in_numeric_input(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    before = draft.document
    controller.add_box(deepcopy(BOX))
    view.show()
    view.activateWindow()
    view.position_fields[0].setFocus()
    QTest.keyClick(view.position_fields[0], Qt.Key.Key_Escape)
    assert controller._pending_box is None and draft.document == before
    controller.close_preview()
    view.close()


def test_tree_delete_and_undo_share_the_draft_stack(qapp, monkeypatch):
    draft, view, controller, connection, viewer = editor(qapp)
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    controller.select('box-1')
    before = draft.document
    view.show()
    view.tree.setFocus()
    QTest.keyClick(view.tree, Qt.Key.Key_Delete)
    assert 'box-1' not in draft.document['environment_edit']['upserts']
    QTest.keyClick(view.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert draft.document == before
    controller.close_preview()
    view.close()


def test_compact_properties_and_reparented_tree_keep_edit_behavior(qapp, monkeypatch):
    from PySide6.QtWidgets import QHBoxLayout, QWidget
    draft, view, controller, connection, viewer = editor(qapp)
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    window = QWidget()
    layout = QHBoxLayout(window)
    view.layout().removeWidget(view.tree)
    view.tree.setFixedWidth(200)
    layout.addWidget(view.tree)
    layout.addWidget(QWidget(window), 1)
    view.setFixedWidth(300)
    layout.addWidget(view)
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    assert view.property_scroll.horizontalScrollBar().maximum() == 0
    assert view.minimumSizeHint().width() <= 300
    controller.select('box-1')
    before = draft.document
    view.tree.setFocus()
    QTest.keyClick(view.tree, Qt.Key.Key_Delete)
    assert 'box-1' not in draft.document['environment_edit']['upserts']
    QTest.keyClick(view.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert draft.document == before
    controller.close_preview()
    window.close()


def test_empty_scene_cannot_apply_a_count_to_a_nonexistent_group(qapp):
    from muse_gui.scene.scene_editor_view import SceneEditorView
    view = SceneEditorView()
    view.set_objects({}, [])
    assert not view.apply_count_button.isEnabled() and not view.group_count.isEnabled()
    view.close()


def test_disconnected_pairing_event_does_not_replay_view_intents(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.session_id = 'session'
    controller._stream = 'control'
    controller.preview_status = 'ready'
    controller._compiled = {'box-1'}
    controller.selected = 'box-1'
    connection.result_received.emit(1, 'control', {'ok': True, 'data': {
        'kind': 'view', 'session_id': 'session', 'event': {
            'kind': 'pairing', 'client_id': 1, 'paired': True, 'connected': False}}})
    assert connection.intents == []
    controller.close_preview()
    view.close()


def test_tree_key_receiver_is_not_blocked_by_another_windows_text_focus(qapp, wait_until, monkeypatch):
    from PySide6.QtWidgets import QLineEdit
    draft, view, controller, connection, viewer = editor(qapp)
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    controller.select('box-1')
    foreign = QLineEdit('another window input')
    foreign.show()
    foreign.activateWindow()
    foreign.setFocus()
    wait_until(lambda: qapp.focusWidget() is foreign)
    try:
        # QTest定向送给本树的真实KeyPress；不能被全局其它窗口的旧焦点吞掉。
        QTest.keyClick(view.tree, Qt.Key.Key_Delete)
        assert 'box-1' not in draft.document['environment_edit']['upserts']
    finally:
        controller.close_preview()
        foreign.close()
        foreign.deleteLater()
        QCoreApplication.sendPostedEvents(foreign, QEvent.Type.DeferredDelete)
        view.close()


def test_environment_change_releases_cpu_session_but_keeps_stale_image(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.session_id = 'old-session'
    controller._viewer_url = 'http://old-view'
    viewer.unloaded = False
    draft.set_value(('request', 'environment_id'), 'another-environment')
    assert controller.stale and controller.session_id is None
    assert not viewer.unloaded
    assert any(item[1:3] == ('DELETE', '/previews/old-session') for item in connection.requests)
    controller.close_preview()
    view.close()


def type_scene_text(field, text):
    field.selectAll()
    QTest.keyClicks(field, text)


def stage_schema_input(draft, parent, path, text='-', *, kind='number'):
    from muse_gui.experiments.schema_editor import SchemaEditor, at, path_text
    schema = SchemaEditor(parent)
    schema.value_reader = lambda path: at(draft.document, path)
    schema.input_committed.connect(lambda path, value, origin, subtree: draft.commit_input(
        path, value, pending_path=path_text(origin), pending_subtree=subtree))
    schema.pending_changed.connect(draft.stage_input)
    value = at(draft.document, path)
    schema.set_section({'path': path, 'schema': {'type': kind}, 'effective': value,
                        'origins': {}, 'missing': [], 'errors': []}, value, draft.document['pending_inputs'])
    type_scene_text(schema.root_field.editor, text)
    schema.flush()
    assert draft.document['pending_inputs'][path_text(path)] == text
    return schema


@pytest.mark.parametrize('operation,slot', [('remove', 1), ('reduce', 2)])
@pytest.mark.parametrize('confirmed', [False, True])
def test_g3_pending_is_confirmed_and_cancelled_by_g5_structure(qapp, monkeypatch, operation, slot, confirmed):
    draft, view, controller, connection, viewer = editor(qapp)
    request = draft.document['request']
    request['scenario']['placements']['other'] = [deepcopy(POSE)]
    draft.set_value(('request',), request)
    pose_path = ('request', 'scenario', 'placements', 'start', slot, 'position', 0)
    count_path = ('request', 'scenario', 'entity_groups', 0, 'count')
    other_path = ('request', 'scenario', 'placements', 'other', 0, 'position', 0)
    stage_schema_input(draft, view, pose_path, '1e-')
    stage_schema_input(draft, view, count_path)
    stage_schema_input(draft, view, other_path)
    stage_schema_input(draft, view, ('request', 'scenario', 'entity_groups', 0, 'model_revision'))
    controller.select(f'entity/uav/{slot}')
    type_scene_text(view.position_fields[0], '-')
    controller.select('box-1')
    type_scene_text(view.position_fields[0], '-')
    draft.stage_input('request.private', 'unfinished')
    before, count = draft.document, draft.undo_stack.count()
    notices = []
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: notices.extend(impacts) or confirmed)
    if operation == 'remove':
        controller.remove_object('entity/uav/0')
    else:
        controller.set_group_count('uav', 2)
    if confirmed:
        expected = {'scene.box-1': before['pending_inputs']['scene.box-1'], 'request.private': 'unfinished',
                    'request.scenario.placements.other.0.position.0': '-',
                    'request.scenario.entity_groups.0.model_revision': '-'}
        assert draft.document['pending_inputs'] == expected
        assert draft.undo_stack.count() == count + 1
        draft.undo_stack.undo()
        assert draft.document['request'] == before['request']
        assert draft.document['pending_inputs'] == expected
        draft.undo_stack.redo()
        assert draft.document['pending_inputs'] == expected
    else:
        assert draft.document == before
        assert draft.undo_stack.count() == count
    notice = '\n'.join(notices)
    assert f'request.scenario.placements.start.{slot}.position.0' in notice
    assert 'request.scenario.entity_groups.0.count' in notice and '1e-' in notice
    assert 'request.scenario.placements.other' not in notice
    controller.close_preview()


@pytest.mark.parametrize('operation', ['remove', 'reduce', 'remove_group'])
def test_g5_shared_placement_keeps_g3_raw_for_unchanged_group(qapp, monkeypatch, operation):
    draft, view, controller, connection, viewer = editor(qapp)
    request = draft.document['request']
    mirror = deepcopy(request['scenario']['entity_groups'][0])
    mirror['group_id'] = 'mirror'
    request['scenario']['entity_groups'].append(mirror)
    draft.set_value(('request',), request)
    stage_schema_input(draft, view, ('request', 'scenario', 'placements', 'start', 1, 'position', 0))
    stage_schema_input(draft, view, ('request', 'scenario', 'placements'), '{"start":', kind='json')
    stage_schema_input(draft, view, ('request', 'scenario', 'entity_groups', 0, 'count'))
    stage_schema_input(draft, view, ('request', 'scenario', 'entity_groups', 1, 'count'))
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    before = draft.document['request']
    if operation == 'remove':
        controller.remove_object('entity/uav/0')
    else:
        controller.set_group_count('uav', 0 if operation == 'remove_group' else 2)
    expected = {'request.scenario.placements.start.1.position.0': '-'}
    if operation == 'remove_group':
        expected['request.scenario.placements'] = '{"start":'
    else:
        expected['request.scenario.entity_groups.1.count'] = '-'
    assert draft.document['pending_inputs'] == expected
    assert draft.document['request']['scenario']['placements']['start'] == before['scenario']['placements']['start']
    draft.undo_stack.undo()
    assert draft.document['request'] == before
    assert draft.document['pending_inputs'] == expected
    controller.close_preview()


def test_g5_last_slot_cancels_g3_reindexed_groups_and_removed_reference_lists(qapp, monkeypatch):
    draft, view, controller, connection, viewer = editor(qapp)
    request = draft.document['request']
    mirror = deepcopy(request['scenario']['entity_groups'][0])
    mirror['group_id'], mirror['placement_id'] = 'mirror', 'mirror_start'
    request['scenario']['entity_groups'].append(mirror)
    request['scenario']['placements']['mirror_start'] = [deepcopy(POSE)]
    geometry = request['scenario']['radio_geometry']
    geometry['links'] = [{'link_id': 'link', 'tx': 'tx', 'rx': 'tx', 'gain': 2.}]
    request['scenario']['network'] = {
        'endpoint_limits': {'tx': {'power': 1.}, 'other': {'power': 2.}},
        'role_controls': {'pilot': 'tx'}, 'passive_endpoints': ['tx'],
        'background_emitters': [{'endpoints': ['tx'], 'power': 1.}],
        'communication_allocations': [{'link_id': 'link', 'rate': 1.}],
        'services': [{'link_id': 'link', 'rate': 1.}]}
    draft.set_value(('request',), request)
    affected = [
        ('scenario', 'entity_groups', 1, 'model_revision'),
        ('role_config', 'pilot', 'slots', 0),
        ('scenario', 'command_targets_world', 'pilot', 0, 0),
        ('scenario', 'radio_geometry', 'endpoints', 'tx', 'slots', 0),
        ('scenario', 'radio_geometry', 'links', 0, 'gain'),
        ('scenario', 'radio_geometry', 'sensing', 0, 'target', 'slots', 0),
        ('scenario', 'network', 'endpoint_limits', 'tx', 'power'),
        ('scenario', 'network', 'background_emitters', 0, 'power'),
        ('scenario', 'network', 'communication_allocations', 0, 'rate'),
        ('scenario', 'network', 'services', 0, 'rate')]
    for path in affected:
        stage_schema_input(draft, view, ('request', *path))
    stage_schema_input(draft, view, ('request', 'scenario', 'network', 'endpoint_limits', 'other', 'power'))
    notices = []
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: notices.extend(impacts) or True)
    before = draft.document['request']
    count = draft.undo_stack.count()
    controller.set_group_count('uav', 0)
    expected = {'request.scenario.network.endpoint_limits.other.power': '-'}
    assert draft.document['pending_inputs'] == expected
    assert draft.undo_stack.count() == count + 1
    assert draft.document['request']['scenario']['entity_groups'][0]['group_id'] == 'mirror'
    for path in affected:
        assert '.'.join(map(str, ('request', *path))) in '\n'.join(notices)
    draft.undo_stack.undo()
    assert draft.document['request'] == before
    assert draft.document['pending_inputs'] == expected
    draft.undo_stack.redo()
    assert draft.document['pending_inputs'] == expected
    controller.close_preview()


def test_scene_pending_survives_save_load_selection_and_unrelated_undo(qapp, tmp_path):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select('box-1')
    count = draft.undo_stack.count()
    type_scene_text(view.position_fields[0], '-')
    pending = draft.document['pending_inputs']['scene.box-1']
    assert json.loads(pending)['position.0'] == '-'
    assert draft.undo_stack.count() == count
    view.flush_inputs()
    controller.select('entity/uav/0')
    controller.select('box-1')
    assert view.position_fields[0].text() == '-'
    draft.set_value(('name',), 'other edit')
    draft.undo_stack.undo()
    assert draft.document['pending_inputs']['scene.box-1'] == pending
    assert view.position_fields[0].text() == '-'
    path = tmp_path / 'incomplete.json'
    draft.save(path)
    draft.new()
    draft.load(path)
    controller.select('box-1')
    assert view.position_fields[0].text() == '-'
    assert draft.document['environment_edit']['upserts']['box-1'] == BOX
    controller.close_preview()


def test_scene_valid_commit_clears_only_own_pending_in_one_undo(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    draft.stage_input('request.private', 'incomplete')
    controller.select('box-1')
    type_scene_text(view.position_fields[0], '-')
    assert 'scene.box-1' in draft.document['pending_inputs']
    count = draft.undo_stack.count()
    type_scene_text(view.position_fields[0], '8.25')
    view.flush_inputs()
    assert draft.undo_stack.count() == count + 1
    assert draft.document['pending_inputs'] == {'request.private': 'incomplete'}
    assert draft.document['environment_edit']['upserts']['box-1']['pose']['position'][0] == 8.25
    assert draft.document['environment_edit']['upserts']['box-1']['pose']['orientation_wxyz'] == POSE['orientation_wxyz']
    draft.undo_stack.undo()
    assert draft.document['pending_inputs'] == {'request.private': 'incomplete'}
    assert view.position_fields[0].text() == '1.0'
    draft.undo_stack.redo()
    assert view.position_fields[0].text() == '8.25'
    assert 'scene.box-1' not in draft.document['pending_inputs']
    controller.close_preview()


@pytest.mark.parametrize('identity', ['box-1', 'entity/uav/2'])
def test_scene_removal_cancels_own_pending_without_undo_resurrection(qapp, monkeypatch, identity):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select(identity)
    type_scene_text(view.position_fields[0], '-')
    key = 'scene.' + identity
    assert key in draft.document['pending_inputs']
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: False)
    controller.remove_object(identity)
    assert key in draft.document['pending_inputs']
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    count = draft.undo_stack.count()
    controller.remove_object(identity)
    assert draft.undo_stack.count() == count + 1
    assert key not in draft.document['pending_inputs']
    draft.undo_stack.undo()
    assert key not in draft.document['pending_inputs']
    controller.select(identity)
    assert view.position_fields[0].text() == '1.0'
    controller.close_preview()


def test_scene_unplaced_candidate_text_never_creates_hanging_pending(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    before = draft.document
    controller.add_box(deepcopy(BOX))
    type_scene_text(view.position_fields[0], '-')
    controller.cancel_placement()
    assert draft.document == before
    controller.close_preview()


def test_scene_count_reduction_cancels_only_removed_slots_pending(qapp, monkeypatch):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select('entity/uav/0')
    type_scene_text(view.position_fields[0], '-')
    controller.select('entity/uav/2')
    type_scene_text(view.position_fields[0], '-')
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: True)
    count = draft.undo_stack.count()
    controller.set_group_count('uav', 2)
    assert draft.undo_stack.count() == count + 1
    assert set(draft.document['pending_inputs']) == {'scene.entity/uav/0'}
    draft.undo_stack.undo()
    assert set(draft.document['pending_inputs']) == {'scene.entity/uav/0'}
    controller.close_preview()


def test_scene_entity_pending_commit_and_same_value_do_not_add_empty_undo(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select('entity/uav/0')
    type_scene_text(view.position_fields[0], '1e')
    view.flush_inputs()
    assert json.loads(draft.document['pending_inputs']['scene.entity/uav/0']) == {'position.0': '1e'}
    count = draft.undo_stack.count()
    type_scene_text(view.position_fields[0], '1.0')
    view.flush_inputs()
    assert draft.undo_stack.count() == count
    assert draft.document['pending_inputs'] == {}
    type_scene_text(view.position_fields[0], '13')
    view.flush_inputs()
    assert draft.undo_stack.count() == count + 1
    assert draft.document['request']['scenario']['placements']['start'][0]['position'][0] == 13
    draft.undo_stack.undo()
    assert draft.document['pending_inputs'] == {}
    controller.close_preview()


def test_scene_pending_keeps_multi_field_raw_text_and_flags_without_partial_commit(qapp):
    draft, view, controller, connection, viewer = editor(qapp)
    controller.select('box-1')
    type_scene_text(view.label_field, 'renamed')
    type_scene_text(view.rotation_fields[2], '1e')
    type_scene_text(view.size_fields[1], '-2')
    view.collision.setChecked(False)
    view.flush_inputs()
    raw = json.loads(draft.document['pending_inputs']['scene.box-1'])
    assert raw == {'label': 'renamed', 'rotation.2': '1e', 'size.1': '-2', 'collision': 'false'}
    assert draft.document['environment_edit']['upserts']['box-1'] == BOX
    controller.select('ground')
    controller.select('box-1')
    assert view.rotation_fields[2].text() == '1e'
    assert view.size_fields[1].text() == '-2'
    assert not view.collision.isChecked()
    count = draft.undo_stack.count()
    type_scene_text(view.rotation_fields[2], '90')
    type_scene_text(view.size_fields[1], '7')
    view.flush_inputs()
    assert draft.undo_stack.count() == count + 1
    box = draft.document['environment_edit']['upserts']['box-1']
    assert box['label'] == 'renamed' and box['size_m'] == [2., 7., 6.] and not box['collision']
    assert draft.document['pending_inputs'] == {}
    controller.close_preview()


@pytest.mark.parametrize('confirmed', [False, True])
def test_scene_reindex_confirms_cancellation_of_only_affected_raw_inputs(qapp, monkeypatch, confirmed):
    draft, view, controller, connection, viewer = editor(qapp)
    for identity in ('box-1', 'entity/uav/0', 'entity/uav/1', 'entity/uav/2'):
        controller.select(identity)
        type_scene_text(view.position_fields[0], '-')
    draft.stage_input('request.private', 'unfinished')
    before = draft.document
    count = draft.undo_stack.count()
    notices = []
    monkeypatch.setattr(view, 'confirm_removal', lambda impacts: notices.extend(impacts) or confirmed)
    controller.remove_object('entity/uav/1')
    notice = '\n'.join(notices)
    if confirmed:
        expected = {'scene.box-1', 'scene.entity/uav/0', 'request.private'}
        assert set(draft.document['pending_inputs']) == expected
        assert draft.undo_stack.count() == count + 1
        draft.undo_stack.undo()
        assert set(draft.document['pending_inputs']) == expected
        assert draft.document['request'] == before['request']
        draft.undo_stack.redo()
        assert set(draft.document['pending_inputs']) == expected
    else:
        assert draft.document == before
        assert draft.undo_stack.count() == count
    assert '未完成输入将取消' in notice
    assert 'entity/uav/1' in notice and 'entity/uav/2' in notice
    controller.close_preview()
