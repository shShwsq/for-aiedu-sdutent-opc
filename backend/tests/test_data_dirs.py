"""数据目录统一根迁移测试(app/services/data_dirs.py)

直接调用 migrate_legacy_data_dirs(pairs 注入 tmp_path 绝对路径),覆盖:
- 旧目录存在且设置为新默认 → 整目录搬家(内容完整)
- 旧目录不存在(全新部署) → 无事发生
- 新旧并存 → 保留新目录不合并(数据零风险优先)
- 设置被 env 重定位 → 尊重用户选择,绝不移动
- shutil.move 失败 → 不阻断(返回空,旧目录原样)
- REPO_CACHE_DIR 搬家且 sandbox 缓存共享开启 → 额外提醒核查宿主机路径
- 生产对 _LEGACY_DIR_PAIRS 的结构约定(设置名与 config 默认值一致性)
"""
from pathlib import Path

import pytest

import app.services.data_dirs as data_dirs
from app.config import settings
from app.services.data_dirs import migrate_legacy_data_dirs


def _mk(dir_: Path, files: dict[str, str] | None = None) -> Path:
    """建目录并写入若干文件,返回该目录"""
    dir_.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {"meta.json": "{}"}).items():
        (dir_ / name).write_text(content, encoding="utf-8")
    return dir_


def _pairs(tmp_path: Path, names: list[str]) -> list[tuple[str, str, str]]:
    """构造 {name: (设置名, 旧绝对路径, 新绝对路径)} 注入对(生产是相对路径,逻辑等价)"""
    mapping = {
        "REPO_CLONE_DIR": "repos",
        "REPO_CACHE_DIR": "repo_cache",
        "USER_SKILLS_DIR": "user_skills",
        "UPLOADS_DIR": "uploads",
    }
    return [
        (setting, str(tmp_path / f"_{short}"), str(tmp_path / "data" / short))
        for setting, short in ((s, mapping[s]) for s in names)
    ]


def _point_settings(monkeypatch, tmp_path: Path, names: list[str]) -> None:
    """把指定设置指向各自新默认路径(模拟未被 env 重定位)"""
    mapping = {
        "REPO_CLONE_DIR": "repos",
        "REPO_CACHE_DIR": "repo_cache",
        "USER_SKILLS_DIR": "user_skills",
        "UPLOADS_DIR": "uploads",
    }
    for setting in names:
        monkeypatch.setattr(
            settings, setting, str(tmp_path / "data" / mapping[setting])
        )


def _reset_settings_to_defaults(monkeypatch) -> None:
    """把 4 个目录设置重置为 _LEGACY_DIR_PAIRS 的新默认相对路径
    (消除本机 .env 覆盖对生产对测试的干扰)"""
    for setting_name, _legacy_rel, new_rel in data_dirs._LEGACY_DIR_PAIRS:
        monkeypatch.setattr(settings, setting_name, new_rel)


# ============================================================
# 迁移成功
# ============================================================


def test_migrate_moves_legacy_dir(tmp_path, monkeypatch):
    """旧目录存在且设置=新默认 → 整目录搬家,内容完整"""
    pairs = _pairs(tmp_path, ["UPLOADS_DIR"])
    _point_settings(monkeypatch, tmp_path, ["UPLOADS_DIR"])
    legacy = _mk(tmp_path / "_uploads", {"meta.json": '{"user_id": "u1"}'})

    moved = migrate_legacy_data_dirs(pairs)

    assert moved == [(legacy, tmp_path / "data" / "uploads")]
    assert not legacy.exists()  # 旧目录整体消失
    assert (tmp_path / "data" / "uploads" / "meta.json").read_text(
        encoding="utf-8"
    ) == '{"user_id": "u1"}'


def test_migrate_no_legacy_dir_noop(tmp_path, monkeypatch):
    """旧目录不存在(全新部署常态) → 无事发生"""
    pairs = _pairs(tmp_path, ["UPLOADS_DIR"])
    _point_settings(monkeypatch, tmp_path, ["UPLOADS_DIR"])

    assert migrate_legacy_data_dirs(pairs) == []
    assert not (tmp_path / "data" / "uploads").exists()


def test_migrate_multiple_pairs_partial(tmp_path, monkeypatch):
    """多对混合:仅存在旧目录的那对迁移,其余跳过"""
    pairs = _pairs(tmp_path, ["REPO_CLONE_DIR", "UPLOADS_DIR"])
    _point_settings(monkeypatch, tmp_path, ["REPO_CLONE_DIR", "UPLOADS_DIR"])
    legacy_repos = _mk(tmp_path / "_repos", {"a.txt": "hello"})

    moved = migrate_legacy_data_dirs(pairs)

    assert moved == [(legacy_repos, tmp_path / "data" / "repos")]
    assert (tmp_path / "data" / "repos" / "a.txt").exists()
    assert not (tmp_path / "data" / "uploads").exists()


# ============================================================
# 安全分支:不动用户数据
# ============================================================


def test_migrate_skip_when_relocated_by_env(tmp_path, monkeypatch):
    """设置被 env 重定位(≠新默认) → 尊重用户选择,旧目录原样不动"""
    pairs = _pairs(tmp_path, ["UPLOADS_DIR"])
    legacy = _mk(tmp_path / "_uploads")
    # 设置指向第三方位置(既非旧默认也非新默认)
    monkeypatch.setattr(settings, "UPLOADS_DIR", str(tmp_path / "custom-uploads"))

    moved = migrate_legacy_data_dirs(pairs)

    assert moved == []
    assert legacy.exists()  # 旧目录未被碰


