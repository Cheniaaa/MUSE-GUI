import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import zipfile

import pytest

from muse_gui.backend.muse_client import MuseClient
from muse_gui.catalog.package_archive import build_archive


def test_archive_uses_content_framing_and_ignores_python_cache(tmp_path):
    source = tmp_path / "源码"
    source.mkdir()
    (source / "中.py").write_bytes(b"unicode")
    (source / "a").mkdir()
    (source / "a" / "b.py").write_bytes(b"ab")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "cached.py").write_bytes(b"ignored")
    (source / "unused.pyc").write_bytes(b"ignored")
    result = build_archive(source, tmp_path / "out.zip")
    digest = hashlib.sha256()
    for name, data in [("a/b.py", b"ab"), ("中.py", b"unicode")]:
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big") + encoded + len(data).to_bytes(8, "big") + data)
    assert result["sha256"] == digest.hexdigest()
    assert result["file_count"] == 2 and result["size_bytes"] == 9
    with zipfile.ZipFile(result["archive_path"]) as archive:
        assert archive.namelist() == ["a/b.py", "中.py"]
    assert not (source / "out.zip").exists()


def test_selected_zip_is_snapshotted_and_hashes_identically(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "entry.py").write_text("value = 1\n", encoding="utf-8")
    directory = build_archive(root, tmp_path / "directory.zip")
    selected = build_archive(Path(directory["archive_path"]), tmp_path / "selected.zip")
    assert selected["sha256"] == directory["sha256"]
    assert selected["archive_path"] != directory["archive_path"]


def test_source_fact_change_during_copy_requires_retry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    entry = source / "entry.py"
    entry.write_bytes(b"before")
    original = shutil.copyfile

    def copy_then_edit(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        entry.write_bytes(b"edited after copying")
        return result

    monkeypatch.setattr(shutil, "copyfile", copy_then_edit)
    with pytest.raises(ValueError, match="变化.*重试"):
        build_archive(source, tmp_path / "out.zip")
    assert not (tmp_path / "out.zip").exists()


def test_source_removal_during_copy_requires_explicit_retry(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "entry.py").write_bytes(b"content")
    original = shutil.copyfile

    def copy_then_remove(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        shutil.rmtree(source)
        return result

    monkeypatch.setattr(shutil, "copyfile", copy_then_remove)
    with pytest.raises(ValueError, match="变化.*重试"):
        build_archive(source, tmp_path / "out.zip")


@pytest.mark.parametrize("root_link", [False, True])
def test_root_and_child_links_are_rejected(tmp_path, root_link):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.py").write_bytes(b"data")
    link = tmp_path / "link" if root_link else source / "link"
    try:
        link.symlink_to(source if root_link else source / "file.py", target_is_directory=root_link)
    except OSError as error:
        pytest.skip(f"当前 Windows 无创建符号链接权限：{error}")
    with pytest.raises(ValueError, match="链接|特殊"):
        build_archive(link if root_link else source, tmp_path / "out.zip")


@pytest.mark.parametrize("name,mode", [("../escape.py", stat.S_IFREG), ("link", stat.S_IFLNK),
                                       ("pipe", stat.S_IFIFO), ("a\\b", stat.S_IFREG)])
def test_selected_zip_rejects_unsafe_members(tmp_path, name, mode):
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo(name)
        info.external_attr = (mode | 0o644) << 16
        archive.writestr(info, b"data")
    if "\\" in name:
        # Windows ZipInfo 会把反斜杠写成斜杠；恢复实际外部 ZIP 的危险成员名。
        source.write_bytes(source.read_bytes().replace(b"a/b", b"a\\b"))
    with pytest.raises(ValueError):
        build_archive(source, tmp_path / "out.zip")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction 边界")
@pytest.mark.parametrize("root_link", [False, True])
def test_root_and_child_windows_junctions_are_rejected(tmp_path, root_link):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (target / "entry.py").write_bytes(b"content")
    link = tmp_path / "junction" if root_link else source / "junction"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="链接|特殊"):
        build_archive(link if root_link else source, tmp_path / "out.zip")


@pytest.mark.skipif(not os.environ.get("MUSE_G4_URL"), reason="需要明确指定只做资源验证的真实 Server")
def test_archive_hash_matches_server(tmp_path):
    source = tmp_path / "environment"
    source.mkdir()
    (source / "environment.yaml").write_text(json.dumps({"scene": "scene.mjcf", "metadata": {"name": "GUI G4 hash oracle"}}), encoding="utf-8")
    (source / "scene.mjcf").write_text('<mujoco><worldbody><geom name="ground" type="plane" size="5 5 .1"/></worldbody></mujoco>', encoding="utf-8")
    (source / "来源.txt").write_text("独立内容 hash 对照\n", encoding="utf-8")
    result = build_archive(source, tmp_path / "environment.zip")
    client = MuseClient(os.environ["MUSE_G4_URL"], timeout=120)
    try:
        record = client.upload("/catalog/environments", Path(result["archive_path"]))
        assert record["sha256"] == result["sha256"]
        second = client.upload("/catalog/environments", Path(result["archive_path"]), record["resource_id"])
        assert second["revision"] == record["revision"] + 1
        assert second["sha256"] == record["sha256"]
        selected = build_archive(Path(result["archive_path"]), tmp_path / "selected.zip")
        from_zip = client.upload("/catalog/environments", Path(selected["archive_path"]), record["resource_id"])
        assert from_zip["sha256"] == result["sha256"] and from_zip["revision"] == second["revision"] + 1
        evidence = Path(".superpowers/integration/2026-10-18/g4-real-hash.json")
        evidence.write_text(json.dumps({"archive": result, "registered": record, "updated": second, "selected_zip": from_zip}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        client.close()


@pytest.mark.skipif(not os.environ.get("MUSE_G4_PACKAGE_SOURCE"), reason="需显式提供只读参考研究包目录及真实 Server")
def test_training_directory_and_selected_zip_hash_match_server(tmp_path):
    result = build_archive(Path(os.environ["MUSE_G4_PACKAGE_SOURCE"]), tmp_path / "package.zip")
    selected = build_archive(Path(result["archive_path"]), tmp_path / "selected.zip")
    client = MuseClient(os.environ["MUSE_G4_URL"], timeout=120)
    try:
        record = client.upload("/catalog/training-packages", Path(result["archive_path"]))
        updated = client.upload("/catalog/training-packages", Path(selected["archive_path"]), record["resource_id"])
        assert result["sha256"] == selected["sha256"] == record["sha256"] == updated["sha256"]
        assert updated["revision"] == record["revision"] + 1
        Path(".superpowers/integration/2026-10-18/g4-real-training-hash.json").write_text(
            json.dumps({"archive": result, "registered": record, "selected_zip_update": updated}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        client.close()
