"""草稿场景编辑与CPU预览；原生几何/拾取/编译只由Server拥有。"""
from copy import deepcopy
import json
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer, Signal


def _placement(request, group):
    """选中组修改共享placement时拆开引用，不能移动未选中的组。"""
    scenario = request['scenario']
    identity = group['placement_id']
    placements = scenario.setdefault('placements', {})
    if any(other is not group and other.get('placement_id') == identity for other in scenario['entity_groups']):
        new = 'placement-' + uuid4().hex
        placements[new] = deepcopy(placements.get(identity, []))
        group['placement_id'] = identity = new
    return placements.setdefault(identity, [])


def _remove_slot(request, group_id, slot):
    scenario = request['scenario']
    group = next(group for group in scenario['entity_groups'] if group['group_id'] == group_id)
    placements = _placement(request, group)
    if slot < len(placements):
        placements.pop(slot)
    group['count'] -= 1
    if group['count'] == 0:
        scenario['entity_groups'].remove(group)
        scenario['placements'].pop(group['placement_id'])
    roles = request.get('role_config', {})
    deleted_roles = set()
    for name, binding in list(roles.items()):
        if binding.get('group_id') != group_id:
            continue
        old = binding.get('slots', [])
        targets = scenario.get('command_targets_world', {}).get(name, [])
        if slot in old:
            removed = old.index(slot)
            if removed < len(targets):
                targets.pop(removed)
        binding['slots'] = [index - (index > slot) for index in old if index != slot]
        if not binding['slots']:
            del roles[name]
            scenario.get('command_targets_world', {}).pop(name, None)
            deleted_roles.add(name)
    geometry = scenario.get('radio_geometry') or {}
    changed_endpoints, removed_endpoints = set(), set()
    for name, endpoint in list(geometry.get('endpoints', {}).items()):
        if endpoint.get('group_id') != group_id:
            continue
        old = endpoint.get('slots', [])
        if slot in old:
            changed_endpoints.add(name)
        endpoint['slots'] = [index - (index > slot) for index in old if index != slot]
        if not endpoint['slots']:
            del geometry['endpoints'][name]
            removed_endpoints.add(name)
    removed_links = {link['link_id'] for link in geometry.get('links', [])
                     if link['tx'] in removed_endpoints or link['rx'] in removed_endpoints}
    if 'links' in geometry:
        geometry['links'] = [link for link in geometry['links'] if link['link_id'] not in removed_links]
    sensing = []
    for item in geometry.get('sensing', []):
        target = item['target']
        if target.get('group_id') == group_id:
            target['slots'] = [index - (index > slot) for index in target['slots'] if index != slot]
        if target['slots'] and item['tx'] not in removed_endpoints and item['rx'] not in removed_endpoints:
            sensing.append(item)
    if 'sensing' in geometry:
        geometry['sensing'] = sensing
    network = scenario.get('network') or {}
    for name in removed_endpoints:
        network.get('endpoint_limits', {}).pop(name, None)
    if 'role_controls' in network:
        network['role_controls'] = {role: endpoint for role, endpoint in network['role_controls'].items()
            if endpoint not in removed_endpoints and role not in deleted_roles}
    if 'passive_endpoints' in network:
        network['passive_endpoints'] = [name for name in network['passive_endpoints'] if name not in removed_endpoints]
    # 发射配置数组与endpoint展开顺序绑定；整项移除并在确认中列出，不能偷偷错配剩余slot。
    if 'background_emitters' in network:
        network['background_emitters'] = [item for item in network['background_emitters']
            if not changed_endpoints.intersection(item['endpoints'])]
    for field in ('communication_allocations', 'services'):
        if field in network:
            network[field] = [item for item in network[field] if item['link_id'] not in removed_links]


