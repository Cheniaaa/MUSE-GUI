"""唯一可编辑草稿；不承担研究配置解析或科学校验。"""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QIODevice, QObject, QSaveFile, QThread, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack


_MISSING = object()


def _same(left, right):
    if left is _MISSING or right is _MISSING:
        return left is right
    # JSON 联合类型中的 7 / 7.0 / true 必须保持区别；不能用 Python 的数值宽松相等。
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def _read(document, path):
    node = document
    for key in path:
        if isinstance(node, dict) and key not in node:
            return _MISSING
        node = node[key]
    return node


def _write(document, path, value):
    node = document
    for key in path[:-1]:
        if isinstance(node, dict) and key not in node:
            node[key] = {}
        node = node[key]
    if value is _MISSING:
        if isinstance(node, dict):
            node.pop(path[-1], None)
        else:
            del node[path[-1]]
    else:
        node[path[-1]] = deepcopy(value)


def _clear_input(document, cleanup):
    if cleanup is not None:
        prefix, subtree = cleanup
        for key in list(document["pending_inputs"]):
            if key == prefix or subtree and key.startswith(prefix + "."):
                del document["pending_inputs"][key]


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
    def __init__(self, draft, path, before, after, text, cleanup):
        super().__init__(text)
        self.draft = draft
        self.path = path
        self.before = before if before is _MISSING else deepcopy(before)
        self.after = after if after is _MISSING else deepcopy(after)
        self.cleanup = cleanup

    def _apply(self, value):
        document = self.draft.document
        _write(document, self.path, value)
        # 暂存输入不属于语义历史；undo/redo 均不能复活已提交的旧文本。
        _clear_input(document, self.cleanup)
        self.draft._apply(document)

    def undo(self):
        self._apply(self.before)

    def redo(self):
        self._apply(self.after)


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
        return not _same(self._document, self._saved_document)

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

    def _edit_root(self, path):
        node = self._document
        for index, key in enumerate(path):
            # 数组仍整体替换；新建父节点也一起记录，撤销后不残留空字典。
            if isinstance(node, list):
                return path[:index]
            if key not in node:
                return path[:index + 1]
            node = node[key]
        return path

    def _edit(self, after, path, text, cleanup=None):
        if _same(after, self._document):
            return
        root = self._edit_root(path)
        before_value, after_value = _read(self._document, root), _read(after, root)
        if _same(before_value, after_value):
            # 数值没变而仅完成暂存：推进 revision，但不产生空的语义 undo。
            self._apply(after)
        else:
            self.undo_stack.push(_Edit(self, root, before_value, after_value, text, cleanup))

    def set_value(self, path: tuple[str | int, ...], value: object) -> None:
        after = self.document
        _write(after, path, value)
        self._edit(after, path, "修改 " + ".".join(map(str, path)))

    def remove_override(self, path: tuple[str | int, ...], *, pending_prefix: str | None = None) -> None:
        after = self.document
        if _read(after, path) is not _MISSING:
            _write(after, path, _MISSING)
        cleanup = (pending_prefix, True) if pending_prefix is not None else None
        _clear_input(after, cleanup)
        self._edit(after, path, "删除覆盖 " + ".".join(map(str, path)), cleanup)

    def set_pending(self, path: str, text: str | None) -> None:
        if text is None:
            self.remove_override(("pending_inputs", path))
        else:
            self.set_value(("pending_inputs", path), text)

    def stage_input(self, pending_path: str, text: str | None) -> None:
        """G3 原始文本暂存：同一 document 内实时持久化/失效旧检查，不逐字入 undo。"""
        document = self.document
        _write(document, ("pending_inputs", pending_path), _MISSING if text is None else text)
        if not _same(document, self._document):
            self._apply(document)

    def commit_input(self, path: tuple[str | int, ...], value: object, *, pending_path: str,
                     pending_subtree: bool = False) -> None:
        """G3 一次字段提交；数组路径与原始输入路径可不同，但只清来源输入。"""
        document = self.document
        _write(document, path, value)
        cleanup = (pending_path, pending_subtree)
        _clear_input(document, cleanup)
        self._edit(document, path, "提交输入 " + pending_path, cleanup)

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
