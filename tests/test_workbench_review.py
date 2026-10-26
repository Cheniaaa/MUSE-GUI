"""整体审查的跨功能边界回归；网络仅控制顺序，真实Draft/Qt控制器执行。"""
from copy import deepcopy
import json

import pytest

from test_run_view import display, native_slot, record
from test_workbench_integration import integrated
from test_viewer_host import webpage


@pytest.fixture
def transport(integrated, monkeypatch):
    _, _, connection, _ = integrated
    requests, subscriptions = [], {}
    monkeypatch.setattr(connection, 'request', lambda token, method, path, **kwargs:
                        requests.append((token, method, path, kwargs)))
    monkeypatch.setattr(connection, 'subscribe', lambda token, path: subscriptions.update({token: path}))
    monkeypatch.setattr(connection, 'unsubscribe', lambda token: subscriptions.pop(token, None))

    def answer(path, data, method='GET'):
        request = next(item for item in requests if item[1:3] == (method, path))
        requests.remove(request)
        connection.result_received.emit(connection.state['generation'], request[0], {'ok': True, 'data': deepcopy(data)})

    connection._set_state(state='connected', generation=1, catalog_id='catalog-a', instance_id='instance-a')
    return requests, subscriptions, answer


@pytest.mark.parametrize('catalog', ['catalog-a', 'catalog-b'])
def test_current_run_reconnects_only_to_its_catalog(integrated, transport, catalog):
    window, _, connection, controller = integrated
    requests, subscriptions, answer = transport
    controller.mount_views()
    controller.open_run('r')
    answer('/runs/r', record())
    assert controller.run.can_command
    connection._set_state(state='disconnected', generation=2)
    assert not controller.run.can_command
    requests.clear()
    connection._set_state(state='connected', generation=3, catalog_id=catalog, instance_id='restarted')
    assert window.workbench.context_identity == 'r'
    if catalog == 'catalog-a':
        assert any(item[1:3] == ('GET', '/runs/r') for item in requests)
        answer('/runs/r', record())
        answer('/runs/r/display', display(enabled=False, native=native_slot(world=2, seq=7)))
        assert controller.run.state.display['native']['effective']['world_id'] == 2
        assert not controller.run.state.display['viser']['effective']['enabled']
        assert controller.run_viewer.page is None
        assert not any(item[1:3] == ('POST', '/runs/r/display/clients') for item in requests)
        assert controller.run.can_command
        assert '/streams/runs/r/events' in subscriptions.values()
    else:
        assert not any(item[2].startswith('/runs/r') for item in requests)
        assert not controller.run.can_command
    assert not any(method == 'PATCH' or path.endswith(('/commands', '/cancel'))
                   for _, method, path, _ in requests)


@pytest.mark.parametrize('catalog', ['catalog-a', 'catalog-b', None])
def test_reselected_run_retains_source_and_local_snapshot(integrated, transport, catalog):
    window, draft, connection, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    controller.open_run('from-a')
    answer('/runs/from-a', record(run='from-a'))
    answer('/experiments/e/source', {'environment_id': 'env-from-a', 'environment_revision': 1})
    answer('/experiments/e', {'topology': {'world_count': 1}})
    connection._set_state(state='disconnected', generation=2)
    if catalog is not None:
        connection._set_state(state='connected', generation=3, catalog_id=catalog)
    window.workbench.set_context('draft', draft.document['draft_id'])
    requests.clear()
    controller.open_run('from-a')
    if catalog == 'catalog-a':
        assert any(item[1:3] == ('GET', '/runs/from-a') for item in requests)
    else:
        assert not any(item[2].startswith('/runs/from-a') for item in requests)
        assert controller.run.state.record['run_id'] == 'from-a'
        assert controller.run.state.source['environment_id'] == 'env-from-a'
        assert controller.run_view.details_button.isEnabled()
        assert controller.run_view.evaluate_button.isEnabled()
        assert not controller.run.can_command
        assert 'catalog-a' in controller.run_view.connection_status.text()


