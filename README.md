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
| `/清人 天数 [确认]` | 插件管理员 | 清理超过 N 天（1-30）未发言的普通成员（#225：默认预览候选，`/清人 N 确认` 才执行踢人；候选按最后发言时间升序、无发言记录排最后；不含群主/管理员/白名单，无发言记录按未发言计；逐个间隔 0.5s 踢出避免协议端限流） |
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
| `/撤回` | 插件管理员 | 引用撤回某条消息（#250：AstrBot 主人未任群管时，也可引用撤回 bot 自己发的消息） |
| `/撤回自身 N` | 插件管理员 | 撤回机器人最近发送的 N 条消息 |
| `/发群公告 内容` | 插件管理员 | 发送群公告 |
| `/改群头像`（别名 `/设群头像` `/设置群头像`） | 插件管理员 | 引用图片回复即可修改群头像 |
| `/宵禁` / `/解除宵禁` | 插件管理员 | 开启 / 关闭全群禁言 |
| `/禁我 [分钟]` | 任意成员 | 自怼（默认 10 分钟） |
| `/排名` | 任意成员 | 查看本群发言排名 |
| `/清除数据` | 插件管理员 | 清除本群发言计数 |
| `/举报` | 任意成员 | 举报群成员违规行为（@ 成员，或引用/回复其消息后发送 举报，#227） |
| `/添加群待办` | 插件管理员 | 引用消息设为群待办 |
| `/取消群待办` | 插件管理员 | 引用消息取消群待办 |
| `/加群申请待处理` | 插件管理员 | 查看本群未处理的加群申请列表 |
| `/群信息` | 任意成员 | 查看本群资料（名称/号/标签/人数；#276 标签优先经 `get_group_tags` 读取，失败回退 get_group_info 的 tags 字段） |
| `/群相册 相册名` | 插件管理员 | 引用图片消息上传到群相册 |
| `/群名称 新群名` | 插件管理员 | 修改本群名 |
| `/群标签 标签名` | 插件管理员 | 添加本群标签（部分 OneBot 实现仅支持预置列表中的标签且可能要求群主身份；set_group_tag 为 LLOneBot 等个别协议端的扩展接口，go-cqhttp/NapCat/Lagrange 不支持，失败时会提示）；不带参数时查看本群群标签（#276，走 `get_group_tags`） |
| `/添加违禁图片` | 插件管理员 | 引用图片消息加入违禁图列表（按 MD5 比对） |
| `/删除违禁图片 <md5前8位>` | 插件管理员 | 删除本群某张违禁图 |
| `/查看违禁图片` | 插件管理员 | 查看本群+全局违禁图列表（含 WebUI 上传） |
| `/添加加群审核通过关键词 <词>` | 插件管理员 | 添加加群审核自动通过关键词（#186） |
| `/删除加群审核通过关键词 <词>` | 插件管理员 | 删除加群审核自动通过关键词 |
| `/查看加群审核通过关键词` | 插件管理员 | 查看本群加群审核通过关键词 |

> ⚠️ 违禁图检测基于 MD5 比对，仅能阻止原图二次传播。攻击者对图片做轻微改动（裁剪/压缩/加噪）会绕过。建议作为快速预筛，主防御仍依赖 AI 鉴图。
>
> 🗑️ #265：配置中「违禁图片 MD5 列表」（banned_images）填空式录入项已移除，WebUI 无法再配置该项；违禁图统一通过 `/添加违禁图片`（按群覆盖）或 WebUI 上传（`banned_image_files`）维护。老用户 config.json 中残留的全局 banned_images 列表仍兼容生效（避免升级后行为漂移），建议逐步迁移到按群覆盖。

### 口语化指令（#254）

在群聊中 **@机器人** 后直接说人话即可触发 **禁言/解禁 / 踢人 / 设管理/取消管理 / 自我禁言**，无需带 `/` 命令前缀（权限与原命令一致）。可用配置项 `colloquial_enabled`（默认开，支持按群覆盖）一键关闭：

