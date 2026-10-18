import json
from threading import Event

import pytest
from PySide6.QtTest import QSignalSpy

from muse_gui.experiments.experiment_draft import ExperimentDraft


def test_incomplete_draft_roundtrip(qapp, tmp_path):
    draft = ExperimentDraft()
    draft.set_value(("catalog_id",), "catalog-a")
    draft.set_value(("request", "environment"), {"id": "env-a", "revision": "rev-2"})
    draft.set_value(("request", "task_config"), {"nullable": None, "values": [3]})
    draft.set_pending("request.task_config.gain", "1e-")
    path = tmp_path / "incomplete.muse.json"
    draft.save(path)
    reopened = ExperimentDraft()
    reopened.load(path)
    assert reopened.document == draft.document
    assert reopened.document["format_version"] == 1
    assert "missing" not in reopened.document["request"]["task_config"]
    assert reopened.document["request"]["task_config"]["nullable"] is None
    assert reopened.document["pending_inputs"]["request.task_config.gain"] == "1e-"
    assert reopened.source_path == path
    assert not reopened.dirty
    assert "source_path" not in json.loads(path.read_text(encoding="utf-8"))


def test_one_undo_history_for_nested_values_pending_and_removal(qapp, tmp_path):
    draft = ExperimentDraft()
    draft.save(tmp_path / "draft.muse.json")
    original = draft.document
    changed = QSignalSpy(draft.changed)
    draft.set_value(("request", "algorithm_config", "gain"), None)
    draft.set_pending("request.seed", "-")
    draft.remove_override(("request", "algorithm_config", "gain"))
    assert draft.undo_stack.count() == 3
    assert "gain" not in draft.document["request"]["algorithm_config"]
    draft.undo_stack.undo()
    assert draft.document["request"]["algorithm_config"]["gain"] is None
    draft.undo_stack.undo()
    assert draft.document["pending_inputs"] == {}
    draft.undo_stack.undo()
    assert draft.document == original
    assert not draft.dirty
    assert draft.revision == 6
    assert changed.count() == 6
    draft.undo_stack.redo()
    assert draft.dirty


def test_capture_and_document_do_not_leak_mutable_state(qapp):
    draft = ExperimentDraft()
    values = {"values": [1, 2]}
    draft.set_value(("request", "task_config"), values)
    revision, captured = draft.capture()
    values["values"].append(3)
    captured["request"].clear()
    displayed = draft.document
    displayed["request"].clear()
    assert draft.revision == revision
    assert draft.document["request"]["task_config"] == {"values": [1, 2]}
    draft.set_value(("request", "task_config", "values", 0), 7)
    draft.undo_stack.undo()
    assert draft.document["request"]["task_config"]["values"] == [1, 2]


def test_noop_edits_do_not_advance_revision(qapp):
    draft = ExperimentDraft()
    draft.set_value(("name",), draft.document["name"])
    draft.remove_override(("request", "not_there"))
    draft.set_pending("request.seed", None)
    assert draft.revision == 0
    assert draft.undo_stack.count() == 0


def test_save_and_inspect_ignore_later_edits(qapp, tmp_path, wait_until, monkeypatch):
    import muse_gui.experiments.experiment_draft as module

    draft = ExperimentDraft()
    draft.set_value(("name",), "captured")
    n, captured = draft.capture()
    entered, release = Event(), Event()
    real_write = module.write_document

    def delayed_write(path, payload):
        entered.set()
        assert release.wait(5)
        real_write(path, payload)

    monkeypatch.setattr(module, "write_document", delayed_write)
    path = tmp_path / "captured.muse.json"
    draft.save_async(path)
    try:
        wait_until(entered.is_set)
        draft.set_value(("name",), "later")
        draft.mark_validated(n, {"ok": True})
    finally:
        release.set()
    wait_until(lambda: not draft.saving)
    assert json.loads(path.read_text(encoding="utf-8")) == captured
    assert draft.revision == n + 1
    assert draft.dirty is True
    assert draft.validated_revision != draft.revision
    assert draft.document["name"] == "later"


def test_old_save_cannot_rebind_replaced_document(qapp, tmp_path, wait_until, monkeypatch):
    import muse_gui.experiments.experiment_draft as module

    draft = ExperimentDraft()
    entered, release = Event(), Event()
    real_write = module.write_document

    def delayed_write(path, payload):
        entered.set()
        assert release.wait(5)
        real_write(path, payload)

    monkeypatch.setattr(module, "write_document", delayed_write)
    draft.save_async(tmp_path / "old.muse.json")
    try:
        wait_until(entered.is_set)
        draft.new()
        current = draft.document
    finally:
        release.set()
    wait_until(lambda: not draft.saving)
    assert draft.document == current
    assert draft.source_path is None


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"format_version": 2}', '{"format_version": 1, "request": null}'])
def test_load_invalid_file_keeps_current_draft(qapp, tmp_path, contents):
    draft = ExperimentDraft()
    draft.set_value(("name",), "keep")
    before = draft.capture()
    path = tmp_path / "bad.muse.json"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        draft.load(path)
    assert draft.capture() == before
    assert draft.dirty
    assert draft.undo_stack.canUndo()
    assert draft.source_path is None


