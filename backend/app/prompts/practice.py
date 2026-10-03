"""练习出题的提示词资产。

按学习主题切换出题视角(head 段)+ 共享的工具说明段与通用规则段,
外加主题分类器 system、单条发现模板与质量反馈文案。
执行逻辑(工具循环、LLM 调用、解析落库)在 app/services/practice/generator.py。

本模块保持零 app.* 依赖:主题 key 使用与
app/models/practice.py LEARNING_TOPIC_* 一致的字符串字面量
("security" / "architecture" / "coding" / "contract",DB 存储的稳定枚举值)。
"""
from typing import Any

# ============================================================
# 提示词:按学习主题切换出题视角(通用规则段共享)
# ============================================================

_SECURITY_TOPIC_HEAD = """你是一名网络安全培训出题专家。基于给定的真实代码审计发现,改编出用于安全培训的客观题。

## 出题视角
- 漏洞识别:该代码片段存在哪种漏洞
- 成因判断:漏洞根因与触发条件
- 修复选择:正确的修复方式与安全编码实践
- knowledge_key 优先用 CWE 编号(如 "CWE-89");无对应 CWE 时用英文短标识(如 "hardcoded_secrets")"""

_ARCHITECTURE_TOPIC_HEAD = """你是一名软件架构培训出题专家。基于给定的真实代码分析发现,从架构设计视角改编出客观题。

## 出题视角
- 模块边界与职责划分、分层是否合理、依赖方向
- 设计模式的应用与误用、技术选型权衡(一致性/性能/可维护性)
- 耦合与内聚、扩展性、可测试性缺陷及其改进方案
- 即使发现本身是安全/质量问题,也应从架构成因或设计改进角度出题
- knowledge_key 用英文短标识(如 "layering"、"circular_dependency"、"observer_pattern")"""

_CODING_TOPIC_HEAD = """你是一名通用编码能力培训出题专家。基于给定的真实代码分析发现,改编出考察通用编码能力的客观题。

## 出题视角
- bug 识别与边界条件、异常与错误处理
- 代码坏味道与可读性、性能问题(如 N+1 查询、不必要的重复计算)
- 语言特性正确用法与工程最佳实践(测试、命名、API 设计)
- knowledge_key 用英文短标识(如 "null_safety"、"exception_handling"、"n_plus_one_query")"""

_CONTRACT_TOPIC_HEAD = """你是一名合同文书培训出题专家。基于给定的真实文书审查发现(合同/协议类条款),改编出考察条款判断力的客观题。

## 出题视角
- 条款不利识别:以下哪一句条款对你不利、哪条最值得警惕
- 权责对等判断:单方面义务、责任与权利是否失衡
- 付款与违约:付款条件模糊、违约金畸高或缺失的后果
- 知识产权归属:成果归属、署名权、竞业限制的影响
- 霸王条款识别:任意解除权、单方变更权、过度免责
- knowledge_key 用英文短标识(如 "payment_terms"、"ip_ownership"、"unfair_clause")

## 材料规则(文书题特有)
- code_snippet 字段放**条款原文**(不是代码),截取与发现相关的完整句子
- languages 固定给空数组 []
- source_file 为文书文件路径(工作区内相对路径),source_lines 不适用时给 null"""

# key 与 app/models/practice.py 的 LEARNING_TOPIC_* 枚举值一致
_TOPIC_PROMPT_HEADS = {
    "security": _SECURITY_TOPIC_HEAD,
    "architecture": _ARCHITECTURE_TOPIC_HEAD,
    "coding": _CODING_TOPIC_HEAD,
    "contract": _CONTRACT_TOPIC_HEAD,
}

# 工作区可用时注入的工具说明段(工具实际可用与否与 sandbox 存活状态一致)
_TOOL_SECTION = """## 材料查阅工具(工作区已就绪,必须使用)
出题前必须先调工具查阅真实材料(源码或文书原文),禁止跳过直接出题:
- read_file(file_path, max_lines?, offset?):读工作区文件(带行号,分页)
- search_code(pattern, file_glob?, output_mode?):正则搜索文件内容;
  output_mode="files_with_matches" 可快速定位含关键词的文件
- find_files(pattern):按 glob 递归查文件路径(如 **/*.py 或 **/*.txt)
要求:
1. 每道题出题前至少 read_file 一次相关文件,确认材料真实存在
2. 真实材料题(origin=repo):题干与 code_snippet 必须引用读到的真实内容,
   不得虚构,并给出 source_file(工作区内相对路径)与
   source_lines(行区间如 "120-150" 或单行号 "42",取自 read_file 结果)
3. 改编题(origin=synthetic):先读原文件确认问题形态,再原创虚构材料,
   不给 source_file/source_lines
确实在工作区中找不到相关文件时,才退回基于发现描述出题(此时不给 source_file)。"""

