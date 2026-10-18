# 群管插件接口快照（阶段0 基线固化）

> 由 `_gen_snapshot.py` AST 静态解析生成；命令绑定依赖 `handler.__module__ == 'data.plugins.mjy1113451_astrbot_plugin_gm.main'`。


## 1. 命令入口 `@filter.command`

**命令总数：99**（#278 新增 `清人`、`邀请通过`、`开关二维码撤回`，行号 95-97 追加于表尾；#272/#274 新增 `开关不安全网站检测`、`开关链接广告检测`，行号 98-99 追加于表尾）


| # | 命令名 | 说明 | 别名 | 方法 |
|---|--------|------|------|------|
| 1 | `status` | 查看插件配置 | — | `status_cmd` |
| 2 | `举报` | 举报群成员违规行为（需要引用消息） | — | `report_cmd` |
| 3 | `切换骂人检测模式` | 切换 AI 检测 / 关键词检测（按群生效） | — | `toggle_profanity_mode_cmd` |
| 4 | `删除举报通知QQ` | 删除接收举报通知的管理员QQ（按群生效） | — | `remove_report_notify_cmd` |
| 5 | `删除加群审核通过关键词` | 删除加群审核自动通过关键词（按群生效） | — | `remove_join_approve_keyword_cmd` |
| 6 | `删除加群通知QQ` | 删除加群请求通知管理员QQ（按群生效） | — | `remove_join_notify_cmd` |
| 7 | `删除广告关键词` | 删除广告关键词（按群生效） | — | `remove_ad_keyword_cmd` |
| 8 | `删除插件管理` | 按群移除专项权限管理员（兼容旧命令） | — | `remove_group_admin` |
| 9 | `删除白名单用户` | 从白名单移除用户（按群生效） | — | `remove_whitelist_user_cmd` |
| 10 | `删除管理管理` | 按群移除可设置/取消群管理的专项管理员 | — | `remove_group_admin_admin_cmd` |
| 11 | `删除自动撤回关键词` | 删除Bot发言自动撤回关键词（按群生效） | — | `remove_auto_recall_keyword_cmd` |
| 12 | `删除违禁图片` | 删除本群某张违禁图（/删除违禁图片 <md5前8位>） | — | `remove_banned_image_cmd` |
| 13 | `删除链接白名单` | 从链接白名单移除域名（按群生效） | — | `remove_link_whitelist_cmd` |
| 14 | `删除黑名单` | 将用户从本群黑名单移除 | — | `remove_blacklist_cmd` |
| 15 | `加群申请待处理` | 查看本群未处理的加群申请列表 | — | `pending_join_requests_cmd` |
| 16 | `发群公告` | 发送群公告 | — | `send_group_notice_cmd` |
| 17 | `取消管理` | 取消群管理员（支持批量+@；管理员可取消自己） | — | `unset_group_admin_cmd` |
| 18 | `取消群待办` | 引用消息取消群待办 | — | `delete_group_todo_cmd` |
| 19 | `取消设精` | 取消精华消息 | — | `cancel_essence_cmd` |
| 20 | `头衔` | 设置群头衔（@某人 头衔内容） | — | `set_group_title_cmd` |
| 21 | `宵禁` | 开启全群禁言 | — | `whole_ban_cmd` |
| 22 | `开关加群申请提醒` | 开关加群申请群内通知提醒（on/off，按群生效） | — | `toggle_join_notify_cmd` |
| 23 | `开关加群自动审核` | 开关加群申请自动审核总开关（on/off，按群生效） | — | `toggle_join_audit_cmd` |
| 24 | `开关撤回提示` | 开关撤回消息提示（on/off，按群生效） | — | `toggle_recall_notice_cmd` |
| 25 | `开关禁言提示` | 开关禁言/解禁回复结果（on/off，按群生效） | — | `toggle_mute_notice_cmd` |
| 26 | `开关管理员豁免` | 开关管理员/群主跳过违规检测（on/off，按群生效） | — | `toggle_admin_bypass_cmd` |
| 27 | `开关群名提示` | 开关修改群名结果回复（on/off，按群生效） | — | `toggle_group_name_notice_cmd` |
| 28 | `开关语音检测` | 开关语音消息转文字违规检测（on/off，按群生效） | — | `toggle_voice_check_cmd` |
| 29 | `开关踢人拒加` | 开关踢人后拒绝重新加群（on/off，按群生效） | — | `toggle_reject_re_add_cmd` |
| 30 | `开关踢人清历史` | 开关踢人时自动撤回消息（on/off，按群生效） | — | `toggle_kick_recall_cmd` |
| 31 | `开关违规通知` | 开关违规时群内通知（on/off，按群生效） | — | `toggle_notify_on_violation_cmd` |
| 32 | `开关链接检测` | 开启/关闭本群链接检测撤回（/开关链接检测 on|off） | — | `toggle_link_check_cmd` |
| 33 | `排名` | 查看本群发言排名 | — | `rank_cmd` |
| 34 | `撤回` | 撤回消息（/撤回 + 引用消息 / /撤回 @用户 N / /撤回 N） | — | `recall_cmd` |
| 35 | `撤回自身` | 撤回机器人最近发送的消息（/撤回自身 N） | — | `recall_self_cmd` |
| 36 | `改群头像` | 引用图片回复即可修改群头像 | — | `set_group_avatar_cmd` |
| 37 | `加群自动拒绝关键词` | 加群申请命中关键词自动拒绝并拉黑（添加\|删除\|查看，按群覆盖，#229） | — | `join_reject_keywords_cmd` |
| 38 | `查看举报通知QQ` | 查看本群接收举报通知的管理员QQ列表 | — | `list_report_notify_cmd` |
| 39 | `查看加群审核通过关键词` | 查看加群审核自动通过关键词列表（本群） | — | `list_join_approve_keywords_cmd` |
| 40 | `查看加群通知QQ` | 查看本群加群请求通知管理员QQ列表 | — | `list_join_notify_cmd` |
| 41 | `查看广告关键词` | 查看广告关键词列表（本群） | — | `list_ad_keywords_cmd` |
| 42 | `查看白名单` | 查看白名单用户列表（本群+全局） | — | `list_whitelist_cmd` |
| 43 | `查看群配置` | 查看本群生效的配置覆盖 | — | `view_group_config_cmd` |
| 44 | `查看自动撤回关键词` | 查看Bot发言自动撤回关键词列表（本群+全局） | — | `list_auto_recall_keywords_cmd` |
| 45 | `查看违禁图片` | 查看本群违禁图片列表 | — | `list_banned_images_cmd` |
| 46 | `查看违规统计` | 查看违规统计（默认全群；带 QQ 号查个人） | — | `view_violation_stats_cmd` |
| 47 | `查看链接白名单` | 查看链接白名单（本群+全局） | — | `list_link_whitelist_cmd` |
| 48 | `查看黑名单` | 查看本群黑名单 | — | `list_blacklist_cmd` |
| 49 | `添加举报通知QQ` | 添加接收举报通知的管理员QQ（按群生效） | — | `add_report_notify_cmd` |
| 50 | `添加加群审核通过关键词` | 添加加群审核自动通过关键词（按群生效） | — | `add_join_approve_keyword_cmd` |
| 51 | `添加加群通知QQ` | 添加加群请求通知管理员QQ（按群生效） | — | `add_join_notify_cmd` |
| 52 | `添加广告关键词` | 添加广告关键词（按群生效） | — | `add_ad_keyword_cmd` |
| 53 | `添加插件管理` | 按群添加专项权限管理员（兼容旧命令） | — | `add_group_admin` |
| 54 | `添加白名单用户` | 添加白名单用户（不受违规检测限制，按群生效） | — | `add_whitelist_user_cmd` |
| 55 | `添加管理管理` | 按群添加可设置/取消群管理的专项管理员 | — | `add_group_admin_admin_cmd` |
| 56 | `添加群待办` | 引用消息设为群待办 | — | `add_group_todo_cmd` |
| 57 | `添加自动撤回关键词` | 添加Bot发言自动撤回关键词（按群生效） | — | `add_auto_recall_keyword_cmd` |
| 58 | `添加违禁图片` | 引用图片消息加入违禁图列表 | — | `add_banned_image_cmd` |
| 59 | `添加链接白名单` | 添加链接白名单域名（按群生效） | — | `add_link_whitelist_cmd` |
| 60 | `添加黑名单` | 将用户加入本群黑名单（拒绝加群申请） | — | `add_blacklist_cmd` |
| 61 | `清用户历史` | 撤回某用户在本群的最近 N 条消息（/清用户历史 @某人 [N]） | — | `clear_user_history_cmd` |
| 62 | `清除数据` | 清除本群发言计数 | — | `clear_rank_cmd` |
| 63 | `清除群配置` | 清除本群所有覆盖 | — | `clear_group_config_cmd` |
| 64 | `禁我` | 禁言发送者自己（自我禁言/自怼）。当用户说『禁我、把我禁言、禁言我、自我禁言、自怼、我想安静一会儿』等要求禁言其本人的意图时调用，任意成员均可使用；格式：/禁我 [分钟]，默认10分钟，范围1~43200（30天） | — | `mute_self_cmd` |
| 65 | `禁言` | 禁言成员 | — | `mute_cmd` |
| 66 | `禁言列表` | 查看本群当前被禁言成员列表 | — | `mute_list_cmd` |
| 67 | `给我头衔` | 自设群头衔（/给我头衔 标题内容） | — | `self_title_cmd` |
| 68 | `群信息` | 查看本群资料（名称/号/标签/人数；#276 标签优先走 get_group_tags，失败回退 get_group_info 的 tags 字段） | — | `group_info_cmd` |
| 69 | `群友昵称` | 设置他人群昵称（@某人 或 QQ号 + 新昵称；群管/群主/插件管理员） | 别人昵称、群昵称、设群友昵称、设群昵称 | `set_other_card_cmd` |
| 70 | `群名` | 修改本群名称（/群名 新群名） | 修改群名、改群名、群名称 | `set_group_name_cmd` |
| 71 | `群标签` | 添加群标签（/群标签 标签名）；不带参数查看本群群标签（#276，走 get_group_tags） | — | `set_group_tag_cmd` |
| 72 | `群相册` | 引用图片上传到群相册（/群相册 相册名） | — | `group_album_upload_cmd` |
| 73 | `群违规检测状态` | 查看群违规检测插件状态 | — | `moderation_status_cmd` |
| 74 | `自己昵称` | 设置自己的群昵称 | 改昵称、改群昵称 | `set_self_card_cmd` |
| 75 | `解禁` | 解除禁言 | — | `unmute_cmd` |
| 76 | `解除宵禁` | 关闭全群禁言 | — | `unwhole_ban_cmd` |
| 77 | `设涉政禁言时长` | 设置涉政禁言时长（分钟，全局配置，#204） | — | `set_political_ban_duration_cmd` |
| 78 | `设管理` | 设置群管理员（支持批量+@） | — | `set_group_admin_cmd` |
| 79 | `设精` | 设置精华消息 | — | `essence_cmd` |
| 80 | `设置刷屏禁言时长` | 设置刷屏禁言时长（秒，按群生效） | — | `set_spam_ban_duration_cmd` |
| 81 | `设置图片禁言时长` | 设置图片违规禁言时长（秒，按群生效） | — | `set_image_ban_duration_cmd` |
| 82 | `设置广告禁言时长` | 设置广告禁言时长（秒，按群生效） | — | `set_ad_ban_duration_cmd` |
| 83 | `设置拒绝理由` | 设置加群申请自动拒绝理由（按群生效） | — | `set_reject_reason_cmd` |
| 84 | `设置排名人数` | 设置发言排名榜显示人数（按群生效） | — | `set_rank_top_n_cmd` |
| 85 | `设置消息历史条数` | 设置撤回消息历史缓存条数（按群生效） | — | `set_max_history_cmd` |
| 86 | `设置群号推广禁言时长` | 设置群号推广禁言时长（秒，按群生效） | — | `set_group_promotion_ban_duration_cmd` |
| 87 | `设置踢人清条数` | 设置踢人时自动撤回消息条数（按群生效） | — | `set_kick_recall_count_cmd` |
| 88 | `设置踢人阈值` | 设置禁言次数达阈值自动踢出（0=关闭，按群生效） | — | `set_mute_kick_threshold_cmd` |
| 89 | `设置链接禁言时长` | 设置链接禁言时长（秒，按群生效） | — | `set_link_ban_duration_cmd` |
| 90 | `设置骂人禁言时长` | 设置骂人禁言时长（秒，按群生效） | — | `set_profanity_ban_duration_cmd` |
| 91 | `踢` | 踢出群成员（支持批量+@） | — | `kick_cmd` |
| 92 | `重复表情包撤回` | 按群覆盖重复表情包自动撤回（开/关，#196） | — | `toggle_dup_face_recall_cmd` |
| 93 | `鞭尸` | 长期禁言被@的人（29天23小时59分） | — | `whip_corpse_cmd` |
| 94 | `开关公告提示` | 开关发群公告结果回复（on/off，按群生效，#233） | — | `toggle_group_notice_notice_cmd` |
| 95 | `清人` | 清理 N 天未发言成员（/清人 天数 [确认]，1-30） | — | `purge_inactive_cmd` |
| 96 | `邀请通过` | 开关第三方邀请入群自动通过（on/off，按群生效，#241） | — | `toggle_invite_auto_approve_cmd` |
| 97 | `开关二维码撤回` | 开关二维码识别自动撤回（on/off，按群生效，#237） | — | `toggle_qr_code_recall_cmd` |
| 98 | `开关不安全网站检测` | 开关不安全网站检测（on/off，按群生效，#272） | — | `toggle_link_safety_cmd` |
| 99 | `开关链接广告检测` | 开关链接广告检测（on/off，按群生效，#274） | — | `toggle_ad_link_content_cmd` |

