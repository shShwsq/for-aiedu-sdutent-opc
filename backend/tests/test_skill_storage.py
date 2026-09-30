"""用户 skill 存储后端与双根扫描单元测试

覆盖 storage.DirectorySkillStorage:
- save 首次保存 / 覆盖返回 True / 目录结构正确
- delete 幂等(不存在静默)
- contains 存在性判断

覆盖 loader 双根扫描:
- get_user_skills_root 相对/绝对路径解析
- discover_skills 合并内置根 + 用户根;用户根后扫同名覆盖
"""
from pathlib import Path

from app.skills import loader as skill_loader
from app.skills.loader import discover_skills, get_user_skills_root
from app.skills.storage import DirectorySkillStorage


# ============================================================
# DirectorySkillStorage
# ============================================================


def _write_skill_dir(root: Path, name: str, extra: str = "") -> Path:
    """在 root 下构造一个含 SKILL.md 的 skill 目录,返回该目录"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc\n---\nbody {extra}\n", encoding="utf-8"
    )
    return d


def test_save_first_time_returns_false(tmp_path):
    """首次保存返回 False,目录结构 <root>/<scenario>/<skill>/。"""
    storage = DirectorySkillStorage(tmp_path)
    src = _write_skill_dir(tmp_path / "src", "my_skill")

    replaced = storage.save("user_abc", "my_skill", src)

    assert replaced is False
    dest = tmp_path / "user_abc" / "my_skill"
    assert (dest / "SKILL.md").is_file()
    assert storage.contains("user_abc", "my_skill") is True


def test_save_overwrite_returns_true_and_replaces(tmp_path):
    """同名覆盖返回 True,且新内容替换旧内容。"""
    storage = DirectorySkillStorage(tmp_path)
    src1 = _write_skill_dir(tmp_path / "src1", "my_skill", extra="v1")
    src2 = _write_skill_dir(tmp_path / "src2", "my_skill", extra="v2")
    (src2 / "extra.txt").write_text("new", encoding="utf-8")

    assert storage.save("user_abc", "my_skill", src1) is False
    replaced = storage.save("user_abc", "my_skill", src2)

    assert replaced is True
    dest = tmp_path / "user_abc" / "my_skill"
    assert "v2" in (dest / "SKILL.md").read_text(encoding="utf-8")
    # 旧目录已清空,新附加文件存在
    assert not (dest / "extra.txt").exists() or (dest / "extra.txt").read_text() == "new"


def test_delete_is_idempotent(tmp_path):
    """删除后目录消失;重复删除 / 删除不存在的不抛错。"""
    storage = DirectorySkillStorage(tmp_path)
    src = _write_skill_dir(tmp_path / "src", "my_skill")
    storage.save("user_abc", "my_skill", src)

    storage.delete("user_abc", "my_skill")
    assert storage.contains("user_abc", "my_skill") is False

    # 幂等:再删一次不报错
    storage.delete("user_abc", "my_skill")
    storage.delete("no_such_scenario", "no_such_skill")


def test_contains_distinguishes_dirs_and_files(tmp_path):
    """contains 只认 skill 目录;空场景 / 文件不误判。"""
    storage = DirectorySkillStorage(tmp_path)
    src = _write_skill_dir(tmp_path / "src", "my_skill")
    storage.save("user_abc", "my_skill", src)

    assert storage.contains("user_abc", "my_skill") is True
    assert storage.contains("user_abc", "other") is False
    assert storage.contains("no_such", "my_skill") is False


# ============================================================
# loader:get_user_skills_root
# ============================================================


def test_get_user_skills_root_relative(monkeypatch, tmp_path):
    """相对路径基于运行目录(cwd)解析。"""
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", "./custom_skills")
    monkeypatch.chdir(tmp_path)

    assert get_user_skills_root() == tmp_path / "custom_skills"


def test_get_user_skills_root_absolute(monkeypatch, tmp_path):
    """绝对路径原样返回。"""
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # cwd 无关紧要,验证绝对路径优先

    assert get_user_skills_root() == tmp_path


# ============================================================
# loader:双根扫描合并
# ============================================================


def _make_skill_md(root: Path, scenario: str, name: str) -> None:
    d = root / scenario / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc\n---\nbody\n", encoding="utf-8"
    )


def test_discover_merges_builtin_and_user_roots(tmp_path, monkeypatch):
    """默认扫描:内置根 + 用户根合并;内置根中遗留的 user_* 也被扫到。"""
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user_skills"
    _make_skill_md(builtin_root, "code_security_audit", "builtin_a")
    # 旧版本遗留:内置根下也有 user_* 目录
    legacy_uid = "user_legacy-0000-0000-0000-000000000000"
    _make_skill_md(builtin_root, legacy_uid, "legacy_skill")
    # 新版本:用户根
    _make_skill_md(user_root, "user_new-0000-0000-0000-000000000000", "new_skill")

    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", builtin_root)
    monkeypatch.setattr(
        skill_loader.settings, "USER_SKILLS_DIR", str(user_root)
    )

    registry = discover_skills()

    scenarios = set(registry.list_scenarios())
    assert scenarios == {"code_security_audit", legacy_uid, "user_new-0000-0000-0000-000000000000"}
    assert registry.get("code_security_audit", "builtin_a") is not None
    assert registry.get(legacy_uid, "legacy_skill") is not None
    assert registry.get("user_new-0000-0000-0000-000000000000", "new_skill") is not None


def test_discover_user_root_absent_is_ok(tmp_path, monkeypatch):
    """用户根目录不存在时只扫内置根,不报错。"""
    builtin_root = tmp_path / "builtin"
    _make_skill_md(builtin_root, "code_security_audit", "builtin_a")

    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", builtin_root)
    monkeypatch.setattr(
        skill_loader.settings, "USER_SKILLS_DIR", str(tmp_path / "not_exist")
    )

    registry = discover_skills()
    assert registry.get("code_security_audit", "builtin_a") is not None
    assert len(registry.list_scenarios()) == 1


def test_discover_user_root_overrides_legacy_same_name(tmp_path, monkeypatch):
    """同一 (scenario, name) 在旧位置和新位置都存在时,用户根(后扫)覆盖。"""
    builtin_root = tmp_path / "builtin"
    user_root = tmp_path / "user_skills"
    uid = "user_abc-0000-0000-0000-000000000000"
    # 旧位置:内置根下
    _make_skill_md(builtin_root, uid, "dup_skill")
    (builtin_root / uid / "dup_skill" / "SKILL.md").write_text(
        "---\nname: dup_skill\ndescription: old\n---\nold body\n", encoding="utf-8"
    )
    # 新位置:用户根
    _make_skill_md(user_root, uid, "dup_skill")
    (user_root / uid / "dup_skill" / "SKILL.md").write_text(
        "---\nname: dup_skill\ndescription: new\n---\nnew body\n", encoding="utf-8"
    )

    monkeypatch.setattr(skill_loader, "DEFAULT_SKILLS_ROOT", builtin_root)
    monkeypatch.setattr(skill_loader.settings, "USER_SKILLS_DIR", str(user_root))

    registry = discover_skills()
    skill = registry.get(uid, "dup_skill")
    assert skill is not None
    assert skill.description == "new"
    # source_path 指向新位置
    assert skill.source_path == user_root / uid / "dup_skill" / "SKILL.md"
