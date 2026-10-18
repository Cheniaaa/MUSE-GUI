from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QEvent, QObject, Signal, Qt
from PySide6.QtTest import QTest

from muse_gui.experiments.configuration_controller import ConfigurationController
from muse_gui.experiments.experiment_draft import ExperimentDraft
from muse_gui.experiments.research_config_view import ResearchConfigView


@pytest.fixture(autouse=True)
def process_owned_deletions(qapp):
    yield
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class DeferredConnection(QObject):
    result_received = Signal(int, str, dict)
    state_changed = Signal(dict)

    def __init__(self):
        super().__init__()
        self.state = {"state": "connected", "generation": 1, "catalog_id": "catalog-a", "instance_id": "instance-a"}
        self.sent = []

    def request(self, identity, method, path, **kwargs):
        self.sent.append((identity, method, path, deepcopy(kwargs)))


def catalog():
    selection = {"selection_id": "same", "term": "obs", "required": False, "default_enabled": True, "config": {}}
    role = {"entity_kind": "aerial_vehicle", "observation_terms": [selection, dict(selection, selection_id="second")],
            "reward_terms": [dict(selection, term="reward")], "cost_terms": []}
    composition = {"task": None, "algorithm": "algo", "agent_roles": {"alpha": role, "beta": deepcopy(role)},
                   "critic_inputs": {}, "world_reward_terms": [], "world_cost_terms": [], "world_termination_terms": [],
                   "model_bindings": {"radio": {"default": "builtin:radio-a", "options": {"builtin:radio-a": {"config": {}}, "builtin:radio-b": {"config": {}}}},
                                      "sensing": {"default": "sense", "options": {"sense": {"config": {}}}}}}
    descriptor = {"compositions": {"first": composition, "second": deepcopy(composition)},
                  "default_composition_id": "first",
                  "algorithms": {"algo": {"training_schedule": {"collection_steps": "rollout"}}},
                  "sensing_models": {"sense": {"assumptions": {"scope": "declared"}}}}
    properties = {"root_seed": {"type": "integer"}, "topology": {"type": "object", "properties": {"world_count": {"type": "integer"}}},
                  "scenario": {"type": "object", "properties": {"scene": {"type": "object", "properties": {"physics_dt": {"type": "number"}, "control_substeps": {"type": "integer"}}},
                  "entity_groups": {"type": "array", "items": {"type": "object", "properties": {"group_id": {"type": "string"}, "model_id": {"type": "string"}, "model_revision": {"type": "integer"}, "sensor_profile_id": {"type": "string"}, "controller_id": {"type": "string"}}}}}}}
    schema = {"api_revision": 2, "experiment_request": {"type": "object", "properties": properties},
              "run_request": {"type": "object", "properties": {"experiment_id": {"type": "string"}, "mode": {"type": "string", "enum": ["TRAIN", "EVALUATE", "INTERACTIVE"]}, "max_decision_steps": {"type": "integer"}, "checkpoint_interval_updates": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}
    return {"schema": schema, "training_packages": [{"resource_id": "pkg", "revision": 2, "metadata": descriptor}],
            "networks": [{"kind": "radio", "reference": "builtin:" + name, "descriptor": {"assumptions": {"source": name}}} for name in ("radio-a", "radio-b", "unrelated")],
            "entity_models": [{"resource_id": "model", "revision": 1, "metadata": {"sensor_profile_ids": ["none", "imu"], "controller_ids": ["ctrl"], "radio_devices": {}}}],
            "controllers": [{"id": "ctrl"}], "sensors": [{"sensor_profile_id": name, "config_schema": {"type": "object", "properties": {}, "additionalProperties": False}} for name in ("none", "imu", "wrong")]}


@pytest.fixture
def configured(qapp):
    draft = ExperimentDraft()
    draft.set_value(("request",), {"training_package_id": "pkg", "training_package_revision": 2, "composition_id": "first", "root_seed": 7, "topology": {"world_count": 8}, "scenario": {"scene": {"physics_dt": .002, "control_substeps": 5}, "entity_groups": [{"group_id": "uav", "model_id": "model", "model_revision": 1, "sensor_profile_id": "imu", "controller_id": "ctrl"}]}})
    connection = DeferredConnection()
    view = ResearchConfigView()
    controller = ConfigurationController(draft, connection, view, confirm=lambda title, detail: True)
    controller.set_catalog(**catalog())
    view.show()
    qapp.processEvents()
    yield draft, connection, view, controller
    view.close()
    view.deleteLater()


def test_selection_owner_history_and_weight(configured):
    draft, _, view, _ = configured
    first = ("request", "term_config", "roles", "alpha", "observation", "same")
    second = ("request", "term_config", "roles", "alpha", "observation", "second")
    beta = ("request", "term_config", "roles", "beta", "observation", "same")
    fields = view.selection_editors[first].fields
    assert (*first, "history_length") in fields
    assert (*first, "weight") not in fields
    field = fields[(*first, "history_length")]
    field.editor.setText("4")
    view.flush()
    assert draft.document["request"]["term_config"]["roles"]["alpha"]["observation"]["same"]["history_length"] == 4
    assert "second" not in draft.document["request"]["term_config"]["roles"]["alpha"]["observation"]
    assert (*beta, "weight") not in view.selection_editors[beta].fields
    reward = ("request", "term_config", "roles", "alpha", "reward", "same")
    assert (*reward, "weight") in view.selection_editors[reward].fields
    assert (*reward, "history_length") not in view.selection_editors[reward].fields
    assert "beta" not in draft.document["request"]["term_config"]["roles"]


def test_model_change_clears_only_selected_kind_after_confirmation(configured):
    draft, _, view, controller = configured
    draft.set_value(("request", "model_config"), {"radio": {"old": 1}, "sensing": {"keep": 2}})
    draft.set_pending("request.model_config.radio.old", "-")
    before = draft.capture()
    controller.confirm = lambda title, detail: False
    controller.select_model("radio", "builtin:radio-b")
    assert draft.capture() == before
    controller.confirm = lambda title, detail: True
    controller.select_model("radio", "builtin:radio-b")
    request = draft.document["request"]
    assert "radio" not in request["model_config"]
    assert request["model_config"]["sensing"] == {"keep": 2}
    assert request["model_choices"]["radio"] == "builtin:radio-b"
    assert not draft.document["pending_inputs"]
    assert view.model_combos["radio"].findData("builtin:unrelated") == -1
    draft.undo_stack.undo()
    assert draft.document == before[1]


def test_composition_confirmation_precise_clear_and_binding_recheck(configured):
    draft, _, _, controller = configured
    for key in ("task_config", "algorithm_config", "term_config", "cost_config", "model_config", "model_choices"):
        draft.set_value(("request", key), {"old": 1})
    draft.set_value(("run_options",), {"mode": "TRAIN", "max_decision_steps": 99})
    draft.set_value(("request", "role_config"), {"alpha": {"group_id": "uav", "slots": [0]}})
    before = draft.document
    controller.confirm = lambda *args: False
    controller.select_composition("second")
    assert draft.document == before
    controller.confirm = lambda *args: True
    controller.select_composition("second")
    request = draft.document["request"]
    assert not any(key in request for key in ("task_config", "algorithm_config", "term_config", "cost_config", "model_config", "model_choices"))
    for key in ("scenario", "topology", "root_seed", "role_config"):
        assert request[key] == before["request"][key]
    assert draft.document["run_options"] == before["run_options"]
    assert draft.validated_revision is None


def test_disabled_selection_keeps_valid_config(configured):
    draft, _, view, _ = configured
    path = ("request", "term_config", "roles", "alpha", "observation", "same")
    draft.set_value((*path, "config"), {"gain": 2.5})
    field = view.selection_editors[path].fields[(*path, "enabled")]
    field.editor.setCurrentIndex(field.editor.findData(False))
    assert draft.document["request"]["term_config"]["roles"]["alpha"]["observation"]["same"] == {"config": {"gain": 2.5}, "enabled": False}


def test_inspection_captures_revision_and_drops_late_or_old_generation(configured):
    draft, connection, view, controller = configured
    controller.inspect()
    identity, method, path, kwargs = connection.sent[-1]
    assert (method, path) == ("POST", "/experiments/inspect")
    assert kwargs["json"] == draft.document["request"]
    draft.set_value(("request", "root_seed"), 9)
    report = {"sections": [], "errors": [], "resolved_experiment": None}
    connection.result_received.emit(1, identity, {"ok": True, "data": report})
    assert draft.validated_revision is None
    controller.inspect()
    identity = connection.sent[-1][0]
    connection.state["generation"] = 2
    connection.result_received.emit(1, identity, {"ok": True, "data": report})
    assert draft.validated_revision is None
    controller.inspect()
    identity = connection.sent[-1][0]
    connection.result_received.emit(2, identity, {"ok": True, "data": report})
    assert draft.validated_revision == draft.revision


def test_pending_is_saved_before_inspect_and_blocks_stale_numeric_request(configured):
    draft, connection, view, controller = configured
    field = view.fixed_editor.fields[("request", "root_seed")]
    field.editor.setText("-")
    controller.inspect()
    assert draft.document["pending_inputs"]["request.root_seed"] == "-"
    assert not connection.sent
    assert "root_seed" in view.status_label.text()


def test_external_edit_rebuild_keeps_focused_pending_text(configured, qapp):
    draft, _, view, _ = configured
    field = view.fixed_editor.fields[("request", "root_seed")]
    field.editor.setFocus()
    field.editor.selectAll()
    QTest.keyClicks(field.editor, "1e-")
    draft.set_value(("request", "topology", "world_count"), 12)
    qapp.processEvents()
    assert view.fixed_editor.fields[("request", "root_seed")].editor.text() == "1e-"
    assert draft.document["pending_inputs"]["request.root_seed"] == "1e-"


def test_field_input_one_global_undo_redo_and_save(configured, qapp, tmp_path):
    draft, connection, view, controller = configured
    path = tmp_path / "atomic.muse.json"
    draft.save(path)
    index = draft.undo_stack.index()
    controller.inspect()
    request_id = connection.sent[-1][0]
    revision = draft.revision
    edit = view.fixed_editor.fields[("request", "root_seed")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "123")
    assert draft.revision > revision
    assert draft.document["request"]["root_seed"] == 7
    assert draft.document["pending_inputs"]["request.root_seed"] == "123"
    connection.result_received.emit(1, request_id, {"ok": True, "data": {"sections": [], "errors": [], "resolved_experiment": None}})
    assert draft.validated_revision is None
    QTest.keyClick(edit, Qt.Key.Key_Return)
    edit.clearFocus()
    assert draft.document["request"]["root_seed"] == 123
    assert not draft.document["pending_inputs"]
    assert draft.undo_stack.index() == index + 1
    draft.undo_stack.undo()
    view.flush()
    draft.save(path)
    assert draft.document["request"]["root_seed"] == 7
    assert not draft.document["pending_inputs"]
    assert json.loads(path.read_text(encoding="utf-8"))["request"]["root_seed"] == 7
    draft.undo_stack.redo()
    view.flush()
    assert draft.document["request"]["root_seed"] == 123
    assert not draft.document["pending_inputs"]


def test_cross_field_undo_redo_never_resurrects_committed_pending(configured, qapp):
    draft, _, view, _ = configured
    original = draft.document
    draft.undo_stack.clear()
    edit = view.fixed_editor.fields[("request", "root_seed")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "123")
    draft.set_value(("request", "topology", "world_count"), 12)
    qapp.processEvents()
    edit = view.fixed_editor.fields[("request", "root_seed")].editor
    QTest.keyClick(edit, Qt.Key.Key_Return)
    edit.clearFocus()
    assert draft.undo_stack.count() == 2
    draft.undo_stack.undo()
    assert draft.document["request"]["root_seed"] == 7
    assert draft.document["request"]["topology"]["world_count"] == 12
    assert not draft.document["pending_inputs"]
    draft.undo_stack.undo()
    assert draft.document == original
    draft.undo_stack.redo()
    assert draft.document["request"]["topology"]["world_count"] == 12
    assert not draft.document["pending_inputs"]
    draft.undo_stack.redo()
    view.flush()
    assert draft.document["request"]["root_seed"] == 123
    assert not draft.document["pending_inputs"]


def test_invalid_empty_input_can_save_and_reset_without_pending_revival(configured, tmp_path):
    draft, _, view, _ = configured
    edit = view.fixed_editor.fields[("request", "root_seed")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClick(edit, Qt.Key.Key_Backspace)
    view.flush()
    path = tmp_path / "empty.muse.json"
    draft.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["pending_inputs"]["request.root_seed"] == ""
    QTest.mouseClick(view.fixed_editor.fields[("request", "root_seed")].reset_button, Qt.MouseButton.LeftButton)
    assert "root_seed" not in draft.document["request"]
    assert not draft.document["pending_inputs"]
    draft.undo_stack.undo()
    view.flush()
    assert draft.document["request"]["root_seed"] == 7
    assert not draft.document["pending_inputs"]
    draft.undo_stack.redo()
    assert "root_seed" not in draft.document["request"]
    assert not draft.document["pending_inputs"]


def test_array_commit_clears_only_its_originating_input(configured):
    draft, _, view, controller = configured
    records = catalog()
    props = records["schema"]["experiment_request"]["properties"]["scenario"]["properties"]["entity_groups"]["items"]["properties"]
    props.update(count={"type": "integer"}, other={"type": "number"})
    base = ("request", "scenario", "entity_groups", 0)
    draft.set_value((*base, "count"), 1)
    draft.set_value((*base, "other"), 10.)
    controller.set_catalog(**records)
    edit = view.fixed_editor.fields[(*base, "other")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "1e-")
    edit = view.fixed_editor.fields[(*base, "count")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "2")
    QTest.keyClick(edit, Qt.Key.Key_Return)
    key = "request.scenario.entity_groups.0.other"
    assert draft.document["pending_inputs"] == {key: "1e-"}
    assert draft.document["request"]["scenario"]["entity_groups"][0]["count"] == 2
    draft.undo_stack.undo()
    assert draft.document["request"]["scenario"]["entity_groups"][0]["count"] == 1
    assert draft.document["pending_inputs"] == {key: "1e-"}
    draft.undo_stack.redo()
    assert draft.document["request"]["scenario"]["entity_groups"][0]["count"] == 2
    assert draft.document["pending_inputs"] == {key: "1e-"}


def test_array_child_reset_is_atomic_and_requests_fresh_inspection(configured, qapp):
    draft, connection, view, controller = configured
    records = catalog()
    props = records["schema"]["experiment_request"]["properties"]["scenario"]["properties"]["entity_groups"]["items"]["properties"]
    props["count"] = {"type": "integer"}
    path = ("request", "scenario", "entity_groups", 0, "count")
    draft.set_value(path, 1)
    controller.set_catalog(**records)
    field = view.fixed_editor.fields[path]
    field.editor.setFocus()
    field.editor.selectAll()
    QTest.keyClick(field.editor, Qt.Key.Key_Backspace)
    index = draft.undo_stack.index()
    QTest.mouseClick(field.reset_button, Qt.MouseButton.LeftButton)
    assert "count" not in draft.document["request"]["scenario"]["entity_groups"][0]
    assert not draft.document["pending_inputs"]
    assert draft.undo_stack.index() == index + 1
    qapp.processEvents()
    assert connection.sent[-1][2] == "/experiments/inspect"
    assert "count" not in connection.sent[-1][3]["json"]["scenario"]["entity_groups"][0]
    draft.undo_stack.undo()
    view.flush()
    assert draft.document["request"]["scenario"]["entity_groups"][0]["count"] == 1
    assert not draft.document["pending_inputs"]


def test_array_structure_uses_pending_from_the_shared_sensor_editor(configured):
    draft, _, view, controller = configured
    records = catalog()
    records["sensors"][1]["config_schema"] = {"type": "object", "properties": {"rate": {"type": "number"}}}
    base = ("request", "scenario", "entity_groups", 0, "sensor_config")
    draft.set_value(base, {"rate": 10.})
    controller.set_catalog(**records)
    sensor = next(editor for editor in view._editors if tuple(editor.section["path"]) == base)
    edit = sensor.fields[(*base, "rate")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "1e-")
    array = view.fixed_editor.fields[("request", "scenario", "entity_groups")]
    QTest.mouseClick(array.remove_buttons[0], Qt.MouseButton.LeftButton)
    assert len(draft.document["request"]["scenario"]["entity_groups"]) == 1
    assert draft.document["pending_inputs"] == {"request.scenario.entity_groups.0.sensor_config.rate": "1e-"}


@pytest.mark.parametrize("action", ["null", "reset"])
def test_parent_cancellation_flush_does_not_revive_shared_sensor_input(configured, action):
    draft, _, view, controller = configured
    records = catalog()
    scenario = records["schema"]["experiment_request"]["properties"]["scenario"]
    records["schema"]["experiment_request"]["properties"]["scenario"] = {"anyOf": [scenario, {"type": "null"}]}
    records["sensors"][1]["config_schema"] = {"type": "object", "properties": {"rate": {"type": "number"}}}
    base = ("request", "scenario", "entity_groups", 0, "sensor_config")
    draft.set_value(base, {"rate": 10.})
    controller.set_catalog(**records)
    sensor = next(editor for editor in view._editors if tuple(editor.section["path"]) == base)
    edit = sensor.fields[(*base, "rate")].editor
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "1e-")
    draft.stage_input("run_options.unrelated", "keep")
    parent = view.fixed_editor.fields[("request", "scenario")]
    index = draft.undo_stack.index()
    if action == "null":
        parent.mode.setCurrentIndex(parent.mode.findData("null"))
    else:
        QTest.mouseClick(parent.reset_button, Qt.MouseButton.LeftButton)
    assert draft.document["pending_inputs"] == {"run_options.unrelated": "keep"}
    assert draft.undo_stack.index() == index + 1
    # 不先处理排队重建：捕获/保存可以在同一次 Qt 事件中立即 flush。
    view.flush()
    assert draft.document["pending_inputs"] == {"run_options.unrelated": "keep"}
    if action == "null":
        assert draft.document["request"]["scenario"] is None
    else:
        assert "scenario" not in draft.document["request"]
    draft.undo_stack.undo()
    view.flush()
    assert draft.document["request"]["scenario"]["entity_groups"][0]["sensor_config"] == {"rate": 10.}
    assert draft.document["pending_inputs"] == {"run_options.unrelated": "keep"}
    draft.undo_stack.redo()
    view.flush()
    assert draft.document["pending_inputs"] == {"run_options.unrelated": "keep"}


def test_select_package_confirms_and_reuses_precise_component_clear(configured):
    draft, connection, _, controller = configured
    for key in controller.CLEAR_ON_COMPOSITION:
        draft.set_value(("request", key), {"old": 1})
    draft.set_value(("environment_edit",), {"keep": "environment"})
    draft.set_value(("run_options",), {"mode": "TRAIN", "max_decision_steps": 97})
    draft.set_pending("request.algorithm_config.old", "-")
    draft.set_pending("request.scenario.extra", "preserve")
    record = deepcopy(catalog()["training_packages"][0])
    record.update(resource_id="new-package", revision=3, kind="training_package")
    record["metadata"]["default_composition_id"] = "second"
    before = draft.capture()
    controller.confirm = lambda *args: False
    assert controller.select_package(record) is False
    assert draft.capture() == before
    controller.confirm = lambda *args: True
    assert controller.select_package(record) is True
    result = draft.document
    assert result["request"]["training_package_id"] == "new-package"
    assert result["request"]["training_package_revision"] == 3
    assert result["request"]["composition_id"] == "second"
    assert not any(key in result["request"] for key in controller.CLEAR_ON_COMPOSITION)
    for key in ("scenario", "topology", "root_seed"):
        assert result["request"][key] == before[1]["request"][key]
    assert result["run_options"] == before[1]["run_options"]
    assert result["environment_edit"] == before[1]["environment_edit"]
    assert result["catalog_id"] == before[1]["catalog_id"]
    assert result["pending_inputs"] == {"request.scenario.extra": "preserve"}
    draft.undo_stack.undo()
    assert draft.document == before[1]
    draft.undo_stack.redo()
    assert draft.document == result
    assert all(request[1:3] != ("POST", "/runs") for request in connection.sent)


def test_select_package_same_identity_is_noop(configured):
    draft, connection, _, controller = configured
    before = draft.capture()
    controller.confirm = lambda *args: pytest.fail("相同资源身份不应再次确认/清理")
    assert controller.select_package(catalog()["training_packages"][0]) is True
    assert draft.capture() == before
    assert not connection.sent


def test_select_package_does_not_invent_composition_for_empty_descriptor(configured):
    draft, _, _, controller = configured
    record = deepcopy(catalog()["training_packages"][0])
    record.update(resource_id="empty", revision=1)
    record["metadata"].update(compositions={}, default_composition_id=None)
    assert controller.select_package(record) is True
    assert "composition_id" not in draft.document["request"]


def test_cancel_model_change_does_not_flush_or_discard_pending(configured):
    draft, _, view, controller = configured
    field = view.fixed_editor.fields[("request", "root_seed")]
    field.editor.setFocus()
    field.editor.selectAll()
    QTest.keyClicks(field.editor, "123")
    before = draft.capture()
    controller.confirm = lambda *args: False
    controller.select_model("radio", "builtin:radio-b")
    assert draft.capture() == before
    assert view.fixed_editor.fields[("request", "root_seed")].editor.text() == "123"


def test_restore_default_deletes_override_and_requests_fresh_effective(configured, qapp):
    draft, connection, view, _ = configured
    QTest.mouseClick(view.fixed_editor.fields[("request", "root_seed")].reset_button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert "root_seed" not in draft.document["request"]
    assert connection.sent[-1][2] == "/experiments/inspect"
    assert "root_seed" not in connection.sent[-1][3]["json"]


def test_run_options_ownership_and_sensor_choices(configured):
    draft, _, view, _ = configured
    field = view.run_editor.fields[("run_options", "max_decision_steps")]
    field.editor.setText("123")
    view.flush()
    assert draft.document["run_options"]["max_decision_steps"] == 123
    assert "max_decision_steps" not in draft.document["request"]
    sensors = view.fixed_editor.fields[("request", "scenario", "entity_groups", 0, "sensor_profile_id")].editor
    assert sensors.findData("imu") >= 0
    assert sensors.findData("wrong") == -1


def test_pending_run_option_error_locates_its_actual_control(configured):
    _, connection, view, controller = configured
    located = []
    view.field_located.connect(located.append)
    view.run_editor.fields[("run_options", "max_decision_steps")].editor.setText("-")
    controller.inspect()
    assert not connection.sent
    assert located[-1].path == ("run_options", "max_decision_steps")
    assert view.details_view.isReadOnly()


def test_entity_list_edit_keeps_sensor_section_edits_from_same_draft(configured):
    draft, _, view, controller = configured
    records = catalog()
    group_schema = records["schema"]["experiment_request"]["properties"]["scenario"]["properties"]["entity_groups"]["items"]
    group_schema["properties"].update(count={"type": "integer"}, sensor_config={"type": "object", "additionalProperties": True})
    sensor_path = ("request", "scenario", "entity_groups", 0, "sensor_config")
    draft.set_value(("request", "scenario", "entity_groups", 0, "count"), 1)
    draft.set_value(sensor_path, {"rate": 10.})
    controller.set_catalog(**records)
    view.show_report({"sections": [{"path": list(sensor_path[1:]), "schema": {"type": "object", "properties": {"rate": {"type": "number"}}}, "effective": {"rate": 10.}, "origins": {"/rate": "scenario"}, "missing": [], "errors": []}], "errors": [], "resolved_experiment": None})
    sensor_editor = next(editor for editor in view._editors if tuple(editor.section["path"]) == sensor_path)
    located = []
    view.field_located.connect(located.append)
    assert view.focus_error("scenario.entity_groups[0].sensor_config.rate")
    assert located[-1] is sensor_editor.fields[(*sensor_path, "rate")]
    sensor_editor.fields[(*sensor_path, "rate")].editor.setText("20")
    view.flush()
    count = view.fixed_editor.fields[("request", "scenario", "entity_groups", 0, "count")]
    count.editor.setText("2")
    view.flush()
    assert draft.document["request"]["scenario"]["entity_groups"][0]["sensor_config"]["rate"] == 20.
    assert sensor_path not in view.fixed_editor.fields


def test_other_catalog_cannot_supply_same_named_package_choices(configured):
    draft, _, view, controller = configured
    draft.set_value(("catalog_id",), "catalog-foreign")
    controller.set_catalog(**catalog())
    assert view.composition_combo.findData("first") == -1
    assert not view.model_rows["radio"].isVisible()


@pytest.mark.parametrize("succeed", [True, False])
def test_catalog_pagination_is_not_published_until_all_pages_succeed(configured, succeed):
    _, connection, view, controller = configured
    controller.refresh_catalog()
    records = catalog()
    first_requests = list(connection.sent)
    for identity, _, path, _ in first_requests:
        key = next(key for key, value in controller.CATALOG_PATHS.items() if value == path)
        if key == "networks":
            data = deepcopy(records[key][:2])
            data[0]["descriptor"]["assumptions"] = {"updated": True}
            response = {"items": data, "next_offset": 50}
        elif key == "schema":
            response = records[key]
        else:
            response = {"items": records[key], "next_offset": None}
        connection.result_received.emit(1, identity, {"ok": True, "data": response})
        if key == "networks" and not succeed:
            connection.result_received.emit(1, connection.sent[-1][0], {"ok": False, "error": {"kind": "connection", "message": "interrupted"}})
    assert '"updated"' not in view.details_view.toPlainText()
    page = connection.sent[-1]
    assert page[2] == "/catalog/networks"
    assert page[3]["params"]["offset"] == 50
    if succeed:
        connection.result_received.emit(1, page[0], {"ok": True, "data": {"items": records["networks"][2:], "next_offset": None}})
    assert ('"updated"' in view.details_view.toPlainText()) is succeed


def test_workbench_flush_is_actual_configuration_consumer(qapp, tmp_path):
    from PySide6.QtCore import QSettings
    from muse_gui.app.main_window import MuseWindow
    from muse_gui.app.workbench_settings import WorkbenchSettings
    from muse_gui.app.main import mount_configuration

    draft = ExperimentDraft()
    window = MuseWindow(draft, WorkbenchSettings(QSettings(str(tmp_path / "view.ini"), QSettings.Format.IniFormat)))
    connection = DeferredConnection()
    controller = mount_configuration(window, draft, connection)
    controller.set_catalog(**catalog())
    assert window.configuration_view.fixed_editor.root_field.mode.isHidden()
    assert window.configuration_view.fixed_editor.root_field.reset_button.isHidden()
    field = window.configuration_view.fixed_editor.fields[("request", "root_seed")]
    field.editor.setText("invalid")
    window.workbench.flush_inputs()
    assert draft.document["pending_inputs"]["request.root_seed"] == "invalid"
    window.show()
    qapp.processEvents()
    located = []
    window.configuration_view.field_located.connect(located.append)
    item = window.workbench.tree.topLevelItem(1).child(4)
    QTest.mouseClick(window.workbench.tree.viewport(), Qt.MouseButton.LeftButton, pos=window.workbench.tree.visualItemRect(item).center())
    assert located[-1] is window.configuration_view.run_editor
    assert "已连接" in window.workbench.connection_label.text()
    window.workbench.set_context("run", "readonly-context")
    assert window.configuration_view.inspect_button.isHidden()
    assert not window.configuration_view.details_view.isVisible()
    window._ask_unsaved = lambda: "discard"
    window.close()
    window.deleteLater()
