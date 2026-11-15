"""主动对话发送。

对应原插件 sender_service：发送前清洗、按配置分段、段间延迟、
逐段 ctx.send.text。全部失败返回 False（上层据此重排）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List

from . import scheduler as sched
from .config_schema import SegmentedReplyConfig


def _segment_config_to_kwargs(config: SegmentedReplyConfig) -> dict[str, Any]:
    return {
        "enable": bool(config.enable),
        "words_count_threshold": int(config.words_count_threshold),
        "split_mode": str(config.split_mode),
        "regex": str(config.regex),
        "split_words": list(config.split_words or []),
        "enable_content_cleanup": bool(config.enable_content_cleanup),
        "content_cleanup_rule": str(config.content_cleanup_rule),
    }


def _word_count(text: str) -> int:
    if text.isascii():
        return len(text.split())
    return sum(1 for ch in text if ch.isalnum())


async def send_proactive_text(
    send_proxy: Any,
    stream_id: str,
    text: str,
    segmented: SegmentedReplyConfig,
    logger: logging.Logger,
) -> bool:
    """把一条主动对话文本发送到目标聊天流。

    Args:
        send_proxy: ``self.ctx.send`` 能力代理。
        stream_id: 目标聊天流 ID。
        text: 已清洗的完整文本。
        segmented: 分段配置。
        logger: 插件日志器。

    Returns:
        bool: 是否至少成功发送一段。
    """

    segments: List[str]
    if segmented.enable:
        segments = sched.split_text(text, **_segment_config_to_kwargs(segmented))
    else:
        segments = [text.strip()] if text.strip() else []

    any_sent = False
    for index, segment in enumerate(segments):
        if index > 0:
            delay = sched.calc_segment_delay(
                segments[index - 1],
                interval_method=str(segmented.interval_method),
                interval=str(segmented.interval),
                log_base=float(segmented.log_base),
            )
            await asyncio.sleep(delay)
        try:
            sent = await send_proxy.text(segment, stream_id)
            if sent:
                any_sent = True
            else:
                logger.warning(f"[主动对话] 分段发送失败（第 {index + 1}/{len(segments)} 段）stream={stream_id}")
        except Exception as exc:
            logger.warning(f"[主动对话] 分段发送异常（第 {index + 1}/{len(segments)} 段）: {exc}")

    if not any_sent:
        logger.warning(f"[主动对话] 全部分段发送失败 stream={stream_id}")
    return any_sent