| 口语化用法 | 等价命令 | 说明 |
|------|---------|------|
| `@bot 禁言他 10分钟` / `@bot 把小明禁言半小时` / `@bot ban他` | `/禁言 @某人 10` | @到目标时无需写 QQ；支持网络用语 `ban`/`封禁`；时长支持中文，缺省 10 分钟 |
| `@bot 禁我 10分钟` / `@bot 把我禁言半小时` / `@bot 自怼` | `/禁我 10` | **任意成员可自禁**，无需管理员权限；时长规则同禁言，缺省 10 分钟，范围 1 分钟 ~ 30 天 |
| `@bot 给他解除禁言` / `@bot 把小明解禁了` / `@bot unban他` | `/解禁 @某人` | 解除指定成员的禁言 |
| `@bot 把张三踢了` / `@bot 踢出去` | `/踢 @某人` | 复用踢人清历史（开启 `kick_recall_enabled` 时） |
| `@bot 给李四上管理` / `@bot 设管理 @李四` | `/设管理 @某人` | 需插件管理员或群管理员权限 |
| `@bot 取消他管理` / `@bot 把李四下管理` | `/取消管理 @某人` | 需插件管理员或群管理员权限 |

- **总开关**：`colloquial_enabled`（默认 `true`）：关闭后仅识别带 `/` 前缀的标准命令，口语化请求不再触发。支持按群覆盖（`group_overrides[群号]["colloquial_enabled"]`）。
- **目标识别**（依次尝试）：① @ 提及目标；② 回复（引用）该成员的消息；③ 消息里的 5-12 位 QQ 号；④ `@名字` 文本。
- **中文时长解析**：支持阿拉伯数字与中文数字混用，以及分钟/小时/天/周/月、`半小时`、`一个半小时`、`两小时`、`一天`、`永久`/`无限` 等说法；统一换算为分钟，单次范围夹取 1 分钟 ~ 30 天，「永久」按 29 天 23 小时 59 分（OneBot 上限）处理。
- **提示与边界**：未找到目标、权限不足、bot 权限不足、目标为群主/管理员、时长无效（自动回落缺省）、重复操作等均返回清晰中文提示；命令形式的 `/禁言 @某人 30`、`/禁言 @某人 30分钟`、`/禁言 @某人 半小时` 也会解析时长，且不改变原有 `/禁言 @某人` = 10 分钟语义。

### 按群覆盖配置（#193）

每项按群配置都有独立指令（#193 已移除 `/设置群配置`），覆盖值写入 `group_overrides`，优先级高于全局配置：

| 命令 | 说明 |
|------|------|
| `/开关撤回提示 on/off` | 撤回操作群内提示（show_recall_notice） |
| `/开关禁言提示 on/off` | 禁言/解禁结果回复（mute_notice） |
| `/开关群名提示 on/off` | 修改群名结果回复（group_name_notice） |
| `/开关公告提示 on/off` | 发群公告结果回复（group_notice_notice，#233） |
| `/开关踢人拒加 on/off` | 踢人后拒绝再次加群（reject_re_add） |
| `/开关管理员豁免 on/off` | 管理员违规豁免（admin_bypass） |
| `/开关违规通知 on/off` | 违规群内通知（notify_on_violation） |
| `/开关加群申请提醒 on/off` | 加群申请群内提醒（join_request_notify_in_group） |
| `/开关加群自动审核 on/off` | 加群自动审核总开关（join_audit_enabled） |
| `/邀请通过 on/off` | 第三方邀请入群自动通过（invite_auto_approve，默认关，#241：仅对协议端扩展的「A 邀请 B」事件生效，黑名单成员仍被拦截，bot 被邀请入群不受影响） |
| `/开关踢人清历史 on/off` | 踢人时撤回历史消息（kick_recall_enabled） |
| `/开关语音检测 on/off` | 语音转文字违规检测（voice_check_enabled） |
| `/开关二维码撤回 on/off` | 二维码识别检测（qr_code_recall_enabled，默认关，#237：普通码仅撤回；拉群广告码/黄赌毒码撤回+禁言，需 openai_vision 审核 API） |
| `/开关不安全网站检测 on/off` | 不安全网站分析（link_safety_check_enabled，默认关，#272：内置黄赌毒特征/自定义关键词/目标站拦截服务器请求 → 撤回+禁言） |
| `/开关链接广告检测 on/off` | 链接目标页广告内容检测（ad_check_link_content，默认关，#274：文本未命中广告词时抓取链接目标页正文，命中同一份 ad_keywords 按广告处置） |
| `/设置排名人数 N` | 发言排名显示人数（rank_top_n，≥1） |
| `/设置踢人阈值 N` | 刷屏禁言转踢人的阈值（mute_kick_threshold，≥0） |
| `/设置消息历史条数 N` | 本地撤回消息缓存条数（max_message_history，≥10） |
| `/设置踢人清条数 N` | 踢人撤回消息条数（kick_recall_count，1-50） |
| `/设置拒绝理由 理由` | 加群自动拒绝理由（join_reject_reason） |
| `/加群自动拒绝关键词 添加 <词>` / `/删除 <词>` / `/查看` | 加群申请命中关键词即自动拒绝并把申请人拉黑（按群覆盖，全局配置 `join_reject_keywords`，#229） |
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
| 骂人 | AI 识别（`profanity_use_ai=true` 默认）或关键词匹配双模式，关键词可动态增删；AI 判定时可按严重程度分级禁言（`profanity_ban_duration_severity`，#243） | 开 |
| 广告 | 预设 24 个常见广告关键词（加群 / 加微信 / 代练 / 外挂 / 刷钻等），可动态增删 | 开 |
| 链接 | 匹配 http/https/www 等链接格式 | 关（`link_check_enabled`） |
| 群号推广 | 推广关键词（进群 / 加群 / 群号 / 入群 / 拉群 / 建群）+ 识别 5-12 位群号 | 开 |