def test_old_run_results_button_rejects_foreign_catalog_without_replacing_snapshot(integrated, transport):
    window, _, connection, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    controller.open_run('from-a')
    answer('/runs/from-a', record(run='from-a'))
    window.workbench.set_page('config')
    controller.run_view.results_button.click()
    results = controller.results
    answer('/runs/from-a', record(run='from-a'))
    connection._set_state(state='disconnected', generation=2)
    connection._set_state(state='connected', generation=3, catalog_id='catalog-b')
    requests.clear()
    controller.run_view.results_button.click()
    assert requests == []
    assert controller.results is results
    assert results.controller.runs['from-a']['run_id'] == 'from-a'
    assert 'Catalog' in window.workbench.message_label.text()
    assert controller.run_view.message.isVisible()
    assert 'Catalog' in controller.run_view.message.text()


def test_reselected_snapshot_keeps_existing_error_and_shows_catalog(integrated, transport):
    window, draft, connection, controller = integrated
    _, _, answer = transport
    controller.mount_views()
    controller.open_run('from-a')
    answer('/runs/from-a', record(run='from-a'))
    error = {'operation': 'command', 'outcome': 'unknown', 'message': '命令结果未确认'}
    controller.run.state.error = deepcopy(error)
    connection._set_state(state='disconnected', generation=2)
    window.workbench.set_context('draft', draft.document['draft_id'])
    controller.open_run('from-a')
    assert controller.run.state.error == error
    assert 'catalog-a' in controller.run_view.connection_status.text()


def test_old_results_navigation_does_not_rebind_on_foreign_reselect(integrated, transport):
    window, draft, connection, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    controller.show_results(['from-a'])
    results = controller.results
    connection._set_state(state='disconnected', generation=2)
    connection._set_state(state='connected', generation=3, catalog_id='catalog-b')
    window.workbench.set_context('draft', draft.document['draft_id'])
    requests.clear()
    controller.open_run('from-a')
    assert requests == []
    assert controller.results is results
    assert not results.controller.active


@pytest.mark.parametrize('online', [False, True])
def test_copy_old_run_keeps_catalog_and_checkpoint_without_foreign_preview(integrated, transport, online):
    window, draft, connection, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    controller.open_run('from-a')
    answer('/runs/from-a', record(run='from-a'))
    source = {'environment_id': 'env-from-a', 'environment_revision': 1}
    answer('/experiments/e/source', source)
    answer('/experiments/e', {'topology': {'world_count': 1}})
    answer('/runs/from-a/artifacts', [{'id': 'checkpoint-from-a', 'kind': 'checkpoint'}])
    controller.run_view.checkpoint_selector.setCurrentIndex(1)
    connection._set_state(state='disconnected', generation=2)
    if online:
        connection._set_state(state='connected', generation=3, catalog_id='catalog-b')
    window._ask_unsaved = lambda: 'discard'
    requests.clear()
    controller.run_view.continue_button.click()
    controller.scene.refresh_preview()
    assert draft.document['catalog_id'] == 'catalog-a'
    assert draft.document['request'] == source
    assert draft.document['run_options']['checkpoint_artifact_id'] == 'checkpoint-from-a'
    assert not any(item[2].startswith(('/catalog/environments/env-from-a', '/previews')) for item in requests)
    if online:
        assert 'Catalog' in controller.scene_view.message.text()


@pytest.mark.parametrize('catalog', ['catalog-a', 'catalog-b'])
def test_draft_preview_reconnect_respects_draft_catalog(integrated, transport, catalog):
    _, draft, connection, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    draft.set_value(('request',), {'environment_id': 'env-from-a', 'environment_revision': 1})
    answer('/catalog/environments/env-from-a/1/editing', {
        'base': {'resource_id': 'env-from-a', 'revision': 1, 'sha256': 'a' * 64}, 'objects': []})
    connection._set_state(state='disconnected', generation=2)
    requests.clear()
    connection._set_state(state='connected', generation=3, catalog_id=catalog)
    controller.scene.flush_preview()
    scene_requests = [item for item in requests if item[0].startswith('scene:')]
    if catalog == 'catalog-a':
        assert any(item[1:3] == ('GET', '/catalog/environments/env-from-a/1/editing') for item in scene_requests)
    else:
        assert scene_requests == []
        assert 'Catalog' in controller.scene_view.message.text()


