"""先取得固定快照，再按 SDK 公开 framing 计算内容身份；不导入研究代码。"""

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from PySide6.QtCore import QThread


def _facts(path):
    value = path.lstat()
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"不允许链接或特殊项：{path}")
    return (value.st_mode, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino, value.st_dev)


def _files(root):
    result = {}

    def visit(directory):
        for path in directory.iterdir():
            facts = _facts(path)
            if stat.S_ISDIR(facts[0]):
                visit(path)
            elif stat.S_ISREG(facts[0]):
                relative = path.relative_to(root)
                if "__pycache__" not in relative.parts and not path.name.endswith(".pyc"):
                    result[relative.as_posix()] = facts
            else:
                raise ValueError(f"不允许链接或特殊项：{path}")

    visit(root)
    return result


def _unpack(archive, destination):
    seen = set()
    with zipfile.ZipFile(archive) as source:
        for item in source.infolist():
            # Windows ZipInfo.filename 会规范化反斜杠；边界检查须使用 ZIP 原名。
            name = item.orig_filename
            path = PurePosixPath(name)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError(f"ZIP 不允许链接或特殊项：{name}")
            if (not name or "\\" in name or "\x00" in name or ":" in name or path.is_absolute() or not path.parts
                    or any(part in ("..", ".") for part in name.rstrip("/").split("/"))):
                raise ValueError(f"ZIP 路径不安全：{name}")
            normalized = path.as_posix()
            if normalized in seen:
                raise ValueError(f"ZIP 路径重复：{name}")
            seen.add(normalized)
            output = destination.joinpath(*path.parts)
            if item.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as src, output.open("xb") as dst:
                    shutil.copyfileobj(src, dst)


def build_archive(source: Path, destination: Path) -> dict:
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    facts = _facts(source)
    if destination == source or destination.is_relative_to(source):
        raise ValueError("输出 ZIP 必须位于源码目录之外")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="muse-snapshot-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        snapshot = temporary / "content"
        snapshot.mkdir()
        if stat.S_ISDIR(facts[0]):
            try:
                before = _files(source)
                for name in before:
                    output = snapshot / name
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / name, output)
                if _facts(source) != facts or _files(source) != before:
                    raise ValueError("复制期间源码事实发生变化，请保存源文件后重试")
            except (FileNotFoundError, NotADirectoryError) as error:
                raise ValueError("复制期间源码事实发生变化，请保存源文件后重试") from error
        elif stat.S_ISREG(facts[0]):
            fixed_zip = temporary / "source.zip"
            try:
                shutil.copyfile(source, fixed_zip)
                if _facts(source) != facts:
                    raise ValueError("复制期间源 ZIP 发生变化，请重试")
            except FileNotFoundError as error:
                raise ValueError("复制期间源 ZIP 发生变化，请重试") from error
            _unpack(fixed_zip, snapshot)
        else:
            raise ValueError("包根不允许链接或特殊项")
        files = sorted(_files(snapshot), key=lambda name: name.encode("utf-8"))
        digest = hashlib.sha256()
        size = 0
        output_zip = temporary / "package.zip"
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                path = snapshot / name
                encoded = name.encode("utf-8")
                length = path.stat().st_size
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                digest.update(length.to_bytes(8, "big"))
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                archive.write(path, name)
                size += length
        os.replace(output_zip, destination)
    return {"archive_path": str(destination), "sha256": digest.hexdigest(), "file_count": len(files), "size_bytes": size}


class ArchiveWorker(QThread):
    """有限本地工作；所有者必须持有线程及临时 ZIP 至上传结果收尾。"""

    def __init__(self, source, destination, parent):
        super().__init__(parent)
        self.source, self.destination = source, destination
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = build_archive(self.source, self.destination)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            self.error = {"kind": "source", "message": str(error)}
