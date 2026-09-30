"""skill 文件列表 / 文件内容读取辅助函数单元测试

覆盖 routers/skills.py 的纯函数:
- _list_skill_files:递归列举、跳过隐藏文件、SKILL.md 置顶、字典序
- _read_skill_file:正常读取、路径穿越拒绝、不存在 404、非 UTF-8 拒绝
"""
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.skills import _list_skill_files, _read_skill_file


def _build_skill_dir(tmp_path: Path) -> Path:
    """构造一个含 SKILL.md、附加文件、子目录与隐藏文件的 skill 目录"""
    d = tmp_path / "my_skill"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: my_skill\ndescription: d\n---\nbody\n", encoding="utf-8")
    (d / "b_rules.md").write_text("rules", encoding="utf-8")
    (d / "a_example.txt").write_text("example", encoding="utf-8")
    (d / "references" / "note.md").write_text("note", encoding="utf-8")
    # 隐藏文件 / 隐藏目录应被跳过
    (d / ".DS_Store").write_bytes(b"\x00")
    (d / ".hidden").mkdir()
    (d / ".hidden" / "x.txt").write_text("x", encoding="utf-8")
    return d


# ============================================================
# _list_skill_files
# ============================================================


def test_list_files_skill_md_first_and_sorted(tmp_path):
    """SKILL.md 置顶,其余按路径字典序,隐藏文件不出现。"""
    d = _build_skill_dir(tmp_path)

    paths = [e.path for e in _list_skill_files(d)]

    assert paths == ["SKILL.md", "a_example.txt", "b_rules.md", "references/note.md"]


def test_list_files_sizes(tmp_path):
    """条目携带正确的文件大小。"""
    d = _build_skill_dir(tmp_path)

    entries = {e.path: e.size for e in _list_skill_files(d)}

    assert entries["b_rules.md"] == len("rules")
    assert entries["references/note.md"] == len("note")


# ============================================================
# _read_skill_file
# ============================================================


def test_read_file_ok(tmp_path):
    """正常读取嵌套文件的 UTF-8 内容。"""
    d = _build_skill_dir(tmp_path)

    resp = _read_skill_file(d, "references/note.md")

    assert resp.content == "note"
    assert resp.path == "references/note.md"
    assert resp.size == len("note")


def test_read_file_rejects_path_traversal(tmp_path):
    """路径穿越(../)拒绝,返回 404。"""
    d = _build_skill_dir(tmp_path)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        _read_skill_file(d, "../outside.txt")

    assert exc_info.value.status_code == 404


def test_read_file_missing_returns_404(tmp_path):
    """文件不存在返回 404。"""
    d = _build_skill_dir(tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        _read_skill_file(d, "nope.txt")

    assert exc_info.value.status_code == 404


def test_read_file_rejects_non_utf8(tmp_path):
    """非 UTF-8 二进制内容返回 400。"""
    d = _build_skill_dir(tmp_path)
    (d / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

    with pytest.raises(HTTPException) as exc_info:
        _read_skill_file(d, "blob.bin")

    assert exc_info.value.status_code == 400
