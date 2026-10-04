#267 新增加群号推广存在性验证（GET p.qlogo.cn 头像 MD5 比对腾讯默认群头像固定值
185ff6f0cfc14f3bb8b838288d7dcc3c）——命中关键词+格式的群号推广消息需进一步验证群号是否
真实存在，不存在的群号放行；可按群覆盖开关 `group_promotion_exists_check`（默认开）与
缓存 TTL `group_promotion_exists_cache_ttl`（默认 6 小时）；aiohttp 缺失/网络异常/无法
判定时保守放行，不误撤回误禁言。新增 cop/moderation/group_exists.py（GroupExistsProbe）
与 cop/constants.py 探测常量；`_check_group_promotion` 抽出 `_extract_promotion_group_numbers`
供 service.py 共用；语音检测链路同步接入存在性验证。无需附带默认头像图片文件。

#225 新增 `/清人 N` 命令——按 N 天未发言清理群成员（基于 OneBot `last_sent_time` 字段），
群主/管理员始终跳过；协议端不支持该字段时报「无法执行」。新增
`StatsService.clean_inactive_members()` 方法；`GM_COMMAND_NAMES` 新增「清人」。

#233 新增群公告发布后群内通知开关（`announce_notify`，默认开）——`/发群公告` 成功发布后
自动发送「📢 管理员已发布群公告，请各位成员注意查看」提醒；可按群覆盖，指令 `/开关公告通知`。

#237 新增图片二维码检测（pyzbar + Pillow）——检测图片中是否包含 QR 码，命中即撤回+禁言；
独立开关 `qr_check_enabled`（默认关），可按群覆盖；依赖 pyzbar + Pillow（requirements.txt
已声明，缺失时静默跳过）。新增 `_check_qr_code` 方法接入检测链路；配置项
`qr_ban_duration` 控制禁言时长；指令 `/开关二维码检测`。

#241 新增群员邀请自动通过（`invite_auto_approve`，默认开）——加群请求 `sub_type=invite` 时
在黑名单检查之后、关键词审核之前自动同意；受 `join_audit_enabled` 总开关与 `enabled_groups`
范围控制，可按群覆盖。`sub_type` 字段已由 #252 引入，无需额外协议判断。

0.01 新增已知功能(禁言，踢人，头衔等)

0.02-0.04 修复了已知问题，并添加了一些新功能

2.4.0 #254 支持口语化指令：@bot 后自然语言触发禁言/解禁/踢人/设管理/取消管理（含中文数字与时长解析，禁言支持网络用语 ban/封禁）；新增 colloquial_enabled 开关
2.4.1 纯重构（无功能变更）：main.py 由 5020 行瘦身至约 2594 行，业务逻辑下沉至插件私有子包 cop/，采用「门面 + 服务层」分层架构（含 L0-L4 依赖方向、显式依赖注入、无全局单例，消除循环依赖）；全部命令/事件/钩子等对外接口与配置键、数据文件结构保持不变；新增 docs/ARCHITECTURE.md 架构说明
2.5.0 修复与增强：合并  上游 #249 #250 #251 #252 #256 #257 #258（/禁言列表 兼容各协议端禁言字段并区分列表获取失败；加群通知昵称/QQ等级改走 get_stranger_info 正确通路；加群通知 message_id 改走 OneBot send_group_msg 并在拿不到 ID 时按通知内容兜底定位；加群审核透传原始 sub_type 并逐候选记录真实失败原因；bot 主人可引用撤回 bot 自身消息；/改群头像 补 /设群头像 /设置群头像 别名；/群标签 失败提示说明协议端支持范围）。另含：#261 加群通知昵称/等级在多字段兜底（nick/nickname/card/name + level）基础上，获取失败时明确标注「获取失败(QQ号)」并记录日志；#260 Bot 发言自动撤回关键词仅匹配本次发送的可见文本，纯图片/表情包不再因文件名/URL 误命中被撤回，新增 `spam_exclude_pure_media`（纯媒体不计入刷屏窗口，默认关）与图片审核命中诊断日志；#227 `/举报` 支持引用（回复）被举报成员的消息触发，无需 @ 成员（保留 @ 触发兜底）

2.5.0.1 修复 #249 #250 #251 #252 #256 #257 #258：/禁言列表 兼容各协议端禁言字段并区分列表获取失败；加群通知昵称/QQ等级改走 get_stranger_info 正确通路；加群通知 message_id 获取改走 OneBot send_group_msg、拿不到 ID 时也落待处理记录并支持按通知内容兜底定位（引用回复 /同意 即可生效）；加群审核透传原始 sub_type 并逐候选记录真实失败原因；bot 主人可引用撤回 bot 自身消息；/改群头像 补 /设群头像 /设置群头像 别名；/群标签 失败提示说明协议端支持范围。

2.5.1 修复 #265：加群审核群内提醒（【新人加群】通知）被重复发送两次。根因是 `_send_group_text` 只认 `{"data": {"message_id": ...}}` 一种响应形态，而 aiocqhttp 的 `bot.call_action("send_group_msg")` 成功时直接返回 data 本身（`{"message_id": ...}`），提取 message_id 失败后误判为发送失败并回退 `_send(event, ...)`，导致同一段文本发出两次（且 message_id 记为空的兜底路径）。现新增 `_extract_message_id` 统一兼容裸值 / 裸 data / 完整信封三种形态，并保证「只要判定发送成功就立即返回」，仅首次发送确实失败时才走其它通路与 `_send` 回退；顺带修正 #186 自动同意群内通知的同源重复发送问题，且 message_id 可正确写入待处理表，引用回复 /同意 定位更可靠。

2.5.2 解决 #229 新增加群申请关键词自动拒绝（全局配置 + 按群覆盖指令）并清理重复配置项：插件配置新增全局 `join_reject_keywords`——加群验证消息命中任一关键词即自动拒绝，按 `join_reject_reason` 给出理由，并把申请人加入本群黑名单，此后其再申请直接命中黑名单分支（#194）且不再发群内提醒；新增按群覆盖指令 `/加群自动拒绝关键词 添加 <关键词> | 删除 <关键词> | 查看`（本群列表非空时优先于全局；查看同时列出本群/全局列表与当前生效来源），判定顺序调整为 黑名单 → 自动拒绝关键词（拒绝+拉黑） → 违禁词拒绝 → 关键词同意 → 群内提醒；同时按 owner 对「圈出的两项加群通知配置略微重复」的判定删除 `join_request_notify_enabled`（#205 全局开关）及其指令 `/新人加群申请通知`，群内提醒统一由 `join_request_notify_in_group`（`/开关加群申请提醒`，按群生效）负责，需要彻底静音时清空 `join_notify_admins` / `/查看加群通知QQ` 列表即可。同步更新 `_conf_schema.json`、`cop/join_review.py`、`cop/constants.py`（GM_COMMAND_NAMES）、README 与 docs（interfaces_snapshot 命令与配置项清单）。
