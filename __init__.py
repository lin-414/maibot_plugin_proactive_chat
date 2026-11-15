"""主动对话插件（Proactive Chat）— MaiBot 移植版。

移植自 Pancakes-Labs/astrbot_plugin_proactive_chat（AGPL-3.0）。
"""

from .plugin import ProactiveChatPlugin, create_plugin

__all__ = ["ProactiveChatPlugin", "create_plugin"]
