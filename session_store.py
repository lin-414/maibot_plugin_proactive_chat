"""会话状态持久化。

对应原插件的 SessionStore（session_data.json）：按会话键保存
最后消息时间、未回复计数、下次触发时间与缓存的聊天流 ID。
写入统一采用「临时文件 + replace」原子落盘。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

SESSION_DATA_FILE = "session_data.json"

# 会话载荷中允许出现的字段（其余字段写入时会被忽略）
KNOWN_FIELDS = {
    "enabled",  # 会话级开关覆写：True / False / 缺省
    "stream_id",  # 已知聊天流 ID 缓存
    "chat_name",  # 展示用名称
    "self_id",  # Bot 自身账号 ID
    "last_message_time",  # 用户最后消息时间戳（仅记录插件启动之后的值）
    "unanswered_count",  # 连续主动对话未获回复次数
    "next_trigger_time",  # 下次触发时间戳（持久化定时任务）
    "last_scheduled_at",  # 上次排期时刻
    "last_schedule_min_interval_seconds",
    "last_schedule_max_interval_seconds",
    "last_schedule_random_interval_seconds",
    "last_sender_id",  # 群聊最近真实发送者
    "last_sender_name",
}


class SessionStore:
    """session_data.json 的同步读写封装（插件子进程内单线程事件循环使用）。"""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / SESSION_DATA_FILE
        self._data: Dict[str, Dict[str, Any]] = {}
        self.load()

    # ── 基础读写 ────────────────────────────────────────────────

    def load(self) -> None:
        """从磁盘加载；文件缺失或损坏时回退为空表。"""

        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            self._data = parsed if isinstance(parsed, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except Exception:
            self._data = {}

    def save(self) -> bool:
        """原子落盘；返回是否成功。"""

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._data, indent=4, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)
            return True
        except Exception:
            return False

    @property
    def data(self) -> Dict[str, Dict[str, Any]]:
        return self._data

    # ── 会话级操作 ──────────────────────────────────────────────

    def get(self, session_key: str) -> Dict[str, Any]:
        """取会话载荷；不存在时返回空字典（不自动创建）。"""

        return self._data.get(session_key, {})

    def ensure(self, session_key: str) -> Dict[str, Any]:
        """取会话载荷；不存在时创建并返回。"""

        payload = self._data.setdefault(session_key, {})
        if not isinstance(payload, dict):
            payload = {}
            self._data[session_key] = payload
        return payload

    def has(self, session_key: str) -> bool:
        return session_key in self._data

    def keys(self) -> list[str]:
        return list(self._data.keys())

    def remove(self, session_key: str) -> None:
        self._data.pop(session_key, None)

    def set_field(self, session_key: str, field: str, value: Any) -> None:
        """写入单个白名单字段。"""

        if field not in KNOWN_FIELDS:
            return
        self.ensure(session_key)[field] = value

    def get_field(self, session_key: str, field: str, default: Any = None) -> Any:
        return self.get(session_key).get(field, default)

    # ── 常用语义操作 ────────────────────────────────────────────

    def record_user_message(
        self,
        session_key: str,
        now: float,
        *,
        plugin_start_time: float,
        sender_id: str = "",
        sender_name: str = "",
        is_group: bool,
        stream_id: str = "",
        chat_name: str = "",
        self_id: str = "",
    ) -> None:
        """记录用户消息：更新时间戳、清零未回复计数、缓存发送者与聊天流。

        对应原插件 on_friend_message / on_group_message 的公共落库部分：
        仅记录插件启动之后的消息时间。
        """

        payload = self.ensure(session_key)
        if now >= plugin_start_time:
            payload["last_message_time"] = now
        payload["unanswered_count"] = 0
        if sender_id:
            payload["last_sender_id"] = sender_id
        if sender_name:
            payload["last_sender_name"] = sender_name
        if stream_id:
            payload["stream_id"] = stream_id
        if chat_name:
            payload["chat_name"] = chat_name
        if self_id:
            payload["self_id"] = self_id
        if is_group:
            # 群聊：清空调度状态，等待沉默计时器重新排期
            payload.pop("next_trigger_time", None)
            payload.pop("last_scheduled_at", None)

    def clear_schedule_state(self, session_key: str) -> None:
        """清除持久化排期字段（任务已取消或已消费）。"""

        payload = self.get(session_key)
        if not payload:
            return
        payload.pop("next_trigger_time", None)
        payload.pop("last_scheduled_at", None)

    def apply_schedule(
        self,
        session_key: str,
        next_trigger_time: float,
        *,
        min_interval: int,
        max_interval: int,
        random_interval: int,
    ) -> None:
        """写入持久化排期三元组。"""

        payload = self.ensure(session_key)
        payload["next_trigger_time"] = next_trigger_time
        payload["last_scheduled_at"] = time.time()
        payload["last_schedule_min_interval_seconds"] = int(min_interval)
        payload["last_schedule_max_interval_seconds"] = int(max_interval)
        payload["last_schedule_random_interval_seconds"] = int(random_interval)

    def get_next_trigger(self, session_key: str) -> Optional[float]:
        value = self.get_field(session_key, "next_trigger_time")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def get_unanswered(self, session_key: str) -> int:
        try:
            return int(self.get_field(session_key, "unanswered_count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def increment_unanswered(self, session_key: str) -> int:
        payload = self.ensure(session_key)
        payload["unanswered_count"] = self.get_unanswered(session_key) + 1
        return int(payload["unanswered_count"])

    def get_enabled_override(self, session_key: str) -> Optional[bool]:
        value = self.get_field(session_key, "enabled")
        if value is None:
            return None
        return bool(value)

    def set_enabled_override(self, session_key: str, enabled: Optional[bool]) -> None:
        """写会话级开关覆写；None 表示移除覆写。"""

        if enabled is None:
            payload = self.get(session_key)
            if payload:
                payload.pop("enabled", None)
            return
        self.set_field(session_key, "enabled", bool(enabled))