> 白名单用户（`whitelist_users`）不受检测限制；管理员默认豁免（`admin_bypass`）；检测到违规后可选择群内通知（`notify_on_violation`）。

**AI 骂人分级禁言（#243）**

- 仅在 `profanity_use_ai=true` 且配置了 `api_endpoint` 时生效：提示词会要求模型在判定结果中额外返回 `severity`（`mild` / `medium` / `severe`）。
- **开关 `profanity_severity_enabled`（默认开）**：关闭后一律按 `profanity_ban_duration` 固定时长处理（模型仍照常检测，只是不使用分级）；可按群覆盖。
- 提示词可在插件配置 **`profanity_detection_prompt`** 中自定义（默认已预填内置提示词，留空同样走内置默认）。提示词里可写 `{text}` 占位符插入待检测文本；不写占位符时文本会追加在末尾。若自定义提示词不再要求返回 `severity`，则不会分级、一律按 `profanity_ban_duration` 处理。
- 命中 `profanity_ban_duration_severity` 分级表时按级别取禁言秒数（默认 mild 180 / medium 600 / severe 1800）；模型未返回、返回非法值或表中缺少该级别时，回退 `profanity_ban_duration`（默认 600 秒），与旧版行为一致。
- 关键词硬清单（`profanity_keywords` / 按群骂人关键词）命中不参与分级，仍用 `profanity_ban_duration`。
- 按群覆盖：编辑 `group_overrides[群号]["profanity_ban_duration_severity"]`（该表暂无独立指令）；`group_overrides` 不在 WebUI 配置页展示，见上方「按群覆盖」说明。

### 加群申请自动审核

加群申请验证流程（受总开关 `join_audit_enabled` 控制，关闭后仅保留管理员手动审核；黑名单用户始终直接拒绝）：

1. **黑名单直接拒绝**：申请人已在 `blacklisted_users`（`/添加黑名单`、引用回复「拉黑」都会写入）→ 直接拒绝，不检查任何关键词（#194）
2. **自动拒绝关键词并拉黑（#229）**：申请验证消息命中 `join_reject_keywords`（本群列表非空时优先，否则用全局列表）→ 自动拒绝并按 `join_reject_reason` 给出理由，同时**把申请人加入本群黑名单**，此后其再次申请直接命中黑名单分支，且不再发群内提醒
3. **违禁词自动拒绝**：申请验证消息命中 `violation_keywords` → 自动拒绝，并按 `join_reject_reason` 给出理由
4. **关键词自动同意**：验证消息命中 `join_approve_keywords` → 自动同意，并在该群发送通知「该用户触碰到加群审核通过词语，已自动同意！」（#186）
5. **群内提醒人工审核**：`join_request_notify_in_group=true` 时，申请信息发到群内（含昵称/QQ号/QQ等级/验证消息），管理员**引用回复「同意」或「拒绝 [理由]」或「拉黑」**即可完成审核（#189/#194）；回复「拉黑」= 拒绝申请 + 将该用户加入本群黑名单，此后其再次申请自动拒绝
6. **管理员私聊通知**：处理结果推送给 `join_notify_admins` 列表中的 QQ（列表为空即不通知任何人；#229 起不再有 `join_request_notify_enabled` 全局开关与 `/新人加群申请通知` 指令）

