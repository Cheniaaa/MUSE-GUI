from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLineEdit, QPlainTextEdit, QTextEdit, QVBoxLayout, QWidget,
)
from qfluentwidgets import (
    BodyLabel, ComboBox, FluentIcon as FIF, FluentWindow, MessageBox,
    MessageBoxBase, NavigationItemPosition, PushButton, SubtitleLabel, qrouter,
)

from .workbench_view import WorkbenchView


class UnsavedDraftDialog(MessageBoxBase):
    def __init__(self, parent):
        super().__init__(parent)
        self.choice = "cancel"
        self.viewLayout.addWidget(SubtitleLabel("保存草稿修改？", self))
        detail = BodyLabel("草稿可以不完整保存。放弃将丢失未保存的修改。\n关闭窗口不会停止后台 Run。", self)
        detail.setWordWrap(True)
        self.viewLayout.addWidget(detail)
        self.yesButton.setText("保存")
        self.cancelButton.setText("取消")
        self.discard_button = PushButton("放弃", self.buttonGroup)
        self.buttonLayout.insertWidget(1, self.discard_button)
        self.yesButton.clicked.connect(lambda: self._choose("save"))
        self.discard_button.clicked.connect(lambda: self._choose("discard"))
        self.widget.setMinimumWidth(430)

    def _choose(self, choice):
        self.choice = choice
        self.accept()


