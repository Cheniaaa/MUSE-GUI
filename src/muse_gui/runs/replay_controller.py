"""历史HTTP/WS所有者；关闭只释放历史会话，不控制源Run。"""
from copy import deepcopy
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, Signal

from .replay_state import ReplayState


class ReplayController(QObject):
    changed = Signal()
    error = Signal(dict)
    artifact_published = Signal(dict)

    def __init__(self, connection, viewer, parent=None):
        super().__init__(parent)
        self.connection, self.viewer = connection, viewer
        self.state = None
        self.active = False
        self._prefix = f'replay:{uuid4().hex}:'
        self._serial = self._epoch = self._command_seq = 0
        self._pending = {}
        self._stream = self._opened_url = None
        self._export_pending = False
        self._export_id = None
        self._published = set()
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)
        viewer.page_failed.connect(lambda message: self._error('viewer', {'message': message}) if self.active else None)

    @property
    def releasing(self):
        return bool(self._pending and any(data[2] in ('create', 'delete') for data in self._pending.values()))

    def _current(self):
        connection = self.connection.state
        return bool(self.active and self.state and connection['state'] == 'connected'
                    and connection['generation'] == self.state.generation
                    and connection['catalog_id'] == self.state.source['catalog_id'])

    @property
    def ready(self):
        return bool(self._current() and self._stream and self.state.data and self.state.data['status'] == 'ready')

    @property
    def can_export(self):
        export = self.state.data['export'] if self.state and self.state.data else None
        return bool(self.ready and self.state.data['camera_ready'] and not self._export_pending
                    and (self._export_id is None or export is not None and export['export_id'] == self._export_id)
                    and (export is None or export['status'] in ('completed', 'cancelled', 'failed')))

    @property
    def can_cancel_export(self):
        export = self.state.data['export'] if self.state and self.state.data else None
        return bool(self.ready and self._export_id and not any(value[2] == 'cancel_export' for value in self._pending.values())
                    and (export is None or export['export_id'] != self._export_id
                         or export['status'] in ('preparing', 'rendering', 'publishing')))

    def open(self, source: dict) -> None:
        self.close()
        connection = self.connection.state
        if (connection['state'] != 'connected' or source['catalog_id'] != connection['catalog_id']
                or source['generation'] != connection['generation']):
            self._error('source', {'message': '旧结果只读；请在原Catalog连接中重新打开Run。'})
            return
        self.state = ReplayState(source, source['generation'])
        self.active = True
        self._command_seq = 0
        self._export_id, self._export_pending = None, False
        self._request('create', 'POST', '/replays', json={key: source[key] for key in ('run_id', 'trajectory_artifact_id')})
        self._request('recording', 'GET', f"/runs/{quote(source['run_id'], safe='')}")
        self.changed.emit()

    def playback(self, kind: str, **target) -> None:
        if not self.ready:
            return
        self._command_seq += 1
        command = dict(command_seq=self._command_seq, kind=kind, **target)
        self.state.requested_command = deepcopy(command)
        self.state.error = None
        self.connection.send_stream(self._stream, dict(kind='playback', command=command))
        self.changed.emit()

    def view_intent(self, intent: dict) -> None:
        if (self.ready and self.state.pairing.get('paired') and self.state.pairing.get('connected')
                and intent['kind'] in ('select', 'focus', 'visibility', 'reset_camera')):
            self.connection.send_stream(self._stream, dict(kind='view', intent=intent))

    def export(self, options: dict) -> None:
        if self.can_export:
            self._export_pending = True
            self._request('export', 'POST', self._path('/recordings'), json=deepcopy(options))
            self.changed.emit()

    def cancel_export(self) -> None:
        if self.can_cancel_export:
            self._request('cancel_export', 'DELETE', self._path('/recordings/') + quote(self._export_id, safe=''))
            self.changed.emit()

    def close(self) -> None:
        if self._current() and self.state.session_id is not None:
            self._request('delete', 'DELETE', self._path())
        self.active = False
        if self._stream is not None:
            self.connection.unsubscribe(self._stream)
            self._stream = None
        if self._opened_url is not None:
            self.viewer.unload()
            self._opened_url = None
        self._epoch += 1
        self._pending = {token: value for token, value in self._pending.items() if value[2] in ('create', 'delete')}
        self.changed.emit()

    def _path(self, suffix=''):
        return '/replays/' + quote(self.state.session_id, safe='') + suffix

    def _token(self):
        self._serial += 1
        return self._prefix + str(self._serial)

    def _request(self, operation, method, path, **kwargs):
        token = self._token()
        self._pending[token] = (self.state.generation, self._epoch, operation, self.state.source)
        self.connection.request(token, method, path, **kwargs)

    def _error(self, operation, error):
        value = dict(operation=operation, **error)
        if self.state is not None:
            self.state.error = value
        self.error.emit(value)
        self.changed.emit()

    def _received(self, generation, token, result):
        pending = self._pending.pop(token, None)
        if pending is not None:
            expected, epoch, operation, source = pending
            connection = self.connection.state
            if (generation != expected or generation != connection['generation']
                    or connection['catalog_id'] != source['catalog_id']):
                return
            if operation == 'delete':
                # WS断开和显式DELETE共享Server所有者，先完成的一方已释放会话。
                if not result['ok'] and result['error'].get('status_code') != 404:
                    self._error(operation, result['error'])
                return
            if epoch != self._epoch or not self.active:
                if operation == 'create' and result['ok'] and connection['state'] == 'connected':
                    identity = result['data']['session_id']
                    cleanup = self._token()
                    self._pending[cleanup] = (expected, epoch, 'delete', source)
                    self.connection.request(cleanup, 'DELETE', '/replays/' + quote(identity, safe=''))
                return
        elif token == self._stream and self._current() and generation == self.state.generation:
            operation = 'stream'
        else:
            return
        if not result['ok']:
            if operation == 'export':
                self._export_pending = False
            if operation == 'stream':
                self.close()
            self._error(operation, result['error'])
            return
        data = result['data']
        if operation == 'create':
            if not self.state.apply(data, generation=generation):
                return
            self._stream = self._token()
            self.connection.subscribe(self._stream, '/streams/replays/' + quote(self.state.session_id, safe=''))
            self._sync_view()
        elif operation == 'recording':
            self.state.recording = deepcopy(data['recording'])
        elif operation == 'export':
            self._export_id = data['export_id']
            self._export_pending = False
        elif operation == 'stream':
            if data['session_id'] != self.state.session_id:
                return
            if data['kind'] == 'state':
                if not self.state.apply(data, generation=generation):
                    return
                self._command_seq = max(self._command_seq, data['last_command_seq'])
                export = data['export']
                if export is not None:
                    self._export_id = export['export_id']
                    if export['status'] == 'completed' and export['artifact_id'] not in self._published:
                        self._published.add(export['artifact_id'])
                        self.artifact_published.emit(dict(self.state.source, artifact_id=export['artifact_id'], generation=generation))
                self._sync_view()
            elif data['kind'] == 'command_result':
                if data['command_seq'] == self._command_seq and data['status'] == 'error':
                    self.state.requested_command = None
                    self._error('playback', {'message': data['error']})
            elif data['kind'] == 'view':
                event = data['event']
                if event['kind'] == 'pairing':
                    self.state.pairing = deepcopy(event)
                elif event['kind'] == 'selection' and event['frame'] is not None and self.state.data['diagnostics'] is not None:
                    frame = self.state.data['diagnostics']
                    if all(event['frame'][key] == frame[key] for key in ('world_id', 'sample_index')):
                        self.state.selected_object = event['object_id']
                elif event['kind'] == 'error':
                    self._error('view', {'message': event['message']})
        self.changed.emit()

    def _sync_view(self):
        data = self.state.data
        if data['status'] == 'ready' and data['viewer_url'] and data['viewer_url'] != self._opened_url:
            self._opened_url = data['viewer_url']
            self.viewer.open(self._opened_url, self.state.session_id)
        elif data['status'] in ('error', 'closing') and self._opened_url is not None:
            self.viewer.unload()
            self._opened_url = None

    def _connection_changed(self, connection):
        if self.state is not None and (connection['state'] != 'connected'
                or connection['generation'] != self.state.generation
                or connection['catalog_id'] != self.state.source['catalog_id']):
            self.close()
            self._pending = {token: value for token, value in self._pending.items() if value[0] == connection['generation']}
            self._error('connection', {'message': '连接已失效；历史结果只读，请重新打开。'})