```
/加群申请待处理                 # 查看本群未处理的加群申请列表
/添加加群审核通过关键词 <词>      # 添加关键词（命中自动同意）
/删除加群审核通过关键词 <词>      # 删除关键词
/查看加群审核通过关键词           # 查看本群关键词
/加群自动拒绝关键词 添加 <词>     # 添加自动拒绝关键词（命中即拒绝 + 拉黑，#229）
/加群自动拒绝关键词 删除 <词>     # 删除自动拒绝关键词
/加群自动拒绝关键词 查看          # 查看本群/全局列表与当前生效来源
```

关键词配置示例（在群内执行）：

```
/添加加群审核通过关键词 学生
/加群自动拒绝关键词 添加 广告
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

> ⚠️ **默认行为（#192 owner 拍板，统一为「留空 = 全群启用」）**：`enabled_groups` **留空 = 全群启用**（违规检测，含刷屏/图片 AI 等；需收窄时配置列表：`*` / `all` / 群号，或按群 bool 覆盖）；`auto_recall_enabled_groups` **留空 = 全群启用**（命中 `auto_recall_keywords` 时才实际撤回，未配关键词则无命中）。旧配置 `violation_enabled_groups` 非空时仍按旧列表判定（迁移兼容，老用户行为不漂移）。需单独关闭的群用 `group_overrides` / `enabled_groups` 按群覆盖为 false。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `show_recall_notice` | bool | `true` | 撤回操作后在群里发送提示 |
| `mute_notice` | bool | `true` | 禁言 / 解禁后回复结果 |
| `colloquial_enabled` | bool | `true` | 启用口语化群管指令（#254）：@bot 后用自然语言触发禁言/解禁/踢人/设管理/取消管理；关闭后仅识别 `/` 前缀标准命令。推荐按群覆盖 |
| `group_name_notice` | bool | `true` | 修改群名成功后是否群内通知（关闭时仅失败提示；推荐按群覆盖） |
| `group_notice_notice` | bool | `true` | `/发群公告` 成功后是否回复回执（关闭时仅失败提示，#233；推荐按群覆盖） |
| `reject_re_add` | bool | `false` | 踢人后自动拒绝该用户再次加群 |
| `spam_exclude_pure_media` | bool | `false` | 刷屏检测排除纯媒体消息（仅含图片/表情包且无可见文本的消息不计入刷屏窗口，#260；推荐按群覆盖） |
| `auto_recall_keywords` | list | `[]` | Bot 发言自动撤回关键词列表（推荐按群覆盖） |
| `auto_recall_enabled_groups` | list | `[]` | 启用自动撤回的群 ID 列表（**留空 = 全群启用**，#192；`*` / `all` 表示全部，或填指定群号） |
| `enabled_groups` | list | `[]` | 启用违规检测的群号列表（**留空 = 全群启用**，#192；`*` / `all` 表示全部，或填指定群号；推荐按群覆盖） |
| `max_message_history` | int | `50` | 每群内存缓存的撤回消息历史条数（用于 /撤回 N 与 /撤回自身 N） |
| `join_reject_reason` | string | `"不满足加群条件"` | 加群申请自动拒绝时展示的默认理由（管理员可通过「拒绝 理由」自定义） |
| `join_audit_enabled` | bool | `true` | 加群申请自动审核总开关（关闭后违禁词/关键词自动审核都跳过；管理员手动审核不受影响） |
| `invite_auto_approve` | bool | `false` | 第三方邀请入群自动通过（#241：协议端上报 sub_type=invite 且被邀请人非机器人时自动同意，不走审核与群内提醒；黑名单成员仍被拒绝，bot 被邀请入群不受影响；推荐按群覆盖 `/邀请通过 on|off`） |
| `profanity_detection_prompt` | text | 内置默认提示词（已预填） | AI 骂人检测提示词（#243；支持 `{text}` 占位符，留空用内置默认；可按群覆盖） |
| `profanity_severity_enabled` | bool | `true` | AI 判骂人分级禁言开关（#243；关闭后一律按 `profanity_ban_duration` 固定时长；可按群覆盖） |
| `profanity_ban_duration_severity` | dict | `{"mild":180,"medium":600,"severe":1800}` | AI 判骂人按严重程度分级禁言时长（秒，#243；模型未返回/返回非法时回退 `profanity_ban_duration`；可按群覆盖） |
| `group_admin_admins_by_group` | dict | `{}` | **按群设置**可设置/取消群管理的专项管理员：`{"群号": ["QQ1","QQ2"]}`（命中该群用本群名单；群内 `/添加管理管理` / `/删除管理管理` 优先级更高；值也可写 `"10001,10002"`） |
| `banned_image_files` | file | `[]` | WebUI 上传违禁图片文件（自动计算 MD5 参与比对；#184；**全局配置**，需 AstrBot v4.13.0+；#265 起原「填空式 MD5 列表」banned_images 配置项已移除） |
| `qr_code_recall_enabled` | bool | `false` | 二维码识别检测（#237：图片审核通路额外用视觉模型识别二维码并分类内容——普通二维码（收款码/好友码/活动码等）仅撤回；拉群广告二维码按广告违规禁言、黄赌毒二维码按违规图片禁言（撤回+禁言+计数）；仅 api_type=openai_vision 时生效；模型/接口异常时按未命中放行（fail-open，误放行优于误撤回），失败会记录 error 日志；推荐按群覆盖 `/开关二维码撤回 on|off`） |
| `kick_recall_enabled` | bool | `false` | 踢人时自动撤回该成员最近消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian） |
| `kick_recall_count` | int | `10` | 踢人撤回消息条数（1-50，#145） |
| `link_whitelist` | list | `[]` | 全局链接白名单域名列表（#195，命中不检测/撤回/禁言；本群白名单用 `/添加链接白名单` 维护） |
| `ad_check_link_content` | bool | `false` | 链接目标页广告内容检测（#274：消息文本未命中广告关键词但含链接时，抓取目标页正文用同一份 `ad_keywords` 匹配，命中按广告处置（撤回+禁言）；抓取失败/无文本静默放行（fail-open），链接白名单不抓取；仅 `ad_check_enabled` 开启时生效；推荐按群覆盖 `/开关链接广告检测 on|off`） |
| `link_safety_check_enabled` | bool | `false` | 不安全网站检测（#272：对群消息中的链接做安全分析——内置黄赌毒特征/自定义关键词命中，或目标站拦截服务器请求时按不安全网站处置（撤回+禁言+计数）；检测失败/无命中放行（fail-open，误放行优于误禁言）；推荐按群覆盖 `/开关不安全网站检测 on|off`） |
| `link_safety_whitelist` | list | `["douyin.com","bilibili.com"]` | 不安全网站检测域名白名单（#272，含子域不参与检测，另复用 `link_whitelist`；可按群覆盖） |
| `link_safety_blocked_is_unsafe` | bool | `true` | 目标站拦截服务器请求时视为不安全（#272：403/451/超时/连接重置即处置；连续多次抓取失败疑似断网时自动熔断放行，恢复后继续） |
| `link_safety_ban_duration` | int | `600` | 不安全网站违规禁言时长（秒，#272；可按群覆盖） |
| `link_safety_keywords` | list | 预填黄赌毒常见词 | 不安全网站关键词（#272，与链接域名或目标页正文（不区分大小写）匹配，命中即处置；可按需增删） |
| `link_fetch_timeout` | int | `5` | 链接目标页抓取超时秒数（#272/#274 共用，1-15 异常值回退 5；单条消息最多探测 3 个链接） |
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

也可在机器人停止后直接编辑数据文件 `data/plugin_data/group_admin/runtime.json`（旧版 `config.json` 的数据会在首次启动时自动迁移过来），结构如下：

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

按群覆盖的可配置 key 包括：基础配置（`show_recall_notice`、`auto_recall_keywords`、`auto_recall_enabled_groups`、`rank_top_n`、`report_notify_admins`、`join_approve_keywords`、`join_notify_admins`、`join_request_notify_in_group`、`enabled_groups`）+ 违规检测全部子项（`spam_*`、`profanity_*`、`ad_*`、`link_*`、`group_promotion_*`、`ban_duration`、`whitelist_users`、`blacklisted_users`（#194）、`admin_bypass`、`notify_on_violation`)+ 权限细分（`mute_kick_threshold`）+ 撤回历史（`max_message_history`）+ 踢人清历史（`kick_recall_enabled`、`kick_recall_count`）+ 语音违规检测开关（`voice_check_enabled`）。
> 加群自动拒绝关键词（`join_reject_keywords`）支持按群覆盖：群内 `/加群自动拒绝关键词 添加|删除|查看` 维护本群列表，**本群列表非空时优先于全局列表**（#229）。
> 语音转文字相关配置（`voice_check_provider_id`、`voice_asr_endpoint`、`voice_asr_api_key`、`voice_asr_model`、`voice_check_timeout`）为**全局配置**，不支持按群覆盖。
> `group_overrides` 为**内部存储项，已从配置项 schema 移除、不在 WebUI 配置页展示**（#192/#219 owner），按群覆盖功能不受影响，仍由各管理指令（禁言时长/关键词/白名单等）维护；实际存储在 `data/plugin_data/group_admin/runtime.json`，需手动修改时请先停止机器人再编辑（运行中会被插件写回覆盖）。

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
| 关键词自动拒绝并拉黑 | `join_reject_keywords` / `/加群自动拒绝关键词 添加 <词>` | 验证消息命中即自动拒绝并把申请人加入本群黑名单（#229），本群列表优先于全局 |
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
2. **专项权限管理员**：通过 `group_admin_admins_by_group` **按群设置**（可设置/取消群管理），名单中的人执行 `/设管理` `/取消管理` 时不受群管理身份限制（#219 owner 09-18：头衔/踢人专项权限列表已移除，相应操作回归插件管理员）。

**按群设置名单（WebUI）**：`group_admin_admins_by_group` 可在插件配置里直接给每个群填名单，格式 `{"群号": ["QQ1","QQ2"]}`（值也支持 `"10001,10002"` 这种写法）。同时可用群内指令 `/添加管理管理 QQ` / `/删除管理管理 QQ`（旧命令 `/添加插件管理` `/删除插件管理` 保留为兼容别名，同样只操作该名单）维护本群名单，其优先级高于 WebUI 名单（命中即替换，不与上层合并）。群内 `/status` 可查看本群实际生效的名单。

> 插件管理员身份完全由 QQ 群管理员 / 群主自动识别，不再提供 `plugin_admins` 配置项与 `/设管` `/取管` 命令。如需向非群管理员用户授予设管理权限，使用 `group_admin_admins_by_group`（或群内 `/添加管理管理`）。

---

## 命令使用示例

```
# 禁言某成员 30 分钟
/禁言 @小明 30

