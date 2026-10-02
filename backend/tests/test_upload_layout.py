"""upload_layout 纯函数单测(沙箱拷贝与回退浏览共用的布局约定)

覆盖:
- safe_dirname:basename 提取 / `..` 清洗 / 非法字符替换 / 前后缀 dot 剔除 / 限长 / 回退
- extract_creation_ids:legacy 单数 + 列表合并去重保序 / 空值过滤
- extract_followup_ids:累积列表读取 / 空值过滤
- compute_upload_layout:单上传平铺根 / 多上传 {i}-{name} / followup 全局下标 / meta 缺失回退
- resolve_path:平铺根 / 前缀槽位 / followup 最长前缀优先 / 槽位目录本身 / 无匹配
"""
from app.services.upload_layout import (
    SKIP_DIRS_LIST,
    UploadSlot,
    compute_upload_layout,
    extract_creation_ids,
    extract_followup_ids,
    resolve_path,
    safe_dirname,
)


# ============================================================
# safe_dirname
# ============================================================


def test_safe_dirname_basic():
    """常规文件名原样保留"""
    assert safe_dirname("报告.zip") == "报告.zip"
    assert safe_dirname("a b.txt") == "a_b.txt"  # 空格 → _


def test_safe_dirname_takes_basename():
    """带路径的名字只取 basename(客户端可能传带路径的名字)"""
    assert safe_dirname("C:\\dir\\file.txt") == "file.txt"
    assert safe_dirname("/etc/passwd") == "passwd"


def test_safe_dirname_neutralizes_dotdot():
    """`..` 换为 `_`,防目录穿越(与 sandbox_tools 传输侧一致)

    单独的 ".." 清洗后为空,落到 fallback。
    """
    assert safe_dirname("..") == "upload"
    assert safe_dirname("a..b") == "a_b"


def test_safe_dirname_strips_dots():
    """前后缀 dot 剔除,全非法字符回退 fallback"""
    assert safe_dirname(".hidden.") == "hidden"
    assert safe_dirname("!!!") == "upload"
    assert safe_dirname("", fallback="x") == "x"


def test_safe_dirname_length_limit():
    """超长名字限 80 字符"""
    assert len(safe_dirname("a" * 200)) == 80


# ============================================================
# extract_creation_ids / extract_followup_ids
# ============================================================


def test_extract_creation_ids_merges_legacy_and_list():
    """legacy 单数在前,列表随后,去重保序"""
    assert extract_creation_ids(
        {"upload_id": "a", "upload_ids": ["a", "b", "c"]}
    ) == ["a", "b", "c"]


def test_extract_creation_ids_variants():
    assert extract_creation_ids({"upload_id": "legacy"}) == ["legacy"]
    assert extract_creation_ids({"upload_ids": ["x", "", "y", None]}) == ["x", "y"]
    assert extract_creation_ids({}) == []
    assert extract_creation_ids(None) == []


def test_extract_followup_ids_variants():
    assert extract_followup_ids(
        {"followup_upload_ids": ["f1", "", "f2"]}
    ) == ["f1", "f2"]
    assert extract_followup_ids({}) == []
    assert extract_followup_ids(None) == []


# ============================================================
# compute_upload_layout(与 sandbox_tools 传输布局一一对应)
# ============================================================


def test_layout_single_creation_flat_root():
    """单创建上传:文件平铺根,前缀为空(transfer_upload_to_workspace 语义)"""
    slots = compute_upload_layout(["u1"], [], {"u1": {"filename": "报告.zip"}})
    assert slots == [UploadSlot("u1", "")]


def test_layout_multi_creation_indexed_dirs():
    """多创建上传:各占 {i}-{name}/ 子目录,i 从 0 起"""
    slots = compute_upload_layout(
        ["u1", "u2"],
        [],
        {"u1": {"filename": "报告.zip"}, "u2": {"filename": "笔记.pdf"}},
    )
    assert slots == [
        UploadSlot("u1", "0-报告.zip"),
        UploadSlot("u2", "1-笔记.pdf"),
    ]


