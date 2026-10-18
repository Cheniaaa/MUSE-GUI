import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest

from muse_gui.experiments.experiment_draft import ExperimentDraft
from muse_gui.experiments.schema_editor import SchemaEditor, path_text


@pytest.fixture(autouse=True)
def dispose_editors(qapp):
    yield
    for widget in qapp.topLevelWidgets():
        if isinstance(widget, SchemaEditor):
            widget.close()
            widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def make_editor(qapp, schema, explicit=None, effective=None, pending=None):
    draft = ExperimentDraft()
    if explicit is not None:
        draft.set_value(("request", "task_config"), explicit)
    editor = SchemaEditor()
    editor.input_committed.connect(lambda path, value, origin, subtree: draft.commit_input(
        path, value, pending_path=path_text(origin), pending_subtree=subtree))
    editor.override_removed.connect(lambda path: draft.remove_override(path, pending_prefix=path_text(path)))
    editor.pending_changed.connect(draft.stage_input)
    editor.set_section({"path": ["request", "task_config"], "schema": schema,
                        "effective": effective or {}, "origins": {"/gain": "package"},
                        "missing": [], "errors": []}, explicit or {}, pending or {})
    editor.show()
    qapp.processEvents()
    return draft, editor


def enter(field, text):
    field.editor.setFocus()
    field.editor.selectAll()
    QTest.keyClicks(field.editor, text)
    QTest.keyClick(field.editor, Qt.Key.Key_Return)


def test_schema_absence_null_and_list_replacement(qapp):
    schema = {"type": "object", "properties": {
        "nullable_field": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "values": {"type": "array", "items": {"type": "integer"}},
    }}
    draft, editor = make_editor(qapp, schema, effective={"nullable_field": 4., "values": [1, 2]})
    field = editor.fields[("request", "task_config", "nullable_field")]
    field.mode.setCurrentIndex(field.mode.findData("null"))
    assert draft.document["request"]["task_config"]["nullable_field"] is None
    QTest.mouseClick(field.reset_button, Qt.MouseButton.LeftButton)
    assert "nullable_field" not in draft.document["request"]["task_config"]
    array = editor.fields[("request", "task_config", "values")]
    enter(array.children[0], "3")
    QTest.mouseClick(array.remove_buttons[1], Qt.MouseButton.LeftButton)
    assert draft.document["request"]["task_config"]["values"] == [3]
    editor.close()


def test_invalid_text_pending_survives_rebuild_and_file_roundtrip(qapp, tmp_path):
    schema = {"type": "object", "properties": {"gain": {"type": "number", "unit": "W"}}}
    draft, editor = make_editor(qapp, schema, explicit={"gain": 2.}, effective={"gain": 2.})
    field = editor.fields[("request", "task_config", "gain")]
    enter(field, "1e-")
    assert draft.document["pending_inputs"]["request.task_config.gain"] == "1e-"
    assert draft.document["request"]["task_config"]["gain"] == 2.
    assert field.error_label.text()
    path = tmp_path / "pending.muse.json"
    draft.save(path)
    draft.load(path)
    editor.set_section(editor.section, draft.document["request"]["task_config"], draft.document["pending_inputs"])
    field = editor.fields[("request", "task_config", "gain")]
    assert field.editor.text() == "1e-"
    enter(field, "0.25")
    assert not draft.document["pending_inputs"]
    assert draft.document["request"]["task_config"]["gain"] == .25
    editor.close()


def test_union_enum_nested_map_and_unknown_fields_preserved(qapp):
    schema = {"type": "object", "$defs": {"Nested": {"type": "object", "properties": {
        "choice": {"enum": ["a", "b"], "type": "string"}}}}, "properties": {
        "nested": {"$ref": "#/$defs/Nested"},
        "union": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
        "mapping": {"type": "object", "additionalProperties": {"type": "number"}},
    }, "additionalProperties": False}
    draft, editor = make_editor(qapp, schema, explicit={"nested": {"choice": "a"}, "mapping": {"x": 3.}, "unknown": {"keep": [1]}})
    union = editor.fields[("request", "task_config", "union")]
    union.type_combo.setCurrentIndex(union.type_combo.findData("string"))
    enter(union, "7")
    choice = editor.fields[("request", "task_config", "nested", "choice")]
    choice.editor.setCurrentIndex(choice.editor.findData("b"))
    enter(editor.fields[("request", "task_config", "mapping", "x")], "4.5")
    document = draft.document["request"]["task_config"]
    assert document["union"] == "7"
    assert document["nested"]["choice"] == "b"
    assert document["mapping"]["x"] == 4.5
    assert document["unknown"] == {"keep": [1]}
    editor.close()


