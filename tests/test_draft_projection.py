"""可保存的部分草稿仅在场景投影边界区分几何，不改原输入。"""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import Qt

from test_scene_editor import BASE, POSE, empty_scene, release_owned_scene_widgets


@pytest.mark.parametrize('scenario,reason', [
    (None, 'scenario'),
    ({'entity_groups': [None]}, 'entity_groups[0]'),
    ({'entity_groups': None}, 'entity_groups'),
])
def test_partial_scenario_is_saved_and_reported_without_projection_error(empty_scene, tmp_path, scenario, reason):
    draft, view, controller, _, _ = empty_scene
    controller.close_preview()
    draft.set_value(('request',), {'scenario': deepcopy(scenario)})
    before, count = draft.document, draft.undo_stack.count()
    # 直接调用同一投影入口，避免 Qt 吞掉 slot 异常造成假绿。
    controller._render()
    assert controller.objects == {}
    assert reason in view.projection_status.text()
    path = tmp_path / 'partial.json'
    draft.save(path)
    assert json.loads(path.read_text(encoding='utf-8')) == before
    assert draft.undo_stack.count() == count
    draft.undo_stack.undo()
    controller._render()
    assert draft.document['request'] == {}
    draft.undo_stack.redo()
    controller._render()
    assert draft.document == before


@pytest.mark.parametrize('pose,reason', [
    ({'position': [1., 2., 3.]}, 'orientation_wxyz'),
    ({'orientation_wxyz': [1., 0., 0., 0.]}, 'position'),
    ({'position': [1., 2.], 'orientation_wxyz': [1., 0., 0., 0.]}, 'position'),
    ({'position': [1., 2., 3.], 'orientation_wxyz': [0., 0., 0., 0.]}, 'orientation_wxyz'),
])
def test_partial_pose_is_not_converted_or_previewed_and_valid_neighbor_survives(empty_scene, pose, reason):
    draft, view, controller, connection, _ = empty_scene
    controller.set_environment_manifest({'base': BASE, 'objects': []})
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1, 'scenario': {
        'entity_groups': [{'group_id': 'uav', 'model_id': 'model', 'model_revision': 1,
                                'placement_id': 'start', 'count': 2}],
        'placements': {'start': [deepcopy(pose), deepcopy(POSE)]},
    }})
    before, count = draft.document, draft.undo_stack.count()
    controller._render()
    controller.select('entity/uav/0')
    assert reason in view.reason.text()
    assert reason in view.tree.topLevelItem(0).text(1)
    assert controller.objects['entity/uav/0']['pose'] is None
    assert all(field.text() == '' for field in view.rotation_fields)
    view.flush_inputs()
    assert draft.document == before
    controller.flush_preview()
    payload = next(item[3]['json'] for item in connection.requests if item[1:3] == ('POST', '/previews'))
    assert [(item['object_id'], item['pose']) for item in payload['entities']] == [('entity/uav/1', POSE)]
    assert draft.document == before and draft.undo_stack.count() == count
    controller.select('entity/uav/1')
    assert view.tree.currentItem().data(0, Qt.ItemDataRole.UserRole) == 'entity/uav/1'
    draft.undo_stack.undo()
    controller._render()
    draft.undo_stack.redo()
    controller._render()
    assert draft.document == before


def test_formed_neighbor_pose_edit_preserves_null_group_and_undo(empty_scene):
    draft, _, controller, _, _ = empty_scene
    draft.set_value(('request',), {'scenario': {
        'entity_groups': [None, {'group_id': 'uav', 'count': 1, 'placement_id': 'start'}],
        'placements': {'start': [deepcopy(POSE)]},
    }})
    before = draft.document
    pose = {**deepcopy(POSE), 'position': [9., 8., 7.]}
    controller.set_pose('entity/uav/0', pose)
    assert draft.document['request']['scenario']['entity_groups'][0] is None
    assert draft.document['request']['scenario']['placements']['start'] == [pose]
    draft.undo_stack.undo()
    assert draft.document == before


def test_corrected_scenario_clears_projection_reason_without_hiding_preview_error(empty_scene):
    from qfluentwidgets import BodyLabel
    draft, view, controller, _, _ = empty_scene
    draft.set_value(('request',), {'scenario': None})
    assert any('scenario' in label.text() for label in view.findChildren(BodyLabel))
    draft.set_value(('request',), {'scenario': {}})
    assert not any('scenario' in label.text() for label in view.findChildren(BodyLabel))
    controller._fail('controlled preview failure')
    controller._render()
    assert 'controlled preview failure' in view.message.text()


def test_partial_environment_box_is_omitted_without_poisoning_formed_neighbor(empty_scene):
    from test_scene_editor import BOX
    draft, view, controller, connection, _ = empty_scene
    draft.set_value(('request',), {'environment_id': 'env', 'environment_revision': 1})
    controller.set_environment_manifest({'base': BASE, 'objects': [{
        'object_id': 'existing', 'label': 'existing', 'editable': True, 'reason': None, 'box': deepcopy(BOX)}]})
    partial = {**deepcopy(BOX), 'pose': {'position': [1., 2., 3.]}}
    draft.set_value(('environment_edit',), {'base': BASE,
        'upserts': {'existing': partial, 'formed': deepcopy(BOX)}, 'removed': []})
    before, count = draft.document, draft.undo_stack.count()
    controller._render()
    controller.select('existing')
    assert 'orientation_wxyz' in view.reason.text()
    assert all(field.text() == '' for field in view.rotation_fields)
    controller.flush_preview()
    edit = next(item[3]['json']['environment_edit'] for item in connection.requests if item[1:3] == ('POST', '/previews'))
    assert edit['upserts'] == {'formed': BOX}
    assert edit['removed'] == ['existing']
    assert draft.document == before and draft.undo_stack.count() == count


@pytest.mark.parametrize('placements', [None, [], {'start': None}])
def test_incomplete_placement_container_is_not_exposed_to_geometry_mutations(empty_scene, placements):
    draft, view, controller, _, _ = empty_scene
    draft.set_value(('request',), {'scenario': {
        'entity_groups': [{'group_id': 'uav', 'count': 1, 'placement_id': 'start'}],
        'placements': deepcopy(placements),
    }})
    before = draft.document
    controller._render()
    controller.select('entity/uav/0')
    assert 'placements' in view.reason.text()
    assert not view.apply_count_button.isEnabled()
    assert not any(field.isEnabled() for field in view.position_fields)
    view.flush_inputs()
    assert draft.document == before
