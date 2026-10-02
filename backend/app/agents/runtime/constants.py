"""跨智能体共享常量(单一事实源)

此前 react_agent 与 agent2 各自定义 MAX_HISTORY_MSG_CHARS /
MAX_HISTORY_TOTAL_CHARS,靠注释约定"与对方保持一致"——两处手工同步
容易漂移。提取到 runtime 后由导入共享,改一处即全局生效。
"""

# 跨轮记忆传递:单条历史消息(用户原话 / 执行总结 / 评审反馈 / 审查记录)
# 的最大字符数,超出截断。agent1 与 agent2 的历史注入共用同一上限
MAX_HISTORY_MSG_CHARS = 3000

# agent2 跨轮自记忆(agent2 自己之前各轮审查记录)的总字符上限。
# 注:agent1 侧历史压缩按 token 预算(MAX_HISTORY_TOKEN_BUDGET,
# 见 react_agent 模块内),策略不同,不在此列
MAX_HISTORY_TOTAL_CHARS = 12000
