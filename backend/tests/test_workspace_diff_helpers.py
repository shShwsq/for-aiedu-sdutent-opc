"""workspace_diff 辅助函数单元测试:patch 格式化 + files_changed 解析。

这两个纯函数负责把 git diff 输出转成可读、可 git apply 的 patch 文本。
- _format_new_file_patch:未跟踪文件内容 → 标准 new file patch
- _parse_files_changed:git diff --stat 末行 → 文件数

格式错误会导致:前端展示乱码、git apply 还原失败、files_changed 元数据失真。
"""
import pytest

from app.services.workspace_diff import (
    _format_new_file_patch,
    _parse_files_changed,
)


# ============================================================
# _format_new_file_patch:未跟踪文件 → git apply 兼容 patch
# ============================================================

def test_format_new_file_patch_basic_structure():
    """基本结构:diff --git / new file mode / --- / +++ / @@ / +行。"""
    patch = _format_new_file_patch("src/new.py", "print('hello')\n")
    assert patch.startswith("diff --git a/src/new.py b/src/new.py\n")
    assert "new file mode 100644\n" in patch
    assert "--- /dev/null\n" in patch
    assert "+++ b/src/new.py\n" in patch
    # @@ -0,0 +1,N @@ 头:N = 行数
    assert "@@ -0,0 +1,1 @@" in patch
    # 内容行以 + 开头
    assert "+print('hello')" in patch


def test_format_new_file_patch_multiple_lines():
    """多行文件:每行加 + 前缀,@@ 头的行数正确。"""
    content = "import os\nimport sys\n\nprint('main')\n"
    patch = _format_new_file_patch("main.py", content)
    # 4 行(含空行)
    assert "@@ -0,0 +1,4 @@" in patch
    assert "+import os" in patch
    assert "+import sys" in patch
    assert "+\n" in patch  # 空行也要 + 前缀
    assert "+print('main')" in patch


def test_format_new_file_patch_no_trailing_newline_marker():
    """内容末尾无换行 → 补 "\\ No newline at end of file" 标记。

    git apply 要求此标记,否则行尾行为不一致。
    """
    patch = _format_new_file_patch("no_nl.txt", "no newline at end")
    assert "\\ No newline at end of file" in patch


def test_format_new_file_patch_with_trailing_newline_no_marker():
    """内容末尾有换行 → 不补 No newline 标记。"""
    patch = _format_new_file_patch("with_nl.txt", "has newline\n")
    assert "\\ No newline at end of file" not in patch


def test_format_new_file_patch_empty_content():
    """空内容:0 行,@@ -0,0 +1,0 @@(空文件)。"""
    patch = _format_new_file_patch("empty.txt", "")
    assert "@@ -0,0 +1,0 @@" in patch
    # 空内容不应有 No newline 标记(实现:content 为 falsy 时不补)
    assert "\\ No newline at end of file" not in patch


def test_format_new_file_patch_path_with_spaces():
    """路径含空格:保留原样(patch 头用 a/ b/ 前缀,git apply 兼容)。"""
    patch = _format_new_file_patch("my dir/file.py", "x\n")
    assert "diff --git a/my dir/file.py b/my dir/file.py" in patch
    assert "+++ b/my dir/file.py" in patch


def test_format_new_file_patch_path_with_subdir():
    """子目录路径保留层级。"""
    patch = _format_new_file_patch("src/deep/nested/file.py", "x\n")
    assert "a/src/deep/nested/file.py" in patch
    assert "b/src/deep/nested/file.py" in patch


def test_format_new_file_patch_unicode_content():
    """中文内容:UTF-8 字符正确处理(不乱码)。"""
    patch = _format_new_file_patch("中文.py", "# 中文注释\nprint('你好')\n")
    assert "+# 中文注释" in patch
    assert "+print('你好')" in patch


def test_format_new_file_patch_line_count_matches_content():
    """@@ 头声明的行数与实际 + 行数一致(防 off-by-one)。"""
    for content in ["a\n", "a\nb\n", "a\nb\nc\nd\n", "a"]:
        patch = _format_new_file_patch("f", content)
        lines = content.splitlines()
        expected_count = len(lines)
        # @@ -0,0 +1,N @@ 中的 N
        import re
        m = re.search(r"@@ -0,0 \+1,(\d+) @@", patch)
        assert m, f"未找到 @@ 头: {patch}"
        assert int(m.group(1)) == expected_count, (
            f"行数不匹配: content={content!r}, 声明={m.group(1)}, 实际={expected_count}"
        )


# ============================================================
# _parse_files_changed:git diff --stat 末行解析
# ============================================================

def test_parse_files_changed_single_file():
    """1 file changed。"""
    stat = " src/a.py | 5 +-\n 1 file changed, 3 insertions(+), 2 deletions(-)"
    assert _parse_files_changed(stat) == 1


def test_parse_files_changed_multiple_files():
    """N files changed。"""
    stat = (
        " src/a.py | 5 +-\n"
        " src/b.py | 10 +--\n"
        " src/c.py | 2 +-\n"
        " 3 files changed, 12 insertions(+), 8 deletions(-)"
    )
    assert _parse_files_changed(stat) == 3


def test_parse_files_changed_takes_last_match():
    """多行匹配时取最后一个(实际 git stat 末行是汇总)。"""
    # 不常见的输入,但实现从后往前找,应取最后一个
    stat = "1 file changed\n2 files changed"
    assert _parse_files_changed(stat) == 2


def test_parse_files_changed_empty_string():
    """空字符串 → 0。"""
    assert _parse_files_changed("") == 0


def test_parse_files_changed_none_returns_zero():
    """None → 0(不抛异常)。"""
    assert _parse_files_changed(None) == 0


def test_parse_files_changed_no_match_returns_zero():
    """无 'N files changed' 模式 → 0。"""
    stat = "some random git output\nwithout the expected summary"
    assert _parse_files_changed(stat) == 0


def test_parse_files_changed_ignores_insertions_line():
    """不误匹配 'N insertions' 行(只匹配 'N files changed')。"""
    stat = " 1 file changed, 100 insertions(+)"
    # 不应把 100 insertions 当成文件数
    assert _parse_files_changed(stat) == 1


def test_parse_files_changed_handles_zero_files():
    """0 files changed(理论上不应出现,但应正确解析)。"""
    stat = " 0 files changed"
    assert _parse_files_changed(stat) == 0


def test_parse_files_changed_only_summary_line():
    """只有汇总行(无文件明细)也能解析。"""
    assert _parse_files_changed("5 files changed, 20 insertions(+), 10 deletions(-)") == 5