# 口语化禁言（@bot 后自然语言，支持中文时长）
@bot 禁言 @小明 30分钟
@bot 把小明禁言 1小时
@bot 禁言他 半小时

# 口语化自禁言（任意成员可用，等价 /禁我）
@bot 禁我 30分钟
@bot 把我禁言 1小时
@bot 自怼

# 口语化解禁 / 踢人 / 设群管理 / 取消管理
@bot 给他解除禁言
@bot 把张三踢了
@bot 给李四上管理
@bot 取消他管理

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

# 本群独立启用违规检测（写 group_overrides，WebUI 不展示，见上方按群覆盖说明）
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
A: 调大 `spam_threshold` 和 `spam_time_window`，例如 10 条 / 20 秒更宽松；若只是纯图片/表情包被误判，可开启 `spam_exclude_pure_media`（#260，纯媒体消息不计入刷屏窗口）。

**Q: 表情包只发一次却被自动撤回？**
A: 已修复（#260）：Bot 发言自动撤回关键词只匹配「本次发送的可见文本」，纯图片/表情包消息不再被误撤回；若开启 `spam_exclude_pure_media` 还可让纯媒体不计入刷屏窗口。仍被撤回时请检查 `/查看自动撤回关键词` 与 `spam_check_enabled`。

**Q: 加群通知里昵称显示成 QQ 号 / 等级显示「未知」？**
A: 多数字段名兜底与失败标注见 #261：若协议端 `get_stranger_info` 未返回昵称/等级，通知会显示「获取失败(QQ号)」/「未知」并记录日志，需排查协议端实现与版本。

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

