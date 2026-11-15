"""主动对话插件（Proactive Chat）— MaiBot 移植版。

移植自 Pancakes-Labs/astrbot_plugin_proactive_chat（AGPL-3.0）。

核心能力：
- 每会话持久化定时任务：用户消息重置计时，按 [min, max] 随机间隔主动发起对话；
- 群聊沉默倒计时：冷场 X 分钟后主动破冰；
- 启动后静默自动触发；
- 管理员手动触发；
- 未回复计数上限（达到后暂停，用户回复即恢复）；
- 免打扰时段、分段发送（模拟打字节奏）、上下文感知（最近聊天记录注入）；
- 重启恢复：从 session_data.json 恢复未执行任务。

触发来源统一进入 check_and_chat 主流程：
GATE 准入 → 上下文收集 → 提示词渲染 → LLM 生成 → 输出清洗 →
发送前新消息检测（REVIEW）→ 分段发送（DELIVER）→ 计数与重排（RECORD/RESCHEDULE）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from . import context_builder as ctxb
from . import scheduler as sched
from .config_schema import ProactiveChatConfig, ScheduleConfig
from .sender import send_proactive_text
from .session_store import SessionStore

PLUGIN_VERSION = "1.0.0"

# 持久化任务有效性判定余量（秒）：now < next_trigger_time + GRACE
PERSISTED_TASK_GRACE_SECONDS = 60

TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
TRIGGER_GROUP_IDLE = "group_idle"
TRIGGER_AUTO_TRIGGER = "auto_trigger"

_SCOPE_CURRENT_WORDS = {"当前会话", "当前", "本会话", "这个会话", "current", "here"}
_SCOPE_GLOBAL_WORDS = {"全局", "全体", "所有", "all", "global"}
_SWITCH_ON_WORDS = {"on", "open", "开", "开启", "打开"}
_SWITCH_OFF_WORDS = {"off", "close", "关", "关闭", "停", "停用"}

STATE_FILE = "plugin_state.json"


def create_plugin() -> "ProactiveChatPlugin":
    return ProactiveChatPlugin()


class PluginStateStore:
    """plugin_state.json：保存全局开关覆写等插件级状态。"""

    def __init__(self, data_dir: Any) -> None:
        self._path = data_dir / STATE_FILE
        self._data: Dict[str, Any] = {}
        try:
            raw = self._path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            self._data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            self._data = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=4, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


class ProactiveChatPlugin(MaiBotPlugin):
    """主动对话插件主类。"""

    config_model = ProactiveChatConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[SessionStore] = None
        self._state: Optional[PluginStateStore] = None
        self._plugin_start_time: float = 0.0
        # 每会话持久化定时任务（键 -> asyncio.Task）
        self._scheduled_tasks: Dict[str, asyncio.Task] = {}
        # 群沉默计时任务（键 -> asyncio.Task）与代次表（防旧回调）
        self._silence_tasks: Dict[str, asyncio.Task] = {}
        self._silence_generations: Dict[str, int] = {}
        # 自动触发检查任务（键 -> asyncio.Task）
        self._auto_trigger_tasks: Dict[str, asyncio.Task] = {}
        # 生成中的会话与「生成期间来过新消息」标记
        self._sending: set[str] = set()
        self._abort_flags: Dict[str, bool] = {}
        # 手动触发防并发占位
        self._manual_trigger_sessions: set[str] = set()

    # ── 生命周期 ────────────────────────────────────────────────

    async def on_load(self) -> None:
        self._plugin_start_time = time.time()
        self._store = SessionStore(self.ctx.paths.data_dir)
        self._state = PluginStateStore(self.ctx.paths.data_dir)
        self._reinitialize()
        self.ctx.logger.info(
            f"[主动对话] 插件已加载 v{PLUGIN_VERSION}，恢复任务 {len(self._scheduled_tasks)} 个，"
            f"群沉默计时 {len(self._silence_tasks)} 个，自动触发 {len(self._auto_trigger_tasks)} 个"
        )

    async def on_unload(self) -> None:
        self._cancel_all_tasks()
        if self._store is not None:
            self._store.save()
        self.ctx.logger.info("[主动对话] 插件已卸载，计时任务已清理")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        if scope != "self":
            return
        self.ctx.logger.info("[主动对话] 检测到配置更新，重新初始化调度")
        self._cancel_all_tasks()
        self._reinitialize()

    # ── 初始化与任务编排 ────────────────────────────────────────

    def _reinitialize(self) -> None:
        """（重）建立持久化任务、群沉默计时与自动触发。"""

        self._restore_persisted_jobs()
        self._setup_auto_triggers()

    def _cancel_all_tasks(self) -> None:
        for task in list(self._scheduled_tasks.values()) + list(self._silence_tasks.values()) + list(
            self._auto_trigger_tasks.values()
        ):
            task.cancel()
        self._scheduled_tasks.clear()
        self._silence_tasks.clear()
        self._auto_trigger_tasks.clear()
        self._sending.clear()
        self._abort_flags.clear()

    def _settings_for(self, chat_type: str):
        """按聊天类型取全局配置段。"""

        if chat_type == sched.CHAT_TYPE_GROUP:
            return self.config.group_settings
        return self.config.friend_settings

    def _global_enabled(self, chat_type: str) -> bool:
        """插件启用 + 组级启用 + 全局覆写三者的与。"""

        if not self.config.plugin.enabled:
            return False
        if chat_type == sched.CHAT_TYPE_GROUP:
            settings = self.config.group_settings
            override_key = "group_settings_enabled"
        else:
            settings = self.config.friend_settings
            override_key = "friend_settings_enabled"
        if not settings.enable:
            return False
        override = self._state.get(override_key) if self._state else None
        if override is False:
            return False
        return True

    def _membership_enabled(self, session_key: str) -> bool:
        """会话是否命中配置列表。"""

        parsed = sched.parse_session_key(session_key)
        if parsed is None:
            return False
        platform, chat_type, _target_id = parsed
        if not self._global_enabled(chat_type):
            return False
        settings = self._settings_for(chat_type)
        return any(sched.match_session_key(entry, session_key) for entry in settings.session_list)

    def effective_enabled(self, session_key: str) -> bool:
        """会话最终启用状态：组级启用 + （会话覆写优先于命中列表）。"""

        parsed = sched.parse_session_key(session_key)
        if parsed is None:
            return False
        chat_type = parsed[1]
        if not self._global_enabled(chat_type):
            return False
        if self._store is None:
            return False
        override = self._store.get_enabled_override(session_key)
        if override is True:
            return True
        if override is False:
            return False
        return self._membership_enabled(session_key)

    def _resolve_configured_session(self, entry: str, chat_type: str) -> Optional[str]:
        """把会话列表条目解析为规范化会话键。"""

        entry = str(entry or "").strip()
        if not entry:
            return None
        parsed = sched.parse_session_key(entry)
        if parsed is not None:
            return sched.normalize_session_key(*parsed)
        platform = str(self.config.plugin.default_platform or "qq").strip() or "qq"
        return sched.normalize_session_key(platform, chat_type, entry)

    def _spawn_scheduled_task(self, session_key: str, run_at: float, trigger: str) -> None:
        old = self._scheduled_tasks.pop(session_key, None)
        if old is not None and not old.done():
            old.cancel()
        delay = max(0.0, run_at - time.time())
        task = asyncio.create_task(self._scheduled_task_worker(session_key, delay, trigger))
        self._scheduled_tasks[session_key] = task

    async def _scheduled_task_worker(self, session_key: str, delay: float, trigger: str) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._scheduled_tasks.pop(session_key, None)
        try:
            await self._check_and_chat(session_key, trigger)
        except Exception:
            self.ctx.logger.error(f"[主动对话] 定时任务执行异常 key={session_key}", exc_info=True)

    def _cancel_session_schedule(self, session_key: str) -> None:
        """取消某会话的持久化任务与排期字段。"""

        task = self._scheduled_tasks.pop(session_key, None)
        if task is not None and not task.done():
            task.cancel()
        if self._store is not None:
            self._store.clear_schedule_state(session_key)

    def _cancel_silence_timer(self, session_key: str) -> None:
        task = self._silence_tasks.pop(session_key, None)
        if task is not None and not task.done():
            task.cancel()
        # 注意：代次号不回绕。每次重置都会 +1，旧回调凭代次不匹配自行失效；
        # 若在此处弹出代次，新旧回调的代次可能重新对齐导致旧回调误触发。

    def _cancel_auto_trigger(self, session_key: str) -> None:
        task = self._auto_trigger_tasks.pop(session_key, None)
        if task is not None and not task.done():
            task.cancel()

    def _schedule_next(
        self,
        session_key: str,
        settings: Any,
        *,
        save: bool = True,
        trigger: str = TRIGGER_SCHEDULE,
    ) -> Optional[float]:
        """按 [min, max] 随机间隔排下一次持久化任务。"""

        if self._store is None:
            return None
        schedule: ScheduleConfig = settings.schedule
        next_ts, min_sec, max_sec, rand_sec = sched.compute_next_trigger(
            schedule.min_interval_minutes, schedule.max_interval_minutes
        )
        self._store.apply_schedule(
            session_key,
            next_ts,
            min_interval=min_sec,
            max_interval=max_sec,
            random_interval=rand_sec,
        )
        self._spawn_scheduled_task(session_key, next_ts, trigger)
        if save:
            self._store.save()
        self.ctx.logger.info(
            f"[主动对话] 已排期 {session_key}：{sched.format_current_time(next_ts)}"
            f"（随机间隔 {rand_sec // 60} 分钟）"
        )
        return next_ts

    def _reset_group_silence_timer(self, session_key: str) -> None:
        """重置群沉默倒计时（群用户消息 / Bot 群发言后调用）。"""

        if self._store is None:
            return
        settings = self.config.group_settings
        idle_minutes = int(settings.group_idle_trigger_minutes)
        self._cancel_silence_timer(session_key)
        if not self.effective_enabled(session_key) or idle_minutes <= 0:
            return
        generation = self._silence_generations.get(session_key, 0) + 1
        self._silence_generations[session_key] = generation
        delay = idle_minutes * 60
        task = asyncio.create_task(self._group_silence_worker(session_key, delay, generation))
        self._silence_tasks[session_key] = task

    async def _group_silence_worker(self, session_key: str, delay: float, generation: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # 代次校验：期间有新消息/新发言会重建计时器，旧回调直接放弃
        if self._silence_generations.get(session_key) != generation:
            return
        self._silence_tasks.pop(session_key, None)
        self._silence_generations.pop(session_key, None)
        settings = self.config.group_settings
        if not self.effective_enabled(session_key):
            return
        self.ctx.logger.info(f"[主动对话] 群沉默计时结束，排期主动对话 {session_key}")
        self._schedule_next(session_key, settings, trigger=TRIGGER_GROUP_IDLE)

    def _restore_persisted_jobs(self) -> None:
        """启动/重载时从 session_data.json 恢复未执行的持久化任务。"""

        if self._store is None:
            return
        now = time.time()
        for session_key in list(self._store.keys()):
            parsed = sched.parse_session_key(session_key)
            if parsed is None:
                continue
            _platform, chat_type, _target_id = parsed
            settings = self._settings_for(chat_type)
            if not self.effective_enabled(session_key):
                self._store.clear_schedule_state(session_key)
                continue
            next_ts = self._store.get_next_trigger(session_key)
            if next_ts is None:
                continue
            if now >= next_ts + PERSISTED_TASK_GRACE_SECONDS:
                self._store.clear_schedule_state(session_key)
                continue
            self._spawn_scheduled_task(session_key, next_ts, TRIGGER_SCHEDULE)
            self.ctx.logger.info(
                f"[主动对话] 恢复持久化任务 {session_key}：{sched.format_current_time(next_ts)}"
            )
        self._store.save()

    def _setup_auto_triggers(self) -> None:
        """为启用自动触发的会话建立启动静默检查任务。"""

        if self._store is None:
            return
        for chat_type, settings in (
            (sched.CHAT_TYPE_PRIVATE, self.config.friend_settings),
            (sched.CHAT_TYPE_GROUP, self.config.group_settings),
        ):
            auto = settings.auto_trigger
            minutes = int(auto.minutes)
            if not self._global_enabled(chat_type) or not auto.enabled or minutes <= 0:
                continue
            for entry in settings.session_list:
                session_key = self._resolve_configured_session(entry, chat_type)
                if session_key is None:
                    continue
                self._setup_auto_trigger_for_session(session_key, settings, minutes)

    def _setup_auto_trigger_for_session(self, session_key: str, settings: Any, minutes: int) -> None:
        if self._store is None:
            return
        if self._store.get_next_trigger(session_key) is not None:
            return  # 已有持久化任务
        schedule: ScheduleConfig = settings.schedule
        max_unanswered = int(schedule.max_unanswered_times)
        if max_unanswered > 0 and self._store.get_unanswered(session_key) >= max_unanswered:
            return  # 已达未回复上限
        self._cancel_auto_trigger(session_key)
        delay = max(0.0, minutes * 60 - (time.time() - self._plugin_start_time))
        task = asyncio.create_task(self._auto_trigger_worker(session_key, settings, minutes, delay))
        self._auto_trigger_tasks[session_key] = task

    async def _auto_trigger_worker(self, session_key: str, settings: Any, minutes: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        self._auto_trigger_tasks.pop(session_key, None)
        if self._store is None:
            return
        now = time.time()
        # 条件校验（对应原插件 handle_auto_trigger_callback）
        last_message = self._store.get_field(session_key, "last_message_time")
        if last_message is not None and float(last_message) >= self._plugin_start_time:
            return  # 启动后已有用户消息
        if now - self._plugin_start_time < minutes * 60:
            return
        if self._store.get_next_trigger(session_key) is not None:
            return  # 已有持久化任务
        schedule: ScheduleConfig = settings.schedule
        max_unanswered = int(schedule.max_unanswered_times)
        if max_unanswered > 0 and self._store.get_unanswered(session_key) >= max_unanswered:
            return
        self.ctx.logger.info(f"[主动对话] 启动后一直无人说话，补排主动对话 {session_key}")
        self._schedule_next(session_key, settings, trigger=TRIGGER_AUTO_TRIGGER)

    # ── 消息观察 Hook ───────────────────────────────────────────

    @HookHandler("chat.receive.after_process", mode=HookMode.OBSERVE, name="proactive_chat_on_user_message")
    async def on_user_message(self, message: Any = None, **kwargs: Any) -> None:
        """入站用户消息：重置计时、恢复计数、（重新）排期。"""

        del kwargs
        if not isinstance(message, dict) or self._store is None or not self.config.plugin.enabled:
            return
        try:
            self._handle_user_message(message)
        except Exception:
            self.ctx.logger.error("[主动对话] 处理用户消息异常", exc_info=True)

    def _handle_user_message(self, message: Dict[str, Any]) -> None:
        info = message.get("message_info") or {}
        user_info = info.get("user_info") or {}
        group_info = info.get("group_info")
        account_id = str(message.get("account_id") or "")
        user_id = str(user_info.get("user_id") or "")
        if not user_id or (account_id and user_id == account_id):
            return  # Bot 自身回显
        if bool(message.get("is_notify")):
            return

        is_group = bool(group_info and str(group_info.get("group_id") or "").strip())
        if is_group:
            target_id = str(group_info.get("group_id"))
            chat_type = sched.CHAT_TYPE_GROUP
            chat_name = str(group_info.get("group_name") or "")
        else:
            target_id = user_id
            chat_type = sched.CHAT_TYPE_PRIVATE
            chat_name = str(user_info.get("user_nickname") or "")
        platform = str(message.get("platform") or "").strip()
        if not platform:
            return
        session_key = sched.normalize_session_key(platform, chat_type, target_id)
        stream_id = str(message.get("session_id") or "")
        now = time.time()

        self._store.record_user_message(
            session_key,
            now,
            plugin_start_time=self._plugin_start_time,
            sender_id=user_id,
            sender_name=str(user_info.get("user_nickname") or ""),
            is_group=is_group,
            stream_id=stream_id,
            chat_name=chat_name,
            self_id=account_id,
        )

        if not self.effective_enabled(session_key):
            self._store.save()
            return

        # 生成期间来新消息 → REVIEW 阶段丢弃本次发送
        if session_key in self._sending:
            self._abort_flags[session_key] = True

        self._cancel_session_schedule(session_key)
        self._cancel_auto_trigger(session_key)
        if is_group:
            self._store.clear_schedule_state(session_key)
            self._reset_group_silence_timer(session_key)
        else:
            # 私聊：用户回复后立即按新随机间隔排下一次
            self._schedule_next(session_key, self.config.friend_settings, trigger=TRIGGER_SCHEDULE)
        self._store.save()

    @HookHandler("send_service.after_send", mode=HookMode.OBSERVE, name="proactive_chat_on_bot_sent")
    async def on_bot_message_sent(self, message: Any = None, sent: bool = False, **kwargs: Any) -> None:
        """Bot 群聊发言后：重置该群沉默计时（包括本插件自己的发言）。"""

        del kwargs
        if not sent or not isinstance(message, dict) or self._store is None or not self.config.plugin.enabled:
            return
        info = message.get("message_info") or {}
        group_info = info.get("group_info")
        if not group_info:
            return
        group_id = str(group_info.get("group_id") or "").strip()
        platform = str(message.get("platform") or "").strip()
        if not group_id or not platform:
            return
        session_key = sched.normalize_session_key(platform, sched.CHAT_TYPE_GROUP, group_id)
        if not self.effective_enabled(session_key):
            return
        self._reset_group_silence_timer(session_key)

    # ── 主流程：check_and_chat ─────────────────────────────────

    async def _resolve_stream(self, session_key: str) -> Optional[str]:
        """把会话键解析为可发送的聊天流 ID（open_session 会按需创建）。"""

        parsed = sched.parse_session_key(session_key)
        if parsed is None:
            return None
        platform, chat_type, target_id = parsed
        try:
            if chat_type == sched.CHAT_TYPE_GROUP:
                result = await self.ctx.chat.open_session(platform=platform, chat_type="group", group_id=target_id)
            else:
                result = await self.ctx.chat.open_session(platform=platform, chat_type="private", user_id=target_id)
        except Exception as exc:
            self.ctx.logger.warning(f"[主动对话] 解析聊天流失败 {session_key}: {exc}")
            return None
        if not isinstance(result, dict) or not result.get("success"):
            self.ctx.logger.warning(f"[主动对话] 解析聊天流失败 {session_key}: {result}")
            return None
        stream = result.get("stream") or result
        stream_id = str(stream.get("stream_id") or stream.get("session_id") or "")
        if not stream_id:
            return None
        self._store.set_field(session_key, "stream_id", stream_id)
        return stream_id

    async def _fetch_history(
        self,
        session_key: str,
        stream_id: str,
        settings: Any,
    ) -> Tuple[List[str], Optional[float]]:
        """拉取最近聊天记录并渲染为历史行。"""

        context_cfg = settings.context
        count = int(context_cfg.history_count)
        if count <= 0:
            return [], None
        hours = max(0.1, float(context_cfg.history_hours))
        try:
            response = await self.ctx.message.get_recent(chat_id=stream_id, limit=count, hours=hours)
        except Exception as exc:
            self.ctx.logger.warning(f"[主动对话] 拉取历史失败 {session_key}: {exc}")
            return [], None
        if not isinstance(response, dict) or not response.get("success"):
            return [], None
        messages = response.get("messages") or []
        if not isinstance(messages, list):
            return [], None
        return ctxb.format_history_lines(
            messages,
            include_bot_messages=bool(context_cfg.include_bot_messages),
            max_line_chars=int(context_cfg.max_line_chars),
            max_total_chars=int(context_cfg.max_total_chars),
            now=time.time(),
        )

    async def _resolve_persona(self, settings: Any) -> str:
        """解析主动对话人格（current / custom / none）。"""

        persona = settings.persona
        mode = str(persona.mode or "current").lower()
        if mode == "none":
            return ""
        if mode == "custom":
            return str(persona.custom or "").strip()
        try:
            result = await self.ctx.personality.get_current()
        except Exception as exc:
            self.ctx.logger.warning(f"[主动对话] 读取当前人设失败，跳过人格注入: {exc}")
            return ""
        payload = result.get("result") if isinstance(result, dict) else None
        if not isinstance(payload, dict):
            payload = result if isinstance(result, dict) else {}
        return str(payload.get("personality") or "").strip()

    def _in_quiet_hours(self, settings: Any, *, now: Optional[float] = None) -> bool:
        schedule: ScheduleConfig = settings.schedule
        if not schedule.enable_quiet_hours:
            return False
        ranges = sched.parse_quiet_hours(schedule.quiet_hours)
        if not ranges:
            return False
        return sched.in_quiet_hours(now if now is not None else time.time(), ranges)

    async def _check_and_chat(self, session_key: str, trigger: str) -> bool:
        """主动对话主流程。返回是否成功发送。"""

        parsed = sched.parse_session_key(session_key)
        if parsed is None or self._store is None:
            return False
        _platform, chat_type, _target_id = parsed
        settings = self._settings_for(chat_type)

        # GATE：启用与未回复上限
        if not self.effective_enabled(session_key):
            self.ctx.logger.info(f"[主动对话] 会话未启用，跳过触发 {session_key}（来源 {trigger}）")
            return False
        max_unanswered = int(settings.schedule.max_unanswered_times)
        if max_unanswered > 0 and self._store.get_unanswered(session_key) >= max_unanswered:
            self.ctx.logger.info(
                f"[主动对话] 未回复次数已达上限（{self._store.get_unanswered(session_key)}/{max_unanswered}），"
                f"暂停主动对话 {session_key}（等待用户回复恢复）"
            )
            return False
        # GATE：免打扰时段（手动触发已在指令层预检并给出提示）
        if trigger != TRIGGER_MANUAL and self._in_quiet_hours(settings):
            self.ctx.logger.info(f"[主动对话] 当前处于免打扰时段，重排 {session_key}")
            self._schedule_next(session_key, settings, trigger=trigger)
            return False
        if session_key in self._sending or session_key in self._manual_trigger_sessions:
            self.ctx.logger.info(f"[主动对话] 会话正在生成中，跳过并发触发 {session_key}")
            return False

        stream_id = await self._resolve_stream(session_key)
        if not stream_id:
            self.ctx.logger.warning(f"[主动对话] 聊天流不可用，重排 {session_key}")
            self._schedule_next(session_key, settings, trigger=trigger)
            return False

        generation_start = time.time()
        self._sending.add(session_key)
        self._abort_flags.pop(session_key, None)
        try:
            # 上下文与提示词
            lines, last_ts = await self._fetch_history(session_key, stream_id, settings)
            now_text = sched.format_current_time()
            unanswered = self._store.get_unanswered(session_key)
            if lines:
                user_content = ctxb.build_platform_history_block(
                    settings.history_prompt,
                    lines=lines,
                    last_timestamp=last_ts,
                    now_text=now_text,
                    unanswered_count=unanswered,
                )
            else:
                user_content = ctxb.render_placeholders(
                    settings.proactive_prompt,
                    {"current_time": now_text, "unanswered_count": str(unanswered)},
                )
                if "{{platform_history_lines}}" not in settings.proactive_prompt:
                    user_content = user_content + "\n" + ctxb.build_dynamic_context(
                        chat_type=chat_type,
                        unanswered_count=unanswered,
                        now_text=now_text,
                    )

            persona = await self._resolve_persona(settings)
            prompt_messages = ctxb.build_llm_messages(persona, user_content)

            # LLM 生成
            llm_task = str(self.config.plugin.llm_task or "").strip()
            try:
                result = await self.ctx.llm.generate(prompt=prompt_messages, model=llm_task)
            except Exception as exc:
                self.ctx.logger.error(f"[主动对话] LLM 调用异常 {session_key}: {exc}")
                result = {"success": False, "response": ""}
            if not isinstance(result, dict) or not result.get("success"):
                self.ctx.logger.warning(f"[主动对话] LLM 生成失败 {session_key}，重排（来源 {trigger}）")
                self._schedule_next(session_key, settings, trigger=trigger)
                return False

            text = sched.clean_llm_text(str(result.get("response") or ""))
            if not text:
                self.ctx.logger.warning(f"[主动对话] LLM 输出清洗后为空 {session_key}，重排")
                self._schedule_next(session_key, settings, trigger=trigger)
                return False

            # REVIEW：生成期间来过新消息则丢弃
            if self._abort_flags.get(session_key):
                self.ctx.logger.info(f"[主动对话] 生成期间收到新消息，丢弃本次结果并重排 {session_key}")
                self._schedule_next(session_key, settings, trigger=trigger)
                return False
            last_message = self._store.get_field(session_key, "last_message_time")
            if last_message is not None and float(last_message) > generation_start:
                self.ctx.logger.info(f"[主动对话] 生成期间用户有新发言，丢弃本次结果并重排 {session_key}")
                self._schedule_next(session_key, settings, trigger=trigger)
                return False

            # DELIVER：分段发送
            sent = await send_proactive_text(
                self.ctx.send,
                stream_id,
                text,
                settings.segmented_reply,
                self.ctx.logger,
            )
            if not sent:
                self.ctx.logger.warning(f"[主动对话] 发送失败 {session_key}，重排")
                self._schedule_next(session_key, settings, trigger=trigger)
                return False

            # RECORD：计数 + 排下一次
            count = self._store.increment_unanswered(session_key)
            self._store.clear_schedule_state(session_key)
            self._schedule_next(session_key, settings, trigger=TRIGGER_SCHEDULE)
            self.ctx.logger.info(
                f"[主动对话] 已发送 {session_key}（来源 {trigger}，正文 {len(text)} 字），"
                f"未回复计数 {count}/{max_unanswered if max_unanswered > 0 else '∞'}"
            )
            return True
        finally:
            self._sending.discard(session_key)
            self._abort_flags.pop(session_key, None)

    # ── 指令集 ─────────────────────────────────────────────────

    def _is_admin(self, user_id: str, is_local_operator: bool) -> bool:
        if is_local_operator:
            return True
        admin_ids = {str(item).strip() for item in (self.config.plugin.admin_users or []) if str(item).strip()}
        return str(user_id).strip() in admin_ids

    def _extract_scope(self, matched_groups: Dict[str, Any], fallback: str = "") -> str:
        for name in ("scope", "arg", "args"):
            value = matched_groups.get(name)
            if value:
                return str(value).strip()
        return fallback

    def _resolve_scope_keys(
        self,
        scope: str,
        command_message: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[List[str]], Optional[str]]:
        """解析 scope 为会话键列表。返回 (keys, 错误消息)；keys=None 表示全局。"""

        scope = str(scope or "").strip()
        if not scope or scope.lower() in _SCOPE_CURRENT_WORDS:
            key = self._key_from_command_message(command_message)
            if key is None:
                return None, "无法识别当前会话（本会话可能不支持主动对话）。"
            return [key], None
        if scope.lower() in _SCOPE_GLOBAL_WORDS:
            return None, None
        parsed = sched.parse_session_key(scope)
        if parsed is not None:
            return [sched.normalize_session_key(*parsed)], None
        # 裸 ID：先在已知会话数据中找，再按默认平台构造两种候选
        platform = str(self.config.plugin.default_platform or "qq").strip() or "qq"
        for key in (self._store.keys() if self._store else []):
            p = sched.parse_session_key(key)
            if p and p[2] == scope:
                return [key], None
        return (
            [
                sched.normalize_session_key(platform, sched.CHAT_TYPE_PRIVATE, scope),
                sched.normalize_session_key(platform, sched.CHAT_TYPE_GROUP, scope),
            ],
            None,
        )

    def _key_from_command_message(self, command_message: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(command_message, dict):
            return None
        platform = str(command_message.get("platform") or "").strip()
        info = command_message.get("message_info") or {}
        group_info = info.get("group_info")
        user_info = info.get("user_info") or {}
        if group_info and str(group_info.get("group_id") or "").strip():
            return sched.normalize_session_key(platform, sched.CHAT_TYPE_GROUP, str(group_info.get("group_id")))
        user_id = str(user_info.get("user_id") or "").strip()
        account_id = str(command_message.get("account_id") or "").strip()
        if user_id and (not account_id or user_id != account_id):
            return sched.normalize_session_key(platform, sched.CHAT_TYPE_PRIVATE, user_id)
        return None

    def _session_status_text(self, session_key: str) -> str:
        """生成单会话状态描述。"""

        parsed = sched.parse_session_key(session_key)
        if parsed is None:
            return f"- {session_key}：会话键不合法"
        chat_type, _target_id = parsed[1], parsed[2]
        settings = self._settings_for(chat_type)
        payload = self._store.get(session_key) if self._store else {}

        if self._store is not None and self._store.get_enabled_override(session_key) is not None:
            state_text = "已启用（会话覆写）" if self.effective_enabled(session_key) else "已停用（会话覆写）"
        elif self.effective_enabled(session_key):
            state_text = "已启用（命中配置列表）"
        elif not self._global_enabled(chat_type):
            state_text = "未启用（插件或组级开关关闭）"
        else:
            state_text = "未启用（不在配置列表中）"

        parts = [f"- 会话 {session_key}：{state_text}"]
        if not self.effective_enabled(session_key):
            return "\n".join(parts)

        next_ts = self._store.get_next_trigger(session_key) if self._store else None
        if next_ts is not None:
            remaining = max(0, int(next_ts - time.time()))
            parts.append(
                f"  下次触发：{sched.format_current_time(next_ts)}（约 {remaining // 60} 分钟后，来源：定时任务）"
            )
        elif chat_type == sched.CHAT_TYPE_GROUP and session_key in self._silence_tasks:
            parts.append("  下次触发：等待群沉默计时结束")
        elif session_key in self._auto_trigger_tasks:
            parts.append("  下次触发：等待启动静默自动触发检查")
        else:
            parts.append("  下次触发：暂无排期（等待用户消息/群沉默触发）")

        unanswered = self._store.get_unanswered(session_key) if self._store else 0
        max_unanswered = int(settings.schedule.max_unanswered_times)
        parts.append(
            f"  未回复计数：{unanswered}/{max_unanswered if max_unanswered > 0 else '不限'}"
        )
        quiet_text = (
            f"{settings.schedule.quiet_hours}（{'生效中' if self._in_quiet_hours(settings) else '未生效'}）"
            if settings.schedule.enable_quiet_hours
            else "未启用"
        )
        parts.append(f"  免打扰时段：{quiet_text}")
        parts.append(
            f"  调度间隔：{settings.schedule.min_interval_minutes}-{settings.schedule.max_interval_minutes} 分钟"
        )
        if chat_type == sched.CHAT_TYPE_GROUP:
            parts.append(f"  群沉默阈值：{settings.group_idle_trigger_minutes} 分钟")
        auto = settings.auto_trigger
        parts.append(
            f"  自动触发：{'启用' if auto.enabled else '停用'}（启动静默 {auto.minutes} 分钟）"
        )
        parts.append(f"  人格模式：{settings.persona.mode}")
        if payload.get("chat_name"):
            parts.append(f"  会话名称：{payload['chat_name']}")
        return "\n".join(parts)

    @Command("proactive_help", description="主动对话帮助", pattern=r"^/主动对话$")
    async def handle_help(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, Optional[str], bool]:
        del kwargs
        text = (
            "主动对话（Proactive Chat）指令：\n"
            "- /主动对话状态 [当前会话|全局|<会话ID>]：查看运行状态（管理员）\n"
            "- /主动对话发送 [同上]：立即触发一次主动对话（管理员）\n"
            "- /主动对话开关 [同上] [on|off]：启停会话或全局（管理员）\n"
            "- /主动对话重载：重载会话数据并重建调度（管理员）\n"
            "会话 ID 支持：platform:private:用户ID / platform:group:群ID / 裸 ID"
        )
        await self.ctx.send.text(text, stream_id)
        return True, None, True

    @Command(
        "proactive_status",
        description="查看主动对话状态",
        pattern=r"^/主动对话状态(?:\s+(?P<scope>.+))?$",
    )
    async def handle_status(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> Tuple[bool, Optional[str], bool]:
        if not self._is_admin(user_id, is_local_operator):
            await self.ctx.send.text("该指令需要插件管理员权限。", stream_id)
            return True, None, True
        if not self.config.plugin.enabled:
            await self.ctx.send.text("主动对话插件未启用（config.plugin.enabled = false）。", stream_id)
            return True, None, True

        matched_groups = kwargs.get("matched_groups") or {}
        command_message = kwargs.get("message")
        scope = self._extract_scope(matched_groups)
        keys, error = self._resolve_scope_keys(scope, command_message)
        if error:
            await self.ctx.send.text(error, stream_id)
            return True, None, True

        if keys is None:
            friend = self.config.friend_settings
            group = self.config.group_settings
            lines = [
                "主动对话 全局状态：",
                f"- 版本：v{PLUGIN_VERSION}，运行时长 {int((time.time() - self._plugin_start_time) // 60)} 分钟",
                f"- 私聊：{'启用' if self._global_enabled('private') else '停用'}，"
                f"配置会话 {len(friend.session_list)} 个",
                f"- 群聊：{'启用' if self._global_enabled('group') else '停用'}，"
                f"配置会话 {len(group.session_list)} 个",
                f"- 定时任务 {len(self._scheduled_tasks)} 个，群沉默计时 {len(self._silence_tasks)} 个，"
                f"自动触发 {len(self._auto_trigger_tasks)} 个，生成中 {len(self._sending)} 个",
                f"- 会话数据 {len(self._store.keys()) if self._store else 0} 条",
                "",
                "逐会话：",
            ]
            known_keys = sorted(
                set((self._store.keys() if self._store else []))
                | {self._resolve_configured_session(e, "private") or "" for e in friend.session_list}
                | {self._resolve_configured_session(e, "group") or "" for e in group.session_list}
            )
            known_keys = [key for key in known_keys if key]
            lines.extend(self._session_status_text(key) for key in known_keys)
            await self.ctx.send.text("\n".join(lines), stream_id)
            return True, None, True

        texts = [self._session_status_text(key) for key in keys]
        await self.ctx.send.text("\n\n".join(texts), stream_id)
        return True, None, True

    @Command(
        "proactive_send",
        description="立即触发主动对话",
        pattern=r"^/主动对话发送(?:\s+(?P<scope>.+))?$",
    )
    async def handle_send(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> Tuple[bool, Optional[str], bool]:
        if not self._is_admin(user_id, is_local_operator):
            await self.ctx.send.text("该指令需要插件管理员权限。", stream_id)
            return True, None, True
        if not self.config.plugin.enabled:
            await self.ctx.send.text("主动对话插件未启用。", stream_id)
            return True, None, True

        matched_groups = kwargs.get("matched_groups") or {}
        command_message = kwargs.get("message")
        scope = self._extract_scope(matched_groups)
        keys, error = self._resolve_scope_keys(scope, command_message)
        if error:
            await self.ctx.send.text(error, stream_id)
            return True, None, True

        if keys is None:
            keys = sorted(
                set(self._store.keys() if self._store else [])
                | {self._resolve_configured_session(e, "private") or "" for e in self.config.friend_settings.session_list}
                | {self._resolve_configured_session(e, "group") or "" for e in self.config.group_settings.session_list}
            )
            keys = [key for key in keys if self.effective_enabled(key)]

        results: List[str] = []
        for key in keys:
            parsed = sched.parse_session_key(key)
            if parsed is None:
                continue
            chat_type = parsed[1]
            settings = self._settings_for(chat_type)
            if not self.effective_enabled(key):
                results.append(f"- {key}：会话未启用，已跳过")
                continue
            if self._in_quiet_hours(settings):
                results.append(
                    f"- {key}：当前处于免打扰时段（{settings.schedule.quiet_hours}），已拒绝"
                )
                continue
            if key in self._manual_trigger_sessions or key in self._sending:
                results.append(f"- {key}：正在生成中，已跳过")
                continue
            self._manual_trigger_sessions.add(key)
            try:
                ok = await self._check_and_chat(key, TRIGGER_MANUAL)
                results.append(f"- {key}：{'已发送' if ok else '触发未成功'}")
            finally:
                self._manual_trigger_sessions.discard(key)
        await self.ctx.send.text("\n".join(results) if results else "没有可触发的会话。", stream_id)
        return True, None, True

    @Command(
        "proactive_switch",
        description="启停主动对话会话",
        pattern=r"^/主动对话开关(?:\s+(?P<scope>\S+))?(?:\s+(?P<switch>\S+))?$",
    )
    async def handle_switch(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> Tuple[bool, Optional[str], bool]:
        if not self._is_admin(user_id, is_local_operator):
            await self.ctx.send.text("该指令需要插件管理员权限。", stream_id)
            return True, None, True
        if not self.config.plugin.enabled:
            await self.ctx.send.text("主动对话插件未启用。", stream_id)
            return True, None, True

        matched_groups = dict(kwargs.get("matched_groups") or {})
        command_message = kwargs.get("message")
        scope_raw = self._extract_scope(matched_groups)
        switch_raw = str(matched_groups.get("switch") or "").strip()

        # 开关词与作用域可互换位置；拼错直接拒绝
        if switch_raw.lower() not in _SWITCH_ON_WORDS | _SWITCH_OFF_WORDS:
            if scope_raw.lower() in _SWITCH_ON_WORDS | _SWITCH_OFF_WORDS:
                scope_raw, switch_raw = switch_raw, scope_raw
            else:
                await self.ctx.send.text(
                    f"无法识别开关参数「{switch_raw or scope_raw}」，请使用 on 或 off。", stream_id
                )
                return True, None, True
        turning_on = switch_raw.lower() in _SWITCH_ON_WORDS

        keys, error = self._resolve_scope_keys(scope_raw, command_message)
        if error:
            await self.ctx.send.text(error, stream_id)
            return True, None, True

        if keys is None:
            # 全局：写插件状态覆写并重建调度
            self._state.set("friend_settings_enabled", turning_on)
            self._state.set("group_settings_enabled", turning_on)
            self._cancel_all_tasks()
            self._store.save()
            if turning_on:
                self._reinitialize()
            await self.ctx.send.text(
                f"主动对话全局已{'开启' if turning_on else '关闭'}（私聊与群聊组级开关同步覆写）。", stream_id
            )
            return True, None, True

        lines: List[str] = []
        for key in keys:
            parsed = sched.parse_session_key(key)
            if parsed is None:
                lines.append(f"- {key}：会话键不合法，已跳过")
                continue
            chat_type = parsed[1]
            settings = self._settings_for(chat_type)
            self._store.set_enabled_override(key, turning_on)
            if turning_on:
                if chat_type == sched.CHAT_TYPE_GROUP:
                    self._reset_group_silence_timer(key)
                else:
                    self._schedule_next(key, settings, trigger=TRIGGER_MANUAL)
                lines.append(f"- {key}：已开启")
            else:
                self._cancel_session_schedule(key)
                self._cancel_silence_timer(key)
                self._cancel_auto_trigger(key)
                lines.append(f"- {key}：已关闭")
        self._store.save()
        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, None, True

    @Command("proactive_reload", description="重载主动对话会话数据", pattern=r"^/主动对话重载$")
    async def handle_reload(
        self,
        stream_id: str = "",
        user_id: str = "",
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> Tuple[bool, Optional[str], bool]:
        del kwargs
        if not self._is_admin(user_id, is_local_operator):
            await self.ctx.send.text("该指令需要插件管理员权限。", stream_id)
            return True, None, True
        self._cancel_all_tasks()
        if self._store is not None:
            self._store.load()
            self._store.save()
        self._reinitialize()
        await self.ctx.send.text(
            f"已重载会话数据并重建调度：定时任务 {len(self._scheduled_tasks)} 个，"
            f"群沉默计时 {len(self._silence_tasks)} 个。",
            stream_id,
        )
        return True, None, True
