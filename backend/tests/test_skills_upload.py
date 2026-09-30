"""skill zip 上传解析与可见性过滤单元测试

覆盖 uploader.extract_skill_zip:
- 两种 zip 结构(标准 <skill_name>/SKILL.md / 简化根目录 SKILL.md)
- frontmatter 缺失 / 多个 SKILL.md / 层级过深
- zip-slip 路径穿越 / 绝对路径 / 非 UTF-8 文件名
- 大小限制(zip 本体 / 单文件 / 解压总量,用 monkeypatch 缩小阈值)
- 扩展名白名单 / Mac 噪音跳过

覆盖 loader 的用户隔离:
- scenario_owner_id 解析(user_ 前缀 / 非法 UUID / 内置场景)
- list_visible_skills 过滤(内置全局共享,他人私有不可见)
"""
import io
import uuid
import zipfile

import pytest

from app.skills import loader as skill_loader
from app.skills import uploader
from app.skills.loader import list_visible_skills, scenario_owner_id
from app.skills.schema import ParsedSkill, SkillRegistry
from app.skills.uploader import extract_skill_zip


# ============================================================
# 构造 zip 的辅助
# ============================================================


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """按 {路径: 内容} 构造 zip 字节"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


SKILL_MD = """---
name: my_custom_skill
description: 我的自定义技能
---

