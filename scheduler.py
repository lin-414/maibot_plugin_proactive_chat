"""调度与时间相关的纯函数。

对应原插件 scheduler_service.compute_next_trigger、gating_feature 的
免打扰判定，以及 sender_service 的分段延迟计算。全部为无副作用函数，
便于单独测试。
"""

from __future__ import annotations

import math
import random
import re
import time
from datetime import datetime
from typing import List, Optional, Tuple

# ── 会话键 ──────────────────────────────────────────────────────

CHAT_TYPE_PRIVATE = "private"
CHAT_TYPE_GROUP = "group"


def normalize_session_key(platform: str, chat_type: str, target_id: str) -> str:
    """规范化会话键：platform:private:user_id / platform:group:group_id。"""

    return f"{platform}:{chat_type}:{target_id}"


def parse_session_key(session_key: str) -> Optional[Tuple[str, str, str]]:
    """解析会话键；不合法返回 None。"""

    parts = str(session_key or "").split(":")
    if len(parts) != 3:
        return None
    platform, chat_type, target_id = (part.strip() for part in parts)
    if not platform or not target_id or chat_type not in (CHAT_TYPE_PRIVATE, CHAT_TYPE_GROUP):
        return None
    return platform, chat_type, target_id


def match_session_key(candidate: str, session_key: str) -> bool:
    """判断用户输入是否命中会话键（完整键 / 裸目标 ID / 任意前缀）。"""

    candidate = str(candidate or "").strip()
    if not candidate:
        return False
    if candidate == session_key:
        return True
    parsed = parse_session_key(session_key)
    if parsed and candidate == parsed[2]:
        return True
    # 允许 platform:group: 这类前缀匹配
    return session_key.startswith(candidate + ":") if ":" in candidate else False


# ── 触发间隔 ────────────────────────────────────────────────────


def compute_next_trigger(
    min_interval_minutes: int,
    max_interval_minutes: int,
    *,
    now: Optional[float] = None,
) -> Tuple[float, int, int, int]:
    """计算下次触发时间。

    Returns:
        Tuple[float, int, int, int]: (下次触发时间戳, 最小间隔秒, 最大间隔秒, 随机间隔秒)。
    """

    current = time.time() if now is None else now
    min_interval = max(0, int(min_interval_minutes)) * 60
    max_interval = max(min_interval, int(max_interval_minutes) * 60)
    random_interval = random.randint(min_interval, max_interval)
    return current + random_interval, min_interval, max_interval, random_interval


# ── 免打扰时段 ──────────────────────────────────────────────────

_QUIET_SEGMENT_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")


def parse_quiet_hours(value: str) -> List[Tuple[int, int]]:
    """解析免打扰时段字符串，如 "1-7" 或 "1-7,23-24"。

    start ∈ [0,23]、end ∈ [0,24]；start == end 的段忽略。
    解析失败的段跳过；整体为空表示全天可聊。
    """

    ranges: List[Tuple[int, int]] = []
    for segment in re.split(r"[,，、]", str(value or "")):
        if not segment.strip():
            continue
        match = _QUIET_SEGMENT_RE.match(segment)
        if match is None:
            continue
        start, end = int(match.group(1)), int(match.group(2))
        if not (0 <= start <= 23 and 0 <= end <= 24):
            continue
        if start == end:
            continue
        ranges.append((start, end))
    return ranges


def in_quiet_hours(timestamp: float, ranges: List[Tuple[int, int]]) -> bool:
    """判定时间戳是否落在免打扰时段内（使用本地时间）。

    同日段：start <= hour < end；跨日段（start > end）：hour >= start or hour < end。
    """

    if not ranges:
        return False
    hour = datetime.fromtimestamp(timestamp).hour
    for start, end in ranges:
        if start < end:
            if start <= hour < end:
                return True
        elif start > end:
            if hour >= start or hour < end:
                return True
    return False


# ── 时间格式化 ──────────────────────────────────────────────────

_WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def format_current_time(timestamp: Optional[float] = None) -> str:
    """格式化为「2026年05月12日 周二 09:53」。"""

    dt = datetime.fromtimestamp(time.time() if timestamp is None else timestamp)
    return f"{dt.year}年{dt.month:02d}月{dt.day:02d}日 {_WEEKDAY_ZH[dt.weekday()]} {dt.strftime('%H:%M')}"