### 别名明细（命令名 → 别名集合）

- `群友昵称`（4 个别名）：`别人昵称`、`群昵称`、`设群友昵称`、`设群昵称`
- `群名`（3 个别名）：`修改群名`、`改群名`、`群名称`
- `自己昵称`（2 个别名）：`改昵称`、`改群昵称`

**带别名命令统计**：3 个


### 重点核对项

- `群友昵称`：命令名 1 + 别名 4 = **5 个名称**（期望 4）：`别人昵称`、`群昵称`、`设群友昵称`、`设群昵称`

- `自己昵称`：命令名 1 + 别名 2 = **3 个名称**（期望 2）：`改昵称`、`改群昵称`

- `群名`：命令名 1 + 别名 3 = **4 个名称**（期望 4）：`修改群名`、`改群名`、`群名称`


## 2. 事件钩子

### 2.1 `@filter.event_message_type`（2 个）

- `on_group_message` ← `filter.EventMessageType.GROUP_MESSAGE`
- `on_group_event` ← `filter.EventMessageType.ALL`

### 2.2 `@filter.after_message_sent`（1 个）

- `after_message_sent`


**配置键总数：73**


| # | key | type | default |
|---|-----|------|---------|
| 1 | `show_recall_notice` | bool | `true` |
| 2 | `mute_notice` | bool | `true` |
| 3 | `colloquial_enabled` | bool | `true` |
| 4 | `group_name_notice` | bool | `true` |
| 5 | `group_notice_notice` | bool | `true` |
| 6 | `reject_re_add` | bool | `false` |
| 7 | `auto_recall_keywords` | list | `[]` |
| 8 | `auto_recall_enabled_groups` | list | `[]` |
| 9 | `dup_face_recall_enabled` | bool | `false` |
| 10 | `qr_code_recall_enabled` | bool | `false` |
| 11 | `report_notify_admins` | list | `[]` |
| 12 | `rank_top_n` | int | `10` |
| 13 | `join_approve_keywords` | list | `[]` |
| 14 | `join_reject_keywords` | list | `[]` |
| 15 | `join_notify_admins` | list | `[]` |
| 16 | `join_request_notify_in_group` | bool | `false` |
| 17 | `join_reject_reason` | string | `"不满足加群条件"` |
| 18 | `join_audit_enabled` | bool | `true` |
| 19 | `invite_auto_approve` | bool | `false` |
| 20 | `group_admin_admins_by_group` | dict | `{}` |
| 21 | `api_type` | string | `"openai_vision"` |
| 22 | `api_endpoint` | string | `""` |
| 23 | `api_key` | string | `""` |
| 24 | `model_name` | string | `"gpt-4o"` |
| 25 | `detection_prompt` | text | `""` |
| 26 | `threshold` | float | `0.7` |
| 27 | `check_porn` | bool | `true` |
| 28 | `check_sexy` | bool | `true` |
| 29 | `enabled_groups` | list | `[]` |
| 30 | `spam_check_enabled` | bool | `true` |
| 31 | `spam_threshold` | int | `5` |
| 32 | `spam_time_window` | int | `10` |
| 33 | `spam_exclude_pure_media` | bool | `false` |
| 34 | `spam_ban_duration` | int | `600` |
| 35 | `profanity_check_enabled` | bool | `true` |
| 36 | `profanity_use_ai` | bool | `true` |
| 37 | `profanity_detection_prompt` | text | `"你是严格的内容审核助手。请判断以下文本是否包含骂人、侮辱、人身攻击。\n同时评估严重程度 severity，取值...` |
| 38 | `profanity_ban_duration` | int | `600` |
| 39 | `profanity_severity_enabled` | bool | `true` |
| 40 | `profanity_ban_duration_severity` | dict | `{"mild": 180, "medium": 600, "severe": 1800}` |
| 41 | `political_keywords` | list | `[]` |
| 42 | `political_ban_duration` | int | `600` |
| 43 | `profanity_keywords` | list | `["傻逼", "操你妈", "妈的", "他妈的", "草你妈", "艹你妈", "你妈死了", "去你妈的", ...` |
| 44 | `ad_check_enabled` | bool | `true` |
| 45 | `ad_ban_duration` | int | `600` |
| 46 | `ad_keywords` | list | `["加群", "加微信", "加QQ", "联系我", "私聊", "代练", "代打", "刷钻", "刷币",...` |
| 47 | `link_check_enabled` | bool | `false` |
| 48 | `link_ban_duration` | int | `600` |
| 49 | `link_whitelist` | list | `[]` |
| 50 | `blacklisted_users` | list | `[]` |
| 51 | `group_promotion_check_enabled` | bool | `true` |
| 52 | `group_promotion_ban_duration` | int | `600` |
| 53 | `ban_duration` | int | `600` |
| 54 | `whitelist_users` | list | `[]` |
| 55 | `banned_image_files` | file | `[]` |
| 56 | `admin_bypass` | bool | `true` |
| 57 | `notify_on_violation` | bool | `true` |
| 58 | `max_message_history` | int | `50` |
| 59 | `kick_recall_enabled` | bool | `false` |
| 60 | `kick_recall_count` | int | `10` |
| 61 | `voice_check_enabled` | bool | `false` |
| 62 | `voice_check_provider_id` | string | `""` |
| 63 | `voice_asr_endpoint` | string | `""` |
| 64 | `voice_asr_api_key` | string | `""` |
| 65 | `voice_asr_model` | string | `""` |
| 66 | `voice_check_timeout` | int | `15` |
| 67 | `ad_check_link_content` | bool | `false` |
| 68 | `link_safety_check_enabled` | bool | `false` |
| 69 | `link_safety_whitelist` | list | `["douyin.com","bilibili.com"]` |
| 70 | `link_safety_blocked_is_unsafe` | bool | `true` |
| 71 | `link_safety_ban_duration` | int | `600` |
| 72 | `link_safety_keywords` | list | 预填黄赌毒常见词 |
| 73 | `link_fetch_timeout` | int | `5` |