class SceneController(QObject):
    selection_changed = Signal(str)
    error = Signal(dict)
    preview_changed = Signal(dict)

    def __init__(self, draft, connection, view, viewer, parent=None):
        super().__init__(parent)
        self.draft, self.connection, self.view, self.viewer = draft, connection, view, viewer
        self.session_id = None
        self.applied_revision = None
        self.preview_status = 'closed'
        self.stale = True
        self.selected = None
        self.objects = {}
        self._manifest = None
        self._manifest_key = None
        self._draft_id = draft.document['draft_id']
        self._prefix = 'scene:' + uuid4().hex + ':'
        self._requests = {}
        self._stream = None
        self._epoch = 0
        self._updating = False
        self._last_sent = None
        self._force_next = False
        self._enabled = True
        self._connection_generation = connection.state['generation'] if connection.state['state'] == 'connected' else None
        self._gesture = None
        self._pending_box = None
        self._visible = {}
        self._compiled = set()
        self._viewer_url = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._send_preview)
        draft.changed.connect(self._changed)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)
        view.selection_requested.connect(self.select)
        view.pose_requested.connect(self.set_pose)
        view.box_requested.connect(self.set_box)
        view.input_staged.connect(self._stage_input)
        view.add_requested.connect(self.add_box)
        view.duplicate_requested.connect(self.duplicate_box)
        view.remove_requested.connect(self.remove_object)
        view.count_requested.connect(self.set_group_count)
        view.placement_confirmed.connect(self.confirm_placement)
        view.placement_cancelled.connect(self.cancel_placement)
        view.visibility_requested.connect(self.set_visibility)
        view.focus_requested.connect(lambda: self._intent('focus', self.selected))
        view.reset_camera_requested.connect(lambda: self._intent('reset_camera'))
        view.preview_requested.connect(self.refresh_preview)
        view.undo_requested.connect(draft.undo_stack.undo)
        view.redo_requested.connect(draft.undo_stack.redo)
        viewer.page_failed.connect(lambda message: self._fail(message))
        self._changed()

    def _request(self, operation, method, path, **kwargs):
        token = self._prefix + uuid4().hex
        self._requests[token] = (operation, self.connection.state['generation'], self._epoch,
                                 self.draft.document['draft_id'], self.draft.revision)
        self.connection.request(token, method, path, **kwargs)

    def set_environment_manifest(self, manifest):
        self._manifest = deepcopy(manifest)
        self._manifest_key = (manifest['base']['resource_id'], manifest['base']['revision'])
        self._render()
        self._schedule()

    def _render(self):
        document = self.draft.document
        edit = document['environment_edit']
        upserts, removed = edit.get('upserts', {}), edit.get('removed', [])
        objects = {}
        for item in self._manifest['objects'] if self._manifest else []:
            if item['object_id'] in removed:
                continue
            box = deepcopy(upserts.get(item['object_id'], item['box']))
            objects[item['object_id']] = {**item, 'box': box, 'pose': box['pose'] if box else None,
                                         'kind': 'environment'}
        for identity, box in upserts.items():
            if identity not in objects:
                objects[identity] = dict(label=box['label'], editable=True, reason=None,
                    box=deepcopy(box), pose=deepcopy(box['pose']), kind='environment')
        scenario = document['request'].get('scenario', {})
        groups = scenario.get('entity_groups', [])
        for group in groups:
            poses = scenario.get('placements', {}).get(group.get('placement_id'), [])
            if not group.get('group_id') or type(group.get('count')) is not int:
                continue
            for slot in range(group['count']):
                pose = poses[slot] if slot < len(poses) else None
                identity = f"entity/{quote(group['group_id'], safe='')}/{slot}"
                objects[identity] = dict(label=f"{group['group_id']} [{slot}]", editable=True,
                    reason='待摆放 / 待绑定' if pose is None else None, box=None, pose=deepcopy(pose),
                    kind='entity', group_id=group['group_id'], slot=slot)
        self.objects = objects
        self.view.set_objects(objects, [g for g in groups if g.get('group_id') and type(g.get('count')) is int])
        if self._pending_box is None:
            self._show_object()

    def _show_object(self):
        raw = self.draft.document['pending_inputs'].get('scene.' + str(self.selected))
        self.view.set_object(self.selected, self.objects.get(self.selected),
            visible=self._visible.get(self.selected, True), inputs=json.loads(raw) if raw is not None else None)

    def _stage_input(self, object_id, inputs):
        self.draft.stage_input('scene.' + object_id, json.dumps(inputs, ensure_ascii=False) if inputs else None)

    def _changed(self, *args):
        document = self.draft.document
        if document['draft_id'] != self._draft_id:
            self._release()
            self._draft_id = document['draft_id']
            self.selected = self._pending_box = None
            self._visible.clear()
        if self._gesture is not None:
            self._gesture = None
            self._intent('select', self.selected)
        self.stale = True
        request = document['request']
        key = (request.get('environment_id'), request.get('environment_revision'))
        if key != self._manifest_key:
            self._release(unload=False)
            self._manifest = None
            self._manifest_key = key
            if all(part is not None for part in key) and self._enabled and self.connection.state['state'] == 'connected':
                self._request('manifest', 'GET', f'/catalog/environments/{key[0]}/{key[1]}/editing')
        self._render()
        self.view.status.setText('预览更新中 / 旧图已过期' if self._manifest else '等待环境引用与可编辑manifest；研究配置可不完整。')
        self._schedule()

    def select(self, object_id):
        if object_id not in self.objects:
            return
        self.view.flush_inputs()
        self.selected = object_id
        self._show_object()
        self.selection_changed.emit(object_id)
        self._intent('select', object_id)

    def set_pose(self, object_id, pose):
        obj = self.objects[object_id]
        if not obj['editable']:
            self._fail(obj['reason'])
            return
        if obj['kind'] == 'environment':
            box = deepcopy(obj['box'])
            box['pose'] = deepcopy(pose)
            self.set_box(object_id, box)
        else:
            request = self.draft.document['request']
            group = next(g for g in request['scenario']['entity_groups'] if g['group_id'] == obj['group_id'])
            poses = _placement(request, group)
            while len(poses) <= obj['slot']:
                poses.append(None)  # 显式未摆放，不制造Pose进入preview。
            poses[obj['slot']] = deepcopy(pose)
            self.draft.commit_input(('request',), request, pending_path='scene.' + object_id)

    def set_box(self, object_id, box):
        obj = self.objects[object_id]
        if not obj['editable'] or obj['box'] is None:
            self._fail(obj.get('reason') or '该对象不是可编辑静态box')
            return
        edit = self._edit()
        edit['upserts'][object_id] = deepcopy(box)
        self.draft.commit_input(('environment_edit',), edit, pending_path='scene.' + object_id)

    def _edit(self):
        edit = self.draft.document['environment_edit']
        return edit if edit else {'base': deepcopy(self._manifest['base']), 'upserts': {}, 'removed': []}

    def add_box(self, box):
        if self._manifest is None:
            self._fail('新增box需要已登记环境manifest')
            return
        self._pending_box = ('box-' + uuid4().hex, deepcopy(box))
        self.view.set_object(self._pending_box[0], dict(label=box['label'], editable=True,
            reason=None, kind='environment', box=box, pose=box.get('pose')), pending=True)

    def duplicate_box(self, object_id):
        obj = self.objects[object_id]
        if not obj['editable'] or obj['box'] is None:
            self._fail('只能复制可编辑box')
            return
        box = deepcopy(obj['box'])
        box['label'] += ' 副本'
        self.add_box(box)

    def confirm_placement(self, pose):
        if self._pending_box is None:
            return
        identity, _ = self._pending_box
        box = self.view.pending_box()
        box['pose'] = deepcopy(pose)
        self._pending_box = None
        edit = self._edit()
        edit['upserts'][identity] = box
        self.selected = identity
        self.draft.set_value(('environment_edit',), edit)
        self.flush_preview()

    def cancel_placement(self):
        self._pending_box = None
        self._render()

    def _slot_pending_keys(self, group_id, slots):
        pending = self.draft.document['pending_inputs']
        return ['scene.' + identity for identity, obj in self.objects.items()
                if obj['kind'] == 'entity' and obj['group_id'] == group_id and obj['slot'] >= min(slots)
                and 'scene.' + identity in pending]

    def _remove_impacts(self, request, group_id, slots):
        scenario = request.get('scenario', {})
        impacts = [f'{group_id}: 删除slot {sorted(slots)}；其后slot编号在同一次编辑重排。']
        cancelled = self._slot_pending_keys(group_id, slots)
        if cancelled:
            impacts.append('删除及slot重排影响的对象未完成输入将取消，撤销删除也不会恢复这些文本：'
                           + ', '.join(key.removeprefix('scene.') for key in cancelled))
        for role, binding in request.get('role_config', {}).items():
            if binding.get('group_id') == group_id:
                impacts.append('role / command target: ' + role + ' ' + str(binding.get('slots', [])))
        geometry = scenario.get('radio_geometry') or {}
        for name, endpoint in geometry.get('endpoints', {}).items():
            if endpoint.get('group_id') == group_id:
                impacts.append('endpoint: ' + name + ' ' + str(endpoint.get('slots', [])))
        for item in geometry.get('sensing', []):
            if item['target'].get('group_id') == group_id:
                impacts.append('sensing target: ' + item['sensing_id'])
        network = scenario.get('network') or {}
        impacts += ['发射/服务关联将清理，缺失绑定需重新配置：' + json_name for json_name in (
            'role_controls', 'background_emitters', 'communication_allocations', 'services') if network.get(json_name)]
        impacts.append('Task/Algorithm私有参数的引用由研究者复核；不会自动重新绑定。')
        return impacts

    def remove_object(self, object_id):
        obj = self.objects[object_id]
        if not obj['editable']:
            self._fail(obj['reason'])
            return
        revision = self.draft.revision
        if obj['kind'] == 'entity':
            request = self.draft.document['request']
            impacts = self._remove_impacts(request, obj['group_id'], [obj['slot']])
            cancelled = self._slot_pending_keys(obj['group_id'], [obj['slot']])
            if not self.view.confirm_removal(impacts):
                return
            if self.draft.revision != revision:
                self._fail('确认期间草稿已变，请重新检查删除影响')
                return
            _remove_slot(request, obj['group_id'], obj['slot'])
            self.selected = None
            self.draft.commit_input(('request',), request, pending_path='scene.' + object_id)
            for key in cancelled:
                self.draft.stage_input(key, None)
            self.view.show_issue('已清理引用；缺失绑定需补齐，未自动转绑：\n' + '\n'.join(impacts))
        else:
            if not self.view.confirm_removal(['删除box：' + obj['label']]):
                return
            if self.draft.revision != revision:
                self._fail('确认期间草稿已变，请重试')
                return
            edit = self._edit()
            edit['upserts'].pop(object_id, None)
            if any(item['object_id'] == object_id for item in self._manifest['objects']):
                edit['removed'].append(object_id)
            self.selected = None
            self.draft.commit_input(('environment_edit',), edit, pending_path='scene.' + object_id)

    def set_group_count(self, group_id, count):
        request = self.draft.document['request']
        group = next(g for g in request['scenario']['entity_groups'] if g['group_id'] == group_id)
        old = group['count']
        if count == old:
            return
        if count < 0:
            raise ValueError('实体数量不能为负')
        if count < old:
            revision = self.draft.revision
            cancelled = self._slot_pending_keys(group_id, range(count, old))
            if not self.view.confirm_removal(self._remove_impacts(request, group_id, list(range(count, old)))):
                return
            if revision != self.draft.revision:
                self._fail('确认期间草稿已变，请重试')
                return
            for slot in range(old - 1, count - 1, -1):
                _remove_slot(request, group_id, slot)
        else:
            group['count'] = count
        self.draft.set_value(('request',), request)
        if count < old:
            for key in cancelled:
                self.draft.stage_input(key, None)
            self.view.show_issue('已清理被删slot引用；请补齐role/endpoint/target绑定，再由Server验证。')
        else:
            self.view.message.setText('新增slot待摆放/绑定；未放置的slot不进入CPU预览。')

    def preview_payload(self):
        document = self.draft.document
        entities = []
        for identity, obj in self.objects.items():
            if obj['kind'] != 'entity' or obj['pose'] is None:
                continue
            group = next(g for g in document['request']['scenario']['entity_groups'] if g['group_id'] == obj['group_id'])
            if not group.get('model_id') or not group.get('model_revision'):
                continue
            entities.append(dict(object_id=identity, group_id=obj['group_id'], slot=obj['slot'],
                model_id=group['model_id'], model_revision=group['model_revision'], pose=deepcopy(obj['pose']),
                sensor_profile_id=group.get('sensor_profile_id'), sensor_config=deepcopy(group.get('sensor_config', {}))))
        return dict(draft_id=document['draft_id'], revision=self.draft.revision,
            environment=deepcopy(self._manifest['base']), environment_edit=document['environment_edit'] or None, entities=entities)

    def _schedule(self):
        if self._enabled and self._manifest and self.connection.state['state'] == 'connected' and not self._timer.isActive():
            self._timer.start()

    def flush_preview(self):
        self._timer.stop()
        self._force_next = True
        self._send_preview()

    def _send_preview(self):
        if not self._enabled or not self._manifest or self.connection.state['state'] != 'connected':
            return
        if self._updating:
            return
        if self._last_sent == self.draft.revision:
            return
        self._force_next = False
        self._updating = True
        self._last_sent = self.draft.revision
        self._request('update' if self.session_id else 'create', 'PUT' if self.session_id else 'POST',
                      '/previews/' + self.session_id if self.session_id else '/previews', json=self.preview_payload())

    def _received(self, generation, token, result):
        if token == self._stream:
            if not result['ok']:
                self._release(unload=False)
                self._fail(str(result['error']))
            else:
                message = result['data']
                if message['kind'] == 'state':
                    self.accept_state(message)
                elif message['kind'] == 'gesture':
                    self.accept_gesture(message)
                elif message['kind'] == 'error':
                    self._fail(message['message'])
                elif message['kind'] == 'view' and message['session_id'] == self.session_id:
                    event = message['event']
                    if event['kind'] == 'pairing' and event['connected'] and event['paired']:
                        self._intent('select', self.selected)
                        for identity, visible in self._visible.items():
                            if identity in self._compiled:
                                self._intent('visibility', identity, visible=visible)
                    elif event['kind'] == 'selection' and event.get('frame', {}).get('revision') == self.draft.revision:
                        identity = event['object_id']
                        if identity in self.objects:
                            self.selected = identity
                            self._show_object()
                            self.selection_changed.emit(identity)
            return
        pending = self._requests.pop(token, None)
        if pending is None or pending[1] != generation or generation != self.connection.state['generation']:
            return
        operation, _, epoch, draft_id, revision = pending
        if epoch != self._epoch or draft_id != self.draft.document['draft_id']:
            if operation == 'create' and result['ok']:
                self._request('delete', 'DELETE', '/previews/' + result['data']['session_id'])
            return
        if operation in ('create', 'update'):
            self._updating = False
        if not result['ok']:
            self._fail(str(result['error']))
            return
        data = result['data']
        if operation == 'manifest':
            self.set_environment_manifest(data)
        elif operation == 'create':
            self.session_id = data['session_id']
            self._stream = self._prefix + 'control:' + self.session_id
            self.connection.subscribe(self._stream, '/streams/previews/' + self.session_id)
            self.accept_state(data)
        elif operation == 'update':
            self.accept_state(data)
        if operation in ('create', 'update') and revision != self.draft.revision:
            if self._force_next:
                self._send_preview()
            else:
                self._schedule()

    def accept_state(self, state):
        if state['session_id'] != self.session_id:
            return
        self.preview_status, self.applied_revision = state['status'], state['applied_revision']
        self.stale = state['status'] != 'ready' or state['applied_revision'] != self.draft.revision
        self._compiled = {item['object_id'] for item in state['objects']}
        if state['status'] == 'ready' and state['viewer_url'] != self._viewer_url:
            self._viewer_url = state['viewer_url']
            self.viewer.open(state['viewer_url'], self.session_id)
        self.view.status.setText(f"CPU预览 {state['status']} / applied={state['applied_revision']} / draft={self.draft.revision}"
            + ('；旧图已过期' if self.stale else '；静态编译初态，非episode reset')
            + ('；sensor省略：' + str(state['sensor_omissions']) if state['sensor_omissions'] else '')
            + ('；' + str(state['error']) if state['error'] else ''))
        self.preview_changed.emit(deepcopy(state))

    def _intent(self, kind, object_id=None, **kwargs):
        if self._stream and self.preview_status == 'ready' and (object_id is None or object_id in self._compiled):
            self.connection.send_stream(self._stream, dict(kind=kind, object_id=object_id, **kwargs))

    def set_visibility(self, object_id, visible):
        self._visible[object_id] = visible
        self._intent('visibility', object_id, visible=visible)

    def accept_gesture(self, message):
        if message['session_id'] != self.session_id:
            return
        gesture = message['gesture']
        identity = gesture['object_id']
        if (gesture['base_revision'] != self.draft.revision or self.applied_revision != self.draft.revision
                or self.preview_status != 'ready' or identity not in self.objects or not self.objects[identity]['editable']):
            self._gesture = None
            self._intent('select', self.selected)
            self._render()
            return
        key = (gesture['gesture_id'], identity, gesture['base_revision'])
        if gesture['phase'] == 'start':
            self._gesture = key
        elif self._gesture != key:
            self._intent('select', self.selected)
            return
        self.view.message.setText('临时拖动，尚未写入草稿：' + str(gesture['pose']))
        if gesture['phase'] == 'end':
            self._gesture = None
            self.set_pose(identity, gesture['pose'])
            self.flush_preview()
            self.view.message.setText('拖动结束，已由草稿单命令路径处理；预览更新/错误见上方状态。')

    def refresh_preview(self):
        self._release()
        self._enabled = True
        self._changed()
        self.flush_preview()

    def _release(self, *, unload=True):
        self._epoch += 1
        self._timer.stop()
        identity, stream = self.session_id, self._stream
        self.session_id = self._stream = None
        self._last_sent = None
        self._updating = False
        self._gesture = None
        self._viewer_url = None
        self._compiled.clear()
        self.preview_status = 'closed'
        self.stale = True
        if identity and self.connection.state['state'] == 'connected':
            self._request('delete', 'DELETE', '/previews/' + identity)
        if stream:
            self.connection.unsubscribe(stream)
        if unload:
            self.viewer.unload()

    def close_preview(self):
        self._enabled = False
        self._pending_box = None
        self._release()

    def _connection_changed(self, state):
        if state['state'] != 'connected':
            self._connection_generation = None
            was_active = self.session_id is not None or self._updating
            self._release(unload=False)
            if was_active:
                self._fail('控制连接已断开；预览为旧图，重连后需取得新session')
        elif self._enabled and state['generation'] != self._connection_generation:
            self._connection_generation = state['generation']
            self._manifest_key = None
            self._changed()

    def _fail(self, message):
        self.stale = True
        self.view.show_issue(str(message))
        self.error.emit({'message': str(message)})
