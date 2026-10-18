import json
from threading import Event

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import LineEdit

from muse_gui.app.main_window import MuseWindow, UnsavedDraftDialog
from muse_gui.app.workbench_settings import WorkbenchSettings
from muse_gui.experiments.experiment_draft import ExperimentDraft


@pytest.fixture
def window(qapp, tmp_path):
    preferences = WorkbenchSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    window = MuseWindow(ExperimentDraft(), preferences)
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    yield window
    window._ask_unsaved = lambda: "discard"
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_context_switch_keeps_panels_and_run_readonly(window, qapp):
    view = window.workbench
    panels = (view.tree, view.horizontal_splitter, view.inspector_host, view.draft_config_host)
    assert view.context_kind == "draft"
    assert not view.read_only
    assert view.metrics_host.isHidden()
    assert view.context_tabs.count() == 1
    view.set_context("run", "run-real-id")
    qapp.processEvents()
    assert view.read_only
    assert view.name_edit.isReadOnly()
    assert "run-real-id" in view.context_label.text()
    assert not view.draft_config_host.isVisible()
    assert view.context_tabs.count() == 2
    view.set_context("draft", window.draft.document["draft_id"])
    qapp.processEvents()
    assert not view.name_edit.isReadOnly()
    assert panels == (view.tree, view.horizontal_splitter, view.inspector_host, view.draft_config_host)
    assert view.metrics_host.isHidden()


def test_splitters_collapse_restore_and_wide_scrolling_consumer(window, qapp):
    view = window.workbench
    assert view.output_collapsed
    QTest.mouseClick(view.output_toggle, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert not view.output_collapsed
    assert view.output_content.isVisible()
    view.horizontal_splitter.setSizes([180, 660, 260])
    view.restore_layout()
    assert view.output_collapsed
    assert view.horizontal_splitter.sizes()[1] > view.horizontal_splitter.sizes()[0]
    content = QWidget()
    layout = QVBoxLayout(content)
    edits = []
    for index in range(35):
        edit = LineEdit(content)
        edit.setText(f"consumer field {index}")
        layout.addWidget(edit)
        edits.append(edit)
    view.draft_config_host.layout().addWidget(content)
    view.set_page("config")
    qapp.processEvents()
    view.config_scroll.ensureWidgetVisible(edits[-1])
    qapp.processEvents()
    assert view.config_scroll.verticalScrollBar().value() > 0


def test_save_open_save_as_and_recent_files(window, tmp_path, wait_until, monkeypatch):
    first, second = tmp_path / "first.muse.json", tmp_path / "second.muse.json"
    paths = iter([first, second])
    monkeypatch.setattr(window, "_choose_save_path", lambda: next(paths))
    window.draft.set_value(("name",), "first")
    window.save_draft()
    wait_until(lambda: not window.draft.saving)
    window.draft.set_value(("name",), "second")
    window.save_draft(save_as=True)
    wait_until(lambda: not window.draft.saving)
    assert json.loads(first.read_text(encoding="utf-8"))["name"] == "first"
    assert json.loads(second.read_text(encoding="utf-8"))["name"] == "second"
    window.open_draft(first)
    assert window.draft.document["name"] == "first"
    assert window.preferences.recent_files[:2] == [str(first), str(second)]


@pytest.mark.parametrize("choice", ["save", "discard", "cancel"])
def test_open_dirty_save_discard_cancel(window, tmp_path, wait_until, monkeypatch, choice):
    target = tmp_path / "target.muse.json"
    other = ExperimentDraft()
    other.set_value(("name",), "target")
    other.save(target)
    saved = tmp_path / "saved.muse.json"
    window.draft.set_value(("name",), "current")
    monkeypatch.setattr(window, "_ask_unsaved", lambda: choice)
    monkeypatch.setattr(window, "_choose_save_path", lambda: saved)
    window.open_draft(target)
    wait_until(lambda: not window.draft.saving)
    if choice == "cancel":
        assert window.draft.document["name"] == "current"
        assert window.draft.dirty
    else:
        assert window.draft.document["name"] == "target"
        assert not window.draft.dirty
    assert saved.exists() == (choice == "save")


def test_save_as_cancel_does_not_replace_dirty_draft(window, tmp_path, monkeypatch):
    window.draft.set_value(("name",), "current")
    monkeypatch.setattr(window, "_ask_unsaved", lambda: "save")
    monkeypatch.setattr(window, "_choose_save_path", lambda: None)
    window.new_draft()
    assert window.draft.document["name"] == "current"
    assert window.draft.dirty


def test_close_cancel_and_save(window, tmp_path, wait_until, monkeypatch):
    window.draft.set_value(("name",), "unsaved")
    monkeypatch.setattr(window, "_ask_unsaved", lambda: "cancel")
    assert not window.close()
    assert window.isVisible()
    path = tmp_path / "closed.muse.json"
    monkeypatch.setattr(window, "_ask_unsaved", lambda: "save")
    monkeypatch.setattr(window, "_choose_save_path", lambda: path)
    window.close()
    wait_until(lambda: not window.isVisible())
    assert path.exists()


def test_text_focus_undo_does_not_undo_draft(window, qapp):
    window.draft.set_value(("request", "seed"), 7)
    edit = window.workbench.name_edit
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "new name")
    QTest.keyClick(edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    qapp.processEvents()
    assert window.draft.document["request"]["seed"] == 7
    assert edit.text() != "new name"


def test_uncommitted_name_survives_other_draft_edits(window, qapp):
    edit = window.workbench.name_edit
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "unfinished name")
    window.draft.set_value(("request", "seed"), 17)
    qapp.processEvents()
    assert edit.text() == "unfinished name"
    window.workbench.flush_inputs()
    assert window.draft.document["name"] == "unfinished name"
    assert window.draft.document["request"]["seed"] == 17