def test_migrate_skip_when_both_exist(tmp_path, monkeypatch, caplog):
    """新旧并存 → 保留新目录不合并,打 warning 提示手动处理"""
    pairs = _pairs(tmp_path, ["UPLOADS_DIR"])
    _point_settings(monkeypatch, tmp_path, ["UPLOADS_DIR"])
    legacy = _mk(tmp_path / "_uploads", {"old.txt": "旧数据"})
    _mk(tmp_path / "data" / "uploads", {"new.txt": "新数据"})

    with caplog.at_level("WARNING"):
        moved = migrate_legacy_data_dirs(pairs)

    assert moved == []
    assert (legacy / "old.txt").exists()  # 旧目录原样
    assert (tmp_path / "data" / "uploads" / "new.txt").exists()  # 新目录原样
    assert "不合并" in caplog.text


def test_migrate_move_failure_not_fatal(tmp_path, monkeypatch, caplog):
    """shutil.move 失败(占用/跨卷) → 不阻断启动,旧目录继续可用"""
    pairs = _pairs(tmp_path, ["UPLOADS_DIR"])
    _point_settings(monkeypatch, tmp_path, ["UPLOADS_DIR"])
    legacy = _mk(tmp_path / "_uploads")

    def _boom(src, dst):
        raise OSError("device busy")

    monkeypatch.setattr(data_dirs.shutil, "move", _boom)
    with caplog.at_level("WARNING"):
        moved = migrate_legacy_data_dirs(pairs)

    assert moved == []
    assert legacy.exists()  # 旧目录留在原地,各消费方按旧路径继续可用
    assert "继续用旧目录" in caplog.text


# ============================================================
# repo_cache 搬家的 sandbox 共享提醒
# ============================================================


def test_migrate_repo_cache_warns_sandbox_share(tmp_path, monkeypatch, caplog):
    """REPO_CACHE_DIR 搬家且 sandbox 缓存共享开启 → 提醒核查宿主机路径"""
    pairs = _pairs(tmp_path, ["REPO_CACHE_DIR"])
    _point_settings(monkeypatch, tmp_path, ["REPO_CACHE_DIR"])
    monkeypatch.setattr(settings, "REPO_CACHE_SANDBOX_ENABLED", True)
    _mk(tmp_path / "_repo_cache")

    with caplog.at_level("WARNING"):
        moved = migrate_legacy_data_dirs(pairs)

    assert len(moved) == 1
    assert "REPO_CACHE_SANDBOX_HOST_DIR" in caplog.text


def test_migrate_repo_cache_no_warn_when_sandbox_share_off(
    tmp_path, monkeypatch, caplog
):
    """sandbox 缓存共享关闭时搬家 → 不打宿主机路径提醒"""
    pairs = _pairs(tmp_path, ["REPO_CACHE_DIR"])
    _point_settings(monkeypatch, tmp_path, ["REPO_CACHE_DIR"])
    monkeypatch.setattr(settings, "REPO_CACHE_SANDBOX_ENABLED", False)
    _mk(tmp_path / "_repo_cache")

    with caplog.at_level("WARNING"):
        moved = migrate_legacy_data_dirs(pairs)

    assert len(moved) == 1
    assert "REPO_CACHE_SANDBOX_HOST_DIR" not in caplog.text


# ============================================================
# 生产对配置的结构约定
# ============================================================


def test_legacy_pairs_match_config_defaults():
    """_LEGACY_DIR_PAIRS 的 4 个新默认路径必须与 config.py 类级默认值一致
    (漂移则迁移判定失真:设置永远 ≠"新默认",迁移静默失效)。
    取 Settings.model_fields 而非实例值,避免本机 .env 覆盖造成误报"""
    from app.config import Settings

    for setting_name, _legacy_rel, new_rel in data_dirs._LEGACY_DIR_PAIRS:
        default = Settings.model_fields[setting_name].default
        assert default == new_rel, (
            f"{setting_name} 默认值已改为 {default!r},"
            f"但 data_dirs._LEGACY_DIR_PAIRS 仍写 {new_rel!r},请同步"
        )


def test_migrate_production_pairs_noop_on_clean_tree(monkeypatch, tmp_path):
    """生产对在空目录环境下幂等安全(设置即默认值,旧目录不存在 → 全跳过)"""
    monkeypatch.chdir(tmp_path)  # 相对路径按 CWD 解析,与生产语义一致
    _reset_settings_to_defaults(monkeypatch)
    assert migrate_legacy_data_dirs() == []


def test_migrate_production_pairs_moves_legacy_tree(monkeypatch, tmp_path):
    """生产对端到端:旧默认目录 → data/ 子目录(相对路径语义)"""
    monkeypatch.chdir(tmp_path)
    _reset_settings_to_defaults(monkeypatch)
    for _setting, legacy_rel, new_rel in data_dirs._LEGACY_DIR_PAIRS:
        _mk(Path(legacy_rel))
    moved = migrate_legacy_data_dirs()
    assert len(moved) == len(data_dirs._LEGACY_DIR_PAIRS)
    for _setting, legacy_rel, new_rel in data_dirs._LEGACY_DIR_PAIRS:
        assert not Path(legacy_rel).exists()
        assert Path(new_rel).is_dir()
