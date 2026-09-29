# -*- coding: utf-8 -*-
"""AI 能力分层包（对话智能问答 + 公共 LLM 调用底座）。

分层（参照 AI 开发标准模板）：
  llm_config.py   模型建立：env 函数内懒读 / 去引号 / 成本闸门 / 能力开关
  llm_client.py   模型调用边界：call_chat + 超时/错误归一/重试/并发 + 复用辅助函数
  chat_prompt.py  对话角色 + 提示词 + 安全边界 + 版本化
  chat_context.py 对话上下文构建：分层注入 / 长度裁剪 / 截断告知
  chat_normalize.py 对话引用软校验（不阻断）
  chat_engine.py  对话编排：检索 → 构造 → 调用 → 校验 → 持久化 → 审计
"""