def test_run_does_not_display_active_draft_revision(window, qapp):
    view = window.workbench
    assert view.revision_label.isVisible()
    view.set_context("run", "run-context")
    window.draft.set_value(("request", "seed"), 19)
    qapp.processEvents()
    assert not view.revision_label.isVisible()
    view.set_context("draft", window.draft.document["draft_id"])
    qapp.processEvents()
    assert view.revision_label.isVisible()


def test_close_during_save_keeps_later_edits_and_thread_host(window, tmp_path, wait_until, monkeypatch):
    import muse_gui.experiments.experiment_draft as module

    entered, release = Event(), Event()
    real_write = module.write_document

    def delayed_write(path, payload):
        entered.set()
        assert release.wait(5)
        real_write(path, payload)

    monkeypatch.setattr(module, "write_document", delayed_write)
    path = tmp_path / "late-close.muse.json"
    window.draft.set_value(("name",), "captured")
    window.draft.save_async(path)
    try:
        wait_until(entered.is_set)
        window.draft.set_value(("name",), "later")
        assert not window.close()
        assert window.isVisible()
    finally:
        release.set()
    wait_until(lambda: not window.draft.saving)
    assert json.loads(path.read_text(encoding="utf-8"))["name"] == "captured"
    assert window.isVisible()
    assert window.draft.document["name"] == "later"
    assert window.draft.dirty


def test_unsaved_dialog_has_three_real_choices(window):
    dialog = UnsavedDraftDialog(window)
    QTest.mouseClick(dialog.discard_button, Qt.MouseButton.LeftButton)
    assert dialog.choice == "discard"


def test_preferences_do_not_enter_document(window, tmp_path, qapp):
    view = window.workbench
    window.preferences.set_theme("dark")
    view.set_output_collapsed(False)
    window.preferences.save_window(window, view)
    settings = WorkbenchSettings(QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat))
    restored = MuseWindow(ExperimentDraft(), settings)
    restored.show()
    qapp.processEvents()
    assert settings.theme == "dark"
    assert not restored.workbench.output_collapsed
    assert "theme" not in window.draft.document
    assert "layout" not in window.draft.document
    assert not restored.windowIcon().isNull()
    restored.close()
    restored.deleteLater()