@pytest.mark.parametrize('destination', ['config', 'settings_host', 'history_host', 'resources_host'])
def test_hidden_run_viewport_is_released_and_restored_locally(integrated, transport, wait_until, destination):
    window, _, _, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    with webpage() as url:
        controller.open_run('r')
        answer('/runs/r', record())
        answer('/runs/r/display', display(url=url))
        answer('/runs/r/display/clients', {'view_id': 'first'}, 'POST')
        wait_until(lambda: controller.run.state.page_loaded, 20000)
        destroyed = []
        controller.run_viewer.profile.destroyed.connect(lambda: destroyed.append(True))
        if destination == 'config':
            window.workbench.set_page('config')
        else:
            window.switchTo(getattr(window, destination))
        try:
            wait_until(lambda: controller.run_viewer.page is None and bool(destroyed))
            assert controller.run.can_command
            assert any(item[1:3] == ('DELETE', '/runs/r/display/clients/first') for item in requests)
            window.workbench.set_page('scene')
            window.switchTo(window.workbench)
            wait_until(lambda: any(item[1:3] == ('POST', '/runs/r/display/clients') for item in requests))
            answer('/runs/r/display/clients', {'view_id': 'second'}, 'POST')
            wait_until(lambda: controller.run.state.page_loaded, 20000)
            assert controller.run_viewer.view_id == 'second'
            assert not any(method == 'PATCH' or path.endswith(('/commands', '/cancel'))
                           for _, method, path, _ in requests)
        finally:
            controller.run.close_tab('r')
            wait_until(lambda: not controller.run_viewer.releasing)


def test_registration_arriving_after_hiding_does_not_load_page(integrated, transport, wait_until):
    window, _, _, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    controller.open_run('r')
    answer('/runs/r', record())
    answer('/runs/r/display', display())
    window.workbench.set_page('config')
    answer('/runs/r/display/clients', {'view_id': 'late'}, 'POST')
    try:
        assert controller.run_viewer.page is None
        assert any(item[1:3] == ('DELETE', '/runs/r/display/clients/late') for item in requests)
    finally:
        controller.run.close_tab('r')
        wait_until(lambda: not controller.run_viewer.releasing)


def test_catalog_change_invalidates_other_categories_and_late_pages(integrated, transport):
    _, draft, connection, controller = integrated
    requests, _, answer = transport
    resources = controller.resources
    resources.resource_selected.disconnect(controller.select_resource)
    selected = []
    resources.resource_selected.connect(selected.append)
    old = {'resource_id': 'old-env', 'kind': 'environment', 'revision': 1, 'sha256': 'a'}
    resources.accept_registered(old)
    resources.kind_selector.setCurrentIndex(resources.kind_selector.findData('environment'))
    resources.next_page()
    resources.refresh()
    late = next(item[0] for item in requests if item[0].startswith('g4:') and item[2] == '/catalog/environments')
    resources.accept_registered(old)
    resources.table.selectRow(0)
    resources.kind_selector.setCurrentIndex(resources.kind_selector.findData('training_package'))
    before = draft.document
    connection._set_state(state='disconnected', generation=2)
    connection._set_state(state='connected', generation=3, catalog_id='catalog-b')
    resources.kind_selector.setCurrentIndex(resources.kind_selector.findData('environment'))
    assert resources.table.rowCount() == 0
    connection.result_received.emit(1, late, {'ok': True, 'data': {'items': [old], 'next_offset': None}})
    resources.use_selected_resource()
    assert not selected and draft.document == before
    assert resources.table.rowCount() == 0
    requests.clear()
    resources.refresh()
    new = {**old, 'resource_id': 'new-env'}
    answer('/catalog/environments', {'items': [new], 'next_offset': None})
    resources.table.selectRow(0)
    resources.use_selected_resource()
    assert selected == [new]


