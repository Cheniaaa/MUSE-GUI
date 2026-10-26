"""跨功能组装：晚到模板仍确认换稿，关闭只回收本客户端。"""
from copy import deepcopy
import json
import os

import pytest
from PySide6.QtCore import QSettings

from muse_gui.app.main_window import MuseWindow
from muse_gui.app.workbench_settings import WorkbenchSettings
from muse_gui.backend.connection_controller import ConnectionController
from muse_gui.experiments.experiment_draft import ExperimentDraft


@pytest.fixture
def integrated(qapp, tmp_path):
    from muse_gui.app.workbench_controller import WorkbenchController
    from muse_gui.app.main import mount_configuration
    draft = ExperimentDraft()
    window = MuseWindow(draft, WorkbenchSettings(QSettings(str(tmp_path / 'prefs.ini'), QSettings.Format.IniFormat)))
    connection = ConnectionController(cache_dir=tmp_path / 'cache')
    configuration = mount_configuration(window, draft, connection)
    controller = WorkbenchController(window, draft, connection, configuration)
    window.show()
    yield window, draft, connection, controller
    window._ask_unsaved = lambda: 'discard'
    assert controller.prepare_close()
    window.close()
    assert connection.close(5000)
    window.deleteLater()
    qapp.processEvents()


def test_late_example_import_requires_current_draft_confirmation(integrated):
    window, draft, connection, controller = integrated
    draft.set_value(('request', 'root_seed'), 91)
    before = deepcopy(draft.document)
    example = dict(request={'root_seed': 7, 'environment_id': 'env'}, run_options={'mode': 'TRAIN'},
                   resources=[], research_status={'learning': 'not_met'})
    window._ask_unsaved = lambda: 'cancel'
    controller.accept_example(example)
    assert draft.document == before
    window._ask_unsaved = lambda: 'discard'
    controller.accept_example(example)
    assert draft.document['draft_id'] != before['draft_id']
    assert draft.document['request'] == example['request']
    assert draft.document['run_options'] == {'mode': 'TRAIN'}
    assert draft.document['pending_inputs'] == {} and draft.document['environment_edit'] == {}
    assert 'not_met' in window.workbench.message_label.text()


def test_copy_source_keeps_explicit_input_and_independent_run_options(integrated):
    window, draft, _, controller = integrated
    source = {'algorithm_config': {}, 'task_config': {'nullable': None}, 'root_seed': 9}
    window._ask_unsaved = lambda: 'discard'
    controller.copy_source(source)
    assert draft.document['request'] == source
    assert draft.document['run_options'] == {}
    assert draft.document['environment_edit'] == {}
    source['task_config']['nullable'] = 'changed'
    assert draft.document['request']['task_config']['nullable'] is None


def test_copy_run_keeps_frozen_recording_config(integrated):
    window, draft, _, controller = integrated
    window._ask_unsaved = lambda: 'discard'
    recording = {'world_ids': [1], 'every_n_decisions': 3, 'max_data_bytes': 1024}
    context = {'record': {'max_decision_steps': 10, 'recording': {'config': recording}},
               'source': {'root_seed': 7}, 'checkpoint_artifact_id': None}
    controller.copy_run(context, 'EVALUATE')
    assert draft.document['run_options']['recording'] == recording
    recording['world_ids'].append(2)
    assert draft.document['run_options']['recording']['world_ids'] == [1]


def test_retired_results_and_close_wait_for_replay_release(integrated, monkeypatch):
    from types import SimpleNamespace
    _, _, _, controller = integrated
    retired = SimpleNamespace(controller=SimpleNamespace(has_pending_downloads=False), releasing=True,
                              deleteLater=lambda: None)
    controller._retired_results.append(retired)
    controller._retire_results()
    assert controller._retired_results == [retired]
    assert not controller.prepare_close()
    retired.releasing = False
    assert controller.prepare_close()


def test_successful_responses_do_not_restart_catalog_and_history_queries(integrated, monkeypatch):
    _, _, connection, controller = integrated
    requests = []
    monkeypatch.setattr(connection, 'request', lambda token, method, path, **kwargs:
                        requests.append((token, method, path, kwargs)))
    connection._set_state(state='connected', generation=1, catalog_id='catalog', instance_id='instance')
    catalog_request = next(item for item in requests if item[2] == '/catalog/training-packages'
                           and item[0].startswith('g4:'))
    history_request = next(item for item in requests if item[2] == '/runs')
    for token, _, _, _ in (catalog_request, history_request):
        connection.result_received.emit(1, token, {'ok': True, 'data': {'items': [], 'next_offset': None}})
    requested = len(requests)
    connection._set_state(last_seen='2026-10-18T10:00:00Z')
    connection._set_state(last_seen='2026-10-18T10:00:01Z')
    assert len(requests) == requested
    connection._set_state(state='disconnected')
    connection._set_state(state='connected', generation=2, instance_id='restarted')
    assert sum(path == '/runs' for _, _, path, _ in requests) == 2