## 项目结构

插件采用「门面 + 服务层」分层架构：`main.py` 仅保留 `@register` 类与全部对外接口（`@filter` 命令 / 事件 / 钩子），业务逻辑下沉至插件私有子包 `cop/`（L0-L4 单向依赖、显式依赖注入、无全局单例）。

```
astrbot_plugin_gm/
├── main.py              # 门面：@register 类 + 全部 @filter 入口（约 2447 行）
├── metadata.yaml         # 插件元信息
├── _conf_schema.json     # 配置项说明
├── README.md             # 本文件
├── NOTICE                # 第三方代码声明（astrbot_plugin_group_moderation 移植）
├── LICENSE               # AGPL-3.0 License
├── requirements.txt      # Python 依赖（aiohttp）
├── docs/
│   └── ARCHITECTURE.md   # 架构说明（分层、依赖方向、模块职责、扩展与测试）
├── cop/                  # 插件私有子包（业务逻辑分层，20 个模块）
│   ├── compat.py / constants.py / text_utils.py          # L0 基础层
│   ├── json_store.py / config_store.py                   # L1 存储层
│   ├── permissions.py / message_parse.py / onebot_api.py / runtime.py  # L2 能力层
│   ├── history.py / stats.py / messaging.py              # L3a 领域服务
│   ├── conversational.py                                 # L3b 重复表情包 + 口语化指令
│   ├── join_review.py                                    # L3b 加群审核
│   ├── moderation/                                       # L3b 违规检测业务域
│   └── ...                                               # 其余分层模块
└── .github/              # GitHub 配置
```