# 指令
按以下步骤执行审计...
"""


# ============================================================
# 正常解析:两种结构
# ============================================================

def test_extract_standard_layout(tmp_path):
    """标准结构 <skill_name>/SKILL.md,附加资源保留。"""
    data = _make_zip({
        "my_custom_skill/SKILL.md": SKILL_MD,
        "my_custom_skill/rules.txt": "rule1\nrule2\n",
    })
    skill = extract_skill_zip(data, tmp_path)
    assert skill.name == "my_custom_skill"
    assert skill.description == "我的自定义技能"
    assert "按以下步骤执行审计" in skill.body
    # 附加资源已解压
    assert (tmp_path / "my_custom_skill" / "rules.txt").read_text() == "rule1\nrule2\n"
    assert skill.skill_dir == tmp_path / "my_custom_skill"


def test_extract_flat_layout(tmp_path):
    """简化结构:根目录直接 SKILL.md。"""
    data = _make_zip({"SKILL.md": SKILL_MD})
    skill = extract_skill_zip(data, tmp_path)
    assert skill.name == "my_custom_skill"
    assert skill.skill_dir == tmp_path


def test_extract_ignores_mac_noise(tmp_path):
    """Mac zip 噪音(__MACOSX/ 资源分叉、.DS_Store)跳过,不影响解析。"""
    data = _make_zip({
        "__MACOSX/my_custom_skill/._SKILL.md": b"\x00\x01",
        "my_custom_skill/SKILL.md": SKILL_MD,
        ".DS_Store": b"\x00\x02",
    })
    skill = extract_skill_zip(data, tmp_path)
    assert skill.name == "my_custom_skill"


def test_extract_name_from_frontmatter_not_dirname(tmp_path):
    """skill 名以 frontmatter.name 为准,不依赖目录名。"""
    data = _make_zip({"whatever_dir/SKILL.md": SKILL_MD})
    skill = extract_skill_zip(data, tmp_path)
    assert skill.name == "my_custom_skill"
    assert skill.skill_dir == tmp_path / "whatever_dir"


# ============================================================
# 结构错误
# ============================================================

def test_extract_missing_skill_md(tmp_path):
    """zip 内没有 SKILL.md → 拒绝。"""
    data = _make_zip({"foo.txt": b"hello"})
    with pytest.raises(ValueError, match="缺少 SKILL.md"):
        extract_skill_zip(data, tmp_path)


def test_extract_empty_zip(tmp_path):
    """空 zip → 拒绝。"""
    data = _make_zip({})
    with pytest.raises(ValueError, match="缺少 SKILL.md"):
        extract_skill_zip(data, tmp_path)


def test_extract_multiple_skill_md(tmp_path):
    """多个 SKILL.md → 拒绝(无法确定归属)。"""
    data = _make_zip({
        "a/SKILL.md": SKILL_MD,
        "b/SKILL.md": SKILL_MD,
    })
    with pytest.raises(ValueError, match="多个 SKILL.md"):
        extract_skill_zip(data, tmp_path)


def test_extract_nested_skill_md(tmp_path):
    """SKILL.md 层级过深(a/b/SKILL.md)→ 拒绝。"""
    data = _make_zip({"a/b/SKILL.md": SKILL_MD})
    with pytest.raises(ValueError, match="层级过深"):
        extract_skill_zip(data, tmp_path)


def test_extract_bad_frontmatter(tmp_path):
    """frontmatter 缺 name/description → 拒绝,且清理已解压内容。"""
    data = _make_zip({"s/SKILL.md": "# 没有 frontmatter\n"})
    with pytest.raises(ValueError, match="frontmatter"):
        extract_skill_zip(data, tmp_path)
    # 校验失败后临时目录被清理
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


# ============================================================
# 安全:路径穿越 / 非法类型
# ============================================================

def test_extract_zip_slip_rejected(tmp_path):
    """zip-slip(../ 逃逸)→ 拒绝。"""
    data = _make_zip({"../evil/SKILL.md": SKILL_MD})
    with pytest.raises(ValueError, match="路径穿越"):
        extract_skill_zip(data, tmp_path)


def test_extract_absolute_path_rejected(tmp_path):
    """绝对路径条目(/etc/SKILL.md)→ 拒绝。"""
    data = _make_zip({"/etc/SKILL.md": SKILL_MD})
    with pytest.raises(ValueError, match="绝对路径"):
        extract_skill_zip(data, tmp_path)


def test_extract_unsafe_extension_rejected(tmp_path):
    """附加资源为可执行文件(.exe)→ 拒绝。"""
    data = _make_zip({
        "s/SKILL.md": SKILL_MD,
        "s/trojan.exe": b"MZ...",
    })
    with pytest.raises(ValueError, match="不允许的文件类型"):
        extract_skill_zip(data, tmp_path)


def test_extract_zip_archive_inside_rejected(tmp_path):
    """附加资源为压缩包(.zip)→ 拒绝。"""
    inner = _make_zip({"SKILL.md": SKILL_MD})
    data = _make_zip({
        "s/SKILL.md": SKILL_MD,
        "s/evil.zip": inner,
    })
    with pytest.raises(ValueError, match="不允许的文件类型"):
        extract_skill_zip(data, tmp_path)


def test_extract_image_extension_allowed(tmp_path):
    """附加资源为位图(.png)→ 允许(默认白名单含常见图片格式)。"""
    data = _make_zip({
        "s/SKILL.md": SKILL_MD,
        "s/assets/logo.png": b"\x89PNG\r\n\x1a\nfake",
    })
    skill = extract_skill_zip(data, tmp_path)
    assert (skill.skill_dir / "assets" / "logo.png").is_file()


# ============================================================
# 大小限制(monkeypatch 缩小阈值)
# ============================================================

def test_extract_zip_too_large(tmp_path, monkeypatch):
    """zip 本体超限 → 拒绝。"""
    monkeypatch.setattr(uploader, "MAX_ZIP_SIZE", 100)
    data = _make_zip({"s/SKILL.md": SKILL_MD})
    with pytest.raises(ValueError, match="超过大小上限"):
        extract_skill_zip(data, tmp_path)


def test_extract_single_file_too_large(tmp_path, monkeypatch):
    """单文件超限 → 拒绝。"""
    monkeypatch.setattr(uploader, "MAX_SINGLE_FILE_SIZE", 10)
    data = _make_zip({"s/SKILL.md": SKILL_MD})
    with pytest.raises(ValueError, match="单文件超过大小上限"):
        extract_skill_zip(data, tmp_path)


def test_extract_total_size_too_large(tmp_path, monkeypatch):
    """解压总量超限 → 拒绝。"""
    monkeypatch.setattr(uploader, "MAX_EXTRACT_SIZE", 200)
    data = _make_zip({
        "s/SKILL.md": SKILL_MD,
        "s/a.txt": b"x" * 150,
        "s/b.txt": b"y" * 150,
    })
    with pytest.raises(ValueError, match="总大小超过上限"):
        extract_skill_zip(data, tmp_path)


def test_extract_not_a_zip(tmp_path):
    """非 zip 内容 → BadZipFile。"""
    with pytest.raises(zipfile.BadZipFile):
        extract_skill_zip(b"this is not a zip", tmp_path)


# ============================================================
# loader:scenario owner 解析
# ============================================================

def test_scenario_owner_parsing():
    """user_<uuid> 前缀解析出 owner;内置场景/非法后缀返回 None。"""
    uid = uuid.uuid4()
    assert scenario_owner_id(f"user_{uid}") == uid
    assert scenario_owner_id("code_security_audit") is None
    assert scenario_owner_id("user_not_a_uuid") is None
    assert scenario_owner_id("") is None


def _make_skill(name: str, scenario: str, tmp_path) -> ParsedSkill:
    d = tmp_path / scenario / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: desc\n---\nbody\n", encoding="utf-8"
    )
    return ParsedSkill(
        name=name,
        description="desc",
        scenario_id=scenario,
        skill_dir=d,
        body="body\n",
        source_path=md,
    )


def test_list_visible_skills_isolation(tmp_path, monkeypatch):
    """可见性:内置全局共享;用户私有仅 owner 可见;他人私有不可见。"""
    alice, bob = uuid.uuid4(), uuid.uuid4()
    registry = SkillRegistry()
    registry.register(_make_skill("builtin_a", "code_security_audit", tmp_path))
    registry.register(_make_skill("alice_priv", f"user_{alice}", tmp_path))
    registry.register(_make_skill("bob_priv", f"user_{bob}", tmp_path))
    monkeypatch.setattr(skill_loader, "REGISTRY", registry)

    alice_visible = {s.name for s in list_visible_skills(alice)}
    assert alice_visible == {"builtin_a", "alice_priv"}

    bob_visible = {s.name for s in list_visible_skills(bob)}
    assert bob_visible == {"builtin_a", "bob_priv"}

    anon_visible = {s.name for s in list_visible_skills(None)}
    assert anon_visible == {"builtin_a"}


def test_upload_name_conflict_allows_cross_user_same_name(tmp_path, monkeypatch):
    """上传重名判定:内置同名拒绝;他人同名允许(运行时隔离);自己同名返回 existing。"""
    from fastapi import HTTPException

    from app.routers.skills import _check_upload_name_conflict

    alice, bob = uuid.uuid4(), uuid.uuid4()
    registry = SkillRegistry()
    registry.register(_make_skill("builtin_a", "code_security_audit", tmp_path))
    registry.register(_make_skill("alice_priv", f"user_{alice}", tmp_path))
    registry.register(_make_skill("bob_priv", f"user_{bob}", tmp_path))
    monkeypatch.setattr(skill_loader, "REGISTRY", registry)

    # 与内置同名 → 409 拒绝
    with pytest.raises(HTTPException) as ei:
        _check_upload_name_conflict("builtin_a", alice)
    assert ei.value.status_code == 409

    # 与自己同名 → 返回 existing(交由 force 判断)
    assert _check_upload_name_conflict("alice_priv", alice) is not None

    # 与他人同名 → 允许(运行时按用户隔离,返回 None)
    assert _check_upload_name_conflict("bob_priv", alice) is None

    # 全新名称 → 无冲突
    assert _check_upload_name_conflict("brand_new", alice) is None


# ============================================================
# skill_tool:按用户过滤 + allowed_skills 白名单叠加
# ============================================================


def test_skill_tool_user_isolation(tmp_path, monkeypatch):
    """skill 工具按当前任务所属用户过滤:内置共享,私有仅 owner。"""
    from app.tools import skill_tool

    alice, bob = uuid.uuid4(), uuid.uuid4()
    registry = SkillRegistry()
    registry.register(_make_skill("builtin_a", "code_security_audit", tmp_path))
    registry.register(_make_skill("alice_priv", f"user_{alice}", tmp_path))
    registry.register(_make_skill("bob_priv", f"user_{bob}", tmp_path))
    monkeypatch.setattr(skill_loader, "REGISTRY", registry)

    # alice 视角:内置 + 自己的
    skill_tool.set_current_user_id(alice)
    result = skill_tool.list_available_skills()
    names = {s["name"] for s in result["skills"]}
    assert names == {"builtin_a", "alice_priv"}

    # bob 视角:内置 + 自己的,看不到 alice 的
    skill_tool.set_current_user_id(bob)
    result = skill_tool.list_available_skills()
    names = {s["name"] for s in result["skills"]}
    assert names == {"builtin_a", "bob_priv"}

    # 匿名任务:仅内置
    skill_tool.set_current_user_id(None)
    result = skill_tool.list_available_skills()
    names = {s["name"] for s in result["skills"]}
    assert names == {"builtin_a"}

    # run_skill 对他人私有 skill 返回未知(不泄露存在性)
    skill_tool.set_current_user_id(bob)
    result = skill_tool.run_skill("alice_priv")
    assert "error" in result
    assert result["error"].startswith("未知 skill")


def test_skill_tool_allowed_skills_combined(tmp_path, monkeypatch):
    """allowed_skills 白名单在用户可见范围内叠加过滤。"""
    from app.tools import skill_tool

    alice = uuid.uuid4()
    registry = SkillRegistry()
    registry.register(_make_skill("builtin_a", "code_security_audit", tmp_path))
    registry.register(_make_skill("alice_priv", f"user_{alice}", tmp_path))
    monkeypatch.setattr(skill_loader, "REGISTRY", registry)

    skill_tool.set_current_user_id(alice)
    skill_tool.set_current_allowed_skills(["alice_priv"])
    result = skill_tool.list_available_skills()
    names = {s["name"] for s in result["skills"]}
    assert names == {"alice_priv"}
    assert result["filtered"] is True

    # 恢复默认,避免影响其他测试
    skill_tool.set_current_user_id(None)
    skill_tool.set_current_allowed_skills(None)