def test_explicit_package_choice_rebinds_catalog_only_after_acceptance(integrated):
    _, draft, connection, controller = integrated
    draft.set_value(('catalog_id',), 'old-catalog')
    connection._state.update(catalog_id='new-catalog')
    record = {'kind': 'training_package', 'resource_id': 'new-package', 'revision': 1,
              'metadata': {'default_composition_id': None}}
    controller.configuration.confirm = lambda *_: False
    controller.select_resource(record)
    assert draft.document['catalog_id'] == 'old-catalog'
    controller.configuration.confirm = lambda *_: True
    controller.select_resource(record)
    assert draft.document['catalog_id'] == 'new-catalog'
    assert draft.document['request']['training_package_id'] == 'new-package'


def test_window_waits_for_local_resources_before_final_close(integrated, qapp):
    window, draft, _, controller = integrated
    ready = False
    calls = []
    # 独立关闭边界模拟仍在保存/下载的本客户端，实际窗口不得提前销毁。
    def close_resources():
        calls.append('close')
        return ready
    window.prepare_resources_close = close_resources
    assert window.close() is False and window.isVisible()
    assert calls == ['close']
    ready = True
    window.close()
    assert not window.isVisible()


def test_save_captures_scene_field_without_leaving_its_focus(integrated, wait_until, tmp_path):
    from PySide6.QtTest import QTest
    window, draft, _, controller = integrated
    controller.mount_views()
    draft.set_value(('request', 'scenario'), {
        'entity_groups': [{'group_id': 'aircraft', 'count': 1, 'placement_id': 'start'}],
        'placements': {'start': [{'position': [1., 2., 3.], 'orientation_wxyz': [1., 0., 0., 0.]}]},
    })
    controller.scene.select('entity/aircraft/0')
    field = controller.scene_view.position_fields[0]
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, '4.5')
    path = tmp_path / 'scene.muse.json'
    window._choose_save_path = lambda: path
    window.save_draft()
    wait_until(lambda: not draft.saving)
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['request']['scenario']['placements']['start'][0]['position'] == [4.5, 2., 3.]


def test_mounted_view_context_hides_draft_editors_and_can_close_run_tab(integrated, qapp):
    window, draft, _, controller = integrated
    controller.mount_views()
    window.workbench.set_page('scene')
    qapp.processEvents()
    assert controller.scene_view.isVisible()
    assert controller.scene_view.tree.parent() is window.workbench.tree_panel
    assert controller.draft_viewer.page is None
    controller.open_run('offline-run')
    qapp.processEvents()
    assert not controller.scene_view.isVisible()
    assert not controller.start_button.isVisible()
    assert controller.run_view.object_details.isVisible()
    controller.close_current_run()
    qapp.processEvents()
    assert window.workbench.context_kind == 'draft'
    assert controller.scene_view.isVisible()
    assert window.workbench.context_tabs.count() == 1


def test_results_follow_run_context_and_stop_polling_in_draft(integrated, monkeypatch):
    window, draft, connection, controller = integrated
    requests = []
    monkeypatch.setattr(connection, 'request', lambda *args, **kwargs: requests.append((args, kwargs)))
    connection._set_state(state='connected', generation=1, catalog_id='catalog', instance_id='instance')
    controller.mount_views()
    controller.show_results(['first', 'compared'])
    controller.open_run('second')
    assert controller.results.metric_run.currentText() == 'second'
    assert controller.results.metric_run.count() == 1
    window.workbench.set_context('draft', draft.document['draft_id'])
    assert not controller.results.controller.active
    controller.open_run('second')
    assert controller.results.controller.active
    assert controller.results.metric_run.currentText() == 'second'


def test_destroyed_workbench_does_not_leave_dead_fluent_routes(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    first = MuseWindow(ExperimentDraft(), WorkbenchSettings(QSettings(str(tmp_path / 'one.ini'), QSettings.Format.IniFormat)))
    first.switchTo(first.history_host)
    first.close()
    first.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    second = MuseWindow(ExperimentDraft(), WorkbenchSettings(QSettings(str(tmp_path / 'two.ini'), QSettings.Format.IniFormat)))
    try:
        second.workbench.set_context('run', 'new-run')
        second.workbench.close_run_context('new-run')
        assert second.workbench.context_kind == 'draft'
        assert second.workbench.context_tabs.count() == 1
    finally:
        second.close()
        second.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.skipif(os.environ.get('QT_QPA_PLATFORM') != 'windows', reason='验证 Windows 原生配色提示')
def test_fluent_theme_keeps_native_form_labels_readable(qapp, tmp_path, wait_until):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QLabel
    from qfluentwidgets import BodyLabel
    settings = WorkbenchSettings(QSettings(str(tmp_path / 'theme.ini'), QSettings.Format.IniFormat))
    native, fluent = QLabel('原生字段'), BodyLabel('Fluent 字段')
    try:
        for theme in ('dark', 'light'):
            settings.set_theme(theme)
            dark = theme == 'dark'
            def readable():
                palette = native.palette()
                background = palette.color(QPalette.ColorRole.Window).lightness()
                return all((label.palette().color(QPalette.ColorRole.WindowText).lightness() > background) == dark
                           for label in (native, fluent))
            wait_until(readable, timeout=1000)
    finally:
        settings.set_theme('auto')
        native.deleteLater()
        fluent.deleteLater()