class MuseWindow(FluentWindow):
    def __init__(self, draft, preferences, parent=None):
        super().__init__(parent)
        self.draft = draft
        self.preferences = preferences
        self._after_save = None
        self._close_after_save = False
        self._close_confirmed = False
        stacked = self.stackedWidget

        def release_routes():
            # 固定Fluent Router保留强引用且无注销方法；只清除此窗口拥有的已销毁stack。
            qrouter.stackHistories.pop(stacked, None)
            qrouter.history[:] = [item for item in qrouter.history if item.stacked is not stacked]

        stacked.destroyed.connect(release_routes)
        self.resize(1280, 800)
        self.setMinimumSize(1080, 640)
        self.setWindowTitle("Muse · 研究工作台")
        pixmap = QPixmap()
        pixmap.loadFromData(files("muse_gui").joinpath("assets/Icon/AppIcon.png").read_bytes())
        self.setWindowIcon(QIcon(pixmap))
        self.navigationInterface.setMinimumExpandWidth(10000)
        self.navigationInterface.setExpandWidth(220)
        self.navigationInterface.setReturnButtonVisible(False)
        self.workbench = WorkbenchView(draft, self)
        self.addSubInterface(self.workbench, FIF.HOME, "工作台")
        self.history_host = self._feature_page("historyHost", "历史运行", "Run 记录由连接的 Muse Server 提供。")
        self.resources_host = self._feature_page("resourcesHost", "研究资源", "资源目录与不可变内容身份由 Muse Server 提供。")
        self.settings_host = self._feature_page("settingsHost", "设置", "外观偏好保存在本机，不写入实验草稿。")
        self.addSubInterface(self.history_host, FIF.HISTORY, "历史运行")
        self.addSubInterface(self.resources_host, FIF.LIBRARY, "资源")
        self.addSubInterface(self.settings_host, FIF.SETTING, "设置", NavigationItemPosition.BOTTOM)
        self.theme_combo = ComboBox(self.settings_host)
        self.theme_combo.addItems(["跟随系统", "浅色", "深色"])
        self.theme_combo.setCurrentIndex(["auto", "light", "dark"].index(preferences.theme))
        self.theme_combo.currentIndexChanged.connect(lambda index: preferences.set_theme(["auto", "light", "dark"][index]))
        self.settings_host.layout().insertWidget(2, self.theme_combo)
        self.workbench.new_requested.connect(self.new_draft)
        self.workbench.open_requested.connect(self.open_draft)
        self.workbench.save_requested.connect(self.save_draft)
        draft.saved.connect(self._saved)
        draft.save_failed.connect(self._save_failed)
        draft.state_changed.connect(self._update_title)
        self._shortcut("保存", "Ctrl+S", self.save_draft)
        self._shortcut("另存为", "Ctrl+Shift+S", lambda: self.save_draft(True))
        self._shortcut("打开", "Ctrl+O", self.open_draft)
        self._shortcut("新建", "Ctrl+N", self.new_draft)
        self._shortcut("撤销", "Ctrl+Z", lambda: self._undo(False))
        self._shortcut("重做", "Ctrl+Y", lambda: self._undo(True))
        preferences.restore_window(self, self.workbench)
        self._update_title()

    def _feature_page(self, name, title, note):
        page = QWidget(self)
        page.setObjectName(name)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)
        layout.addWidget(SubtitleLabel(title, page))
        label = BodyLabel(note, page)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addStretch()
        return page

    def _shortcut(self, text, key, callback):
        action = QAction(text, self)
        action.setShortcut(QKeySequence(key))
        action.triggered.connect(callback)
        self.addAction(action)

    def _undo(self, redo):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit)):
            if not focus.isReadOnly():
                (focus.redo if redo else focus.undo)()
        elif not self.workbench.read_only:
            (self.draft.undo_stack.redo if redo else self.draft.undo_stack.undo)()

    def _update_title(self):
        name = self.draft.document["name"] or "未命名实验"
        self.setWindowTitle(name + (" · 未保存" if self.draft.dirty else "") + " — Muse")

    def _ask_unsaved(self):
        dialog = UnsavedDraftDialog(self)
        dialog.exec()
        return dialog.choice

    def _confirm_replace(self, action):
        self.workbench.flush_inputs()
        if self.draft.saving:
            self.workbench.show_message("正在保存草稿，请保存完成后再切换。")
            return
        if not self.draft.dirty:
            action()
            return
        choice = self._ask_unsaved()
        if choice == "discard":
            action()
        elif choice == "save":
            self._after_save = action
            self.save_draft()

    def new_draft(self):
        def replace():
            self.draft.new()
            self.workbench.set_context("draft", self.draft.document["draft_id"])
        self._confirm_replace(replace)

    def _initial_directory(self):
        recent = self.preferences.recent_files
        return str(self.draft.source_path or (recent[0] if recent else Path.home()))

    def open_draft(self, path=None):
        if path is None or isinstance(path, bool):
            selected, _ = QFileDialog.getOpenFileName(self, "打开草稿", self._initial_directory(), "Muse 草稿 (*.muse.json);;JSON (*.json)")
            if not selected:
                return
            path = Path(selected)
        path = Path(path)

        def replace():
            try:
                self.draft.load(path)
            except (OSError, ValueError) as error:
                self._show_error("打开失败", str(error))
                return
            self.preferences.remember_file(path)
            self.workbench.set_context("draft", self.draft.document["draft_id"])
            self.workbench.show_message("已打开本地草稿：" + str(path))
        self._confirm_replace(replace)

    def _choose_save_path(self):
        selected, _ = QFileDialog.getSaveFileName(self, "保存草稿", self._initial_directory(), "Muse 草稿 (*.muse.json)")
        if not selected:
            return None
        return Path(selected if selected.endswith(".muse.json") else selected + ".muse.json")

    def save_draft(self, save_as=False):
        self.workbench.flush_inputs()
        if self.draft.saving:
            return
        path = self._choose_save_path() if save_as or self.draft.source_path is None else self.draft.source_path
        if path is None:
            self._after_save = None
            self._close_after_save = False
            return
        try:
            self.draft.save_async(path)
        except (OSError, ValueError) as error:
            self._save_failed(str(error))

    def _saved(self, path, revision):
        self.preferences.remember_file(Path(path))
        self.workbench.show_message(f"已保存 revision {revision}：{path}" + ("\n后续修改尚未保存。" if self.draft.dirty else ""))
        callback, self._after_save = self._after_save, None
        close, self._close_after_save = self._close_after_save, False
        if not self.draft.dirty:
            if callback:
                callback()
            elif close:
                self.close()

    def _save_failed(self, error):
        self._after_save = None
        self._close_after_save = False
        self._show_error("保存失败", error)

    def _show_error(self, title, message):
        self.workbench.show_message(title + "：" + message)
        dialog = MessageBox(title, message, self)
        dialog.cancelButton.hide()
        dialog.exec()

    def prepare_resources_close(self):
        """入口组装后替换为本客户端的资源收口；空白本地工作台没有后台资源。"""
        return True

    def closeEvent(self, event):
        self.workbench.flush_inputs()
        if self.draft.saving:
            self._close_after_save = True
            event.ignore()
            return
        if self.draft.dirty and not self._close_confirmed:
            choice = self._ask_unsaved()
            if choice == "cancel":
                event.ignore()
                return
            if choice == "save":
                event.ignore()
                self._close_after_save = True
                self.save_draft()
                return
        self._close_confirmed = True
        if not self.prepare_resources_close():
            self.setEnabled(False)
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self.preferences.save_window(self, self.workbench)
        super().closeEvent(event)
