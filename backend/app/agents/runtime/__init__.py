"""agents/runtime:智能体共享的运行时原语层

react_agent(agent1 内置实现)/ agent2(质检)/ verifier_agent(动态验证)
此前各自手抄的底层实现收敛于此:

- llm_stream:流式 LLM 调用 + 跨 chunk 工具调用累积 + 文本 tool_call 兜底解析
- conversation:对话落库 + SSE 推送(统一 tool_call_id 配对)
- tool_intent:工具意图生成(人类可读卡片标题,三处映射合一)
- constants:跨模块共享常量(消除"注释里约定两边保持一致")

职责边界:本层只放"无业务语义"的原语;各智能体的循环策略(迭代上限、
工具配额、授权拦截、降级语义)、prompt、工具门控不在这一层,
保持在各 agent 模块内独立演进。
"""
