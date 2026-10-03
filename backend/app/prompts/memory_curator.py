"""记忆归纳/精简的提示词资产。

含任务完成后的记忆归纳模板(_SUMMARIZE_PROMPT)与
精简注入模板(_SUMMARIZE_INJECT_PROMPT),以及两类记忆的
固定类别枚举(与模板是 format 参数配对关系,随迁于此)。
执行逻辑(解析 LLM 输出、合并、落库)在 app/services/memory_summarize.py。
"""

# 记忆类别固定枚举(按此顺序输出)
PROJECT_CATEGORIES = [
    "Hard Constraints",
    "Known Issues",
    "Audit Directions",
    "Tech Stack",
    "Lessons Learned",
]
GLOBAL_CATEGORIES = [
    "Hard Constraints",
    "Tech Stack",
    "Preferences",
    "Lessons Learned",
]

# 归纳 prompt(要求输出严格 JSON,带类别结构)
_SUMMARIZE_PROMPT = """You are a memory curator. Based on the task execution records below, extract durable knowledge that will help future tasks of the same kind.

[Repository]
{repo_url}

[User intent]
{user_intent}

[react_agent per-round summaries]
{react_summaries}

[agent2 final evaluation]
{ua_reasoning}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Each item must be a single concise line. No multi-paragraph prose.
- Only include genuinely reusable knowledge (constraints, known pitfalls, audit directions, tech stack facts, preferences, lessons). Skip one-off task details.

Categorize each item. Allowed categories:
- project_memory_update: {project_categories}
- global_memory_update: {global_categories}

Output STRICT JSON (no markdown fences). Use empty arrays if nothing new.
{{
  "project_memory_update": [
    {{"category": "Hard Constraints", "item": "..."}},
    {{"category": "Known Issues", "item": "..."}}
  ],
  "global_memory_update": [
    {{"category": "Preferences", "item": "..."}}
  ]
}}
"""

# 精简注入 prompt:把完整项目记忆压缩到 ≤MAX_PROJECT_MEM_INJECT 字符
_SUMMARIZE_INJECT_PROMPT = """You are condensing a project memory file for injection into an agent's system prompt (max {max_chars} chars).

[Full project memory]
{memory_content}

Rules:
- Write in English. Preserve language-specific Chinese terms, user quotes, and UI strings verbatim (do NOT translate them).
- Output ONLY the condensed memory as a flat list grouped by ## category headers, each item a single line starting with "- ".
- Use these categories in this order (skip empty ones): Hard Constraints, Known Issues, Audit Directions, Tech Stack, Lessons Learned.
- PRIORITIZE Hard Constraints and Known Issues (these most affect audit direction). Drop lower-priority / redundant items first to fit the limit.
- No preamble, no commentary, no markdown fences — only the condensed memory.
"""