> 注：`cop/message_parse.py` 内含 `MessageParser` 与 `TargetResolver`（原 `target_resolver.py` 已并入）；
> `cop/conversational.py` 内含 `ColloquialService` 与 `DupFaceService`（原 `colloquial.py`、`dup_face.py` 已并入）。
> 上述均为**物理归并**，对外类名 / 行为 / 分层契约不变。

> 完整目录树、分层依赖图、各模块职责与扩展/测试指引见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

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

**⚠️ 依据 AGPL-3.0 许可证，本项目未复用其任何代码（包括正则片段、匹配逻辑），仅参考其功能设计文档；如需复用其代码，复用部分须继续以 AGPL-3.0 释出**
- [astrbot_plugin_group_guardian](https://github.com/zcj-ui/astrbot_plugin_group_guardian)（MIT）—— **功能对齐**：踢人撤回历史（#145），未复用其代码，自行实现

感谢 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 提供的强大插件框架！
---

> 本插件仅供学习与交流使用，请遵守 QQ / QQ 群的相关使用规范。


## 命令与默认行为补充说明（review#192）

- 命令别名：群友昵称（alias 别人昵称/群昵称/设群昵称/设群友昵称）、自己昵称（alias 改群昵称/改昵称）、群名（alias 群名称/改群名/修改群名）。若与 AstrBot 内置或其它插件同名指令冲突，请使用主名或停用冲突插件。
- 默认行为（owner 拍板，统一为「留空 = 全群启用」）：`enabled_groups` 留空 = 全群启用；需收窄时配置列表（* / all / 群号）或按群 bool 覆盖。`auto_recall_enabled_groups` 留空 = 全群启用（`auto_recall_keywords` 为空则无命中、不产生撤回）。旧配置 `violation_enabled_groups` 非空时仍按旧列表判定（迁移兼容）。

## 涉政关键词与骂人关键词配置（#204）

- 骂人关键词仅支持全局配置（插件配置 profanity_keywords）；添加/删除/查看骂人关键词指令已移除。
- 涉政关键词（political_keywords）为全局硬清单：命中即撤回+按 political_ban_duration 禁言，并提示「你因触碰涉政关键词(词语∶xx)被禁言xx分钟」；时长可用 /设涉政禁言时长 <分钟> 设置。