@pytest.mark.parametrize('kind', ['training_package', 'environment'])
def test_catalog_rebinding_clears_foreign_references_atomically(integrated, transport, monkeypatch, kind):
    from muse_gui.app import workbench_controller as app_module
    _, draft, connection, controller = integrated
    draft.set_value(('request',), {'training_package_id': 'old-package', 'training_package_revision': 2,
                                  'environment_id': 'old-env', 'environment_revision': 1, 'root_seed': 17})
    draft.set_value(('environment_edit',), {'base': {'resource_id': 'old-env'}, 'upserts': {}, 'removed': []})
    draft.set_value(('run_options',), {'mode': 'EVALUATE', 'max_decision_steps': 48, 'checkpoint_artifact_id': 'old-cp'})
    draft.stage_input('scene.old-box', json.dumps({'size.0': '-'}))
    draft.stage_input('request.topology.world_count', '-')
    if kind == 'training_package':
        draft.stage_input('request.task_config.bad', '-')
    before = draft.document
    connection._set_state(state='disconnected', generation=2)
    connection._set_state(state='connected', generation=3, catalog_id='catalog-b')
    choice = False
    notices = []
    controller.configuration.confirm = lambda title, text: (notices.append(text), choice)[1]
    monkeypatch.setattr(app_module.MessageBox, 'exec', lambda _: choice)
    item = {'resource_id': 'new-' + kind, 'kind': kind, 'revision': 1, 'metadata': {'default_composition_id': 'default'}}
    controller.select_resource(item)
    assert draft.document == before
    choice = True
    controller.select_resource(item)
    assert draft.document['catalog_id'] == 'catalog-b'
    assert draft.document['request'][kind + '_id'] == 'new-' + kind
    other = 'environment' if kind == 'training_package' else 'training_package'
    assert other + '_id' not in draft.document['request']
    assert other + '_revision' not in draft.document['request']
    assert draft.document['environment_edit'] == {}
    assert draft.document['run_options'] == {'mode': 'EVALUATE', 'max_decision_steps': 48}
    assert draft.document['pending_inputs'] == {'request.topology.world_count': '-'}
    assert any('old-env' in notice and 'old-cp' in notice for notice in notices)
    draft.undo_stack.undo()
    expected = deepcopy(before)
    del expected['pending_inputs']['scene.old-box']
    if kind == 'training_package':
        del expected['pending_inputs']['request.task_config.bad']
    assert draft.document == expected
    draft.undo_stack.redo()
    assert other + '_id' not in draft.document['request']
    assert 'scene.old-box' not in draft.document['pending_inputs']


def test_environment_replacement_cancels_only_environment_pending(integrated, transport, monkeypatch):
    from muse_gui.app import workbench_controller as app_module
    _, draft, _, controller = integrated
    draft.set_value(('request',), {'environment_id': 'old-env', 'environment_revision': 1})
    draft.stage_input('scene.old-box', json.dumps({'position.0': '1e-'}))
    draft.stage_input('scene.entity/uav/0', json.dumps({'position.0': '-'}))
    before = draft.document
    monkeypatch.setattr(app_module.MessageBox, 'exec', lambda _: True)
    controller.select_resource({'kind': 'environment', 'resource_id': 'new-env', 'revision': 1})
    assert 'scene.old-box' not in draft.document['pending_inputs']
    assert draft.document['pending_inputs']['scene.entity/uav/0'] == before['pending_inputs']['scene.entity/uav/0']
    draft.undo_stack.undo()
    assert draft.document['request'] == before['request']
    assert 'scene.old-box' not in draft.document['pending_inputs']


@pytest.mark.parametrize('destination', ['config', 'history_host'])
def test_hidden_draft_preview_releases_page_and_session(integrated, transport, wait_until, destination):
    window, draft, _, controller = integrated
    requests, _, answer = transport
    controller.mount_views()
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1})
    answer('/catalog/environments/env/1/editing', {
        'base': {'resource_id': 'env', 'revision': 1, 'sha256': 'a' * 64}, 'objects': []})
    wait_until(lambda: any(item[1:3] == ('POST', '/previews') for item in requests))
    with webpage() as url:
        state = {'session_id': 'first-preview', 'status': 'ready', 'applied_revision': draft.revision,
                 'viewer_url': url, 'objects': [], 'sensor_omissions': [], 'error': None}
        answer('/previews', state, 'POST')
        wait_until(lambda: controller.draft_viewer.page is not None)
        destroyed = []
        controller.draft_viewer.profile.destroyed.connect(lambda: destroyed.append(True))
        if destination == 'config':
            window.workbench.set_page('config')
        else:
            window.switchTo(window.history_host)
        try:
            wait_until(lambda: controller.draft_viewer.page is None and bool(destroyed))
            assert any(item[1:3] == ('DELETE', '/previews/first-preview') for item in requests)
            window.workbench.set_page('scene')
            window.switchTo(window.workbench)
            wait_until(lambda: any(item[1:3] == ('POST', '/previews') for item in requests))
            answer('/previews', {**state, 'session_id': 'second-preview'}, 'POST')
            assert controller.draft_viewer.view_id == 'second-preview'
            assert not any(path.startswith('/runs/') for _, _, path, _ in requests)
        finally:
            controller.scene.close_preview()
            wait_until(lambda: not controller.draft_viewer.releasing)
