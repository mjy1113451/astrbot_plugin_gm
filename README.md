# AstrBot QQ 群管插件

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-插件-green.svg)](https://github.com/Snowyyu/AstrBot)
[![License](https://img.shields.io/badge/License-AGPL--3.0-red.svg)](LICENSE)

> 本项目采用 **AGPL-3.0** 许可证，是基于网络分发（Bot / 服务器场景）的**主动选择**：以服务器形式对外提供功能的项目，AGPL 要求部署方开放修改后的源码，与 QQ 群管 Bot 的部署形态契合。

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

### 按群覆盖配置（#193）

每项按群配置都有独立指令（#193 已移除 `/设置群配置`），覆盖值写入 `group_overrides`，优先级高于全局配置：

| 命令 | 说明 |
|------|------|
| `/开关撤回提示 on/off` | 撤回操作群内提示（show_recall_notice） |
| `/开关禁言提示 on/off` | 禁言/解禁结果回复（mute_notice） |
| `/开关踢人拒加 on/off` | 踢人后拒绝再次加群（reject_re_add） |
| `/开关管理员豁免 on/off` | 管理员违规豁免（admin_bypass） |
| `/开关违规通知 on/off` | 违规群内通知（notify_on_violation） |
| `/开关加群申请提醒 on/off` | 加群申请群内提醒（join_request_notify_in_group） |
| `/开关加群自动审核 on/off` | 加群自动审核总开关（join_audit_enabled） |
| `/开关踢人清历史 on/off` | 踢人时撤回历史消息（kick_recall_enabled） |
| `/开关语音检测 on/off` | 语音转文字违规检测（voice_check_enabled） |
| `/设置排名人数 N` | 发言排名显示人数（rank_top_n，≥1） |
| `/设置踢人阈值 N` | 刷屏禁言转踢人的阈值（mute_kick_threshold，≥0） |
| `/设置消息历史条数 N` | 本地撤回消息缓存条数（max_message_history，≥10） |
| `/设置踢人清条数 N` | 踢人撤回消息条数（kick_recall_count，1-50） |
| `/设置拒绝理由 理由` | 加群自动拒绝理由（join_reject_reason） |
| `/添加自动撤回关键词 词` / `/删除自动撤回关键词 词` / `/查看自动撤回关键词` | 自动撤回关键词（按群） |
| `/添加举报通知QQ QQ` / `/删除举报通知QQ QQ` / `/查看举报通知QQ` | 举报结果通知管理员（按群） |
| `/添加加群通知QQ QQ` / `/删除加群通知QQ QQ` / `/查看加群通知QQ` | 加群申请处理结果通知管理员（按群） |
| `/查看群配置` | 查看本群生效的配置覆盖 |
| `/清除群配置` | 清除本群所有覆盖 |
| `/status` | 查看插件配置 |

### 链接白名单（#195）

| 命令 | 所需权限 | 说明 |
|------|---------|------|
| `/添加链接白名单 域名` | 插件管理员 | 添加本群链接白名单域名（按群覆盖，如 `example.com`） |
| `/删除链接白名单 域名` | 插件管理员 | 从本群链接白名单移除域名 |
| `/查看链接白名单` | 插件管理员 | 查看本群 + 全局链接白名单 |

> 命中白名单域名的链接不参与链接违规检测（不撤回、不禁言）。全局白名单通过 WebUI `link_whitelist` 配置，本群白名单通过指令维护。

### 群黑名单（#194）

| 命令 | 所需权限 | 说明 |
|------|---------|------|
| `/添加黑名单 @某人或QQ号` | 插件管理员 | 将用户加入本群黑名单 |
| `/删除黑名单 @某人或QQ号` | 插件管理员 | 从本群黑名单移除用户 |
| `/查看黑名单` | 插件管理员 | 查看本群黑名单列表 |

> 黑名单为**按群**生效：黑名单用户申请加群时自动拒绝；在群内加群提醒消息上引用回复 `/拉黑` 可一步完成「拒绝申请 + 加入黑名单」。

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

### 加群申请自动审核

加群申请验证流程（受总开关 `join_audit_enabled` 控制，关闭后仅保留管理员手动审核）：

1. **违禁词自动拒绝**：申请验证消息命中 `violation_keywords` → 自动拒绝，并按 `join_reject_reason` 给出理由
2. **关键词自动同意**：验证消息命中 `join_approve_keywords` → 自动同意，并在该群发送通知「该用户触碰到加群审核通过词语，已自动同意！」（#186）
3. **群内提醒人工审核**：`join_request_notify_in_group=true` 时，申请信息发到群内（含昵称/QQ号/QQ等级/验证消息），管理员**引用回复「同意」或「拒绝 [理由]」或「拉黑」**即可完成审核（#189/#194）；回复「拉黑」= 拒绝申请 + 将该用户加入本群黑名单，此后其再次申请自动拒绝
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
/设置拒绝理由 请填写真实验证信息
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

插件提供以下可配置项（在 AstrBot 配置文件 / WebUI 中设置，或用上方「按群覆盖配置」的独立指令按群覆盖）：

> ⚠️ **默认行为（#192，owner 拍板）**：`enabled_groups` **留空 = 全群启用**（非空时仅 `*` / `all` / 指定群号启用；按群 bool 覆盖可单群显式关闭）；`auto_recall_enabled_groups` **留空 = 全群启用**（命中 `auto_recall_keywords` 时才实际撤回）。旧配置 `violation_enabled_groups` 非空时仍按旧列表判定（迁移兼容，老用户行为不漂移）。需单独关闭的群用 `group_overrides` 按群覆盖为 false。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `show_recall_notice` | bool | `true` | 撤回操作后在群里发送提示 |
| `mute_notice` | bool | `true` | 禁言 / 解禁后回复结果 |
| `reject_re_add` | bool | `false` | 踢人后自动拒绝该用户再次加群 |
| `auto_recall_keywords` | list | `[]` | Bot 发言自动撤回关键词列表（推荐按群覆盖） |
| `auto_recall_enabled_groups` | list | `[]` | 启用自动撤回的群 ID 列表（**留空 = 全群启用**，#192；`*` / `all` 表示全部，或填指定群号） |
| `enabled_groups` | list | `[]` | 启用违规检测的群号列表（**留空 = 全群启用**，#192；`*` / `all` 表示全部，或填指定群号；推荐按群覆盖） |
| `max_message_history` | int | `50` | 每群内存缓存的撤回消息历史条数（用于 /撤回 N 与 /撤回自身 N） |
| `join_reject_reason` | string | `"不满足加群条件"` | 加群申请自动拒绝时展示的默认理由（管理员可通过「拒绝 理由」自定义） |
| `join_audit_enabled` | bool | `true` | 加群申请自动审核总开关（关闭后违禁词/关键词自动审核都跳过；管理员手动审核不受影响） |
| `group_admin_admins` | list | `[]` | 可设置/取消群管理的专项管理员 QQ 列表（全局默认；按群覆盖优先级更高） |
| `banned_image_files` | file | `[]` | WebUI 上传违禁图片文件（自动计算 MD5 参与比对；#184；**全局配置**，需 AstrBot v4.13.0+） |
| `kick_recall_enabled` | bool | `false` | 踢人时自动撤回该成员最近消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian） |
| `kick_recall_count` | int | `10` | 踢人撤回消息条数（1-50，#145） |
| `link_whitelist` | list | `[]` | 全局链接白名单域名列表（#195，命中不检测/撤回/禁言；本群白名单用 `/添加链接白名单` 维护） |
| `blacklisted_users` | list | `[]` | 按群黑名单 QQ 列表（#194，存于 `group_overrides`，用 `/添加黑名单` 维护；黑名单用户加群自动拒绝） |
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

通过群内指令按群独立配置（推荐）。**管理指令（禁言时长/关键词/白名单等）直接在对应群内执行即可按群生效，无需 `/设置群配置`（#192）**：

```
/设置图片禁言时长 300          # 本群图片违规禁言 300 秒
/添加骂人关键词 笨蛋           # 本群骂人关键词
/添加白名单用户 123456         # 本群白名单
/开关违规通知 on
/添加自动撤回关键词 测试
/设置排名人数 20
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

按群覆盖的可配置 key 包括：基础配置（`show_recall_notice`、`auto_recall_keywords`、`auto_recall_enabled_groups`、`rank_top_n`、`report_notify_admins`、`join_approve_keywords`、`join_notify_admins`、`join_request_notify_in_group`、`enabled_groups`）+ 违规检测全部子项（`spam_*`、`profanity_*`、`ad_*`、`link_*`、`group_promotion_*`、`ban_duration`、`whitelist_users`、`admin_bypass`、`notify_on_violation`)+ 权限细分（`group_admin_admins`、`mute_kick_threshold`）+ 撤回历史（`max_message_history`）+ 踢人清历史（`kick_recall_enabled`、`kick_recall_count`）+ 语音违规检测开关（`voice_check_enabled`）。
> 语音转文字相关配置（`voice_check_provider_id`、`voice_asr_endpoint`、`voice_asr_api_key`、`voice_asr_model`、`voice_check_timeout`）为**全局配置**，不支持按群覆盖。
> `group_overrides` 内部存储项不再展示在 WebUI 配置页（#192 owner），按群覆盖功能不受影响，仍由各管理指令（禁言时长/关键词/白名单等）维护。
> 兼容旧配置三项 `violation_action` / `violation_mute_minutes` / `violation_enabled_groups` 不再展示在 WebUI 配置页（#192 owner，schema 以 invisible 保留兼容）；旧 config.json 残留值不会被删除，其中 `violation_enabled_groups` 仅在 `enabled_groups` 留空时用于运行时迁移兼容判定。

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
| 违禁词自动拒绝 | `enabled_groups` + `violation_keywords` | 命中违禁词自动拒绝（#129）；`enabled_groups` 留空 = 全群启用（#192） |
| 关键词自动同意 | `join_approve_keywords` | 验证消息命中关键词自动同意 |
| 群内提醒管理员 | `join_request_notify_in_group = true` | 申请消息发送到群内，引用回复同意/拒绝/拉黑（#57/#194） |
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
2. **专项权限管理员**：仅保留 `group_admin_admins`（可设/取消群管理），名单中的人执行 `/设管理` `/取消管理` 时不受群管理身份限制。头衔/踢人专项权限列表（`title_admins`/`kick_admins`）已移除（#219，owner 09-18），`/头衔` `/踢` 等操作由插件管理员（群管理员/群主）执行。

`group_admin_admins` 支持 **全局配置**（在插件配置 / WebUI 面板中设置，作为默认值）与 **按群覆盖**（`group_overrides`，优先级更高）。

> 插件管理员身份完全由 QQ 群管理员 / 群主自动识别，不再提供 `plugin_admins` 配置项与 `/设管` `/取管` 命令。如需向非群管理员用户授予设管理权限，使用 `group_admin_admins` 列表。

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

# 本群独立启用违规检测（在配置文件 group_overrides 或 WebUI 中设置）
# /开关违规通知 on
/开关语音检测 on

# 自怼（禁言自己 60 分钟）
/禁我 60

# 加群申请：设置自动同意关键词
/添加加群审核通过关键词 学生

# 违规检测：添加骂人关键词
/添加骂人关键词 笨蛋
```

---

## 常见问题

### 违规检测相关

**Q: 消息没有被撤回？**
A: ① 确认机器人有群管理员权限（撤回 + 禁言都需要）；② 检查该群是否已启用检测（`enabled_groups`，可用 `*` 表示全部群）；③ 检查日志中是否有撤回相关输出。

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

**⚠️ 依据 AGPL-3.0 许可证，本项目未复用其任何代码（包括正则片段、匹配逻辑），仅参考其功能设计文档；如需复用其代码，复用部分须继续以 AGPL-3.0 释出**
- [astrbot_plugin_group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian)（MIT）—— **功能对齐**：踢人撤回历史（#145），未复用其代码，自行实现

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供的强大插件框架！
---

> 本插件仅供学习与交流使用，请遵守 QQ / QQ 群的相关使用规范。


## 命令与默认行为补充说明（review#192）

- 命令别名：群友昵称（alias 别人昵称/群昵称/设群昵称/设群友昵称）、自己昵称（alias 改群昵称/改昵称）、群名（alias 群名称/改群名/修改群名）。若与 AstrBot 内置或其它插件同名指令冲突，请使用主名或停用冲突插件。
- 默认行为（owner 09-16 拍板）：`enabled_groups` 留空 = 不启用；启用需显式配置列表（* / all / 群号）或按群 bool 覆盖。`auto_recall_enabled_groups` 留空且无关键词 = 不启用；配了关键词未配 enabled 时兼容全群启用（#170）。

## 涉政关键词与骂人关键词配置（#204）

- 骂人关键词仅支持全局配置（插件配置 profanity_keywords）；添加/删除/查看骂人关键词指令已移除。
- 涉政关键词（political_keywords）为全局硬清单：命中即撤回+按 political_ban_duration 禁言，并提示「你因触碰涉政关键词(词语∶xx)被禁言xx分钟」；时长可用 /设涉政禁言时长 <分钟> 设置。