def format_relative_age(age_seconds: float) -> str:
    """把时长格式化为「刚刚 / N 分钟前 / N 小时前 / N 天前」。"""

    seconds = max(0, int(age_seconds))
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def format_history_time(timestamp: float, *, now: Optional[float] = None) -> str:
    """格式化历史行时间：同日 HH:MM，跨天 MM-DD HH:MM。"""

    current = time.time() if now is None else now
    dt = datetime.fromtimestamp(timestamp)
    today = datetime.fromtimestamp(current)
    if dt.date() == today.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


# ── 分段发送 ────────────────────────────────────────────────────

_DEFAULT_SEGMENT_REGEX = r".*?[。？！~…\n]+|.+$"


def split_text(
    text: str,
    *,
    enable: bool,
    words_count_threshold: int,
    split_mode: str,
    regex: str,
    split_words: List[str],
    enable_content_cleanup: bool,
    content_cleanup_rule: str,
) -> List[str]:
    """按配置切分文本；enable=False 或切分失败时返回原文本单段。"""

    if not enable or not text:
        return [text] if text else []

    segments: List[str] = []
    threshold = max(1, int(words_count_threshold))
    if str(split_mode).lower() == "words":
        # 长标点优先，避免 “。。” 被逐字切开
        words = sorted({str(word) for word in split_words if word}, key=len, reverse=True)
        if not words:
            return [text]
        pattern = "|".join(re.escape(word) for word in words)
        try:
            segments = re.findall(rf".*?(?:{pattern})+|.+$", text, re.DOTALL)
        except re.error:
            segments = re.findall(_DEFAULT_SEGMENT_REGEX, text, re.DOTALL)
    else:
        try:
            segments = re.findall(regex or _DEFAULT_SEGMENT_REGEX, text, re.DOTALL | re.MULTILINE)
        except re.error:
            segments = re.findall(_DEFAULT_SEGMENT_REGEX, text, re.DOTALL)

    cleaned: List[str] = []
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        if enable_content_cleanup and content_cleanup_rule:
            try:
                segment = re.sub(content_cleanup_rule, "", segment)
            except re.error:
                pass
        if not segment:
            continue
        # 超长段按阈值硬切，防止个别长句产生超大分段
        while len(segment) > threshold:
            cleaned.append(segment[:threshold])
            segment = segment[threshold:].strip()
            if not segment:
                break
        if segment:
            cleaned.append(segment)
    return cleaned or ([text.strip()] if text.strip() else [])


def calc_segment_delay(text: str, *, interval_method: str, interval: str, log_base: float) -> float:
    """计算段间延迟秒数（仅段与段之间使用，最后一段后不等待）。

    log 法：word_count = 纯 ASCII 时按空格分词，否则按字母数字计数；
    delay = uniform(log(word_count+1, base), log(...)+0.5)。
    random 法：解析 interval "1.5, 3.5"；失败回退 [1.5, 3.5]。
    """

    if str(interval_method).lower() == "random":
        low, high = 1.5, 3.5
        match = re.match(r"^\s*([\d.]+)\s*[,，]\s*([\d.]+)\s*$", str(interval or ""))
        if match:
            low = float(match.group(1))
            high = float(match.group(2))
            if high < low:
                low, high = high, low
        return random.uniform(low, high)

    base = float(log_base) if log_base and float(log_base) > 1 else 1.8
    if text.isascii():
        word_count = len(text.split())
    else:
        word_count = sum(1 for ch in text if ch.isalnum())
    value = _log(max(0, word_count) + 1, base)
    return random.uniform(value, value + 0.5)


def _log(value: float, base: float) -> float:
    """以 base 为底的对数（value >= 1）。"""

    return math.log(max(value, 1.0), base)


# ── LLM 输出清洗 ────────────────────────────────────────────────

_SPECIAL_TOKEN_RE = re.compile(r"<\|[^>]{1,40}>\||\[[Pp]LATFORM\]|\[[Aa]SST:[^\]]{0,60}\]")


def clean_llm_text(text: str) -> str:
    """轻量清洗 LLM 输出：剥特殊标记、剔 [object Object]、压缩空行。"""

    cleaned = str(text or "").strip()
    cleaned = _SPECIAL_TOKEN_RE.sub("", cleaned)
    cleaned = cleaned.replace("[object Object]", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
