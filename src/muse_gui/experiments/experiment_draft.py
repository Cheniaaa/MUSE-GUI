"""唯一可编辑草稿；不承担研究配置解析或科学校验。"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QIODevice, QObject, QSaveFile, QThread, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack


def write_document(path: Path, payload: bytes) -> None:
    """同步及异步保存共用 Qt 原子替换，不启用非原子 fallback。"""
    file = QSaveFile(str(path))
    if not file.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(file.errorString())
    if file.write(payload) != len(payload):
        file.cancelWriting()
        raise OSError(file.errorString())
    if not file.commit():
        raise OSError(file.errorString())


class _SaveThread(QThread):
    def __init__(self, path, payload, parent):
        super().__init__(parent)
        self.path = path
        self.payload = payload
        self.error = None

    def run(self):
        try:
            write_document(self.path, self.payload)
        except OSError as error:
            self.error = str(error)


class _Edit(QUndoCommand):
    def __init__(self, draft, before, after, text):
        super().__init__(text)
        self.draft = draft
        self.before = before
        self.after = after

    def undo(self):
        self.draft._apply(self.before)

    def redo(self):
        self.draft._apply(self.after)


class ExperimentDraft(QObject):
    changed = Signal(int)
    state_changed = Signal()
    saved = Signal(str, int)
    save_failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.undo_stack = QUndoStack(self)
        self.revision = 0
        self.validated_revision = None
        self.validation_report = None
        self.source_path = None
        self._generation = 0
        self._save_thread = None
        self._document = self._blank()
        self._saved_document = deepcopy(self._document)

    @staticmethod
    def _blank():
        return {
            "format_version": 1,
            "draft_id": str(uuid4()),
            "name": "未命名实验",
            "catalog_id": None,
            "request": {},
            "environment_edit": {},
            "run_options": {},
            "pending_inputs": {},
        }

    @property
    def document(self) -> dict:
        return deepcopy(self._document)

    @property
    def dirty(self) -> bool:
        return self._document != self._saved_document

    @property
    def saving(self) -> bool:
        return self._save_thread is not None

    def capture(self) -> tuple[int, dict]:
        return self.revision, self.document

    def _apply(self, document):
        self._document = deepcopy(document)
        self.revision += 1
        self.validated_revision = None
        self.validation_report = None
        self.changed.emit(self.revision)
        self.state_changed.emit()

    def _edit(self, after, text):
        if after != self._document:
            self.undo_stack.push(_Edit(self, self.document, after, text))

    def set_value(self, path: tuple[str | int, ...], value: object) -> None:
        after = self.document
        node = after
        for key in path[:-1]:
            if isinstance(node, dict) and key not in node:
                node[key] = {}
            node = node[key]
        node[path[-1]] = deepcopy(value)
        self._edit(after, "修改 " + ".".join(map(str, path)))

    def remove_override(self, path: tuple[str | int, ...]) -> None:
        after = self.document
        node = after
        for key in path[:-1]:
            if isinstance(node, dict) and key not in node:
                return
            node = node[key]
        if isinstance(node, dict) and path[-1] not in node:
            return
        del node[path[-1]]
        self._edit(after, "删除覆盖 " + ".".join(map(str, path)))

    def set_pending(self, path: str, text: str | None) -> None:
        if text is None:
            self.remove_override(("pending_inputs", path))
        else:
            self.set_value(("pending_inputs", path), text)

    def mark_validated(self, revision: int, report: dict) -> None:
        if revision == self.revision:
            self.validated_revision = revision
            self.validation_report = deepcopy(report)
            self.state_changed.emit()

    @staticmethod
    def _check_document(document):
        # 文件反序列化边界只检查本地格式；request 的含义归 Server。
        if not isinstance(document, dict) or type(document.get("format_version")) is not int or document["format_version"] != 1:
            raise ValueError("不支持的草稿格式（需要 format_version=1）")
        for key in ("draft_id", "name"):
            if not isinstance(document.get(key), str):
                raise ValueError(f"草稿字段 {key} 必须是字符串")
        if document.get("catalog_id") is not None and not isinstance(document["catalog_id"], str):
            raise ValueError("草稿字段 catalog_id 必须是字符串或 null")
        for key in ("request", "environment_edit", "run_options", "pending_inputs"):
            if not isinstance(document.get(key), dict):
                raise ValueError(f"草稿字段 {key} 必须是 JSON 对象")
        if not all(isinstance(value, str) for value in document["pending_inputs"].values()):
            raise ValueError("pending_inputs 必须保存原始文本")

    def replace_document(self, document: dict, *, source_path: Path | None = None) -> None:
        """打开文件或新建的统一换稿入口；换稿确认由 app 完成。"""
        self._check_document(document)
        self._generation += 1
        self.undo_stack.clear()
        self.source_path = source_path
        self._saved_document = deepcopy(document) if source_path else None
        self._apply(document)

    def new(self) -> None:
        document = self._blank()
        self.replace_document(document)
        self._saved_document = deepcopy(document)
        self.state_changed.emit()

    def load(self, path: Path) -> None:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        self.replace_document(document, source_path=Path(path))

    def _capture_save(self):
        revision, document = self.capture()
        payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        return revision, document, payload, self._generation

    def _accept_save(self, path, revision, document, generation):
        if generation == self._generation:
            self.source_path = path
            self._saved_document = document
            self.state_changed.emit()
            self.saved.emit(str(path), revision)

    def save(self, path: Path) -> None:
        if self.saving:
            raise RuntimeError("已有草稿正在保存")
        revision, document, payload, generation = self._capture_save()
        path = Path(path)
        write_document(path, payload)
        self._accept_save(path, revision, document, generation)

    def save_async(self, path: Path) -> None:
        """窗口使用；只传捕获字节给线程，所有状态回写仍在 GUI 线程。"""
        if self.saving:
            raise RuntimeError("已有草稿正在保存")
        revision, document, payload, generation = self._capture_save()
        path = Path(path)
        thread = _SaveThread(path, payload, self)
        self._save_thread = thread

        def finished():
            self._save_thread = None
            if thread.error is not None:
                self.save_failed.emit(thread.error)
            else:
                self._accept_save(path, revision, document, generation)
            self.state_changed.emit()
            thread.deleteLater()

        thread.finished.connect(finished)
        self.state_changed.emit()
        thread.start()
