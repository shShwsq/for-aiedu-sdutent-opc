"""plan 计划清单解析(runtime 共享原语)。

从 LLM 输出内容中提取 <plan>...</plan> 计划清单:内置 react_agent(从
thinking content)与 CLI 执行器(acp_base,从最终 content)共用同一解析,
消除两处手抄实现。此前的两份逐字相同副本已收敛于此。

格式支持:
1. JSON 数组(system prompt 示范格式,经 json_repair 容错修复):
   [{"id": 1, "text": "步骤描述", "status": "pending"}, ...]
2. 逐行格式:序号 + 可选状态标记 + 文本
"""
import re

from json_repair import repair_json

_PLAN_BLOCK_RE = re.compile(r"<plan>\s*(.*?)\s*</plan>", re.DOTALL)
_PLAN_LINE_RE = re.compile(
    r"^\s*(?:\d+[.、)]\s*)?(?:\[([\w_]+)\]\s*)?(.+)$"
)
# 含文字字符(字母/数字/下划线/中文)才算有效步骤行,
# 纯符号行("["、"]"、"," 等)跳过,避免变成无意义步骤
_PLAN_LINE_HAS_TEXT_RE = re.compile(r"[\w\u4e00-\u9fff]")


def parse_plan_json(block: str) -> list[dict] | None:
    """尝试把 plan 块按 JSON 解析(对象数组,或逐行多个对象)

    system prompt 示范的是 JSON 数组格式,模型照做时逐行解析会把整行 JSON
    当成步骤文本,这里优先走 JSON 解析。依赖 json_repair 容错修复
    (尾逗号/截断/缺引号等)。无包裹数组的逐行对象自动补 [ ] 再修复。
    解析失败/无有效步骤返回 None,由调用方回退逐行解析。
    """
    text = block.strip()
    if not text or text[0] not in "[{":
        return None
    candidate = text if text[0] == "[" else f"[{text}]"
    try:
        repaired = repair_json(candidate, return_objects=True)
    except Exception:
        return None
    if not isinstance(repaired, list):
        repaired = [repaired]
    steps: list[dict] = []
    for e in repaired:
        if not isinstance(e, dict):
            continue
        step_text = str(e.get("text") or e.get("content") or "").strip()
        if not step_text:
            continue
        status = str(e.get("status") or "pending").strip()
        if status not in ("pending", "in_progress", "done"):
            status = "pending"
        steps.append({"id": len(steps) + 1, "text": step_text, "status": status})
    return steps or None


def extract_plan(content: str) -> list[dict] | None:
    """从 LLM 输出内容中提取 <plan>...</plan> 计划清单

    逐行格式(状态标记可选,缺省 pending):
        <plan>
        1. [done] 克隆仓库并查看结构
        2. [in_progress] 审计依赖漏洞
        3. [pending] 审计注入类漏洞
        </plan>

    JSON 格式(system prompt 示范,优先按 JSON 解析):
        <plan>
        [{"id": 1, "text": "克隆仓库", "status": "done"}]
        </plan>

    返回 [{"id": 1, "text": "...", "status": "pending|in_progress|done"}]
    无 plan 块或解析为空时返回 None。
    """
    m = _PLAN_BLOCK_RE.search(content)
    if not m:
        return None
    block = m.group(1)
    # 优先 JSON 解析(模型按 system prompt 示范输出 JSON 数组)
    json_steps = parse_plan_json(block)
    if json_steps:
        return json_steps
    steps: list[dict] = []
    for i, line in enumerate(block.split("\n"), 1):
        line = line.strip()
        if not line:
            continue
        if not _PLAN_LINE_HAS_TEXT_RE.search(line):
            continue
        lm = _PLAN_LINE_RE.match(line)
        if not lm:
            continue
        status = lm.group(1) or "pending"
        text = lm.group(2).strip()
        if status not in ("pending", "in_progress", "done"):
            status = "pending"
        steps.append({"id": i, "text": text, "status": status})
    return steps if steps else None