def test_markov_edit_does_not_normalize(qapp):
    schema = {"type": "object", "properties": {"transition_matrix": {
        "type": "array", "items": {"type": "array", "items": {"type": "number"}}}}}
    draft, editor = make_editor(qapp, schema, explicit={"transition_matrix": [[.3, .7], [.1, .9]]})
    field = editor.fields[("request", "task_config", "transition_matrix")]
    field.editor.item(0, 0).setText("0.8")
    editor.flush()
    assert draft.document["request"]["task_config"]["transition_matrix"] == [[.8, .7], [.1, .9]]
    assert "1.5" in field.row_sums.text()
    field.editor.item(0, 1).setText("-")
    editor.flush()
    assert "-" in draft.document["pending_inputs"]["request.task_config.transition_matrix"]
    assert draft.document["request"]["task_config"]["transition_matrix"][0] == [.8, .7]
    assert editor.focus_field(("request", "task_config", "transition_matrix", "0", "1"))
    assert field.editor.currentRow() == 0
    assert field.editor.currentColumn() == 1
    editor.close()


def test_missing_origins_error_focus_and_unsupported_schema(qapp):
    draft, editor = make_editor(qapp, {"type": "object", "properties": {"gain": {"type": "number"}, "bad": {"type": "object", "allOf": []}}, "required": ["gain"]})
    section = dict(editor.section, missing=["/gain"], errors=[{"field": "gain", "message": "must be positive"}])
    editor.set_section(section, {}, {})
    field = editor.fields[("request", "task_config", "gain")]
    assert "package" in field.source_label.text()
    assert "must be positive" in field.error_label.text()
    assert editor.focus_field(("request", "task_config", "gain"))
    assert editor.fields[("request", "task_config", "bad")].error_label.text()
    assert draft.document["request"] == {}
    editor.close()


def test_union_switches_between_boolean_and_numeric_controls(qapp):
    schema = {"type": "object", "properties": {"choice": {"anyOf": [{"type": "boolean"}, {"type": "integer"}]}}}
    draft, editor = make_editor(qapp, schema, explicit={"choice": True})
    field = editor.fields[("request", "task_config", "choice")]
    field.type_combo.setCurrentIndex(field.type_combo.findData("integer"))
    enter(field, "5")
    assert draft.document["request"]["task_config"]["choice"] == 5
    assert type(draft.document["request"]["task_config"]["choice"]) is int
    editor.close()


def test_nested_list_object_edits_are_whole_list_replacements(qapp):
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}}}}}
    draft, editor = make_editor(qapp, schema, effective={"items": [{"x": 1, "y": 2}]})
    enter(editor.fields[("request", "task_config", "items", 0, "x")], "4")
    enter(editor.fields[("request", "task_config", "items", 0, "y")], "7")
    assert draft.document["request"]["task_config"]["items"] == [{"x": 4, "y": 7}]
    editor.close()


def test_list_effective_defaults_are_display_only_not_index_inheritance(qapp):
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer", "default": 5}}}}}}
    draft, editor = make_editor(qapp, schema, explicit={"items": [{"x": 1}]}, effective={"items": [{"x": 1, "y": 5}]})
    array = editor.fields[("request", "task_config", "items")]
    assert editor.fields[("request", "task_config", "items", 0, "y")].editor.text() == "5"
    assert array.children[0].mode.isHidden()
    assert array.children[0].reset_button.isHidden()
    assert draft.document["request"]["task_config"]["items"] == [{"x": 1}]
    enter(editor.fields[("request", "task_config", "items", 0, "x")], "2")
    assert draft.document["request"]["task_config"]["items"] == [{"x": 2}]
    editor.close()


def test_explicit_null_clears_its_hidden_child_pending_atomically(qapp):
    schema = {"type": "object", "properties": {"group": {"anyOf": [
        {"type": "object", "properties": {"gain": {"type": "number"}}}, {"type": "null"}]}}}
    draft, editor = make_editor(qapp, schema, explicit={"group": {"gain": 1.}})
    enter(editor.fields[("request", "task_config", "group", "gain")], "1e-")
    group = editor.fields[("request", "task_config", "group")]
    index = draft.undo_stack.index()
    group.mode.setCurrentIndex(group.mode.findData("null"))
    assert draft.document["request"]["task_config"]["group"] is None
    assert not draft.document["pending_inputs"]
    assert draft.undo_stack.index() == index + 1
    group.mode.setCurrentIndex(group.mode.findData("override"))
    editor.flush()
    assert draft.document["request"]["task_config"]["group"] == {}
    assert not draft.document["pending_inputs"]
    draft.undo_stack.undo()
    draft.undo_stack.undo()
    editor.set_section(editor.section, draft.document["request"]["task_config"], draft.document["pending_inputs"])
    editor.flush()
    assert draft.document["request"]["task_config"]["group"] == {"gain": 1.}
    assert not draft.document["pending_inputs"]
    draft.undo_stack.redo()
    assert draft.document["request"]["task_config"]["group"] is None
    assert not draft.document["pending_inputs"]
    editor.close()