## 4. 数据文件路径与 data_dir 推导

```python
try:
    from astrbot.api.star import StarTools
    self.data_dir = StarTools.get_data_dir() / "group_admin"
except ImportError:
    self.data_dir = Path(os.getcwd()) / "data" / "group_admin"

self.config_path  = self.data_dir / "config.json"    # 旧版迁移用，迁移后重命名 .migrated.bak
self.runtime_path = self.data_dir / "runtime.json"   # group_overrides/groups/pending_join_requests
self.stats_path   = self.data_dir / "stats.json"     # 发言计数
self.reports_path = self.data_dir / "reports.json"   # 举报待处理
```

> 框架配置另存于 `data/config/<插件目录名>_config.json`（AstrBotConfig 管理），与上述本地 JSON 分离。


## 5. 校验结论

- `GM_COMMAND_NAMES` 项数：104（#277 记 99；#278 新增 3 项后为 102；#272/#274 新增 `开关不安全网站检测`、`开关链接广告检测` 后为 104）
- AST 解析 `@filter.command` 项数：99（#277 记 94；#278 新增 3 个后为 97；#272/#274 新增 2 个后为 99）
- 仅在 GM_COMMAND_NAMES、不在装饰器：['别人昵称', '改群昵称', '群名称', '群昵称', '设群友昵称']
- 仅在装饰器、不在 GM_COMMAND_NAMES：['开关链接检测', '重复表情包撤回']
- 一致：否（差额为上两行的既有差异，#278 未改动）
