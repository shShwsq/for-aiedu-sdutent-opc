"""数据目录统一根迁移(旧默认目录 → data/ 子目录)

历史上 4 个运行时数据目录平铺在后端运行目录根下(_repos / _repo_cache /
user_skills / uploads_data),gitignore、备份、卷挂载各需单独配置。现统一收敛到
单一数据根 data/ 下(repos / repo_cache / user_skills / uploads),见 config.py
各 *_DIR 设置的新默认值。

本模块在应用启动(lifespan)时执行一次性自动迁移,规则:

- 仅当设置仍为新默认值(未被 env 重定位)时迁移;用户显式配置的位置一律尊重,
  绝不移动(env 配成旧路径同样视为用户选择)
- 旧默认目录存在且新目录不存在 → 整目录 rename 搬家(同卷原子操作)
- 两者都存在 → 保留新目录原样,打 warning 提示手动合并(绝不覆盖/合并数据)
- 旧目录不存在 → 无事发生(全新部署的常态)

REPO_CACHE_DIR 搬家时若启用了 sandbox 缓存共享(REPO_CACHE_SANDBOX_ENABLED),
REPO_CACHE_SANDBOX_HOST_DIR 指向的宿主机路径可能随之失效,打 warning 提醒核查。
"""
import logging
import shutil
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# (设置名, 旧默认路径, 新默认路径);相对路径按进程 CWD 解析,与各消费方一致
_LEGACY_DIR_PAIRS: list[tuple[str, str, str]] = [
    ("REPO_CLONE_DIR", "./_repos", "./data/repos"),
    ("REPO_CACHE_DIR", "./_repo_cache", "./data/repo_cache"),
    ("USER_SKILLS_DIR", "./user_skills", "./data/user_skills"),
    ("UPLOADS_DIR", "./uploads_data", "./data/uploads"),
]


def migrate_legacy_data_dirs(
    pairs: list[tuple[str, str, str]] | None = None,
) -> list[tuple[Path, Path]]:
    """把旧默认数据目录搬迁到 data/ 统一根下;返回实际迁移的 (旧, 新) 列表

    pairs 参数供测试注入绝对路径定制对;生产调用不传,用 _LEGACY_DIR_PAIRS。
    """
    moved: list[tuple[Path, Path]] = []
    for setting_name, legacy_rel, new_rel in pairs or _LEGACY_DIR_PAIRS:
        current = Path(getattr(settings, setting_name))
        # 设置已被 env 重定位(≠ 新默认)→ 尊重用户选择,不动
        if current.resolve() != Path(new_rel).resolve():
            continue
        legacy = Path(legacy_rel)
        new = Path(new_rel)
        if not legacy.exists():
            continue  # 全新部署(或早已迁移),常态
        if new.exists():
            logger.warning(
                "[data-dirs] 新目录 %s 与旧目录 %s 同时存在,保留新目录不合并;"
                "如需找回旧数据请手动合并后删除旧目录",
                new, legacy,
            )
            continue
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(legacy), str(new))
        except OSError as e:
            # 搬迁失败(占用/跨卷受限等):不阻断启动,各消费方按旧目录继续可用
            logger.warning("[data-dirs] 迁移 %s → %s 失败(继续用旧目录): %s",
                           legacy, new, e)
            continue
        logger.info("[data-dirs] 已迁移 %s → %s", legacy, new)
        moved.append((legacy, new))
        if setting_name == "REPO_CACHE_DIR" and settings.REPO_CACHE_SANDBOX_ENABLED:
            logger.warning(
                "[data-dirs] 仓库缓存目录已搬家 %s → %s;sandbox 缓存共享启用中,"
                "请核查 REPO_CACHE_SANDBOX_HOST_DIR 是否仍指向该缓存的宿主机路径",
                legacy, new,
            )
    return moved
