"""SKILL 数据模型

参考 Trae / Claude Code 的 SKILL.md 规范:
- 每个 skill 是一个独立目录 `<scenario>/<skill_name>/SKILL.md`
- SKILL.md = YAML frontmatter(name + description) + Markdown body(指令说明)
- 加载时解析 frontmatter,body 原样保留,LLM 调用 skill 工具时拿到 body 自行执行
"""
from pathlib import Path

from pydantic import BaseModel, Field


class ParsedSkill(BaseModel):
    """已解析的 SKILL(对应磁盘上一个 SKILL.md 文件)

    字段:
        name: skill 唯一标识(来自 frontmatter)
        description: 简短说明(来自 frontmatter,给 LLM 选 skill 时看)
        scenario_id: 所属场景(从目录路径推断,如 code_security_audit)
        skill_dir: skill 目录的绝对路径(可能含附加资源,如规则文件)
        body: SKILL.md 正文(frontmatter 之后的 Markdown 指令,LLM 执行时看)
        source_path: SKILL.md 绝对路径(管理 API 用)
    """

    name: str
    description: str
    scenario_id: str
    skill_dir: Path
    body: str
    source_path: Path


class SkillSummary(BaseModel):
    """skill 概要(给 LLM 列出可用 skill 时用)"""

    name: str
    description: str


class SkillRegistry(BaseModel):
    """skill 注册表(按 scenario_id 分组)

    进程级缓存,启动时一次性扫描磁盘所有 SKILL.md 并解析。
    管理员后台增删 skill 后调用 reload() 重新扫描。
    """

    # 外层 key=scenario_id, 内层 key=skill_name
    skills: dict[str, dict[str, ParsedSkill]] = Field(default_factory=dict)

    def register(self, skill: ParsedSkill) -> None:
        """注册一个 skill"""
        self.skills.setdefault(skill.scenario_id, {})[skill.name] = skill

    def get(self, scenario_id: str, skill_name: str) -> ParsedSkill | None:
        """按 (scenario, name) 查找 skill"""
        return self.skills.get(scenario_id, {}).get(skill_name)

    def list_for_scenario(self, scenario_id: str) -> list[ParsedSkill]:
        """列出某场景的所有 skill"""
        return list(self.skills.get(scenario_id, {}).values())

    def list_scenarios(self) -> list[str]:
        """列出所有有 skill 的场景 id"""
        return list(self.skills.keys())

    def remove(self, scenario_id: str, skill_name: str) -> bool:
        """删除一个 skill(从注册表;不删磁盘文件)

        返回是否删除成功
        """
        bucket = self.skills.get(scenario_id)
        if not bucket or skill_name not in bucket:
            return False
        del bucket[skill_name]
        return True
