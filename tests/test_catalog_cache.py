"""目录缓存的真实界面消费者；传输受控，Qt、文件与分页保持真实。"""
from contextlib import contextmanager
import json
from threading import Event

import httpx2
import pytest
from PySide6.QtCore import QSettings

from muse_gui.catalog.resources_view import ResourcesView
from test_connection import controller_for, health
from test_configuration import catalog


def record(identity, kind='training_package'):
    return dict(resource_id=identity, revision=3, sha256='a' * 64, kind=kind, metadata={})


@contextmanager
def resource_view(connection, settings, tmp_path):
    view = ResourcesView(connection, settings, archive_dir=tmp_path / 'archives')
    try:
        yield view
    finally:
        assert view.close_operations()
        view.close()


def test_reopened_resources_show_cached_pages_and_source_without_binding(tmp_path, wait_until):
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        offset = int(request.url.params.get('offset', 0))
        return httpx2.Response(200, json=dict(items=[record('first' if offset == 0 else 'second')],
                                             next_offset=50 if offset == 0 else None))
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    settings.setValue('backend/profile', dict(base_url='http://a', catalog_id='catalog-a'))
    with controller_for(tmp_path / 'cache', wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a', catalog_id='catalog-a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        with resource_view(connection, settings, tmp_path) as view:
            view.refresh()
            wait_until(lambda: view.table.rowCount() == 1)
            view.next_page()
            wait_until(lambda: view.table.rowCount() == 2)
    online_requests = len(seen)
    with controller_for(tmp_path / 'cache', wait_until, handle) as reopened:
        with resource_view(reopened, settings, tmp_path) as view:
            view.refresh()
            assert view.table.rowCount() == 1
            assert view.table.item(0, 0).text() == 'first'
            assert '离线' in view.cache_status.text()
            assert 'catalog-a' in view.cache_status.text() and 'http://a' in view.cache_status.text()
            cached = reopened.cached('/catalog/training-packages', params={'offset': 0, 'limit': 50}, catalog_id='catalog-a')
            assert cached['saved_at'] in view.cache_status.text()
            view.next_page()
            assert view.table.rowCount() == 2
            assert view.table.item(1, 0).text() == 'second'
            selected = []
            view.resource_selected.connect(selected.append)
            view.table.selectRow(0)
            view.use_selected_resource()
            assert selected == []
            assert reopened.state['catalog_id'] is None
            assert len(seen) == online_requests


def test_offline_category_switch_reads_only_selected_catalog(tmp_path, wait_until):
    def handle(request):
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        return httpx2.Response(200, json=dict(items=[record('env-a', 'environment')], next_offset=None))
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    settings.setValue('backend/profile', dict(catalog_id='catalog-a'))
    with controller_for(tmp_path / 'cache', wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        with resource_view(connection, settings, tmp_path) as view:
            view.kind_selector.setCurrentIndex(1)
            view.refresh()
            wait_until(lambda: view.table.rowCount() == 1)
    with controller_for(tmp_path / 'cache', wait_until, handle) as reopened:
        with resource_view(reopened, settings, tmp_path) as view:
            view.kind_selector.setCurrentIndex(1)
            assert view.table.item(0, 0).text() == 'env-a'
        settings.setValue('backend/profile', dict(catalog_id='catalog-b'))
        with resource_view(reopened, settings, tmp_path) as other:
            other.kind_selector.setCurrentIndex(1)
            assert other.table.rowCount() == 0
            assert '缓存' in other.cache_status.text()


def test_online_cache_write_failure_keeps_rows_and_visible_diagnostic(tmp_path, wait_until):
    blocked = tmp_path / 'blocked'
    blocked.write_text('not a directory')
    def handle(request):
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        return httpx2.Response(200, json=dict(items=[record('online')], next_offset=None))
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    with controller_for(blocked, wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        with resource_view(connection, settings, tmp_path) as view:
            view.refresh()
            wait_until(lambda: not connection.has_pending_requests)
            assert view.table.rowCount() == 1
            assert view.table.item(0, 0).text() == 'online'
            assert '缓存' in view.cache_status.text() and '失败' in view.cache_status.text()
            assert connection.state['state'] == 'connected'


def test_corrupt_cache_reports_error_without_offline_network_request(tmp_path, wait_until):
    seen = []
    def handle(request):
        seen.append(request)
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        return httpx2.Response(200, json=dict(items=[record('saved')], next_offset=None))
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    settings.setValue('backend/profile', dict(catalog_id='catalog-a'))
    cache_dir = tmp_path / 'cache'
    with controller_for(cache_dir, wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        connection.request('page', 'GET', '/catalog/training-packages', params={'offset': 0, 'limit': 50})
        wait_until(lambda: not connection.has_pending_requests)
    for path in cache_dir.rglob('*.json'):
        if json.loads(path.read_text(encoding='utf-8'))['params'] == {'offset': 0, 'limit': 50}:
            path.write_text('{invalid', encoding='utf-8')
    before = len(seen)
    with controller_for(cache_dir, wait_until, handle) as reopened:
        with resource_view(reopened, settings, tmp_path) as view:
            view.refresh()
            assert view.table.rowCount() == 0
            assert '缓存' in view.cache_status.text() and '失败' in view.cache_status.text()
            assert len(seen) == before


@pytest.mark.parametrize('damage', [None, 'schema', 'page', 'missing-page'])
def test_workbench_restores_complete_schema_cache_for_offline_editing(tmp_path, wait_until, damage):
    from muse_gui.app.main import mount_configuration
    from muse_gui.app.main_window import MuseWindow
    from muse_gui.app.workbench_controller import WorkbenchController
    from muse_gui.app.workbench_settings import WorkbenchSettings
    from muse_gui.experiments.configuration_controller import ConfigurationController
    from muse_gui.experiments.experiment_draft import ExperimentDraft
    data, seen = catalog(), []
    data['training_packages'][0].update(kind='training_package', sha256='a' * 64)
    paths = {path: key for key, path in ConfigurationController.CATALOG_PATHS.items()}
    def handle(request):
        seen.append(request.url.path)
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        if request.url.path == '/runs':
            return httpx2.Response(200, json=dict(items=[], next_offset=None))
        key = paths[request.url.path]
        return httpx2.Response(200, json=data[key] if key == 'schema' else dict(items=data[key], next_offset=None))
    cache = tmp_path / 'cache'
    with controller_for(cache, wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        for key, path in ConfigurationController.CATALOG_PATHS.items():
            connection.request(key, 'GET', path, **({} if key == 'schema' else {'params': {'offset': 0, 'limit': 50}}))
        wait_until(lambda: not connection.has_pending_requests)
    if damage is not None:
        for path in cache.rglob('*.json'):
            value = json.loads(path.read_text(encoding='utf-8'))
            if damage == 'schema' and value['path'] == '/catalog/schema':
                value['data'] = {}
            elif damage == 'page' and value['path'] == '/catalog/training-packages':
                value['data']['items'] = [{}]
            elif damage == 'missing-page' and value['path'] == '/catalog/training-packages':
                value['data']['next_offset'] = 50
            path.write_text(json.dumps(value), encoding='utf-8')
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    settings.setValue('backend/profile', dict(base_url='http://a', catalog_id='catalog-a'))
    before = len(seen)
    with controller_for(cache, wait_until, handle) as reopened:
        draft = ExperimentDraft()
        window = MuseWindow(draft, WorkbenchSettings(settings))
        configuration = mount_configuration(window, draft, reopened)
        controller = None
        try:
            controller = WorkbenchController(window, draft, reopened, configuration)
            draft.set_value(('catalog_id',), 'catalog-a')
            draft.set_value(('request',), dict(training_package_id='pkg', training_package_revision=2,
                                               composition_id='first', root_seed=7))
            view = configuration.view
            if damage is not None:
                assert view.fixed_editor is None
                assert '缓存' in view.catalog_status.text()
                assert '失败' in view.catalog_status.text() or '不完整' in view.catalog_status.text()
                if damage == 'page':
                    assert controller.resources.table.rowCount() == 0
                    assert '失败' in controller.resources.cache_status.text()
                assert len(seen) == before
                reopened.connect(dict(base_url='http://a', catalog_id='catalog-a'))
                wait_until(lambda: not reopened.has_pending_requests)
                assert view.fixed_editor is not None
                assert '在线' in view.catalog_status.text()
                assert controller.resources.table.item(0, 0).text() == 'pkg'
                return
            assert view.fixed_editor is not None
            field = view.fixed_editor.fields[('request', 'root_seed')]
            field.editor.setText('11')
            view.flush()
            assert draft.document['request']['root_seed'] == 11
            assert '离线' in view.catalog_status.text() and 'catalog-a' in view.catalog_status.text()
            assert 'http://a' in view.catalog_status.text()
            configuration.inspect()
            assert draft.validated_revision is None
            assert len(seen) == before
            draft.set_value(('catalog_id',), 'other-catalog')
            assert view.fixed_editor is None
            assert '缓存' in view.catalog_status.text()
        finally:
            if controller is not None:
                assert controller.prepare_close()
            window._ask_unsaved = lambda: 'discard'
            window.close()


def test_cache_rejects_a_record_from_another_catalog(tmp_path, wait_until):
    def handle(request):
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        return httpx2.Response(200, json=dict(items=[record('saved')], next_offset=None))
    with controller_for(tmp_path / 'cache', wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        connection.request('page', 'GET', '/catalog/training-packages', params={'offset': 0, 'limit': 50})
        wait_until(lambda: not connection.has_pending_requests)
    for path in (tmp_path / 'cache').rglob('*.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        value['catalog_id'] = 'other-catalog'
        path.write_text(json.dumps(value), encoding='utf-8')
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    settings.setValue('backend/profile', dict(catalog_id='catalog-a'))
    with controller_for(tmp_path / 'cache', wait_until, handle) as reopened:
        with resource_view(reopened, settings, tmp_path) as view:
            view.refresh()
            assert view.table.rowCount() == 0
            assert '失败' in view.cache_status.text()


def test_discarded_page_reply_cannot_replace_newer_offline_cache(tmp_path, wait_until):
    entered, release = Event(), Event()
    pages = []
    def handle(request):
        if request.url.path == '/health':
            return httpx2.Response(200, json=health())
        pages.append(request)
        if len(pages) == 1:
            entered.set()
            assert release.wait(5)
            identity = 'older'
        else:
            identity = 'newer'
        return httpx2.Response(200, json=dict(items=[record(identity)], next_offset=None))
    settings = QSettings(str(tmp_path / 'prefs.ini'), QSettings.IniFormat)
    with controller_for(tmp_path / 'cache', wait_until, handle) as connection:
        connection.connect(dict(base_url='http://a'))
        wait_until(lambda: connection.state['state'] == 'connected')
        with resource_view(connection, settings, tmp_path) as view:
            try:
                view.refresh()
                wait_until(entered.is_set)
                view.refresh()
                wait_until(lambda: view.table.rowCount() == 1)
                assert view.table.item(0, 0).text() == 'newer'
                release.set()
                wait_until(lambda: not connection.has_pending_requests)
                assert view.table.item(0, 0).text() == 'newer'
                connection.disconnect()
                assert view.table.item(0, 0).text() == 'newer'
                assert len(pages) == 2
            finally:
                release.set()
