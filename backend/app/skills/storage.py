"""用户上传 skill 的存储后端抽象

职责:用户上传 skill 的持久化(保存 / 删除 / 存在性查询)。
内置 skill 是代码资产,始终由 loader 从 DEFAULT_SKILLS_ROOT 文件系统扫描,不经此接口。

当前实现:
- DirectorySkillStorage:文件系统目录存储,根目录来自 settings.USER_SKILLS_DIR

未来扩展:
- 数据库(PostgreSQL) / 对象存储(S3/MinIO)等,实现同一 SkillStorage 接口即可;
  loader 扫描用户 skill 时使用 loader.get_user_skills_root()(与 DirectorySkillStorage
  同源的 settings 路径),DB 实现落地时再扩展 loader 的枚举分支。
"""
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class SkillStorage(ABC):
    """用户上传 skill 的存储后端"""

    @abstractmethod
    def save(self, scenario_id: str, skill_name: str, src_dir: Path) -> bool:
        """保存 skill(src_dir 为含 SKILL.md + 资源文件的目录)

        返回 True 表示覆盖了已存在的同名 skill。
        """

    @abstractmethod
    def delete(self, scenario_id: str, skill_name: str) -> None:
        """删除 skill(幂等,不存在时静默)"""

    @abstractmethod
    def contains(self, scenario_id: str, skill_name: str) -> bool:
        """是否已存在同名 skill(用于重名 / 覆盖判断)"""


class DirectorySkillStorage(SkillStorage):
    """目录存储:<root>/<scenario_id>/<skill_name>/(SKILL.md + 资源文件)"""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def save(self, scenario_id: str, skill_name: str, src_dir: Path) -> bool:
        dest = self._root / scenario_id / skill_name
        replaced = dest.exists()
        if replaced:
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, dest)
        return replaced

    def delete(self, scenario_id: str, skill_name: str) -> None:
        shutil.rmtree(self._root / scenario_id / skill_name, ignore_errors=True)

    def contains(self, scenario_id: str, skill_name: str) -> bool:
        return (self._root / scenario_id / skill_name).is_dir()