# 各主题共享的输出规则段(代码/文书材料双轨)
_COMMON_RULES = """## 通用要求
1. 出 1~3 道题,题型限定:
   - single_choice(单选):如「该材料存在哪种问题」「正确的处理方式是」
   - true_false(判断):选项固定为 ["正确", "错误"],题干为一个可判定真伪的陈述
2. 题目必须阅读材料才能作答:题干要落到具体材料细节(代码:函数名/调用关系/
   变量/分支逻辑;文书:具体条款原文的措辞与限定),禁止不看材料也能答对的
   通用概念题、常识题
3. 材料片段单独放 code_snippet,必须自包含:代码题含必要的函数签名、导入与
   上下文,文书题放相关条款的完整原句,脱离原文也能读懂;题干不得依赖
   特有路径、内部命名才可作答
4. 出题形式由你自主决定,鼓励真实材料题与改编题混合:
   - origin="repo"(真实材料题):基于发现中的真实材料出题
   - origin="synthetic"(改编题):原创一段含同类问题的完整虚构材料,
     业务场景与命名与原项目完全不同,题干不得提及原项目;考察用户把知识
     泛化应用到新材料的能力
5. 干扰项要有迷惑性但明确错误,正确答案唯一
6. explanation 讲清原理与改进要点,100 字以内
7. difficulty 评估难度(1-5 整数):1=概念识别,3=需理解材料逻辑,5=需深入细节
8. knowledge_name:知识点中文展示名(如 "SQL 注入"、"付款条件模糊")
9. languages:该题涉及的编程语言,小写短名数组(如 ["python", "sql"]);
   文书类题目固定给空数组 []
10. 元信息中若标注 verified: false 或判定为误报的发现,不要出题,直接返回空数组 []

只输出 JSON 数组,不要任何其他文字。每个元素结构:
{"qtype": "single_choice|true_false", "origin": "repo|synthetic",
 "stem": "...", "code_snippet": "...",
 "source_file": "工作区内相对路径"或null, "source_lines": "120-150"或null,
 "options": ["...", "..."], "answer_idx": 0, "explanation": "...",
 "difficulty": 3, "knowledge_key": "...", "knowledge_name": "...",
 "languages": ["python", "sql"]}"""


def build_system_prompt(topic: str, workspace_available: bool) -> str:
    """按学习主题拼出题 system prompt;工作区可用时附工具说明段"""
    head = _TOPIC_PROMPT_HEADS.get(topic) or _TOPIC_PROMPT_HEADS["security"]
    sections = [head]
    if workspace_available:
        sections.append(_TOOL_SECTION)
    sections.append(_COMMON_RULES)
    return "\n\n".join(sections)


# ============================================================
# 主题自动匹配:规则先行 + LLM 批量兜底(不再是用户级设置)
# ============================================================

_TOPIC_CLASSIFY_SYSTEM = """你是审查发现的主题分类器。对给定的每条发现,判断它最适合改编成哪种主题的练习题:
- security:安全漏洞(注入、硬编码凭证、越权、SSRF、配置泄露等)
- architecture:架构设计问题(分层、耦合、模块边界、设计模式、技术选型)
- coding:通用代码质量问题(bug、边界条件、异常处理、性能、可读性、测试)
- contract:合同文书问题(条款不利、权责失衡、付款违约、知识产权、霸王条款)

只输出 JSON 数组,不要任何其他文字。每个元素:
{"id": <发现序号,原样返回>, "topic": "security|architecture|coding|contract", "reason": "一句话理由"}"""


# ============================================================
# 出题工具循环(只读三工具;repo_path/task_id 由宿主注入,LLM 不感知)
# ============================================================

_PRACTICE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取仓库内文件内容(带行号,支持 offset 翻页)。发现描述缺少具体代码时,先读相关源文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "仓库内相对路径"},
                    "max_lines": {"type": "integer", "description": "本次最多返回行数,默认 100"},
                    "offset": {"type": "integer", "description": "从第几行开始读(1-based),默认 1"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "正则搜索仓库代码,定位相关函数、字符串或调用点",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_glob": {"type": "string", "description": "文件名过滤 glob(可选),如 *.py"},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files_with_matches"],
                        "description": "content=匹配行+行号;files_with_matches=仅文件路径(快速定位)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按 glob 模式递归查找文件路径(不看内容),如 **/*.py",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式"},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ============================================================
# 单条发现的出题模板与质量反馈
# ============================================================

_FINDING_TEMPLATE = """以下是审查任务的一条真实发现(代码审计或文书审核):

【标题】{title}

【详细描述】
{content}

【元信息】{metadata}

请基于这条发现出题(1~3 道)。"""

# agent2 标记的学习点(practice_worthy=true)附加提示:引导出题
# 聚焦 agent2 认为值得学的点,而非泛泛复述发现本身
_LEARNING_NOTE_TEMPLATE = (
    "\n\n【学习价值提示】质检 agent 已判定这条发现对用户有学习价值:"
    "{note}\n请让题目围绕这个学习点展开考察。"
)

# 工作区可用但生成的题目全部缺材料上下文时,追加到 user prompt 重试的质量反馈
_NO_CODE_FEEDBACK = (
    "\n\n【质量反馈】上一轮生成的题目缺少真实材料上下文,不看材料也能作答,不合格。"
    "请先用材料查阅工具(read_file 等)读取相关文件,再基于实际材料重新出题:"
    "题干必须落到具体材料细节,每题必须带 code_snippet,并给出 source_file 与 source_lines。"
)
