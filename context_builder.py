"""上下文收集与提示词组装。

对应原插件 context_service / prompt_stage：
- 把 MaiBot 消息库返回的最近聊天记录渲染为「N. 时间 发送者: 内容」行；
- 渲染提示词模板占位符（{{current_time}} / {{unanswered_count}} /
  {{platform_history_lines}} / {{platform_history_last_time}}）；
- 组装 llm.generate 所需的 chat 消息列表。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import scheduler as sched

# 占位符渲染：先渲染具体值，再清除链路不提供的占位符
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

_PROVIDED_PLACEHOLDERS = {
    "current_time",
    "unanswered_count",
    "platform_history_lines",
    "platform_history_last_time",
}


def render_placeholders(template: str, values: Dict[str, str]) -> str:
    """渲染模板占位符；未提供的占位符整体剔除。"""

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        if name in _PROVIDED_PLACEHOLDERS:
            # 链路提供了该占位符但没有值（如空流水）→ 渲染为空
            return values.get(name, "")
        # 未知占位符：保留原文
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, str(template or ""))


def _is_bot_message(message: Dict[str, Any]) -> bool:
    """判断一条序列化消息是否为 Bot 自己的发言（user_id == account_id）。"""

    account_id = str(message.get("account_id") or "")
    info = message.get("message_info") or {}
    user_info = info.get("user_info") or {}
    user_id = str(user_info.get("user_id") or "")
    return bool(account_id) and user_id == account_id


def _coerce_timestamp(message: Dict[str, Any]) -> float:
    raw = message.get("timestamp")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _extract_text(message: Dict[str, Any]) -> str:
    """提取消息可读正文；复合消息降级为占位描述。"""

    text = str(message.get("processed_plain_text") or "").strip()
    if text:
        return text
    raw = message.get("raw_message")
    parts: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type == "text":
                parts.append(str(item.get("content") or item.get("text") or ""))
            elif item_type == "image":
                parts.append("[图片]")
            elif item_type in ("voice", "audio", "record"):
                parts.append("[语音]")
            elif item_type == "video":
                parts.append("[视频]")
            elif item_type == "file":
                parts.append("[文件]")
    return " ".join(part for part in parts if part).strip()


def format_history_lines(
    messages: List[Dict[str, Any]],
    *,
    include_bot_messages: bool,
    max_line_chars: int,
    max_total_chars: int,
    now: Optional[float] = None,
) -> Tuple[List[str], Optional[float]]:
    """把消息列表渲染为历史行。

    Returns:
        Tuple[List[str], Optional[float]]: (行列表, 最后一条时间戳；无有效消息时为 None)。
    """

    reference_now = now if now is not None else time.time()
    max_line = max(1, int(max_line_chars))
    max_total = max(1, int(max_total_chars))

    lines: List[str] = []
    last_timestamp: Optional[float] = None
    total_chars = 0
    dropped_count = 0

    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            continue
        is_bot = _is_bot_message(message)
        if is_bot and not include_bot_messages:
            continue
        if bool(message.get("is_command")):
            continue
        text = _extract_text(message)
        if not text:
            continue

        info = message.get("message_info") or {}
        user_info = info.get("user_info") or {}
        sender_name = str(user_info.get("user_nickname") or "").strip() or (
            "Bot" if is_bot else "未知"
        )
        timestamp = _coerce_timestamp(message)
        time_text = sched.format_history_time(timestamp, now=reference_now) if timestamp else "--:--"

        body = text[:max_line]
        line = f"{index}. {time_text} {sender_name}: {body}"

        # 整份正文保险丝：超出后从最早一条开始丢弃
        if total_chars + len(line) > max_total and lines:
            dropped_count += len(lines)
            total_chars = 0
            lines.clear()
        if len(line) > max_total:
            line = line[:max_total]
        lines.append(line)
        total_chars += len(line)
        last_timestamp = timestamp if timestamp else last_timestamp

    if dropped_count > 0:
        lines.insert(0, f"[较早历史已截断 {dropped_count} 条]")
    return lines, last_timestamp


def build_dynamic_context(
    *,
    chat_type: str,
    unanswered_count: int,
    now_text: str,
) -> str:
    """构造 dynamic_context 临时内容块（模板未含对应占位符时使用）。"""

    scene = "私聊" if chat_type == sched.CHAT_TYPE_PRIVATE else "群聊"
    return (
        "<dynamic_context>\n"
        f"- 当前时间：{now_text}\n"
        f"- 本次主动消息的未回复累计次数：{unanswered_count}\n"
        f"- 会话场景：{scene}\n"
        "</dynamic_context>"
    )


def build_platform_history_block(
    template: str,
    *,
    lines: List[str],
    last_timestamp: Optional[float],
    now_text: str,
    unanswered_count: int,
) -> str:
    """用流水行渲染平台流水提示词模板。"""

    last_time_text = (
        sched.format_relative_age(max(0.0, _now() - last_timestamp)) if last_timestamp else "（无记录）"
    )
    return render_placeholders(
        template,
        {
            "current_time": now_text,
            "unanswered_count": str(max(0, int(unanswered_count))),
            "platform_history_lines": "\n".join(lines),
            "platform_history_last_time": last_time_text,
        },
    )


def build_llm_messages(
    persona_text: str,
    user_content: str,
) -> List[Dict[str, str]]:
    """组装 llm.generate 的 chat 消息列表（system 人格 + 单轮 user 请求）。"""

    messages: List[Dict[str, str]] = []
    if persona_text and persona_text.strip():
        messages.append({"role": "system", "content": persona_text.strip()})
    messages.append({"role": "user", "content": user_content})
    return messages


def _now() -> float:
    return time.time()
