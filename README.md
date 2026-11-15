# 主动对话 (Proactive Chat) — MaiBot 插件

让 MaiBot 在沉默一段时间后，以随机间隔主动向私聊/群聊发起**上下文感知、符合人设**的对话。

移植自 [Pancakes-Labs/astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat)（AGPL-3.0），按 MaiBot 插件 SDK v2 重写。

## 功能一览

- **随机间隔定时触发**：用户最后一条消息后，在 `[min, max]` 分钟内随机取一个间隔主动开口；持续不回复则按同一区间追聊，累计达到 `max_unanswered_times` 后暂停，用户回复即恢复。
- **群聊冷场破冰**：群沉默 `group_idle_trigger_minutes` 分钟后主动活跃气氛；任何人（含 Bot 自己）发言都会重置倒计时。
- **启动静默自动触发**：插件启动后一段时间无人说话时补一次主动对话。
- **手动触发**：管理员指令立即发起。
- **上下文感知**：注入最近 N 条真实聊天记录（含时间与发送者），未回复次数越大语气越失落（写在内置提示词中）。
- **人格注入**：默认读取 MaiBot 当前人设；也支持自定义文本或关闭。
- **免打扰时段**、**分段发送（模拟打字节奏）**、**生成期间新消息检测**（生成过程中对方来了新消息则丢弃本次结果重排）。
- **重启恢复**：任务持久化到 `session_data.json`，重启后自动恢复未执行任务。

> 与 AstrBot 原版的差异：MaiBot 的消息库统一了「对话历史」与「平台流水」，两种上下文模式合并为单一来源；TTS（MaiBot 暂无对应能力）、WebUI 管理端、通知中心、遥测未移植。

## 安装

1. 将整个 `maibot_plugin_proactive_chat` 目录放入 MaiBot 的 `plugins/` 目录；
2. 重启 MaiBot（或在 WebUI 插件页重载）；
3. 在插件配置页开启 `plugin.enabled`。

无第三方依赖，无需额外安装 Python 包。

## 配置

配置文件由 Runner 自动生成（WebUI 插件配置页可视化编辑），主要字段：

### plugin（插件）
| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 插件总开关 |
| `default_platform` | `qq` | 会话列表裸 ID / 指令 scope 默认平台 |
| `admin_users` | `[]` | 插件管理员用户 ID 列表（本地操作员始终可用管理指令） |
| `llm_task` | `""` | 生成使用的模型任务名，留空用 Host 默认 |
| `typing` | `false` | 发送时是否模拟打字 |

### friend_settings（私聊）
| 字段 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 私聊组级开关 |
| `session_list` | `[]` | 私聊目标：`platform:private:用户ID` 或裸用户 ID |
| `persona.mode` | `current` | 人格来源：`current` / `custom` / `none` |
| `auto_trigger` | 关 / 5 分钟 | 启动静默自动触发 |
| `proactive_prompt` | 内置 | 对话记录模式提示词模板 |
| `history_prompt` | 内置 | 聊天记录注入模板（含 `{{platform_history_lines}}` 等占位符） |
| `context.history_count` | `20` | 注入记录条数（0 = 不注入） |
| `context.include_bot_messages` | `true` | 记录中是否包含 Bot 发言 |
| `schedule.min/max_interval_minutes` | `30` / `600` | 触发间隔区间（分钟） |
| `schedule.enable_quiet_hours` / `quiet_hours` | `true` / `1-7` | 免打扰时段（如 `1-7,23-24`） |
| `schedule.max_unanswered_times` | `4` | 连续未回复上限（0 = 不限） |
| `segmented_reply.*` | 关 | 分段发送：阈值、切分模式、段间延迟等 |

### group_settings（群聊）
与私聊同构，另有：
- `session_list`：群目标（`platform:group:群ID` 或裸群 ID）；
- `group_idle_trigger_minutes`：冷场触发分钟数（默认 30，<=0 禁用）；
- 默认间隔 `90-360` 分钟、免打扰 `2-6`、未回复上限 `2`、分段/自动触发默认关。

## 提示词占位符

模板中可用：`{{current_time}}`、`{{unanswered_count}}`、`{{platform_history_lines}}`、`{{platform_history_last_time}}`。未提供的占位符会被整体剔除；`proactive_prompt` 未含流水占位符时会自动追加「当前时间 / 未回复次数 / 会话场景」动态块。

## 指令

| 指令 | 权限 | 说明 |
|---|---|---|
| `/主动对话` | 所有人 | 帮助 |
| `/主动对话状态 [当前会话\|全局\|<会话ID>]` | 管理员 | 运行状态、下次触发、未回复计数、免打扰生效情况等 |
| `/主动对话发送 [同上]` | 管理员 | 立即触发（免打扰时段会拒绝） |
| `/主动对话开关 [同上] [on\|off]` | 管理员 | 会话级开关（覆写，持久化）；全局=覆写两组级开关 |
| `/主动对话重载` | 管理员 | 重载会话数据并重建调度 |

管理员 = 本地操作员 + `plugin.admin_users` 中的用户。会话 ID 支持 `platform:private:123` / `platform:group:456` / 裸 ID。

## 数据文件

位于 `data/plugins/pancakes.proactive-chat/`：

- `session_data.json`：每会话的最后消息时间、未回复计数、下次触发时间、聊天流缓存与会话开关覆写；
- `plugin_state.json`：全局开关覆写（`/主动对话开关 全局 on|off`）。

## 调度语义（与原版一致）

- 用户私聊消息 → 立即按新随机间隔排下一次（并清零未回复计数）；
- 用户群聊消息 → 取消群持久化任务并重置沉默倒计时；
- Bot 群发言（含本插件自己的）→ 重置该群沉默倒计时；
- 主动对话成功发送 → 未回复计数 +1 并排下一次；发送失败/生成失败/生成期间来新消息 → 直接重排；
- 免打扰时段触发 → 重排到之后的随机间隔；未回复达上限 → 暂停（不重排），用户回复后自动恢复。

## License

AGPL-3.0（继承原插件协议）。