def test_layout_followup_global_indices():
    """followup:followup_uploads/{i}-{name},i 为累积列表全局下标"""
    slots = compute_upload_layout(
        [], ["f1", "f2"], {"f1": {"filename": "a.txt"}, "f2": {"filename": "b.txt"}}
    )
    assert slots == [
        UploadSlot("f1", "followup_uploads/0-a.txt"),
        UploadSlot("f2", "followup_uploads/1-b.txt"),
    ]


def test_layout_meta_missing_falls_back_to_uid():
    """meta 缺失(GC/损坏):目录名回退 uid 前 12 位(与传输侧一致)"""
    slots = compute_upload_layout(["u1", "20990101000000-abcdef123456"], [], {})
    assert slots[1].prefix == "1-209901010000"


def test_layout_name_cleaning_applies():
    """槽位名走 safe_dirname 清洗(名字不一致则回退路径全错)

    "我的 附件!!..zip":`..`→`_` 先于字符过滤,故后缀 .zip 的 dot 被
    一并换掉 → "我的_附件___zip"(实现即传输侧语义)。
    """
    slots = compute_upload_layout(
        ["u1"], [], {"u1": {"filename": "我的 附件!!..zip"}}
    )
    assert slots[0].prefix == ""  # 单上传平铺,不体现名字
    slots2 = compute_upload_layout(
        ["u0", "u1"], [], {"u1": {"filename": "我的 附件!!..zip"}}
    )
    assert slots2[1].prefix == "1-我的_附件___zip"


# ============================================================
# resolve_path
# ============================================================


def _slots():
    """典型布局:平铺根创建上传 + 两个追问槽位"""
    return compute_upload_layout(
        ["u1"],
        ["f1", "f2"],
        {
            "u1": {"filename": "报告.zip"},
            "f1": {"filename": "a.txt"},
            "f2": {"filename": "b.txt"},
        },
    )


def test_resolve_flat_root_file():
    """平铺根文件:路径即 files/ 内相对路径"""
    assert resolve_path(_slots(), "doc.md") == ("u1", "doc.md")
    assert resolve_path(_slots(), "src/main.py") == ("u1", "src/main.py")


def test_resolve_followup_prefixed_file():
    """追问槽位文件:剥离前缀得 files/ 内相对路径"""
    assert resolve_path(_slots(), "followup_uploads/1-b.txt/x.md") == ("f2", "x.md")


def test_resolve_longest_prefix_wins():
    """平铺根 zip 内恰好含 followup_uploads/ 目录时,真实追问槽位(更长前缀)优先"""
    assert resolve_path(_slots(), "followup_uploads/0-a.txt/inner.md") == ("f1", "inner.md")


def test_resolve_slot_dir_itself_unreadable():
    """路径恰为槽位目录本身:该槽位不匹配(rel 为空);有平铺槽时落到平铺槽"""
    # 平铺根创建上传存在:followup 槽位不匹配,落到平铺槽(files/ 内同名文件,通常不存在→404)
    assert resolve_path(_slots(), "followup_uploads/0-a.txt") == (
        "u1", "followup_uploads/0-a.txt",
    )
    # 无平铺槽(多上传):槽位目录本身与普通未匹配路径都返回 None
    multi = compute_upload_layout(
        ["u1", "u2"], [], {"u1": {"filename": "a.zip"}, "u2": {"filename": "b.zip"}}
    )
    assert resolve_path(multi, "1-b.zip") is None


def test_resolve_no_match():
    """无平铺槽位时,不匹配任何前缀的路径返回 None"""
    multi = compute_upload_layout(
        ["u1", "u2"], [], {"u1": {"filename": "a.zip"}, "u2": {"filename": "b.zip"}}
    )
    assert resolve_path(multi, "src/main.py") is None
    assert resolve_path(multi, "0-a.zip") is None  # 槽位目录本身
    assert resolve_path([], "anything") is None
    assert resolve_path(_slots(), "") is None


def test_resolve_windows_separator_normalized():
    """反斜杠路径按分隔符归一"""
    assert resolve_path(_slots(), "src\\main.py") == ("u1", "src/main.py")


def test_skip_dirs_list_content():
    """噪声目录集合与沙箱树剪枝口径一致(抽查关键成员)"""
    for d in (".git", "node_modules", "__pycache__", "venv"):
        assert d in SKIP_DIRS_LIST
