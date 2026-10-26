"""草稿场景编辑与CPU预览；原生几何/拾取/编译只由Server拥有。"""
from copy import deepcopy
import json
import math
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from muse_gui.experiments.schema_editor import at, path_text


def _pose_issue(pose):
    """唯一草稿→几何边界：不补缺项、不把原始部分输入交给姿态消费者。"""
    if not isinstance(pose, dict):
        return 'pose 必须是包含 position / orientation_wxyz 的对象'
    for field, length in (('position', 3), ('orientation_wxyz', 4)):
        values = pose.get(field)
        if (not isinstance(values, list) or len(values) != length
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
            return f'{field} 需要 {length} 个有限数'
    if not math.isclose(sum(value * value for value in pose['orientation_wxyz']), 1., abs_tol=1e-6):
        return 'orientation_wxyz 必须是单位四元数'
    return None


def _placement(request, group, groups):
    """选中组修改共享placement时拆开引用，不能移动未选中的组。"""
    scenario = request['scenario']
    identity = group['placement_id']
    placements = scenario.setdefault('placements', {})
    if any(other is not group and other.get('placement_id') == identity for other in groups.values()):
        new = 'placement-' + uuid4().hex
        placements[new] = deepcopy(placements.get(identity, []))
        group['placement_id'] = identity = new
    return placements.setdefault(identity, [])


def _remove_slot(request, group_id, slot, groups):
    scenario = request['scenario']
    group = groups[group_id]
    placements = _placement(request, group, groups)
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
    error = Signal(dict)

    def __init__(self, draft, connection, view, viewer, parent=None):
        super().__init__(parent)
        self.draft, self.connection, self.view, self.viewer = draft, connection, view, viewer
        self.session_id = None
        self.applied_revision = None
        self.preview_status = 'closed'
        self.stale = True
        self.selected = None
        self.objects = {}
        self._group_indices = {}
        self._preview_edit = None
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
        catalog_id = self.draft.document['catalog_id']
        if operation in ('manifest', 'create', 'update') and catalog_id not in (None, self.connection.state['catalog_id']):
            self._fail(f'预览请求已拒绝：草稿来自 Catalog {catalog_id}；请连接原 Catalog 或显式重选资源。')
            return False
        token = self._prefix + uuid4().hex
        self._requests[token] = (operation, self.connection.state['generation'], self._epoch,
                                 self.draft.document['draft_id'], self.draft.revision)
        self.connection.request(token, method, path, **kwargs)
        return True

    def set_environment_manifest(self, manifest):
        self._manifest = deepcopy(manifest)
        self._manifest_key = (self.draft.document['catalog_id'], manifest['base']['resource_id'], manifest['base']['revision'])
        self._render()
        self._schedule()

    def _render(self):
        document = self.draft.document
        edit = document['environment_edit']
        upserts, removed = edit.get('upserts', {}), edit.get('removed', [])
        issues, objects, preview_upserts = [], {}, {}
        usable_edit = True
        if not isinstance(upserts, dict):
            issues.append('environment_edit.upserts 必须是对象；未应用此环境编辑')
            upserts, usable_edit = {}, False
        if not isinstance(removed, list) or any(not isinstance(identity, str) for identity in removed):
            issues.append('environment_edit.removed 必须是对象ID列表；未应用此环境编辑')
            removed, usable_edit = [], False
        if edit and (not isinstance(edit.get('base'), dict)
                     or self._manifest is not None and edit['base'] != self._manifest['base']):
            issues.append('environment_edit.base 需要对应当前环境manifest；未应用此环境编辑')
            usable_edit = False
        preview_removed = list(removed)
        for item in self._manifest['objects'] if self._manifest else []:
            if item['object_id'] in removed:
                continue
            box = deepcopy(item['box'])
            objects[item['object_id']] = {**item, 'box': box, 'pose': box['pose'] if box else None,
                                         'kind': 'environment'}
        for identity, box in upserts.items():
            issue = None
            if not isinstance(box, dict):
                issue = 'box 必须是对象'
            elif not isinstance(box.get('label'), str):
                issue = 'box.label 需要字符串'
            elif any(type(box.get(field)) is not bool for field in ('collision', 'rf_blocking')):
                issue = 'box.collision / rf_blocking 需要布尔值'
            else:
                sizes = box.get('size_m')
                if (not isinstance(sizes, list) or len(sizes) != 3
                        or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in sizes)):
                    issue = 'box.size_m 需要三个有限正数'
                else:
                    issue = _pose_issue(box.get('pose'))
            if identity in removed:
                issue = '对象不能同时更新和删除'
            if issue:
                original = objects.get(identity)
                if original is not None and original['editable'] and identity not in preview_removed:
                    preview_removed.append(identity)
                objects[identity] = dict(label=identity, editable=False, reason=issue + '；不进入预览',
                                         box=None, pose=None, kind='environment')
                issues.append(f'environment_edit.upserts.{identity}: {issue}')
            else:
                preview_upserts[identity] = deepcopy(box)
                original = objects.get(identity)
                objects[identity] = dict(label=box['label'], editable=original['editable'] if original else True,
                    reason=original['reason'] if original else None,
                    box=deepcopy(box), pose=deepcopy(box['pose']), kind='environment')
        self._preview_edit = dict(base=deepcopy(edit['base']), upserts=preview_upserts,
                                  removed=preview_removed) if edit and usable_edit else None
        scenario = document['request'].get('scenario', {})
        groups, group_indices = [], {}
        if not isinstance(scenario, dict):
            issues.append('scenario 必须是对象；原输入保留，请在配置页补齐。')
        else:
            raw_groups = scenario.get('entity_groups', [])
            if not isinstance(raw_groups, list):
                issues.append('scenario.entity_groups 必须是列表')
            else:
                for index, group in enumerate(raw_groups):
                    if (not isinstance(group, dict) or not isinstance(group.get('group_id'), str)
                            or not group['group_id'] or type(group.get('count')) is not int or group['count'] < 0):
                        issues.append(f'scenario.entity_groups[{index}] 需要 group_id 和非负整数 count')
                    elif group['group_id'] in group_indices:
                        issues.append(f"scenario.entity_groups[{index}] 重复 group_id {group['group_id']}")
                    else:
                        groups.append(group)
                        group_indices[group['group_id']] = index
        for group in groups:
            placements = scenario.get('placements', {})
            placement_id = group.get('placement_id')
            if not isinstance(placements, dict) or not isinstance(placement_id, str):
                poses, placement_issue = [], 'placements 需要对象、placement_id 需要字符串'
            else:
                poses = placements.get(placement_id, [])
                placement_issue = None
                if not isinstance(poses, list):
                    poses, placement_issue = [], f'placements.{placement_id} 必须是 pose 列表'
            if placement_issue is not None:
                del group_indices[group['group_id']]
            for slot in range(group['count']):
                pose = poses[slot] if slot < len(poses) else None
                issue = placement_issue or (_pose_issue(pose) if pose is not None else None)
                identity = f"entity/{quote(group['group_id'], safe='')}/{slot}"
                model_ready = (isinstance(group.get('model_id'), str) and bool(group['model_id'])
                               and type(group.get('model_revision')) is int and group['model_revision'] > 0)
                objects[identity] = dict(label=f"{group['group_id']} [{slot}]", editable=issue is None,
                    reason=issue or ('待摆放 / 待绑定' if pose is None else None), box=None,
                    pose=None if issue else deepcopy(pose), kind='entity', group_id=group['group_id'], slot=slot,
                    preview_entity=None if issue or pose is None or not model_ready else dict(
                        object_id=identity, group_id=group['group_id'], slot=slot, model_id=group['model_id'],
                        model_revision=group['model_revision'], pose=deepcopy(pose),
                        sensor_profile_id=group.get('sensor_profile_id'), sensor_config=deepcopy(group.get('sensor_config', {}))))
        self.objects = objects
        self._group_indices = group_indices
        self.view.set_objects(objects, [group for group in groups if group['group_id'] in group_indices])
        self.view.projection_status.setText('\n'.join(issues))
        self.view.projection_status.setVisible(bool(issues))
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
        key = (document['catalog_id'], request.get('environment_id'), request.get('environment_revision'))
        if key != self._manifest_key:
            self._release(unload=False)
            self._manifest = None
            self._manifest_key = key
            if all(part is not None for part in key[1:]) and self._enabled and self.connection.state['state'] == 'connected':
                self._request('manifest', 'GET', f'/catalog/environments/{key[1]}/{key[2]}/editing')
        self._render()
        self.view.status.setText('预览更新中 / 旧图已过期' if self._manifest else '等待环境引用与可编辑manifest；研究配置可不完整。')
        self._schedule()

    def select(self, object_id):
        if object_id not in self.objects:
            return
        self.view.flush_inputs()
        self.selected = object_id
        self._show_object()
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
            groups = self._draft_groups(request)
            group = groups[obj['group_id']]
            poses = _placement(request, group, groups)
            while len(poses) <= obj['slot']:
                poses.append(None)  # 显式未摆放，不制造Pose进入preview。
            poses[obj['slot']] = deepcopy(pose)
            self.draft.commit_input(('request',), request, pending_path='scene.' + object_id)

    def _draft_groups(self, request):
        return {identity: request['scenario']['entity_groups'][index]
                for identity, index in self._group_indices.items()}

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

    def _slot_pending_keys(self, group_id, slots, before, after):
        pending = self.draft.document['pending_inputs']
        cancelled = ['scene.' + identity for identity, obj in self.objects.items()
                     if obj['kind'] == 'entity' and obj['group_id'] == group_id and obj['slot'] >= min(slots)
                     and 'scene.' + identity in pending]
        # 只枚举_remove_slot实际写入的边界；列表结构沿用G3的整列表子树契约，不迁移raw路径。
        groups = before['scenario']['entity_groups']
        index = self._group_indices[group_id]
        if len(after['scenario']['entity_groups']) < len(groups):
            paths = [('scenario', 'entity_groups', i) for i in range(index, len(groups))]
        else:
            paths = [('scenario', 'entity_groups', index, field) for field in ('count', 'placement_id')]
        # 共享placement会拆分；原placement未变时保留另一group仍拥有的输入。
        for parent in (('scenario', 'placements'), ('scenario', 'command_targets_world'),
                       ('scenario', 'network', 'endpoint_limits'), ('scenario', 'network', 'role_controls')):
            paths.extend((*parent, name) for name in dict.fromkeys([*at(before, parent, {}), *at(after, parent, {})]))
        for parent in (('role_config',), ('scenario', 'radio_geometry', 'endpoints')):
            remaining = at(after, parent, {})
            paths.extend((*parent, name, 'slots') if name in remaining else (*parent, name)
                         for name in at(before, parent, {}))
        paths += [('scenario', 'radio_geometry', field) for field in ('links', 'sensing')]
        paths += [('scenario', 'network', field) for field in (
            'passive_endpoints', 'background_emitters', 'communication_allocations', 'services')]
        prefixes = ['request.' + path_text(path) for path in paths if at(before, path) != at(after, path)]
        cancelled += [key for key in pending if any(key == prefix or key.startswith(prefix + '.')
                      or prefix.startswith(key + '.') for prefix in prefixes)]
        return cancelled

    def _remove_impacts(self, request, group_id, slots, cancelled):
        scenario = request.get('scenario', {})
        impacts = [f'{group_id}: 删除slot {sorted(slots)}；其后slot编号在同一次编辑重排。']
        if cancelled:
            pending = self.draft.document['pending_inputs']
            impacts.append('删除及slot重排影响的对象/配置未完成输入将取消，撤销删除也不会恢复这些文本：\n'
                           + '\n'.join(f'{key} = {pending[key]!r}' for key in cancelled))
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
            before = self.draft.document['request']
            request = deepcopy(before)
            _remove_slot(request, obj['group_id'], obj['slot'], self._draft_groups(request))
            cancelled = self._slot_pending_keys(obj['group_id'], [obj['slot']], before, request)
            impacts = self._remove_impacts(before, obj['group_id'], [obj['slot']], cancelled)
            if not self.view.confirm_removal(impacts):
                return
            if self.draft.revision != revision:
                self._fail('确认期间草稿已变，请重新检查删除影响')
                return
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
        groups = self._draft_groups(request)
        group = groups[group_id]
        old = group['count']
        if count == old:
            return
        if count < 0:
            raise ValueError('实体数量不能为负')
        if count < old:
            revision = self.draft.revision
            before = deepcopy(request)
            for slot in range(old - 1, count - 1, -1):
                _remove_slot(request, group_id, slot, groups)
            cancelled = self._slot_pending_keys(group_id, range(count, old), before, request)
            if not self.view.confirm_removal(self._remove_impacts(before, group_id, list(range(count, old)), cancelled)):
                return
            if revision != self.draft.revision:
                self._fail('确认期间草稿已变，请重试')
                return
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
        entities = [deepcopy(obj['preview_entity']) for obj in self.objects.values()
                    if obj['kind'] == 'entity' and obj['preview_entity'] is not None]
        return dict(draft_id=document['draft_id'], revision=self.draft.revision,
            environment=deepcopy(self._manifest['base']), environment_edit=deepcopy(self._preview_edit), entities=entities)

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
        if self._request('update' if self.session_id else 'create', 'PUT' if self.session_id else 'POST',
                         '/previews/' + self.session_id if self.session_id else '/previews', json=self.preview_payload()):
            self._force_next = False
            self._updating = True
            self._last_sent = self.draft.revision

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
        if self._manifest is None:
            self._manifest_key = None
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