def test_save_failure_keeps_path_baseline_and_dirty(qapp, tmp_path, wait_until):
    draft = ExperimentDraft()
    path = tmp_path / "good.muse.json"
    draft.save(path)
    draft.set_value(("name",), "edited")
    failed = QSignalSpy(draft.save_failed)
    draft.save_async(tmp_path / "missing" / "bad.muse.json")
    wait_until(lambda: not draft.saving)
    assert failed.count() == 1
    assert draft.source_path == path
    assert draft.dirty
    assert json.loads(path.read_text(encoding="utf-8"))["name"] != "edited"


def test_validation_is_separate_from_save_and_undo(qapp, tmp_path):
    draft = ExperimentDraft()
    draft.set_value(("request", "seed"), 3)
    n = draft.revision
    draft.mark_validated(n, {"errors": ["incomplete"]})
    assert draft.validated_revision == n
    assert draft.validation_report == {"errors": ["incomplete"]}
    assert draft.dirty
    draft.save(tmp_path / "draft.muse.json")
    assert draft.validated_revision == n
    draft.undo_stack.undo()
    assert draft.validated_revision != draft.revision
    assert draft.dirty


def test_atomic_input_command_preserves_interleaved_field_and_missing_parents(qapp):
    draft = ExperimentDraft()
    initial = draft.document
    revision = draft.revision
    draft.stage_input("request.task_config.a", "2")
    assert draft.revision == revision + 1
    assert draft.undo_stack.count() == 0
    draft.set_value(("request", "task_config", "b"), 3)
    draft.commit_input(("request", "task_config", "a"), 2, pending_path="request.task_config.a")
    assert draft.undo_stack.count() == 2
    draft.undo_stack.undo()
    assert draft.document["request"]["task_config"] == {"b": 3}
    assert not draft.document["pending_inputs"]
    draft.undo_stack.undo()
    assert draft.document == initial
    draft.undo_stack.redo()
    draft.undo_stack.redo()
    assert draft.document["request"]["task_config"] == {"a": 2, "b": 3}
    assert not draft.document["pending_inputs"]


def test_commit_same_value_only_finishes_staging_without_empty_undo(qapp, tmp_path):
    draft = ExperimentDraft()
    draft.set_value(("request", "root_seed"), 7)
    draft.save(tmp_path / "baseline.muse.json")
    index = draft.undo_stack.index()
    draft.stage_input("request.root_seed", "7")
    assert draft.dirty
    revision = draft.revision
    draft.commit_input(("request", "root_seed"), 7, pending_path="request.root_seed")
    assert draft.revision == revision + 1
    assert draft.undo_stack.index() == index
    assert not draft.dirty


def test_reset_input_subtree_is_atomic_and_cannot_restore_stale_raw(qapp):
    draft = ExperimentDraft()
    path = ("request", "task_config")
    draft.set_value(path, {"nullable": None, "values": [3]})
    draft.stage_input("request.task_config.nullable", "-")
    draft.stage_input("request.task_config.values.0", "1e-")
    draft.stage_input("request.other", "keep")
    draft.remove_override(path, pending_prefix="request.task_config")
    assert "task_config" not in draft.document["request"]
    assert draft.document["pending_inputs"] == {"request.other": "keep"}
    draft.undo_stack.undo()
    assert draft.document["request"]["task_config"] == {"nullable": None, "values": [3]}
    assert draft.document["pending_inputs"] == {"request.other": "keep"}
    draft.undo_stack.redo()
    assert "task_config" not in draft.document["request"]
    assert draft.document["pending_inputs"] == {"request.other": "keep"}


def test_atomic_union_type_choice_is_not_an_empty_numeric_edit(qapp, tmp_path):
    draft = ExperimentDraft()
    path = ("request", "task_config", "value")
    draft.set_value(path, 7)
    draft.save(tmp_path / "integer.muse.json")
    draft.stage_input("request.task_config.value", "7.0")
    draft.commit_input(path, 7., pending_path="request.task_config.value")
    assert type(draft.document["request"]["task_config"]["value"]) is float
    assert draft.dirty
    draft.undo_stack.undo()
    assert type(draft.document["request"]["task_config"]["value"]) is int
    assert not draft.document["pending_inputs"]
    draft.undo_stack.redo()
    assert type(draft.document["request"]["task_config"]["value"]) is float
