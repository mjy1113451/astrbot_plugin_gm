# AstrBot QQ 群管插件

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-插件-green.svg)](https://github.com/Snowyyu/AstrBot)
[![License](https://img.shields.io/badge/License-AGPL--3.0-red.svg)](LICENSE)

> 本项目采用 **AGPL-3.0** 许可证，是基于网络分发（Bot / 服务器场景）的**主动选择**：以服务器形式对外提供功能的项目，AGPL 要求部署方开放修改后的源码，与 QQ 群管 Bot 的部署形态契合。本插件的违规检测模块移植自 [astrbot_plugin_group_moderation](https://github.com/huangzuan-dev/astrbot_plugin_group_moderation)（同为 AGPL-3.0），见 [NOTICE](NOTICE)。
>
> **⚠️ AGPL §13 网络服务条款：以服务器 / 机器人形式对外提供修改后版本，须向交互方开放修改后全部源码。Fork 与二次分发请审慎评估并遵守 AGPL 全部条款。**

---
#注∶在插件配置设置完会设置为默认信息，即为全局配置

## 功能一览

### 基础管理

| 命令 | 所需权限 | 说明 |
|------|---------|------|
| `/禁言 @某人 [分钟]` | 插件管理员 | 禁言指定成员（默认 10 分钟） |
| `/禁言列表` | 插件管理员 | 查看本群当前被禁言成员列表 |
| `/解禁 @某人` | 插件管理员 | 解除禁言 |
| `/踢 @某人` | 插件管理员 | 踢出群成员（支持批量，可配合配置拒绝重新加群；#145 开启 kick_recall_enabled 时同时清历史） |
| `/清用户历史 @某人 [N]` | 插件管理员 | 撤回某用户在本群的最近 N 条消息（最多 50；#145；#181 起等价于 `/撤回 @某人 N`，保留为兼容别名） |
| `/鞭尸 @某人` | 插件管理员 | 长期禁言被@的人（29 天 23 小时 59 分） |
| `/头衔 @某人 标题` | 插件管理员 | 设置成员专属头衔 |
| `/给我头衔 标题` | 任意成员 | 自设群头衔（普通成员可用） |
| `/设管理 @某人` | 插件管理员 | 设为群管理员 |
| `/取消管理 @某人` | 插件管理员 | 取消群管理员身份 |
| `/设精` / `/取消设精` | 插件管理员 | 设置 / 取消精华消息（引用消息） |
| `/设群昵称 @某人 昵称` | 插件管理员 | 设置指定成员的群昵称 |
| `/改昵称 新昵称` | 任意成员 | 修改自己的群昵称 |
| `/撤回 N` | 插件管理员 | 撤回最近 N 条消息（最多 50，不含指令本身） |
| `/撤回 @用户 N` | 插件管理员 | 撤回该用户最近 N 条消息（最多 50） |
| `/撤回` | 插件管理员 | 引用撤回某条消息 |
| `/撤回自身 N` | 插件管理员 | 撤回机器人最近发送的 N 条消息 |
| `/发群公告 内容` | 插件管理员 | 发送群公告 |
| `/改群头像` | 插件管理员 | 引用图片回复即可修改群头像 |
| `/宵禁` / `/解除宵禁` | 插件管理员 | 开启 / 关闭全群禁言 |
| `/禁我 [分钟]` | 任意成员 | 自怼（默认 10 分钟） |
| `/排名` | 任意成员 | 查看本群发言排名 |
| `/清除数据` | 插件管理员 | 清除本群发言计数 |
| `/举报` | 任意成员 | 举报群成员违规行为（需引用消息） |
| `/添加群待办` | 插件管理员 | 引用消息设为群待办 |
| `/取消群待办` | 插件管理员 | 引用消息取消群待办 |
| `/加群申请待处理` | 插件管理员 | 查看本群未处理的加群申请列表 |
| `/群信息` | 任意成员 | 查看本群资料（名称/号/标签/人数） |
| `/群相册 相册名` | 插件管理员 | 引用图片消息上传到群相册 |
| `/群名称 新群名` | 插件管理员 | 修改本群名 |
| `/群标签 标签名` | 插件管理员 | 添加本群标签 |
| `/添加违禁图片` | 插件管理员 | 引用图片消息加入违禁图列表（按 MD5 比对） |
| `/删除违禁图片 <md5前8位>` | 插件管理员 | 删除本群某张违禁图 |
| `/查看违禁图片` | 插件管理员 | 查看本群+全局违禁图列表（含 WebUI 上传） |
| `/添加加群审核通过关键词 <词>` | 插件管理员 | 添加加群审核自动通过关键词（#186） |
| `/删除加群审核通过关键词 <词>` | 插件管理员 | 删除加群审核自动通过关键词 |
| `/查看加群审核通过关键词` | 插件管理员 | 查看本群加群审核通过关键词 |

> ⚠️ 违禁图检测基于 MD5 比对，仅能阻止原图二次传播。攻击者对图片做轻微改动（裁剪/压缩/加噪）会绕过。建议作为快速预筛，主防御仍依赖 AI 鉴图。

### 按群覆盖配置

| 命令 | 所需权限 | 说明 |
|------|---------|------|
| `/设置群配置 <key> <value>` | 插件管理员 | 为本群覆盖插件配置项（如 `enabled_groups true`） |
| `/查看群配置` | 插件管理员 | 查看本群生效的配置覆盖 |
| `/清除群配置` | 插件管理员 | 清除本群所有覆盖 |
| `/status` | 插件管理员 | 查看插件配置 |

### 群违规检测

| 命令 | 所需权限 | 说明 |
|------|---------|------|
| `/群违规检测状态` | 插件管理员 | 查看群违规检测插件状态 |
| `/查看违规统计 [QQ]` | 插件管理员 | 查看违规统计（带 QQ 号查个人） |
| `/查看白名单` / `/添加白名单用户` / `/删除白名单用户` | 插件管理员 | 白名单管理（不受违规检测限制） |
| `/设置图片禁言时长` / `/设置刷屏禁言时长` / `/设置骂人禁言时长` / `/设置广告禁言时长` / `/设置链接禁言时长` / `/设置群号推广禁言时长` | 插件管理员 | 各违规类型禁言时长（秒） |
| `/添加骂人关键词` / `/删除骂人关键词` / `/查看骂人关键词` / `/切换骂人检测模式` | 插件管理员 | 骂人检测关键词与 AI / 关键词模式切换 |
| `/添加广告关键词` / `/删除广告关键词` / `/查看广告关键词` | 插件管理员 | 广告检测关键词管理 |

> 检测覆盖：图片 AI 审核（色情 / 擦边）、刷屏、骂人（AI 或关键词）、广告、链接、群号推广；命中后一律：撤回 + 按对应时长禁言。

**六大检测能力（移植自 [astrbot_plugin_group_moderation](https://github.com/huangzuan-dev/astrbot_plugin_group_moderation)）：**

| 检测项 | 说明 | 默认状态 |
|--------|------|---------|
| 图片违规 | AI 视觉模型（OpenAI Vision 兼容）分析色情 / 擦边，可设检测阈值（默认 0.7） | 开（需配置 `api_endpoint` / `api_key` / `model_name`） |
| 刷屏 | 时间窗口（默认 10 秒）内消息数超过阈值（默认 5 条）判定刷屏 | 开 |
| 骂人 | AI 识别（`profanity_use_ai=true` 默认）或关键词匹配双模式，关键词可动态增删 | 开 |
| 广告 | 预设 24 个常见广告关键词（加群 / 加微信 / 代练 / 外挂 / 刷钻等），可动态增删 | 开 |
| 链接 | 匹配 http/https/www 等链接格式 | 关（`link_check_enabled`） |
| 群号推广 | 推广关键词（进群 / 加群 / 群号 / 入群 / 拉群 / 建群）+ 识别 5-12 位群号 | 开 |

> 白名单用户（`whitelist_users`）不受检测限制；管理员默认豁免（`admin_bypass`）；检测到违规后可选择群内通知（`notify_on_violation`）。

### 加群申请自动审核（参考 [GroupManager](https://github.com/mjy1113451/group_manager)）

加群申请验证流程（受总开关 `join_audit_enabled` 控制，关闭后仅保留管理员手动审核）：

1. **违禁词自动拒绝**：申请验证消息命中 `violation_keywords` → 自动拒绝，并按 `join_reject_reason` 给出理由
2. **关键词自动同意**：验证消息命中 `join_approve_keywords` → 自动同意，并在该群发送通知「该用户触碰到加群审核通过词语，已自动同意！」（#186）
3. **群内提醒人工审核**：`join_request_notify_in_group=true` 时，申请信息发到群内（含昵称/QQ号/QQ等级/验证消息），管理员**引用回复「同意」或「拒绝 [理由]」**即可完成审核（#189）
4. **管理员私聊通知**：处理结果推送给 `join_notify_admins` 列表中的 QQ

```
/加群申请待处理                 # 查看本群未处理的加群申请列表
/添加加群审核通过关键词 <词>      # 添加关键词（命中自动同意）
/删除加群审核通过关键词 <词>      # 删除关键词
/查看加群审核通过关键词           # 查看本群关键词
```

关键词配置示例（在群内执行）：

```
/添加加群审核通过关键词 学生
/设置群配置 join_approve_keywords ["学生", "老师"]
/设置群配置 join_reject_reason "请填写真实验证信息"
```

> 常用验证思路：学习群放行「学生 / 老师 / 手机号」，工作群放行「部门 / 工号」，兴趣群放行兴趣关键词；对已知可信用户，用违禁词反向拦截（只拒不可信内容）往往比逐个列白名单更高效。

---

## 安装

### 方法一：放入插件目录

1. 克隆本仓库：
   ```bash
   git clone https://github.com/mjy1113451/astrbot_plugin_gm.git
   ```
2. 将 `astrbot_plugin_gm` 目录放入 AstrBot 的 `plugins/` 目录
3. 重启 AstrBot 即可自动加载

### 方法二：通过包管理器安装

```bash
# 视AstrBot安装方式选择对应命令
pip install astrbot_plugin_group_admin
```

---

## 配置

插件提供以下可配置项（在 AstrBot 配置文件中设置，或在群内用 `/设置群配置` 按群覆盖）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `show_recall_notice` | bool | `true` | 撤回操作后在群里发送提示 |
| `mute_notice` | bool | `true` | 禁言 / 解禁后回复结果 |
| `reject_re_add` | bool | `false` | 踢人后自动拒绝该用户再次加群 |
| `auto_recall_keywords` | list | `[]` | Bot 发言自动撤回关键词列表（推荐按群覆盖） |
| `auto_recall_enabled_groups` | list | `[]` | 启用自动撤回的群 ID 列表（`*` / `all` 表示全部；只配置了 `auto_recall_keywords` 而未配置本项时，默认对全部群生效） |
| `enabled_groups` | list | `[]` | 启用违规检测的群号列表（`*` / `all` 表示全部；推荐按群覆盖） |
| `group_overrides` | dict | `{}` | 按群独立配置覆盖：`{群号: {key: value}}` |
| `max_message_history` | int | `50` | 每群内存缓存的撤回消息历史条数（用于 /撤回 N 与 /撤回自身 N） |
| `join_reject_reason` | string | `"不满足加群条件"` | 加群申请自动拒绝时展示的默认理由（管理员可通过「拒绝 理由」自定义） |
| `join_audit_enabled` | bool | `true` | 加群申请自动审核总开关（关闭后违禁词/关键词自动审核都跳过；管理员手动审核不受影响） |
| `group_admin_admins` | list | `[]` | 可设置/取消群管理的专项管理员 QQ 列表（全局默认；按群覆盖优先级更高） |
| `banned_image_files` | file | `[]` | WebUI 上传违禁图片文件（自动计算 MD5 参与比对；#184；**全局配置**，需 AstrBot v4.13.0+） |
| `kick_recall_enabled` | bool | `false` | 踢人时自动撤回该成员最近消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian） |
| `kick_recall_count` | int | `10` | 踢人撤回消息条数（1-50，#145） |
| `voice_check_enabled` | bool | `false` | 启用语音消息转文字违规检测（#128；可按群覆盖） |
| `voice_check_provider_id` | string | `""` | AstrBot 内置 STT provider ID（#128；留空用当前激活 provider；**全局配置**） |
| `voice_asr_endpoint` | string | `""` | 独立 ASR API 端点（#128；可选兜底；**全局配置**） |
| `voice_asr_api_key` | string | `""` | 独立 ASR API Key（#128；可选兜底；**全局配置**） |
| `voice_asr_model` | string | `""` | 独立 ASR 模型名（#128；默认 whisper-1；**全局配置**） |
| `voice_check_timeout` | int | `15` | ASR 识别超时秒数（#128；**全局配置**） |

### 配置示例

```json
{
  "show_recall_notice": true,
  "reject_re_add": false
}
```

> 插件管理员身份完全由 QQ 群管理员 / 群主自动识别，无需在配置中手动指定。

### 按群覆盖示例

通过群内指令按群独立配置（推荐）：

```
/设置群配置 enabled_groups true
/设置群配置 auto_recall_keywords ["测试", "敏感词"]
/设置群配置 rank_top_n 20
```

或在配置文件中直接编辑 `group_overrides`：

```json
{
  "group_overrides": {
    "123456789": {
      "enabled_groups": true,
      "rank_top_n": 20,
      "auto_recall_keywords": ["测试", "敏感词"]
    }
  }
}
```

按群覆盖的可配置 key 包括：基础配置（`show_recall_notice`、`auto_recall_keywords`、`auto_recall_enabled_groups`、`rank_top_n`、`report_notify_admins`、`join_approve_keywords`、`join_notify_admins`、`join_request_notify_in_group`、`enabled_groups`）+ 违规检测全部子项（`spam_*`、`profanity_*`、`ad_*`、`link_*`、`group_promotion_*`、`ban_duration`、`whitelist_users`、`admin_bypass`、`notify_on_violation`)+ 权限细分（`title_admins`、`group_admin_admins`、`kick_admins`、`mute_kick_threshold`）+ 撤回历史（`max_message_history`）+ 踢人清历史（`kick_recall_enabled`、`kick_recall_count`）+ 语音违规检测开关（`voice_check_enabled`）。
> 语音转文字相关配置（`voice_check_provider_id`、`voice_asr_endpoint`、`voice_asr_api_key`、`voice_asr_model`、`voice_check_timeout`）为**全局配置**，不支持按群覆盖。

### 图片 AI 审核配置

图片违规检测使用 OpenAI 兼容的视觉 API，需在插件配置中填写：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `api_type` | 审核方式：`openai_vision`（视觉模型）或 `moderation`（审核 API） | `openai_vision` |
| `api_endpoint` | chat/completions 端点 | `https://api.siliconflow.cn/v1/chat/completions` |
| `api_key` | API 密钥 | `sk-xxxxxxxx` |
| `model_name` | 支持视觉的模型 | `Qwen/Qwen2-VL-72B-Instruct` |
| `threshold` | 违规判定阈值 0-1，越低越严格 | `0.7` |
| `check_porn` / `check_sexy` | 分别开关色情 / 擦边检测 | `true` |
| `detection_prompt` | 自定义检测提示词（留空用内置） | 空 |

推荐视觉模型：`Qwen/Qwen2-VL-72B-Instruct`、`gpt-4o`、`deepseek-ai/deepseek-vl2`。

> 性能提示：图片检测会消耗 API 调用，建议通过 `enabled_groups` 只在需要的群启用，并用 `whitelist_users` 豁免信任用户。链接检测默认关闭（`link_check_enabled`），按需开启。
>
> ⚠️ **凭据安全**：`api_key` 为敏感凭据，请勿提交到公开仓库或截图分享；建议通过本地配置覆盖，日志会尽量脱敏，但请避免在群聊中粘贴完整配置。

---

## `/撤回` 用法与兼容性

`/撤回` 命令支持三种用法：

```
/撤回 + 引用消息        撤回引用消息
/撤回 @用户 N           撤回该用户最近 N 条（最多 50）
/撤回 N                撤回最近 N 条（最多 50，不含指令本身）
```

> ⚠️ 受 OneBot v11 协议限制，`delete_msg` 只能撤回约 **2 分钟内**的消息：超过 2 分钟的历史即使能拉取到，撤回也会静默失败。`/撤回自身 N`、`/清用户历史 @某人 N` 同受此限制。

配套命令：

```
/撤回自身 N             撤回机器人最近发送的 N 条消息
```

**消息历史机制（修复 #117 #118 #122）**：

- 插件在每个群内存缓存最近 `max_message_history` 条（默认 50）消息；用户发送的插件指令消息不记录，避免编号偏移；Bot 自身发言也记录（可用 `/撤回自身`）。
- `/撤回 @用户 N`、`/撤回 N` 优先使用该本地历史；当某群本地历史为空时，自动调用 OneBot 的 `get_group_msg_history` 接口兜底加载。
- 若 OneBot 实现不支持 `get_group_msg_history` 且本地历史也为空，则提示改用「引用消息」撤回。
- 本地历史仅记录进程启动后经过监听的消息，重启前历史不可恢复。

**踢人清历史**：

- 配置 `kick_recall_enabled=true` 后，执行 `/踢 @某人` 会自动撤回被踢成员最近 `kick_recall_count`（默认 10，最多 50）条消息（踢出前完成，因为踢出后无法再拉取其历史）。
- 新增 `/清用户历史 @某人 [N]`：单独执行清历史，不踢人。
- OneBot `delete_msg` 只能撤回约 2 分钟内的消息，超时的会静默失败。

**语音转文字违规检测（#128）**：

- 配置 `voice_check_enabled=true` 后，对群内语音消息自动 ASR 识别，复用违规检测链路（骂人 / 广告 / 链接 / 群号推广）。`voice_check_enabled` 可按群覆盖，便于各群独立启用。
- ASR 模型相关配置（`voice_check_provider_id`、`voice_asr_endpoint`、`voice_asr_api_key`、`voice_asr_model`、`voice_check_timeout`）为**全局配置**，不支持按群覆盖，整个 Bot 共享同一套 ASR 路由。
- ASR 识别顺序：① `voice_check_provider_id` 指定的 AstrBot 内置 STT provider；② 未指定时使用 AstrBot 当前激活的 STT provider；③ AstrBot 不可用时回退到 `voice_asr_endpoint` + `voice_asr_api_key` + `voice_asr_model` 配置的 OpenAI 兼容 `/audio/transcriptions` 接口。
- 命中违规则撤回语音消息并按对应时长禁言。

---

## 加群申请审核（#27 #57 #129 #150 #155 #159）

本插件已完整支持入群申请审核能力，无需合并外部仓库代码：

| 能力 | 配置 / 命令 | 说明 |
|------|------|------|
| 违禁词自动拒绝 | `violation_enabled_groups` + `violation_keywords` | 命中违禁词自动拒绝（#129） |
| 关键词自动同意 | `join_approve_keywords` | 验证消息命中关键词自动同意 |
| 群内提醒管理员 | `join_request_notify_in_group = true` | 申请消息发送到群内，引用回复同意/拒绝（#57） |
| 自定义拒绝理由 | `join_reject_reason` / 引用回复「拒绝 理由」 | 默认"不满足加群条件"，可按群覆盖 |
| 拒绝原因详细化 | 内置（#159） | 违禁词命中时提示「您的加群申请有词触碰到本群违禁词，自动拒绝」 |
| 自动审核总开关 | `join_audit_enabled` | 关闭后跳过所有自动审核（#155） |
| 查看待处理申请 | `/加群申请待处理` | 列出本群未处理加群申请（#150） |
| 私聊通知管理员 | `join_notify_admins` | 申请处理结果私聊通知 |

> 如需 #161 提及的外部仓库 (`BB0813/astrbot_pulgin_group_manager`) 中的某项具体功能，请在该 issue 留言说明具体需求。

---

## 权限说明

本插件采用 **两层权限** 设计：

1. **插件管理员**：拥有使用所有管理命令的权限。识别方式：
   - QQ 群管理员
   - QQ 群主
2. **专项权限管理员**：`group_admin_admins`（可设/取消群管理）等专项权限列表中的人，仅对相应操作生效（不受群管理身份限制）。`title_admins`、`kick_admins` 不再提供 WebUI 全局配置项（#188），仍支持按群覆盖：`/设置群配置 title_admins ["QQ"]`、`/设置群配置 kick_admins ["QQ"]`。

`group_admin_admins` 支持 **全局配置**（在插件配置 / WebUI 面板中设置，作为默认值）与 **按群覆盖**（群内 `/设置群配置`，优先级更高）。

> 插件管理员身份完全由 QQ 群管理员 / 群主自动识别，不再提供 `plugin_admins` 配置项与 `/设管` `/取管` 命令。如需专项权限授予非群管理员用户，使用对应专项权限列表。

---

## 命令使用示例

```
# 禁言某成员 30 分钟
/禁言 @小明 30

# 长期禁言（29 天 23 小时 59 分）
/鞭尸 @小明

# 踢出成员（并拒绝重新加群，需开启配置）
/踢 @小明

# 设置成员头衔
/头衔 @小明 荣誉成员

# 自设群头衔
/给我头衔 传说

# 引用撤回某条消息
/撤回  ← 引用目标消息发送

# 撤回最近 5 条消息
/撤回 5

# 撤回某用户最近 3 条
/撤回 @小明 3

# 撤回机器人最近 3 条
/撤回自身 3

# 修改自己的群昵称
/改昵称 新名字

# 本群独立启用违规检测
/设置群配置 enabled_groups true

# 自怼（禁言自己 60 分钟）
/禁我 60

# 加群申请：设置自动同意关键词
/设置群配置 join_approve_keywords ["学生"]

# 违规检测：添加骂人关键词
/添加骂人关键词 笨蛋
```

---

## 常见问题

### 违规检测相关

**Q: 消息没有被撤回？**
A: ① 确认机器人有群管理员权限（撤回 + 禁言都需要）；② 检查该群是否已启用检测（`/设置群配置 enabled_groups true`）；③ 检查日志中是否有撤回相关输出。

**Q: 图片检测没有反应？**
A: 检查 `api_endpoint` / `api_key` / `model_name` 是否已配置，日志中应有 `[群违规检测] 检测到 X 张图片` 的输出；未配置 API 时图片审核会静默跳过。

**Q: AI 检测不准确？**
A: ① 调整 `threshold`（降低更严格）；② 更换视觉模型；③ 通过 `detection_prompt` 自定义检测提示词。

**Q: 刷屏检测误判？**
A: 调大 `spam_threshold` 和 `spam_time_window`，例如 10 条 / 20 秒更宽松。

**Q: 如何关闭某个检测？**
A: 对应开关配置设为 false（如 `spam_check_enabled`、`profanity_check_enabled`、`ad_check_enabled`、`link_check_enabled`、`group_promotion_check_enabled`），可按群覆盖。

**Q: 白名单用户为什么还会被检测？**
A: 检查 `whitelist_users` 配置，确保 QQ 号为纯数字字符串。

### 加群审核相关

**Q: 自动审核不生效？**
A: ① 检查总开关 `join_audit_enabled` 是否为 true；② 违禁词拒绝 / 关键词同意需要该群在 `enabled_groups` 中（或按群覆盖 `enabled_groups true`）。

**Q: 群内引用回复审核怎么用？**
A: 配置 `join_request_notify_in_group true` 后，新申请会发到群内；管理员**引用那条提醒消息回复「同意」或「拒绝 理由」**即可。

### 撤回相关

**Q: `/撤回 N` 提示不支持？**
A: 基于 OneBot v11 协议，当前实现不支持 `get_group_msg_history` 且本地历史为空，请改用「引用消息 + /撤回」。

**Q: 超过 2 分钟的消息撤不回？**
A: OneBot `delete_msg` 只能撤回约 2 分钟内的消息，超时会静默失败。

---

## 目录结构

```
astrbot_plugin_gm/
├── main.py              # 插件主逻辑（3100+ 行）
├── metadata.yaml         # 插件元信息
├── _conf_schema.json     # 配置项说明
├── README.md             # 本文件
├── NOTICE                # 第三方代码声明（astrbot_plugin_group_moderation 移植）
├── LICENSE               # AGPL-3.0 License
├── requirements.txt      # Python 依赖（aiohttp）
└── .github/              # GitHub 配置
```

---

## 开发相关

- **Python 版本**：3.10+
- **依赖框架**：[AstrBot](https://github.com/Snowyyu/AstrBot)
- **主要 API**：aiocqhttp（QQ 平台）
- **API 调用兼容**：内部对多种 AstrBot 版本做了兼容性适配

---

## 反馈与贡献

- 🐛 发现 Bug？请提交 [Issue](https://github.com/mjy1113451/astrbot_plugin_gm/issues)
- 💡 有功能建议？请先提交 Issue 讨论，待 AI 审核确认后可提 PR
- 🔧 修复难度低到中的 PR，会被优先合并
- 作者的群1075920323

---

## 致谢与第三方代码说明

本插件整合了以下优秀插件的功能。其中**六大违规检测（图片 AI / 刷屏 / 骂人 / 广告 / 链接 / 群号推广）的检测逻辑与 API 调用代码移植自 [astrbot_plugin_group_moderation](https://github.com/huangzuan-dev/astrbot_plugin_group_moderation)（AGPL-3.0，与本插件同许可证）**，已按其许可证要求保留来源声明；其余插件仅为功能设计参考。以下许可证结论均经 [NOTICE](NOTICE) 逐一核实，以上游 LICENSE 文件为准（上游 README 自述与 LICENSE 文件不一致时，以 LICENSE 文件为准）：

- [astrbot_plugin_group_moderation](https://github.com/huangzuan-dev/astrbot_plugin_group_moderation)（AGPL-3.0）—— **代码移植**：六大违规检测（图片 AI / 刷屏 / 骂人 / 广告 / 链接 / 群号推广），详见 [NOTICE](NOTICE)
- [GroupManager](https://github.com/BB0813/astrbot_pulgin_group_manager)（AGPL-3.0）—— **设计参考**：加群申请自动审核（关键词同意 / 违禁词拒绝 / 群内人工审核）。**⚠️ 依据 AGPL-3.0 许可证，本项目未复用其任何代码（包括正则片段、匹配逻辑），仅参考其功能设计文档；如需复用其代码，复用部分须继续以 AGPL-3.0 释出**
- [astrbot_plugin_group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian)（MIT）—— **功能对齐**：踢人撤回历史（#145），未复用其代码，自行实现

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供的强大插件框架！
---

> 本插件仅供学习与交流使用，请遵守 QQ / QQ 群的相关使用规范。