from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain, At

try:
    from astrbot.api.message import MessageChain
except ImportError:
    try:
        from astrbot.api.message_components import MessageChain
    except ImportError:
        MessageChain = None

import json
import os
import re  # 用于群相册命令前缀解析、编号提取等
import time
import asyncio
import base64
import hashlib
from collections import defaultdict
from pathlib import Path

try:
    import aiohttp
except ImportError:
    aiohttp = None


def _parse_qq_list(text: str) -> list:
    """从文本中提取所有合法的QQ号（5-12位数字）。"""
    return list({m.group(1) for m in re.finditer(r"(\d{5,12})", text or "")})


@register(
    "group_admin",
    "YourName",
    "QQ群群管插件 - 禁言/踢人/头衔/精华/撤回/群公告/关键词撤回/违规检测/排名",
    "2.4.0",
    "https://github.com/mjy1113451/astrbot_plugin_gm"
)
class GroupAdminPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        try:
            from astrbot.api.star import StarTools
            self.data_dir = StarTools.get_data_dir() / "group_admin"
        except ImportError:
            self.data_dir = Path(os.getcwd()) / "data" / "group_admin"

        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self.config_path = self.data_dir / "config.json"
        self.stats_path = self.data_dir / "stats.json"
        self.reports_path = self.data_dir / "reports.json"
        self.config = self.load_config()
        # 合并 AstrBot 框架注入的 WebUI 配置（修复 #180）。
        # AstrBot star_manager 会尝试以 config=<AstrBotConfig> 实例化插件；旧版插件
        # __init__ 不接收该参数，会被框架 except 退回只传 context，导致 WebUI 面板配置
        # 全部失效（如 mute_notice=False 不生效）。本插件接收后：
        #   - AstrBotConfig 是 dict 子类，直接取其键值；
        #   - 全局键以 WebUI 注入为准（WebUI 才能改全局配置，本地 config.json 的全局键
        #     只是历史默认值兜底），group_overrides（按群覆盖）保留本地。
        if config is not None:
            ui_config = config if isinstance(config, dict) else {}
            local_overrides = (self.config.get("group_overrides") or {}).copy()
            self.config.update(ui_config)
            if local_overrides:
                self.config["group_overrides"] = {
                    **self.config.get("group_overrides", {}),
                    **local_overrides,
                }
        self.stats = self.load_json(self.stats_path, {"groups": {}})
        self.reports = self.load_json(self.reports_path, {"pending": []})
        self._msg_save_counter = 0  # #152：发言计数批量持久化计数器

        # 群违规检测运行时状态（#109 PR #1 合并自参考插件）
        # spam_records[(group_id, user_id)] -> [timestamp, ...]
        # 仅内存，进程重启清空（与参考插件行为一致）
        self.spam_records = defaultdict(list)

        # 撤回消息历史（对齐 astrbot_plugin_batchrecall，修复 #117 #118 #122）：
        # 结构：message_history[group_id] -> list[(message_id, content_preview, timestamp, sender_id, sender_name, is_bot)]
        # 最新消息在列表最前面，编号从 1 开始；仅记录本进程启动后经过监听的消息，重启前历史不可恢复。
        # 用户发送的插件指令消息不记录（避免编号偏移）；bot 自身消息在 after_message_sent 中记录。
        self.message_history: dict = {}
        self.max_history = max(1, int(self.config.get("max_message_history", 50) or 50))

    # ===================== 通用 IO =====================

    def load_json(self, path: Path, default):
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载 {path.name} 失败: {e}")
        return default

    def save_json(self, path: Path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存 {path.name} 失败: {e}")

    def _get_default_config(self) -> dict:
        return {
            "show_recall_notice": True,
            "mute_notice": True,
            "reject_re_add": False,
            "groups": {},
            # 按操作类型分别配置管理员（#34 权限系统重构）
            "title_admins": [],
            "group_admin_admins": [],
            "kick_admins": [],
            # 关键词自动撤回（#46）
            "auto_recall_keywords": [],
            "auto_recall_enabled_groups": [],
            # 违规检测（#19）
            "violation_keywords": [],
            "violation_action": "none",
            "violation_mute_minutes": 10,
            "violation_enabled_groups": [],
            # 举报（#21）
            "report_notify_admins": [],
            # 群公告与排名（#16, #29）
            "rank_top_n": 10,
            # 禁言次数达到阈值后自动踢出（#103），0 表示关闭
            "mute_kick_threshold": 0,
            # 加群请求关键词同意（#27 增强）
            "join_approve_keywords": [],
            "join_notify_admins": [],
            # 加群申请群内提醒（#57）
            "join_request_notify_in_group": False,
            "pending_join_requests": {},
            "join_reject_reason": "不满足加群条件",
            # #155：加群申请审核总开关（默认启用），支持按群覆盖
            "join_audit_enabled": True,
            # #74 配置按群独立（保留全局默认值）
            "group_overrides": {},
            # ====== 群违规检测（合并自 astrbot_plugin_group_moderation） ======
            # AI 审核 API（图片 / 骂人 AI 检测共用）
            "api_type": "openai_vision",
            "api_endpoint": "",
            "api_key": "",
            "model_name": "gpt-4o",
            "detection_prompt": "",
            "threshold": 0.7,
            "check_porn": True,
            "check_sexy": True,
            # 监控群组（* 或 all 表示全部启用；为空表示不监控；可按群覆盖为 bool）
            "enabled_groups": [],
            "spam_check_enabled": True,
            "spam_threshold": 5,
            "spam_time_window": 10,
            "spam_ban_duration": 600,
            "profanity_check_enabled": True,
            "profanity_use_ai": True,
            "profanity_ban_duration": 600,
            "profanity_keywords": [
                "傻逼", "操你妈", "妈的", "他妈的", "草你妈", "艹你妈",
                "你妈死了", "去你妈的", "狗日的", "王八蛋", "畜生", "杂种",
                "贱人", "婊子",
            ],
            "ad_check_enabled": True,
            "ad_ban_duration": 600,
            "ad_keywords": [
                "加群", "加微信", "加QQ", "联系我", "私聊", "代练", "代打",
                "刷钻", "刷币", "外挂", "辅助", "出售", "转让", "低价",
                "优惠", "促销", "折扣", "代购", "微商", "兼职", "赚钱",
                "日赚", "月入", "进群",
            ],
            "link_check_enabled": False,
            "link_ban_duration": 600,
            "group_promotion_check_enabled": True,
            "group_promotion_ban_duration": 600,
            "ban_duration": 600,
            "whitelist_users": [],
            "admin_bypass": True,
            "notify_on_violation": True,
            # #162：用户自定义违禁图片（按 MD5 比对），支持按群覆盖
            "banned_images": [],
            # ====== 撤回消息历史（对齐 astrbot_plugin_batchrecall，修复 #122） ======
            "max_message_history": 50,
            # ====== 踢人自动撤回该成员近期消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian） ======
            "kick_recall_enabled": False,
            "kick_recall_count": 10,
            # ====== 语音转文字违规检测（#128） ======
            "voice_check_enabled": False,
            "voice_check_provider_id": "",
            "voice_asr_endpoint": "",
            "voice_asr_api_key": "",
            "voice_asr_model": "",
            "voice_check_timeout": 15,
        }

    def load_config(self) -> dict:
        default_config = self._get_default_config()
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    for key, value in default_config.items():
                        if key not in saved:
                            saved[key] = value
                    if "groups" not in saved:
                        saved["groups"] = {}
                    return saved
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                return default_config
        return default_config

    def save_config(self):
        self.save_json(self.config_path, self.config)

    def save_stats(self):
        self.save_json(self.stats_path, self.stats)

    def save_reports(self):
        self.save_json(self.reports_path, self.reports)

    # ===================== 工具方法 =====================

    def _is_authorized(self, raw: dict, user_id: str = "") -> bool:
        """是否具备插件管理权限：仅 QQ 群管理员 + QQ 群主。"""
        return self._is_group_admin_or_owner(raw)

    def get_group_setting(self, group_id: str, key: str, default=None):
        """按群读取配置项，先查 group_overrides[群号][key]，否则用全局配置/默认值。"""
        overrides = self.config.get("group_overrides", {}).get(str(group_id), {})
        if key in overrides:
            return overrides[key]
        return self.config.get(key, default)

    def _is_group_owner(self, raw: dict) -> bool:
        role = raw.get("sender", {}).get("role", "")
        return role == "owner"

    def _is_group_admin(self, raw: dict) -> bool:
        role = raw.get("sender", {}).get("role", "")
        return role in {"admin", "owner"}

    def _is_group_admin_or_owner(self, raw: dict) -> bool:
        return self._is_group_admin(raw) or self._is_group_owner(raw)

    def _is_sender_group_admin_only(self, raw: dict) -> bool:
        return raw.get("sender", {}).get("role", "") == "admin"

    def _sender_has_special_title(self, raw: dict) -> bool:
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        for key in ("title", "special_title"):
            value = str(sender.get(key, "")).strip()
            if value:
                return True
        return False

    def _get_group_override_list(self, group_id: str, key: str) -> list:
        overrides = self.config.setdefault("group_overrides", {})
        gconf = overrides.setdefault(str(group_id), {})
        value = gconf.setdefault(key, [])
        if not isinstance(value, list):
            value = [value] if value else []
            gconf[key] = value
        return value

    def _add_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        admins = self._get_group_override_list(group_id, key)
        added = []
        for qq in qq_list:
            qq = str(qq)
            if qq and qq not in [str(x) for x in admins]:
                admins.append(qq)
                added.append(qq)
        if added:
            self.save_config()
        return added

    def _remove_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        admins = self._get_group_override_list(group_id, key)
        removed = []
        for qq in qq_list:
            qq = str(qq)
            for item in list(admins):
                if str(item) == qq:
                    admins.remove(item)
                    removed.append(qq)
                    break
        if removed:
            self.save_config()
        return removed

    def has_title_admin_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        uid = str(user_id)
        title_admins = [str(x) for x in self.get_group_setting(group_id, "title_admins", [])]
        if uid in title_admins:
            return True
        if self._is_group_admin_or_owner(raw):
            return True
        return False

    def has_kick_admin_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        uid = str(user_id)
        kick_admins = [str(x) for x in self.get_group_setting(group_id, "kick_admins", [])]
        if uid in kick_admins:
            return True
        if self._is_group_admin_or_owner(raw):
            return True
        return False

    def has_group_admin_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        uid = str(user_id)
        ga_admins = [str(x) for x in self.get_group_setting(group_id, "group_admin_admins", [])]
        if uid in ga_admins:
            return True
        if self._is_group_admin_or_owner(raw):
            return True
        return False

    def _get_raw_message(self, event: AstrMessageEvent):
        """Robustly extract the raw message dict from the event."""
        try:
            return event.message_obj.raw_message
        except Exception:
            pass
        return getattr(event, "raw_message", None)

    def _parse_qq(self, text: str) -> str:
        nums = _parse_qq_list(text)
        return nums[0] if nums else ""

    def _extract_at_qq(self, raw: dict) -> str:
        """Extract first QQ number from At components in the raw message."""
        if not raw:
            return ""
        for seg in raw.get("message", []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq:
                    return qq
        return ""

    def _extract_at_qqs(self, raw: dict) -> list:
        """Extract all QQ numbers from At components in the raw message."""
        if not raw:
            return []
        qqs = []
        for seg in raw.get("message", []):
            if isinstance(seg, dict) and seg.get("type") == "at":
                qq = str(seg.get("data", {}).get("qq", ""))
                if qq and qq not in qqs:
                    qqs.append(qq)
        return qqs

    def _extract_image_url(self, event: AstrMessageEvent) -> str:
        """从 event 的消息链中提取第一张图片的 URL。"""
        try:
            chain = getattr(event.message_obj, "message", None) or getattr(event, "message", None)
            if chain is None:
                return ""
            # AstrBot 的 message chain 可能是 MessageChain 或 list
            if hasattr(chain, "chain"):
                segs = chain.chain
            elif isinstance(chain, (list, tuple)):
                segs = chain
            else:
                segs = []
            for seg in segs:
                # 兼容不同的 Image 表示
                if isinstance(seg, dict):
                    if seg.get("type") == "image":
                        return seg.get("data", {}).get("url") or seg.get("data", {}).get("file", "")
                    continue
                if getattr(seg, "type", None) == "image":
                    return getattr(seg, "url", "") or getattr(seg, "file", "")
        except Exception as e:
            logger.error(f"提取图片URL失败: {e}")
        return ""

    def _build_text(self, text: str, at: str = None):
        if at:
            return [Plain(text), At(qq=at)] if text else [At(qq=at)]
        return [Plain(text)]

    async def _send(self, event: AstrMessageEvent, message_list):
        try:
            if hasattr(event, "send"):
                if MessageChain is not None:
                    await event.send(MessageChain(message_list))
                else:
                    await event.send(message_list)
                return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
        return False

    def _action_result_success(self, result) -> bool:
        """把 OneBot 调用返回值规整为布尔成功/失败。"""
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            status = str(result.get("status", "")).lower()
            if status in {"failed", "error"}:
                return False
            retcode = result.get("retcode")
            if retcode is not None:
                try:
                    return int(retcode) == 0
                except (TypeError, ValueError):
                    return False
            if status in {"ok", "async"}:
                return True
        return bool(result)

    async def _execute_action(self, event: AstrMessageEvent, action: str, return_raw: bool = False, **params):
        """调用 OneBot API。
        优先尝试 event.bot.call_action（AstrBot 推荐方式），
        其次 fallback 到 self.context.{action} 和 event.{action}。
        默认返回 True/False；return_raw=True 时返回 API 原始结果（用于查询类 API）。
        """
        # 参数转换：group_id / user_id / message_id 转为 int（OneBot 要求）
        for k in ("group_id", "user_id", "message_id"):
            if k in params and isinstance(params[k], str) and params[k].isdigit():
                params[k] = int(params[k])

        bot = getattr(event, "bot", None)
        if bot is not None:
            call = getattr(bot, "call_action", None)
            if callable(call):
                try:
                    result = await call(action, **params)
                    if return_raw:
                        return result
                    return self._action_result_success(result)
                except Exception as e:
                    logger.error(f"bot.call_action({action}) 失败: {e}")
            api = getattr(bot, "api", None)
            if api is not None:
                call = getattr(api, "call_action", None)
                if callable(call):
                    try:
                        result = await call(action, **params)
                        if return_raw:
                            return result
                        return self._action_result_success(result)
                    except Exception as e:
                        logger.error(f"bot.api.call_action({action}) 失败: {e}")
        handler = getattr(self.context, action, None)
        if callable(handler):
            try:
                result = await handler(**params)
                if return_raw:
                    return result
                return self._action_result_success(result)
            except Exception as e:
                logger.error(f"调用 {action} 失败: {e}")
        if hasattr(event, action):
            handler = getattr(event, action)
            if callable(handler):
                try:
                    result = await handler(**params)
                    if return_raw:
                        return result
                    return self._action_result_success(result)
                except Exception as e:
                    logger.error(f"调用 event.{action} 失败: {e}")
        return None if return_raw else False

    def _get_reply_id(self, event: AstrMessageEvent):
        """提取被引用/回复的消息 ID。优先从 message_obj，回退 raw message 字段。"""
        mo = getattr(event, "message_obj", None)
        if mo:
            for attr in ("reply_id", "quote_id"):
                v = getattr(mo, attr, None)
                if v:
                    return str(v)
        # 尝试从 raw message 的 segment 中找 Reply 类型
        raw = self._get_raw_message(event)
        if isinstance(raw, dict):
            for seg in raw.get("message", []) or []:
                if isinstance(seg, dict):
                    t = seg.get("type")
                    if t in ("reply", "quote"):
                        data = seg.get("data", {})
                        rid = data.get("id") or data.get("message_id")
                        if rid:
                            return str(rid)
                    if t == "text" and isinstance(seg.get("data", {}).get("text", ""), str):
                        # 部分 OneBot 引用消息嵌在 text 中
                        pass
        return None

    # ===================== OneBot API 封装 =====================

    async def _recall_message(self, event: AstrMessageEvent, message_id: str):
        """撤回消息。OneBot 标准 API 名为 delete_msg。"""
        return await self._execute_action(event, "delete_msg", message_id=message_id)

    async def _set_group_admin(self, event: AstrMessageEvent, group_id: str, qq: str, enable: bool):
        return await self._execute_action(event, "set_group_admin", group_id=group_id, user_id=qq, enable=enable)

    async def _set_group_title(self, event: AstrMessageEvent, group_id: str, qq: str, title: str):
        """设置群头衔。OneBot v11 set_group_special_title 接口。
        注意：不传 duration 参数（属于 set_group_ban 的参数，传了会导致 NapCatQQ 等静默失败）。
        """
        return await self._execute_action(event, "set_group_special_title",
                                          group_id=group_id, user_id=qq, special_title=title)

    async def _clear_group_title(self, event: AstrMessageEvent, group_id: str, qq: str) -> bool:
        """清空群头衔。每步调用后用 get_group_member_info 读回 title 字段校验
        是否真的清空，避免 OneBot 实现返回成功但实际未清空（#111 #119）。

        校验严格判断 title 是否为空字符串 / 字段缺失，不能 strip 后判空——
        否则单空格 " " 会被误判为已清空，导致实际仍存在空格头衔（#119）。
        """
        async def _verify() -> bool:
            info = await self._execute_action(
                event, "get_group_member_info", return_raw=True,
                group_id=group_id, user_id=qq, no_cache=True,
            )
            if isinstance(info, dict):
                data = info.get("data") or info
                after = data.get("title")
                if after is None:
                    after = data.get("special_title")
                # 严格判空：必须是空字符串或字段缺失；空格、不可见字符均视为未清空
                return after is None or after == ""
            return False

        # 1) duration=-1（部分实现要求的清空语义）
        ok1 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq,
            special_title="", duration=-1,
        )
        if ok1 and await _verify():
            return True
        # 2) 空字符串（不带 duration）
        ok2 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq, special_title="",
        )
        if ok2 and await _verify():
            return True
        # 3) 单空格兼容兜底：旧版 OneBot 拒绝空字符串时设置 " "。
        #    但需要校验：若 OneBot 实际把 " " 写回去了，#119 报告就是这种场景，
        #    此时不能视为成功；只有真正被解释为空（接口忽略空白）才算清空。
        ok3 = await self._execute_action(
            event, "set_group_special_title",
            group_id=group_id, user_id=qq, special_title=" ",
        )
        if ok3 and await _verify():
            return True
        return False

    # ===================== 撤回消息历史（对齐 astrbot_plugin_batchrecall，修复 #117 #118 #122） =====================

    def _add_message_to_history(self, group_id: str, message_id, content: str,
                                sender_id: str, sender_name: str, is_bot: bool = False,
                                msg_time=None):
        """添加一条消息到历史。最新消息在列表最前面（编号 1 为最新）。"""
        if not group_id or not message_id or sender_id is None:
            return
        key = str(group_id)
        if key not in self.message_history:
            self.message_history[key] = []
        msg_id = str(message_id)
        # 去重：同一 message_id 只记录一次
        for existing in self.message_history[key]:
            if existing[0] == msg_id:
                return
        if msg_time is None:
            msg_time = int(time.time())
        self.message_history[key].insert(
            0, (msg_id, (content or "")[:100], int(msg_time), str(sender_id), sender_name or "未知", bool(is_bot))
        )
        if len(self.message_history[key]) > self.max_history:
            self.message_history[key] = self.message_history[key][: self.max_history]

    def _remove_message_from_history(self, group_id: str, message_id):
        """从历史中移除已撤回的消息。"""
        if not group_id or not message_id:
            return
        msg_id = str(message_id)
        key = str(group_id)
        if key in self.message_history:
            self.message_history[key] = [
                m for m in self.message_history[key] if m[0] != msg_id
            ]

    def _extract_message_content_from_segments(self, segments) -> str:
        """从 OneBot 消息段列表提取纯文本内容预览（图片/表情/@/引用 标记化）。"""
        content_parts = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            if stype == "text":
                text = seg.get("data", {}).get("text", "").strip()
                if text:
                    content_parts.append(text)
            elif stype == "image":
                content_parts.append("[图片]")
            elif stype == "face":
                content_parts.append("[表情]")
            elif stype == "at":
                content_parts.append("[@]")
            elif stype == "reply":
                content_parts.append("[引用]")
            elif stype == "record":
                content_parts.append("[语音]")
        content = "".join(content_parts)
        return content[:50] if content else "[无文本内容]"

    def _extract_audio_urls(self, segments) -> list:
        """从 OneBot 消息段提取语音/音频 URL（type=record/data.url 或 type=audio）。"""
        urls = []
        for seg in segments or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype in ("record", "audio"):
                url = data.get("url") or data.get("file") or ""
                if url:
                    urls.append(url)
        return urls

    def _extract_audio_url(self, segments) -> str:
        """返回第一条语音 URL，没有则返回空串。"""
        urls = self._extract_audio_urls(segments)
        return urls[0] if urls else ""

    def _strip_command_prefix(self, text: str) -> str:
        """剥离开头的命令前缀符号（/、.、!、# 等）。"""
        return text.lstrip("/.!#$%^&*~-+=?，。、！!＠@＃#＄$％%＾^＆&＊*～~｀`｜|＼\\ 　\t")

    def _extract_command_tail(self, raw_text: str, cmd_names) -> str:
        """从原始文本提取命令名之后的参数部分；非本命令返回空串。"""
        if not raw_text:
            return ""
        stripped = self._strip_command_prefix(raw_text)
        if not stripped:
            return ""
        # 按长度降序匹配，避免短命令名先命中（如"撤回"先于"撤回自身"）
        for cmd in sorted(cmd_names, key=len, reverse=True):
            if stripped.startswith(cmd):
                return stripped[len(cmd):].strip()
        return ""

    _GM_COMMAND_NAMES = (
        "撤回自身", "撤回", "设置图片禁言时长", "设置刷屏禁言时长",
        "设置骂人禁言时长", "设置广告禁言时长", "设置链接禁言时长", "设置群号推广禁言时长",
        "添加骂人关键词", "删除骂人关键词", "查看骂人关键词", "切换骂人检测模式",
        "添加白名单用户", "删除白名单用户", "查看白名单", "查看违规统计",
        "添加广告关键词", "删除广告关键词", "查看广告关键词",
        "添加插件管理", "删除插件管理", "添加头衔管理", "删除头衔管理",
        "添加管理管理", "删除管理管理", "添加踢人管理", "删除踢人管理",
        "设置群配置", "查看群配置", "清除群配置", "群违规检测状态",
        "设管理", "取消管理", "头衔",
        "别人昵称", "改群昵称", "群昵称", "禁言", "禁言列表", "解禁", "踢", "清用户历史", "鞭尸",
        "设精", "取消设精", "改群头像", "宵禁", "解除宵禁", "禁我",
        "发群公告", "排名", "清除数据", "举报", "status",
        "添加群待办", "取消群待办", "给我头衔", "加群申请待处理", "群信息", "群名称", "群标签", "群相册",
        "添加违禁图片", "删除违禁图片", "查看违禁图片",
    )

    def _is_plugin_command(self, text: str) -> bool:
        """判断文本是否为本插件的指令消息（用户发送）。
        指令消息不记录进历史，避免编号偏移。"""
        t = (text or "").strip()
        if not t:
            return False
        stripped = self._strip_command_prefix(t)
        if not stripped:
            return False
        # 指令词后必须是空格、数字、逗号、@ 或结尾，避免误过滤普通聊天
        for cmd in sorted(self._GM_COMMAND_NAMES, key=len, reverse=True):
            if stripped.startswith(cmd):
                after = stripped[len(cmd):]
                if after == "" or after[0] in (" ", "\t", ",", "，", "@", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    return True
        return False

    def _record_message_to_history(self, group_id: str, raw: dict):
        """记录一条群友消息到历史（bot 自身消息由 after_message_sent 记录）。"""
        if not group_id or not isinstance(raw, dict):
            return
        message_id = raw.get("message_id")
        if not message_id:
            return
        # 插件指令消息不记录（避免编号偏移）。用纯文本判断，避免 @ 段被标成 [@] 导致漏判
        if self._is_plugin_command(self._extract_text(raw)):
            return
        content = self._extract_message_content_from_segments(raw.get("message") or [])
        sender = raw.get("sender") or {}
        sender_id = str(sender.get("user_id", "") or raw.get("user_id", "") or "")
        if not sender_id:
            return
        sender_name = sender.get("card") or sender.get("nickname", "未知")
        is_bot = bool(raw.get("self_id")) and str(raw.get("self_id")) == sender_id
        msg_time = raw.get("time", int(time.time()))
        self._add_message_to_history(group_id, message_id, content, sender_id, sender_name, is_bot, int(msg_time))

    async def _recognize_audio_url(self, event, audio_url: str, group_id: str) -> str:
        """#128：将语音 URL 识别为文本。优先 AstrBot 内置 STT provider，回退到插件独立 ASR 配置。
        返回识别文本，失败返回空串。"""
        if not audio_url:
            return ""
        timeout = max(5, int(self.config.get("voice_check_timeout", 15) or 15))
        provider_id = str(self.config.get("voice_check_provider_id", "") or "").strip()
        # 1) AstrBot 内置 provider：优先用户指定 provider_id，否则用当前激活的 STT provider
        try:
            ctx = getattr(self, "context", None)
            provider_manager = getattr(ctx, "provider_manager", None) if ctx else None
            if provider_manager is not None:
                from astrbot.core.provider.entities import ProviderType  # 局部导入，避免硬依赖失败
                prov = None
                if provider_id:
                    try:
                        prov = await provider_manager.get_provider_by_id(provider_id)
                    except Exception:
                        prov = None
                if prov is None:
                    try:
                        prov = provider_manager.get_using_provider(ProviderType.SPEECH_TO_TEXT)
                    except Exception:
                        prov = None
                if prov is not None and hasattr(prov, "get_text"):
                    try:
                        text = await asyncio.wait_for(prov.get_text(audio_url), timeout=timeout)
                        if text:
                            return str(text).strip()
                    except Exception as exc:
                        logger.warning(f"AstrBot STT provider 识别失败: {exc}")
        except ImportError:
            logger.debug("未安装 astrbot.core.provider.entities，回退到插件独立 API")
        except Exception as exc:
            logger.warning(f"AstrBot STT 调用异常: {exc}")
        # 2) 插件独立 ASR API（OpenAI 兼容 /audio/transcriptions）
        endpoint = str(self.config.get("voice_asr_endpoint", "") or "").strip()
        api_key = str(self.config.get("voice_asr_api_key", "") or "").strip()
        model = str(self.config.get("voice_asr_model", "") or "").strip() or "whisper-1"
        if not endpoint:
            return ""
        try:
            import aiohttp
            from openai import AsyncOpenAI  # type: ignore
            base_url = endpoint.rstrip("/")
            if not base_url.endswith("/audio/transcriptions"):
                base_url = base_url + ("/audio/transcriptions" if base_url.endswith("/v1") else "/v1/audio/transcriptions")
            client = AsyncOpenAI(api_key=api_key or "EMPTY", base_url=base_url.rsplit("/audio/transcriptions", 1)[0], timeout=timeout)
            # 简化：下载音频为字节流，调用 transcriptions.create
            async with aiohttp.ClientSession() as session:
                async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        return ""
                    audio_bytes = await resp.read()
            from io import BytesIO
            result = await client.audio.transcriptions.create(
                model=model,
                file=("audio.wav", BytesIO(audio_bytes)),
            )
            return str(getattr(result, "text", "") or "").strip()
        except ImportError:
            logger.warning("未安装 openai / aiohttp，无法调用独立 ASR API")
            return ""
        except Exception as exc:
            logger.warning(f"独立 ASR 调用失败: {exc}")
            return ""

    def _get_self_id(self, event, raw=None) -> str:
        """获取 bot 自身 QQ 号，优先 event.get_self_id()，回退 raw 字段。"""
        try:
            sid = event.get_self_id()
            if sid:
                return str(sid)
        except Exception:
            pass
        if raw:
            sid = raw.get("self_id")
            if sid:
                return str(sid)
        return ""

    async def _load_history_from_api(self, event, group_id: str):
        """从 OneBot get_group_msg_history 加载历史（本地历史为空时兜底），并替换本地历史。"""
        if not group_id:
            return
        try:
            fetch_count = min(self.max_history * 2, 100)
            result = await self._execute_action(
                event, "get_group_msg_history", return_raw=True,
                group_id=group_id, count=fetch_count,
            )
            if not isinstance(result, dict):
                return
            data = result.get("data")
            history_messages = (data.get("messages") if isinstance(data, dict) else None) \
                or result.get("messages") or []
            history_messages = [m for m in history_messages if isinstance(m, dict)]
            # 按时间倒序排列（最新在前）
            history_messages.sort(key=lambda m: m.get("time", 0), reverse=True)

            bot_self_id = self._get_self_id(event)
            history_list = []
            for msg in history_messages[: self.max_history]:
                msg_id = msg.get("message_id")
                if not msg_id:
                    continue
                sender_info = msg.get("sender") or {}
                sender_id = str(sender_info.get("user_id", ""))
                raw_msg = msg.get("message") or []
                if isinstance(raw_msg, list):
                    content = self._extract_message_content_from_segments(raw_msg)
                    # 跳过 API 返回历史中的插件指令消息（避免把历史中的"/撤回"等指令也编号）。
                    # 用纯文本判断，避免 @ 段被标成 [@] 导致漏判
                    if sender_id != bot_self_id and self._is_plugin_command(self._extract_text({"message": raw_msg})):
                        continue
                else:
                    content = str(raw_msg)[:50]
                sender_name = sender_info.get("card") or sender_info.get("nickname", "未知")
                msg_time = msg.get("time", int(time.time()))
                is_bot = bool(bot_self_id) and sender_id == bot_self_id
                history_list.append((str(msg_id), content, int(msg_time), sender_id, sender_name, is_bot))
            if history_list:
                self.message_history[str(group_id)] = history_list
        except Exception as exc:
            logger.error(f"从 API 获取消息历史失败: {exc}")

    async def _get_history_snapshot(self, event, group_id: str, current_msg_id=None) -> list:
        """返回该群的撤回用消息快照（本地历史优先，为空时从 API 加载）。
        排除当前指令消息；返回浅拷贝，避免并发修改影响编号映射。"""
        if not group_id:
            return []
        key = str(group_id)
        hist = self.message_history.get(key, [])
        if not hist:
            await self._load_history_from_api(event, group_id)
            hist = self.message_history.get(key, [])
        if current_msg_id and hist:
            cur = str(current_msg_id)
            return [m for m in hist if m[0] != cur]
        return list(hist)

    async def _do_recall(self, event, message_id) -> tuple:
        """撤回一条消息，返回 (成功, 错误信息)。识别 retcode=1200（消息已撤回或超时）。"""
        mid = str(message_id)
        mid_num = int(mid) if mid.isdigit() else mid
        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if callable(call):
            try:
                result = await call("delete_msg", message_id=mid_num)
                if isinstance(result, dict):
                    retcode = result.get("retcode")
                    if retcode is not None:
                        try:
                            if int(retcode) == 1200:
                                return False, "消息已撤回或超时"
                            if int(retcode) != 0:
                                return False, f"撤回失败(retcode={retcode})"
                        except (TypeError, ValueError):
                            pass
                return True, ""
            except Exception as e:
                retcode = getattr(e, "retcode", None)
                if retcode == 1200:
                    return False, "消息已撤回或超时"
                return False, f"撤回失败: {e}"
        ok = await self._recall_message(event, message_id)
        return (True, "") if ok else (False, "撤回失败")

    async def _set_group_card(self, event: AstrMessageEvent, group_id: str, qq: str, card: str):
        return await self._execute_action(event, "set_group_card",
                                          group_id=group_id, user_id=qq, card=card)

    async def _set_essence(self, event: AstrMessageEvent, message_id: str, group_id: str = None):
        """OneBot 标准 API 名为 set_essence_msg，部分实现也支持 set_essence。"""
        kwargs = {"message_id": message_id}
        if group_id is not None:
            kwargs["group_id"] = group_id
        # 优先尝试标准名 set_essence_msg，再回退 set_essence
        result = await self._execute_action(event, "set_essence_msg", **kwargs)
        if not result:
            result = await self._execute_action(event, "set_essence", **kwargs)
        return result

    async def _delete_essence(self, event: AstrMessageEvent, message_id: str, group_id: str = None):
        """取消精华消息。OneBot 标准 API 名为 delete_essence_msg。"""
        kwargs = {"message_id": message_id}
        if group_id is not None:
            kwargs["group_id"] = group_id
        result = await self._execute_action(event, "delete_essence_msg", **kwargs)
        if not result:
            result = await self._execute_action(event, "delete_essence", **kwargs)
        return result

    async def _mute_member(self, event: AstrMessageEvent, group_id: str, qq: str, duration_seconds: int):
        return await self._execute_action(event, "set_group_ban",
                                          group_id=group_id, user_id=qq, duration=duration_seconds)

    async def _unmute_member(self, event: AstrMessageEvent, group_id: str, qq: str):
        return await self._execute_action(event, "set_group_ban",
                                          group_id=group_id, user_id=qq, duration=0)

    async def _kick_member(self, event: AstrMessageEvent, group_id: str, qq: str):
        return await self._execute_action(event, "kick", group_id=group_id, user_id=qq)

    async def _recall_user_recent_msgs(self, event: AstrMessageEvent, group_id: str, user_id: str, count: int) -> int:
        """撤回某用户在群内最近 count 条消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian）。
        优先使用本地消息历史（_get_history_snapshot），为空时回退 OneBot get_group_msg_history。
        OneBot delete_msg 只能撤回约 2 分钟内的消息，超时的会静默失败。返回实际撤回条数。"""
        gid = str(group_id)
        uid = str(user_id)
        if not gid or not uid or count <= 0:
            return 0
        count = max(1, min(int(count), 50))
        snapshot = await self._get_history_snapshot(event, gid)
        if not snapshot:
            # 本地为空：尝试 OneBot API
            try:
                history = await self._execute_action(event, "get_group_msg_history", return_raw=True,
                                                     group_id=gid, count=min(self.max_history * 2, 100))
                msgs = []
                if isinstance(history, dict):
                    msgs = history.get("data", {}).get("messages") or history.get("messages") or []
                for m in reversed(msgs):
                    sender = m.get("sender") or {}
                    if str(sender.get("user_id", "")) != uid:
                        continue
                    snapshot.append((str(m.get("message_id")), "", int(m.get("time", 0)), uid, "", False))
                    if len(snapshot) >= count:
                        break
            except Exception as exc:
                logger.debug(f"踢人清历史 API 兜底失败({gid}/{uid}): {exc}")
                return 0
        candidates = [m for m in snapshot if m[3] == uid][:count]
        recalled = 0
        for m in candidates:
            ok, err = await self._do_recall(event, m[0])
            if ok or "已撤回" in err:
                self._remove_message_from_history(gid, m[0])
                recalled += 1
            await asyncio.sleep(0.3)
        return recalled

    async def _set_group_avatar(self, event: AstrMessageEvent, group_id: str, file: str):
        """修改群头像，file 可以是 URL 或本地路径或 base64。"""
        return await self._execute_action(event, "set_group_portrait",
                                          group_id=group_id, file=file)

    async def _handle_group_request(self, event: AstrMessageEvent, flag: str, approve: bool, reason: str = ""):
        return await self._execute_action(event, "handle_group_request",
                                          flag=flag, approve=approve, reason=reason)

    # ===================== 消息收发辅助 =====================

    async def _send_private_msg(self, user_id: str, content: str):
        """向指定QQ号发送私聊消息（通过 context）。"""
        if hasattr(self.context, "send_private_msg"):
            try:
                return await self.context.send_private_msg(user_id=user_id, message=content)
            except Exception as e:
                logger.error(f"发送私聊失败: {e}")
        return False

    async def _find_group_owner(self, event: AstrMessageEvent, group_id: str) -> str:
        """查找群主 QQ 号，用于 #140 举报分级路由。返回 QQ 号字符串，找不到返回空串。"""
        member_list = await self._execute_action(event, "get_group_member_list",
                                                 group_id=group_id, return_raw=True)
        if isinstance(member_list, dict):
            data = member_list.get("data") or member_list
            if isinstance(data, list):
                for m in data:
                    if isinstance(m, dict) and m.get("role") == "owner":
                        return str(m.get("user_id", ""))
        return ""

    async def _send_group_text(self, event: AstrMessageEvent, group_id: str, text: str):
        """向指定群发送纯文本消息，返回 message_id（用于后续引用回复关联）。"""
        try:
            if hasattr(self.context, "send_group_msg"):
                # AstrBot 标准方法：send_group_msg(group_id=, message=)
                result = await self.context.send_group_msg(group_id=int(group_id), message=text)
                # 返回值可能直接是 message_id，也可能是含 message_id 的 dict
                if isinstance(result, dict):
                    return str(result.get("message_id") or result.get("data", {}).get("message_id", ""))
                return str(result) if result else ""
            # 回退：使用 _send 但拿不到 message_id
            await self._send(event, [Plain(text)])
        except Exception as e:
            logger.error(f"发送群消息失败: {e}")
        return ""

    async def _get_user_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称（通过 OneBot get_stranger_info API）。"""
        try:
            handler = getattr(self.context, "get_stranger_info", None)
            if callable(handler):
                info = await handler(user_id=int(user_id))
                if isinstance(info, dict):
                    return info.get("nickname") or info.get("data", {}).get("nickname", user_id)
                if hasattr(info, "nickname"):
                    return info.nickname
        except Exception as e:
            logger.error(f"获取昵称失败: {e}")
        return user_id

    async def _notify_admins(self, text: str, group_id: str = ""):
        """向 join_notify_admins 配置的管理员发送私聊通知。"""
        for admin_id in self.get_group_setting(group_id, "join_notify_admins", []) or []:
            await self._send_private_msg(str(admin_id), text)

    # ===================== 计数统计（#29） =====================

    def _increment_message_count(self, group_id: str, user_id: str):
        groups = self.stats.setdefault("groups", {})
        g = groups.setdefault(str(group_id), {"messages": {}})
        msgs = g.setdefault("messages", {})
        msgs[str(user_id)] = msgs.get(str(user_id), 0) + 1
        # #152：每50条消息批量写入磁盘，避免重启丢数据
        self._msg_save_counter += 1
        if self._msg_save_counter >= 50:
            self.save_stats()
            self._msg_save_counter = 0

    def get_rank(self, group_id: str, top_n: int) -> list:
        groups = self.stats.get("groups", {})
        msgs = groups.get(str(group_id), {}).get("messages", {})
        ranked = sorted(msgs.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_n]

    def reset_group_stats(self, group_id: str):
        self.stats.setdefault("groups", {})[str(group_id)] = {"messages": {}}
        self.save_stats()

    async def _record_mute_and_maybe_kick(self, event: AstrMessageEvent, group_id: str, user_id: str, operator_id: str = ""):
        """记录被禁言次数，达到阈值后自动踢出。阈值为 0/空则关闭。"""
        try:
            threshold = int(self.get_group_setting(group_id, "mute_kick_threshold", 0) or 0)
        except (TypeError, ValueError):
            threshold = 0
        if threshold <= 0:
            return
        group_key = str(group_id)
        user_key = str(user_id)
        groups = self.stats.setdefault("groups", {})
        g = groups.setdefault(group_key, {"messages": {}})
        counts = g.setdefault("mute_counts", {})
        counts[user_key] = int(counts.get(user_key, 0)) + 1
        self.save_stats()
        if counts[user_key] >= threshold:
            ok = await self._kick_member(event, group_id, user_id)
            if ok:
                counts[user_key] = 0
                self.save_stats()
                await self._send(event, self._build_text(f"{user_id} 禁言次数达到 {threshold} 次，已自动踢出"))

    # ===================== 群违规检测（合并自 astrbot_plugin_group_moderation） =====================

    def _record_violation(self, group_id: str, user_id: str, kind: str):
        """把一次违规记录到 stats 中（持久化）。"""
        g = self.stats.setdefault("groups", {}).setdefault(str(group_id), {"messages": {}})
        counts = g.setdefault("violation_counts", {})
        bucket = counts.setdefault(kind, {})
        bucket[str(user_id)] = int(bucket.get(str(user_id), 0)) + 1
        self.save_stats()

    def _is_group_monitoring_enabled(self, group_id: str) -> bool:
        """群是否启用违规检测。
        优先级
        1. group_overrides[gid]["enabled_groups"] 为 bool 时，按 bool 决定
        2. top-level enabled_groups 列表：包含 * / all 表示全部；包含群号表示启用
        3. 兼容旧 violation_enabled_groups 列表
        """
        overrides = self.config.get("group_overrides", {}).get(str(group_id), {})
        v = overrides.get("enabled_groups")
        if isinstance(v, bool):
            return v
        enabled = self.config.get("enabled_groups", []) or []
        if not enabled:
            return False
        for x in enabled:
            sx = str(x).lower()
            if sx in ("*", "all"):
                return True
            if str(x) == str(group_id):
                return True
        legacy = self.config.get("violation_enabled_groups", []) or []
        return str(group_id) in [str(x) for x in legacy]

    def _is_user_whitelisted(self, group_id: str, user_id: str) -> bool:
        whitelist = self.get_group_setting(group_id, "whitelist_users", []) or []
        return str(user_id) in [str(x) for x in whitelist]

    def _moderation_admin_bypass(self, group_id: str, raw: dict) -> bool:
        if not self.get_group_setting(group_id, "admin_bypass", True):
            return False
        role = raw.get("sender", {}).get("role", "") if isinstance(raw, dict) else ""
        return role in {"admin", "owner"}

    def _moderation_ban_duration(self, group_id: str, kind: str) -> int:
        """按违规类型读取对应禁言时长（秒）。"""
        key_map = {
            "image": "ban_duration",
            "spam": "spam_ban_duration",
            "profanity": "profanity_ban_duration",
            "ad": "ad_ban_duration",
            "link": "link_ban_duration",
            "group_promotion": "group_promotion_ban_duration",
        }
        key = key_map.get(kind, "ban_duration")
        default_map = {
            "image": 600, "spam": 600, "profanity": 600,
            "ad": 600, "link": 600, "group_promotion": 600,
            "banned_image": 600,
        }
        try:
            v = int(self.get_group_setting(group_id, key, default_map.get(kind, 600)) or 600)
        except (TypeError, ValueError):
            v = default_map.get(kind, 600)
        return max(1, v)

    async def _handle_violation(
        self,
        event: AstrMessageEvent,
        kind: str,
        group_id: str,
        user_id: str,
        message_id: str,
        reason: str = "",
    ) -> bool:
        """处理一条违规：撤回 + 按配置时长禁言 + 计数 + 通知。"""
        ok_any = False
        # 1. 撤回
        if message_id:
            recalled = await self._recall_message(event, str(message_id))
            ok_any = recalled
        # 2. 禁言
        duration = self._moderation_ban_duration(group_id, kind)
        muted = await self._mute_member(event, group_id, user_id, duration)
        if muted:
            ok_any = True
            # 复用现有的 mute_kick_threshold 计数
            await self._record_mute_and_maybe_kick(event, group_id, user_id, "moderation")
        # 3. 计数
        self._record_violation(group_id, user_id, kind)
        # 4. 通知
        if self.get_group_setting(group_id, "notify_on_violation", True):
            label_map = {
                "image": "违规图片", "spam": "刷屏", "profanity": "骂人",
                "ad": "广告", "link": "链接", "group_promotion": "群号推广",
                "banned_image": "违禁图片",
            }
            label = label_map.get(kind, "违规")
            note = f"检测到{label}行为"
            if reason:
                note += f"（{reason}）"
            note += f"，已撤回并禁言 {duration} 秒。"
            await self._send(event, self._build_text(note))
        return ok_any

    async def _moderation_dispatch(self, event, raw, group_id: str, user_id: str) -> bool:
        """群消息违规检测总入口。"""
        if not self._is_group_monitoring_enabled(group_id):
            return False
        if self._is_user_whitelisted(group_id, user_id):
            return False
        if self._moderation_admin_bypass(group_id, raw):
            return False
        msg_text = self._extract_text(raw) if isinstance(raw, dict) else ""
        # 1) 刷屏（不依赖文本）
        if await self._check_spam(group_id, user_id):
            mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
            await self._handle_violation(event, "spam", group_id, user_id, mid)
            return True
        # 2) 文本类检测
        if msg_text:
            if await self._check_profanity(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "profanity", group_id, user_id, mid)
                return True
            if await self._check_ad(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "ad", group_id, user_id, mid)
                return True
            if await self._check_link(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "link", group_id, user_id, mid)
                return True
            if await self._check_group_promotion(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "group_promotion", group_id, user_id, mid)
                return True
        # 3) 图片检测（#162 违禁图 MD5 比对先于 AI 鉴图）
        image_urls = self._collect_image_urls(raw)
        for url in image_urls:
            if await self._check_banned_image(url, group_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "banned_image", group_id, user_id, mid, "图片命中违禁图")
                return True
            violated, reason = await self._check_image(url)
            if violated:
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "image", group_id, user_id, mid, reason)
                return True
        # 4) 语音转文字检测（#128）
        if self.get_group_setting(group_id, "voice_check_enabled", False):
            audio_urls = self._extract_audio_urls(raw.get("message") or []) if isinstance(raw, dict) else []
            for url in audio_urls:
                text = await self._recognize_audio_url(event, url, group_id)
                if not text:
                    continue
                violated_kind = None
                if await self._check_profanity(text, event, group_id, user_id):
                    violated_kind = "profanity"
                elif await self._check_ad(text, event, group_id, user_id):
                    violated_kind = "ad"
                elif await self._check_link(text, event, group_id, user_id):
                    violated_kind = "link"
                elif await self._check_group_promotion(text, event, group_id, user_id):
                    violated_kind = "group_promotion"
                if violated_kind:
                    mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                    await self._handle_violation(event, f"voice_{violated_kind}", group_id, user_id, mid,
                                                 f"语音内容: {text[:50]}")
                    return True
        return False

    # ----- 图片检测 -----

    def _collect_image_urls(self, raw) -> list:
        urls = []
        if not isinstance(raw, dict):
            return urls
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict):
                if seg.get("type") == "image":
                    data = seg.get("data", {}) or {}
                    u = data.get("url") or data.get("file") or ""
                    if u and u not in urls:
                        urls.append(u)
        return urls

    async def _check_banned_image(self, image_url: str, group_id: str) -> bool:
        """#162：检查图片 MD5 是否在全局/本群违禁图列表中。

        仅快速预筛原图二次传播；截断或下载失败一律视为未命中（放行），
        不阻塞后续 AI 鉴图链路。
        """
        image_data, truncated = await self._download_image(image_url)
        if truncated or not image_data:
            return False
        md5 = hashlib.md5(image_data).hexdigest()
        global_banned = set(self.config.get("banned_images", []) or [])
        group_banned = set(self.get_group_setting(group_id, "banned_images", []) or [])
        return md5 in (global_banned | group_banned)

    async def _compute_image_md5(self, image_url: str):
        """下载图片并计算 MD5，供 /添加违禁图片 使用。

        返回 (md5: str|None, truncated: bool)。下载失败返回 (None, False)；
        图片超过 10MB 时返回 (None, True)，调用方应提示用户图片过大。
        """
        image_data, truncated = await self._download_image(image_url)
        if not image_data:
            return None, truncated
        return hashlib.md5(image_data).hexdigest(), truncated

    async def _check_image(self, image_url: str):
        """调用 AI API 审核图片。返回 (is_violation, reason)。"""
        if aiohttp is None:
            logger.warning("[群违规检测] aiohttp 未安装，跳过图片审核")
            return False, ""
        api_endpoint = self.config.get("api_endpoint", "")
        api_key = self.config.get("api_key", "")
        api_type = self.config.get("api_type", "openai_vision")
        if not api_endpoint:
            return False, ""
        try:
            image_data, _truncated = await self._download_image(image_url)
            if not image_data:
                return False, ""
            image_b64 = base64.b64encode(image_data).decode("utf-8")
            if api_type == "moderation":
                return await self._check_with_moderation_api(api_endpoint, api_key, image_b64)
            return await self._check_with_openai_vision(api_endpoint, api_key, image_b64)
        except Exception as e:
            logger.error(f"[群违规检测] 图片审核失败: {e}")
            return False, ""

    async def _check_with_openai_vision(self, api_endpoint: str, api_key: str, image_b64: str):
        model_name = self.config.get("model_name", "gpt-4o")
        prompt = self.config.get("detection_prompt") or (
            "请分析这张图片，判断是否包含违规内容（色情/擦边等）。"
            "请仅返回 JSON：{\"is_violation\": true/false, \"type\": \"porn/sexy/normal\", "
            "\"confidence\": 0.0-1.0, \"reason\": \"简短原因\"}"
        )
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]}],
            "max_tokens": 500,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"[群违规检测] OpenAI Vision API 失败: {resp.status} {text[:200]}")
                        return False, ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            return self._parse_openai_response(content)
        except Exception as e:
            logger.error(f"[群违规检测] OpenAI Vision 调用失败: {e}")
            return False, ""

    def _parse_openai_response(self, content: str):
        if not content:
            return False, ""
        try:
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            text = match.group() if match else content
            data = json.loads(text)
            is_violation = bool(data.get("is_violation", False))
            v_type = str(data.get("type", "normal")).lower()
            try:
                confidence = float(data.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            reason = str(data.get("reason", ""))
            threshold = float(self.config.get("threshold", 0.7) or 0.7)
            check_porn = bool(self.config.get("check_porn", True))
            check_sexy = bool(self.config.get("check_sexy", True))
            if is_violation and confidence >= threshold:
                if v_type == "porn" and check_porn:
                    return True, f"检测到色情内容 (置信度: {confidence:.0%}) - {reason}"
                if v_type == "sexy" and check_sexy:
                    return True, f"检测到擦边内容 (置信度: {confidence:.0%}) - {reason}"
            return False, ""
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[群违规检测] 解析 OpenAI 响应失败: {e}")
            return False, ""

    async def _check_with_moderation_api(self, api_endpoint: str, api_key: str, image_b64: str):
        payload = {"input": image_b64}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"[群违规检测] Moderation API 失败: {resp.status}")
                        return False, ""
                    data = await resp.json()
            results = data.get("results") or []
            if not results:
                return False, ""
            categories = results[0].get("categories", {}) or {}
            scores = results[0].get("category_scores", {}) or {}
            if categories.get("sexual"):
                return True, f"检测到性内容 (置信度: {scores.get('sexual', 0):.0%})"
            return False, ""
        except Exception as e:
            logger.error(f"[群违规检测] Moderation API 调用失败: {e}")
            return False, ""

    async def _download_image(self, url: str, max_size: int = 10 * 1024 * 1024):
        """下载图片。#162 增强：限制最大 10MB，避免慢速/大文件阻塞检测。

        返回 (data, truncated) 元组：truncated=True 表示数据被截断到 max_size+1，
        调用方（如 /添加违禁图片）应拒绝基于截断数据计算指纹。
        """
        if aiohttp is None:
            return None, False
        try:
            if url.startswith("http://") or url.startswith("https://"):
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=timeout) as resp:
                        if resp.status != 200:
                            return None, False
                        # Content-Type 必须为图片
                        ct = resp.headers.get("Content-Type", "")
                        if ct and not ct.startswith("image/"):
                            return None, False
                        data = await resp.content.read(max_size + 1)
                        return data, len(data) > max_size
            elif url.startswith("base64://"):
                # base64 内嵌数据本身即图片内容，无需 Content-Type 校验
                data = base64.b64decode(url[9:])
                truncated = len(data) > max_size
                return (data[:max_size + 1] if truncated else data), truncated
            elif url.startswith("file://"):
                with open(url[7:], "rb") as f:
                    data = f.read(max_size + 1)
                    return data, len(data) > max_size
            elif url and not url.startswith("http"):
                # 某些实现把图片作为本地路径返回
                try:
                    with open(url, "rb") as f:
                        data = f.read(max_size + 1)
                        return data, len(data) > max_size
                except OSError:
                    return None, False
        except Exception as e:
            logger.error(f"[群违规检测] 下载图片失败: {e}")
        return None, False

    # ----- 刷屏检测 -----

    async def _check_spam(self, group_id: str, user_id: str) -> bool:
        try:
            threshold = int(self.get_group_setting(group_id, "spam_threshold", 5) or 5)
            window = int(self.get_group_setting(group_id, "spam_time_window", 10) or 10)
        except (TypeError, ValueError):
            return False
        if not self.get_group_setting(group_id, "spam_check_enabled", True):
            return False
        if threshold <= 0 or window <= 0:
            return False
        now = time.time()
        key = f"{group_id}_{user_id}"
        records = self.spam_records[key]
        records[:] = [t for t in records if now - t < window]
        records.append(now)
        return len(records) >= threshold

    # ----- 骂人检测 -----

    async def _check_profanity(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self.get_group_setting(group_id, "profanity_check_enabled", True):
            return False
        if not msg_text:
            return False
        use_ai = bool(self.get_group_setting(group_id, "profanity_use_ai", True))
        if use_ai and aiohttp is not None:
            api_endpoint = self.config.get("api_endpoint", "")
            api_key = self.config.get("api_key", "")
            if api_endpoint:
                is_profanity, reason = await self._check_profanity_with_ai(api_endpoint, api_key, msg_text)
                if is_profanity:
                    logger.warning(f"[群违规检测] 骂人 用户 {user_id} {reason}")
                    return True
                return False  # AI 模式下不再走关键词
        keywords = self.get_group_setting(group_id, "profanity_keywords", []) or []
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                return True
        return False

    async def _check_profanity_with_ai(self, api_endpoint: str, api_key: str, msg_text: str):
        model_name = self.config.get("model_name", "gpt-4o")
        prompt = (
            "你是严格的内容审核助手。请判断以下文本是否包含骂人、侮辱、人身攻击。\n"
            "请仅返回 JSON：{\"is_profanity\": true/false, \"reason\": \"简短原因\"}"
        )
        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": f"{prompt}\n\n待检测文本：{msg_text}"}],
            "max_tokens": 200,
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                async with session.post(api_endpoint, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status != 200:
                        logger.error(f"[群违规检测] 骂人 AI 失败: {resp.status}")
                        return False, ""
                    data = await resp.json()
            content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
            text = match.group() if match else content
            obj = json.loads(text)
            return bool(obj.get("is_profanity", False)), str(obj.get("reason", ""))
        except Exception as e:
            logger.error(f"[群违规检测] 骂人 AI 解析失败: {e}")
            return False, ""

    # ----- 广告 / 链接 / 群号推广 -----

    async def _check_ad(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self.get_group_setting(group_id, "ad_check_enabled", True):
            return False
        if not msg_text:
            return False
        keywords = self.get_group_setting(group_id, "ad_keywords", []) or []
        text_lower = msg_text.lower()
        for kw in keywords:
            if str(kw).lower() and str(kw).lower() in text_lower:
                return True
        return False

    async def _check_link(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self.get_group_setting(group_id, "link_check_enabled", False):
            return False
        if not msg_text:
            return False
        pattern = r"(https?://[^\s]+|www\.[^\s]+\.[^\s]+|[^\s]+\.(com|cn|net|org|io|xyz|top|vip|cc|me|tv|edu|gov)[^\s]*)"
        return re.search(pattern, msg_text, re.IGNORECASE) is not None

    async def _check_group_promotion(self, msg_text: str, event, group_id: str, user_id: str) -> bool:
        if not self.get_group_setting(group_id, "group_promotion_check_enabled", True):
            return False
        if not msg_text:
            return False
        promotion_keywords = ["进群", "加群", "群号", "入群", "拉群", "建群"]
        if not any(kw in msg_text for kw in promotion_keywords):
            return False
        group_pattern = r"[;；:,，\s]*(\d{5,12})"
        return bool(re.findall(group_pattern, msg_text))

    # ----- 群违规检测管理命令（仅插件管理员） -----

    def _moderation_require_admin(self, event):
        """校验消息发送者是否为插件管理员。是则返回 user_id，否则返回 None。
        #132：群管理员与群主也视为插件管理员。"""
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            return None
        sender_id = str(raw.get("user_id", ""))
        if not self._is_authorized(raw, sender_id):
            return None
        return sender_id

    async def _moderation_require_admin_msg(self, event) -> bool:
        """校验插件管理员/群聊环境，不通过则发提示并返回 False。
        必须保持为普通 async 函数（不能 yield），否则 18 个调用点拿不到 bool。"""
        if self._moderation_require_admin(event) is not None:
            return True
        raw = self._get_raw_message(event)
        if raw and not raw.get("group_id"):
            await self._send(event, self._build_text("此指令只能在群聊中使用"))
            return False
        await self._send(event, self._build_text("只有插件管理员可执行此操作"))
        return False

    @filter.command("群违规检测状态", "查看群违规检测插件状态")
    async def moderation_status_cmd(self, event: AstrMessageEvent):
        if not await self._moderation_require_admin_msg(event):
            return
        api_type = self.config.get("api_type", "openai_vision")
        profanity_use_ai = self.config.get("profanity_use_ai", True)
        profanity_mode = "AI检测" if profanity_use_ai else "关键词检测"
        whitelist_users = self.config.get("whitelist_users", [])
        profanity_keywords = self.config.get("profanity_keywords", [])
        ad_keywords = self.config.get("ad_keywords", [])
        enabled_groups = self.config.get("enabled_groups", [])
        text = (
            "【群违规检测插件状态】\n"
            f"API 类型: {api_type}\n"
            f"API 站点: {self.config.get('api_endpoint', '') or '未配置'}\n"
            f"API Key: {'已配置' if self.config.get('api_key') else '未配置'}\n"
            f"模型: {self.config.get('model_name', 'gpt-4o')}\n"
            f"\n"
            f"监控群组: {enabled_groups if enabled_groups else '全部（需在群内启用 /设置群配置 enabled_groups true）'}\n"
            f"\n"
            f"【禁言时长（秒）】\n"
            f"图片: {self.config.get('ban_duration', 600)}\n"
            f"刷屏: {self.config.get('spam_ban_duration', 600)}\n"
            f"骂人: {self.config.get('profanity_ban_duration', 600)}\n"
            f"广告: {self.config.get('ad_ban_duration', 600)}\n"
            f"链接: {self.config.get('link_ban_duration', 600)}\n"
            f"群号推广: {self.config.get('group_promotion_ban_duration', 600)}\n"
            f"\n"
            f"【检测开关】\n"
            f"图片(色情/擦边): {self.config.get('check_porn', True)}/{self.config.get('check_sexy', True)}\n"
            f"刷屏: {self.config.get('spam_check_enabled', True)}（{self.config.get('spam_threshold', 5)} 条/{self.config.get('spam_time_window', 10)} 秒）\n"
            f"骂人: {self.config.get('profanity_check_enabled', True)}（{profanity_mode}, 关键词 {len(profanity_keywords)} 个）\n"
            f"广告: {self.config.get('ad_check_enabled', True)}（关键词 {len(ad_keywords)} 个）\n"
            f"链接: {self.config.get('link_check_enabled', False)}\n"
            f"群号推广: {self.config.get('group_promotion_check_enabled', True)}\n"
            f"\n"
            f"【其他】\n"
            f"白名单用户: {len(whitelist_users)} 人\n"
            f"管理员豁免: {self.config.get('admin_bypass', True)}\n"
            f"违规通知: {self.config.get('notify_on_violation', True)}\n"
            f"检测阈值: {self.config.get('threshold', 0.7)}"
        )
        yield event.plain_result(text)

    @filter.command("设置图片禁言时长", "设置图片违规禁言时长（秒）")
    async def set_image_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["ban_duration"] = seconds
        yield event.plain_result(f"[成功] 图片违规禁言时长已设置为 {seconds} 秒")

    @filter.command("设置刷屏禁言时长", "设置刷屏禁言时长（秒）")
    async def set_spam_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["spam_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 刷屏禁言时长已设置为 {seconds} 秒")

    @filter.command("设置骂人禁言时长", "设置骂人禁言时长（秒）")
    async def set_profanity_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["profanity_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 骂人禁言时长已设置为 {seconds} 秒")

    @filter.command("添加骂人关键词", "添加骂人关键词（关键词检测模式）")
    async def add_profanity_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self.config.setdefault("profanity_keywords", [])
        if keyword in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        kws.append(keyword)
        self.config["profanity_keywords"] = kws
        yield event.plain_result(f"[成功] 已添加骂人关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("删除骂人关键词", "删除骂人关键词")
    async def remove_profanity_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self.config.get("profanity_keywords", [])
        if keyword not in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在")
            return
        kws.remove(keyword)
        self.config["profanity_keywords"] = kws
        yield event.plain_result(f"[成功] 已删除骂人关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("查看骂人关键词", "查看骂人关键词列表")
    async def list_profanity_keywords_cmd(self, event: AstrMessageEvent):
        if not await self._moderation_require_admin_msg(event):
            return
        kws = self.config.get("profanity_keywords", [])
        if not kws:
            yield event.plain_result("当前没有设置骂人关键词")
            return
        listing = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(kws)])
        yield event.plain_result(f"骂人关键词列表（{len(kws)} 个）：\n{listing}")

    @filter.command("切换骂人检测模式", "切换 AI 检测 / 关键词检测")
    async def toggle_profanity_mode_cmd(self, event: AstrMessageEvent):
        if not await self._moderation_require_admin_msg(event):
            return
        cur = bool(self.config.get("profanity_use_ai", True))
        self.config["profanity_use_ai"] = not cur
        mode = "AI检测" if not cur else "关键词检测"
        yield event.plain_result(f"[成功] 已切换为 {mode} 模式")

    @filter.command("添加白名单用户", "添加白名单用户（不受违规检测限制）")
    async def add_whitelist_user_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        user_id = str(user_id).strip()
        if not user_id:
            yield event.plain_result("[错误] 请提供QQ号")
            return
        wl = self.config.setdefault("whitelist_users", [])
        if user_id in [str(x) for x in wl]:
            yield event.plain_result(f"[错误] 用户 {user_id} 已在白名单中")
            return
        wl.append(user_id)
        self.config["whitelist_users"] = wl
        yield event.plain_result(f"[成功] 已添加 {user_id} 到白名单（当前 {len(wl)} 人）")

    @filter.command("删除白名单用户", "从白名单移除用户")
    async def remove_whitelist_user_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        user_id = str(user_id).strip()
        if not user_id:
            yield event.plain_result("[错误] 请提供QQ号")
            return
        wl = self.config.get("whitelist_users", [])
        new_wl = [u for u in wl if str(u) != user_id]
        if len(new_wl) == len(wl):
            yield event.plain_result(f"[错误] 用户 {user_id} 不在白名单中")
            return
        self.config["whitelist_users"] = new_wl
        yield event.plain_result(f"[成功] 已从白名单移除 {user_id}（当前 {len(new_wl)} 人）")

    @filter.command("查看白名单", "查看白名单用户列表")
    async def list_whitelist_cmd(self, event: AstrMessageEvent):
        if not await self._moderation_require_admin_msg(event):
            return
        wl = self.config.get("whitelist_users", [])
        if not wl:
            yield event.plain_result("当前白名单为空")
            return
        listing = "\n".join([f"{i+1}. {u}" for i, u in enumerate(wl)])
        yield event.plain_result(f"白名单用户（{len(wl)} 人）：\n{listing}")

    @filter.command("查看违规统计", "查看违规统计（默认全群；带 QQ 号查个人）")
    async def view_violation_stats_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        user_id = (user_id or "").strip()
        if user_id:
            g = self.stats.get("groups", {}).get(str(user_id), {})  # 简化：user_id 当群号查
            counts = g.get("violation_counts", {})
            total = sum(sum(b.values()) for b in counts.values())
            yield event.plain_result(
                f"群 {user_id} 违规统计:\n"
                f"图片: {sum(counts.get('image', {}).values())} 次\n"
                f"刷屏: {sum(counts.get('spam', {}).values())} 次\n"
                f"骂人: {sum(counts.get('profanity', {}).values())} 次\n"
                f"广告: {sum(counts.get('ad', {}).values())} 次\n"
                f"链接: {sum(counts.get('link', {}).values())} 次\n"
                f"群号推广: {sum(counts.get('group_promotion', {}).values())} 次\n"
                f"总计: {total} 次"
            )
        else:
            groups = self.stats.get("groups", {})
            total_users = 0
            total_violations = 0
            for g in groups.values():
                for bucket in g.get("violation_counts", {}).values():
                    total_users += len(bucket)
                    total_violations += sum(bucket.values())
            yield event.plain_result(
                f"违规统计概览:\n违规用户数: {total_users} 人\n总违规次数: {total_violations} 次"
            )

    @filter.command("设置广告禁言时长", "设置广告禁言时长（秒）")
    async def set_ad_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["ad_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 广告禁言时长已设置为 {seconds} 秒")

    @filter.command("设置链接禁言时长", "设置链接禁言时长（秒）")
    async def set_link_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["link_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 链接禁言时长已设置为 {seconds} 秒")

    @filter.command("设置群号推广禁言时长", "设置群号推广禁言时长（秒）")
    async def set_group_promotion_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        if not await self._moderation_require_admin_msg(event):
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self.config["group_promotion_ban_duration"] = seconds
        yield event.plain_result(f"[成功] 群号推广禁言时长已设置为 {seconds} 秒")

    @filter.command("添加广告关键词", "添加广告关键词")
    async def add_ad_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self.config.setdefault("ad_keywords", [])
        if keyword in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        kws.append(keyword)
        self.config["ad_keywords"] = kws
        yield event.plain_result(f"[成功] 已添加广告关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("删除广告关键词", "删除广告关键词")
    async def remove_ad_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._moderation_require_admin_msg(event):
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self.config.get("ad_keywords", [])
        if keyword not in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在")
            return
        kws.remove(keyword)
        self.config["ad_keywords"] = kws
        yield event.plain_result(f"[成功] 已删除广告关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("查看广告关键词", "查看广告关键词列表")
    async def list_ad_keywords_cmd(self, event: AstrMessageEvent):
        if not await self._moderation_require_admin_msg(event):
            return
        kws = self.config.get("ad_keywords", [])
        if not kws:
            yield event.plain_result("当前没有设置广告关键词")
            return
        head = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(kws[:20])])
        more = f"\n…还有 {len(kws) - 20} 个" if len(kws) > 20 else ""
        yield event.plain_result(f"广告关键词（{len(kws)} 个）：\n{head}{more}")

    async def _edit_special_admins(self, event: AstrMessageEvent, target: str, key: str, label: str, add: bool):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        qq_list = list({str(x) for x in qq_list if x})
        if not qq_list:
            action = "添加" if add else "删除"
            yield event.plain_result(f"请提供QQ号，例如 /{action}{label}管理 123456")
            return
        if add:
            changed = self._add_group_override_admins(group_id, key, qq_list)
            verb = "添加"
            empty = "所列QQ号均已存在"
        else:
            changed = self._remove_group_override_admins(group_id, key, qq_list)
            verb = "移除"
            empty = "所列QQ号均不存在"
        detail = ", ".join(changed) if changed else empty
        yield event.plain_result(f"已为群 {group_id} {verb}{label}专项管理员: {detail}")

    # ===================== 群管指令 =====================

    @filter.command("添加插件管理", "按群添加专项权限管理员（兼容旧命令）")
    async def add_group_admin(self, event: AstrMessageEvent, target: str = ""):
        """兼容旧命令：按群添加插件管理员已废弃，改为按群添加专项权限管理员。"""
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        qq_list = list({str(x) for x in qq_list if x})
        if not qq_list:
            yield event.plain_result(
                "按群插件管理已改为专项权限配置。\n"
                "请使用：/添加头衔管理 QQ、/添加管理管理 QQ、/添加踢人管理 QQ"
            )
            return
        added = []
        for key in ("title_admins", "group_admin_admins", "kick_admins"):
            added.extend(self._add_group_override_admins(group_id, key, qq_list))
        yield event.plain_result(
            "已按群添加专项权限管理员: " + (", ".join(sorted(set(added))) if added else "所列QQ号均已存在")
        )

    @filter.command("删除插件管理", "按群移除专项权限管理员（兼容旧命令）")
    async def remove_group_admin(self, event: AstrMessageEvent, target: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        qq_list = list({str(x) for x in qq_list if x})
        if not qq_list:
            yield event.plain_result(
                "按群插件管理已改为专项权限配置。\n"
                "请使用：/删除头衔管理 QQ、/删除管理管理 QQ、/删除踢人管理 QQ"
            )
            return
        removed = []
        for key in ("title_admins", "group_admin_admins", "kick_admins"):
            removed.extend(self._remove_group_override_admins(group_id, key, qq_list))
        yield event.plain_result(
            "已按群移除专项权限管理员: " + (", ".join(sorted(set(removed))) if removed else "所列QQ号均不存在")
        )

    @filter.command("添加头衔管理", "按群添加可设置/取消头衔的专项管理员")
    async def add_title_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "title_admins", "头衔", True):
            yield result

    @filter.command("删除头衔管理", "按群移除可设置/取消头衔的专项管理员")
    async def remove_title_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "title_admins", "头衔", False):
            yield result

    @filter.command("添加管理管理", "按群添加可设置/取消群管理的专项管理员")
    async def add_group_admin_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "group_admin_admins", "群管理", True):
            yield result

    @filter.command("删除管理管理", "按群移除可设置/取消群管理的专项管理员")
    async def remove_group_admin_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "group_admin_admins", "群管理", False):
            yield result

    @filter.command("添加踢人管理", "按群添加可踢人的专项管理员")
    async def add_kick_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "kick_admins", "踢人", True):
            yield result

    @filter.command("删除踢人管理", "按群移除可踢人的专项管理员")
    async def remove_kick_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._edit_special_admins(event, target, "kick_admins", "踢人", False):
            yield result

    @filter.command("设管理", "设置群管理员（支持批量+@）")
    async def set_group_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self.has_group_admin_rights(str(raw.get("user_id")), group_id, raw):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标")
            return
        results = []
        for qq in qq_list:
            ok = await self._set_group_admin(event, group_id, qq, True)
            results.append((qq, ok))
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"设置群管理成功: {', '.join(ok_list)}" if ok_list else "设置群管理全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        yield event.plain_result(msg)

    @filter.command("取消管理", "取消群管理员（支持批量+@；管理员可取消自己）")
    async def unset_group_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            qq_list = [sender_id]
        qq_list = list({str(x) for x in qq_list if x})
        self_cancel = len(qq_list) == 1 and qq_list[0] == sender_id and self._is_sender_group_admin_only(raw)
        if not self.has_group_admin_rights(sender_id, group_id, raw) and not self_cancel:
            yield event.plain_result("只有插件管理员、群管理员或被取消者本人可执行此操作")
            return
        results = []
        for qq in qq_list:
            ok = await self._set_group_admin(event, group_id, qq, False)
            results.append((qq, ok))
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"取消群管理成功: {', '.join(ok_list)}" if ok_list else "取消群管理全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        yield event.plain_result(msg)

    @filter.command("头衔", "设置群头衔（@某人 头衔内容）")
    async def set_group_title_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self.has_title_admin_rights(sender_id, group_id, raw):
            yield event.plain_result("只有插件管理员、头衔管理员或群管理员可执行此操作")
            return
        target_qq = self._extract_at_qq(raw)
        if not target_qq:
            target_qq = sender_id  # 操作对象为空时对自身（允许群管理员自设头衔）
        # 从 raw 消息提取所有 text 拼接，去掉命令前缀，得到完整头衔
        title = self._extract_text(raw).strip()
        for prefix in ("/头衔", "头衔"):
            if title.startswith(prefix):
                title = title[len(prefix):].lstrip()
                break
        # 去掉开头的 @ 提及占位（如果 AstrBot 在 text 中保留了 @xxx）
        import re as _re
        title = _re.sub(r"^@[\w（）()\d]+\s*", "", title)
        if not title:
            yield event.plain_result("请提供群头衔内容")
            return
        ok = await self._set_group_title(event, group_id, target_qq, title)
        yield event.plain_result("设置头衔成功" if ok else "设置头衔失败")

    # #18: 别人昵称 - 设置他人的群昵称
    @filter.command("别人昵称", "设置他人群昵称（需要 @某人 + 新昵称）")
    async def set_other_card_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self.has_group_admin_rights(sender_id, group_id, raw):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        target_qq = self._extract_at_qq(raw)
        if not target_qq:
            yield event.plain_result("请通过 @某人 来指定对象")
            return
        # 从原始消息提取所有 text 段拼接为 card（避免被 @ 组件挤掉）
        card = self._extract_text(raw).strip()
        # 去掉开头的 /别人昵称 命令名（如果存在）
        for prefix in ("/别人昵称", "别人昵称"):
            if card.startswith(prefix):
                card = card[len(prefix):].lstrip()
                break
        if not card:
            yield event.plain_result("请提供新昵称内容")
            return
        ok = await self._set_group_card(event, group_id, target_qq, card)
        yield event.plain_result(f"已将 {target_qq} 的群昵称设为 {card}" if ok else "设置群昵称失败")

    # #18: 改群昵称 - 设置自己的群昵称
    @filter.command("改群昵称", "设置自己的群昵称")
    async def set_self_card_cmd(self, event: AstrMessageEvent, card: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not card:
            yield event.plain_result("请提供新昵称内容")
            return
        ok = await self._set_group_card(event, group_id, sender_id, card)
        yield event.plain_result(f"已将你的群昵称设为 {card}" if ok else "设置群昵称失败")

    @filter.command("禁言", "禁言成员")
    async def mute_cmd(self, event: AstrMessageEvent, target: str = "", minutes: int = 10):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq = self._extract_at_qq(raw)
        if not qq and target:
            # #136：仅在 target 为纯数字 QQ 号时 fallback，避免装饰字符误识别
            clean = re.sub(r"[^\d]", "", target)
            if clean and 5 <= len(clean) <= 12:
                qq = clean
        if not qq:
            yield event.plain_result("请指定要禁言的QQ号")
            return
        target_stripped = (target or "").strip()
        if target_stripped.isdigit():
            try:
                minutes = int(target_stripped)
            except ValueError:
                pass
        ok = await self._mute_member(event, group_id, qq, minutes * 60)
        if ok:
            await self._record_mute_and_maybe_kick(event, group_id, qq, sender_id)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result(f"禁言成功（{minutes}分钟）" if ok else "禁言失败")

    @filter.command("解禁", "解除禁言")
    async def unmute_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        qq = self._extract_at_qq(raw)
        if not qq and target:
            # #136：仅在 target 为纯数字 QQ 号时 fallback，避免装饰字符 @用户名 误识别
            clean = re.sub(r"[^\d]", "", target)
            if clean and 5 <= len(clean) <= 12:
                qq = clean
        if not qq:
            return
        ok = await self._unmute_member(event, group_id, qq)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result("解禁成功" if ok else "解禁失败")

    @filter.command("踢", "踢出群成员（支持批量+@）")
    async def kick_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self.has_kick_admin_rights(sender_id, group_id, raw):
            yield event.plain_result("只有插件管理员、踢人管理员或群管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标")
            return
        results = []
        recalled_total = 0
        kick_recall_enabled = bool(self.get_group_setting(group_id, "kick_recall_enabled", False))
        kick_recall_count = max(1, min(int(self.get_group_setting(group_id, "kick_recall_count", 10) or 10), 50))
        for qq in qq_list:
            # #145：踢人前撤回该成员近期消息（踢出后无法再拉取其历史）
            recalled = 0
            if kick_recall_enabled:
                recalled = await self._recall_user_recent_msgs(event, group_id, qq, kick_recall_count)
                recalled_total += recalled
            ok = await self._kick_member(event, group_id, qq)
            if ok and self.config.get("reject_re_add", False):
                await self._execute_action(event, "reject_add", group_id=group_id, user_id=qq)
            results.append((qq, ok))
            if recalled:
                logger.info(f"踢人前撤回 {group_id}/{qq} 近期 {recalled} 条消息")
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"踢出成功: {', '.join(ok_list)}" if ok_list else "踢出全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        if kick_recall_enabled and recalled_total > 0:
            msg += f"\n已撤回被踢成员近期消息 {recalled_total} 条"
        yield event.plain_result(msg)

    @filter.command("清用户历史", "撤回某用户在本群的最近 N 条消息（/清用户历史 @某人 [N]）")
    async def clear_user_history_cmd(self, event: AstrMessageEvent, target: str = ""):
        """手动撤回某用户在群内的最近 N 条消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian）。
        不踢人，仅撤回。"""
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq_list = self._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标，例如：/清用户历史 @某人 20")
            return
        tail_nums = re.findall(r"\d+", target or "")
        count = int(tail_nums[-1]) if tail_nums else 10
        count = max(1, min(count, 50))
        total = 0
        for qq in qq_list:
            recalled = await self._recall_user_recent_msgs(event, group_id, qq, count)
            total += recalled
        yield event.plain_result(f"已尝试撤回 {len(qq_list)} 个用户的最近消息，实际撤回 {total} 条（上限 {count} 条/人）")

    @filter.command("撤回", "撤回消息（/撤回 + 引用消息 / /撤回 @用户 N / /撤回 N）")
    async def recall_cmd(self, event: AstrMessageEvent):
        """统一分发器（#109 #110 #117 #118，修复 #122）：
        - 引用消息 -> 撤回引用消息
        - @用户 + N -> 撤回该用户最近 N 条
        - 仅有 N -> 撤回最近 N 条（不含指令本身，最多 50）
        """
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id", ""))):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return

        reply_id = self._get_reply_id(event)
        target_qq = self._extract_at_qq(raw)
        self_msg_id = str(raw.get("message_id", "")) if raw.get("message_id") else ""
        # 撤回成功提示：show_recall_notice 控制（全局或按群覆盖），关闭时成功静默；失败仍提示。
        recall_notice = bool(self.get_group_setting(
            group_id, "show_recall_notice", self.config.get("show_recall_notice", True)))

        # 参数文本：从 raw 文本段提取（@ 单独成段，避免 QQ 号混入数字）
        tail = self._extract_command_tail(self._extract_text(raw), ("撤回",))
        tail = re.sub(r"@\d+", "", tail)  # 去掉 @QQ 防止 QQ 号被当作编号
        all_numbers = [int(x) for x in re.findall(r"\d+", tail)]

        # 1) 引用消息优先（保持原有语义）
        if reply_id:
            ok = await self._recall_message(event, reply_id)
            if ok and recall_notice:
                yield event.plain_result("撤回成功")
            elif not ok:
                yield event.plain_result("撤回失败")
            return

        # 2) @用户 + N：撤回该用户最近 N 条（#110 #117）
        if target_qq:
            n = all_numbers[0] if all_numbers else 1
            n = max(1, min(n, 50))
            snapshot = await self._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result(
                    "当前 OneBot 实现不支持按用户撤回（缺少 get_group_msg_history，且插件本地历史为空）。\n"
                    "请使用 /撤回 + 引用消息 撤回指定消息。"
                )
                return
            if len([m for m in snapshot if m[3] == str(target_qq)]) < n:
                await self._load_history_from_api(event, group_id)
                snapshot = await self._get_history_snapshot(event, group_id, self_msg_id)
            candidates = [m for m in snapshot if m[3] == str(target_qq)][:n]
            recalled = 0
            for m in candidates:
                ok, err = await self._do_recall(event, m[0])
                if ok:
                    recalled += 1
                    self._remove_message_from_history(group_id, m[0])
                elif "已撤回" in err:
                    self._remove_message_from_history(group_id, m[0])
            if recalled:
                if recall_notice:
                    yield event.plain_result(f"撤回成功（{recalled} 条，用户 {target_qq}）")
            else:
                yield event.plain_result("撤回失败，未找到该用户的可撤回消息")
            return

        # 3) 仅数量：撤回最近 N 条（#109，不撤回指令本身；#118 本地历史兜底）
        if all_numbers:
            n = all_numbers[0]
            if n <= 0:
                yield event.plain_result("撤回数量必须为正整数。")
                return
            n = max(1, min(n, 50))
            snapshot = await self._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result(
                    "撤回失败：当前 OneBot 实现不支持 get_group_msg_history，且插件本地历史为空。\n"
                    "请使用 /撤回 + 引用消息 撤回指定消息。"
                )
                return
            if len(snapshot) < n:
                await self._load_history_from_api(event, group_id)
                snapshot = await self._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result("撤回失败：本地历史为空，无可撤回消息。")
                return
            recalled = 0
            for m in snapshot[:n]:
                ok, err = await self._do_recall(event, m[0])
                if ok:
                    recalled += 1
                    self._remove_message_from_history(group_id, m[0])
                elif "已撤回" in err:
                    self._remove_message_from_history(group_id, m[0])
            if recalled:
                if recall_notice:
                    yield event.plain_result(f"撤回成功（{recalled} 条）")
            else:
                yield event.plain_result("撤回失败，未找到可撤回消息")
            return

        # 4) 用法提示
        yield event.plain_result(
            "用法：\n"
            "/撤回 + 引用消息：撤回引用消息\n"
            "/撤回 @用户 N：撤回该用户最近 N 条\n"
            "/撤回 N：撤回最近 N 条（最多 50，不含指令本身）"
        )

    @filter.command("撤回自身", "撤回机器人最近发送的消息（/撤回自身 N）")
    async def recall_self_cmd(self, event: AstrMessageEvent):
        """撤回机器人自身发送的消息（修复 #122）。"""
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id", ""))):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return

        tail = self._extract_command_tail(self._extract_text(raw), ("撤回自身",))
        nums = re.findall(r"\d+", tail)
        if not nums:
            yield event.plain_result("请在指令后填写需要撤回的数量，例如：撤回自身 5")
            return
        count = int(nums[-1])
        if count <= 0:
            yield event.plain_result("撤回数量必须为正整数。")
            return
        count = max(1, min(count, 50))

        current_msg_id = raw.get("message_id")
        snapshot = await self._get_history_snapshot(event, group_id, current_msg_id)
        bot_messages = [m for m in snapshot if m[5]]  # is_bot=True
        if not bot_messages:
            yield event.plain_result("未找到可撤回的机器人消息。")
            return
        success = 0
        failed_msgs = []
        for m in bot_messages:
            if success >= count:
                break
            ok, err = await self._do_recall(event, m[0])
            if ok:
                success += 1
                self._remove_message_from_history(group_id, m[0])
            elif "已撤回" not in err:
                failed_msgs.append(f"{m[0]}({err})")
            else:
                self._remove_message_from_history(group_id, m[0])
        msg = f"已尝试撤回机器人最近 {success} 条消息。"
        if failed_msgs:
            msg += f"\n失败: {', '.join(failed_msgs[:5])}"
        yield event.plain_result(msg)

    @filter.command("设精", "设置精华消息")
    async def essence_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._is_group_admin(raw) and not self._is_group_owner(raw):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条消息后使用该指令")
            return
        ok = await self._set_essence(event, reply_id, group_id=str(raw.get("group_id")))
        yield event.plain_result("设精成功" if ok else "设精失败")

    @filter.command("取消设精", "取消精华消息")
    async def cancel_essence_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._is_group_admin(raw) and not self._is_group_owner(raw):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条精华消息后使用该指令")
            return
        ok = await self._delete_essence(event, reply_id, group_id=str(raw.get("group_id")))
        yield event.plain_result("取消设精成功" if ok else "取消设精失败")

    # #24: 改群头像
    @filter.command("改群头像", "引用图片回复即可修改群头像")
    async def set_group_avatar_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        image_url = self._extract_image_url(event)
        if not image_url:
            yield event.plain_result("请引用一条图片消息，或在消息中附带图片")
            return
        ok = await self._set_group_avatar(event, group_id, image_url)
        yield event.plain_result("群头像已更新" if ok else "修改群头像失败")

    # #79: 宵禁 - 全体禁言
    @filter.command("宵禁", "开启全群禁言")
    async def whole_ban_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        ok = await self._execute_action(event, "set_group_whole_ban",
                                        group_id=group_id, enable=True)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result("已开启全群禁言" if ok else "开启失败")

    # #79: 解除宵禁 - 解除全体禁言
    @filter.command("解除宵禁", "关闭全群禁言")
    async def unwhole_ban_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        ok = await self._execute_action(event, "set_group_whole_ban",
                                        group_id=group_id, enable=False)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result("已解除全群禁言" if ok else "解除失败")

    # #75: 禁我 [分钟] - 任意成员禁言自己
    @filter.command("禁我", "禁言自己，格式：/禁我 [分钟]，默认10分钟")
    async def mute_self_cmd(self, event: AstrMessageEvent, minutes: int = 10):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        minutes = max(1, min(int(minutes), 43200))  # 限制 1 分钟 ~ 30 天
        ok = await self._mute_member(event, group_id, sender_id, minutes * 60)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result(f"已禁言自己 {minutes} 分钟" if ok else "禁言失败")

    # #76: 群昵称 新昵称 - 插件管理员修改任意成员昵称
    @filter.command("群昵称", "设置指定成员群昵称（仅插件管理员）")
    async def set_member_card_cmd(self, event: AstrMessageEvent, target: str = "", card: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq = self._extract_at_qq(raw) or self._parse_qq(target)
        if not qq:
            yield event.plain_result("请通过 @某人 或提供QQ号")
            return
        if not card:
            yield event.plain_result("请提供新昵称内容")
            return
        ok = await self._set_group_card(event, group_id, qq, card)
        yield event.plain_result(f"已将 {qq} 群昵称设为 {card}" if ok else "设置群昵称失败")

    # #16: 群公告
    @filter.command("发群公告", "发送群公告")
    async def send_group_notice_cmd(self, event: AstrMessageEvent, content: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        if not content:
            yield event.plain_result("请提供公告内容")
            return
        # 尝试通过 send_group_notice / _send_group_notice API
        ok = await self._execute_action(event, "_send_group_notice",
                                        group_id=group_id, content=content)
        if not ok:
            ok = await self._execute_action(event, "send_group_notice",
                                            group_id=group_id, content=content)
        if ok:
            yield event.plain_result("群公告已发布")
        else:
            # 退化为普通消息提示
            await self._send(event, [Plain(f"[群公告] {content}")])
            yield event.plain_result("当前框架不支持发群公告，已以普通消息发送")

    # #29: 鞭尸禁言 + 发言排名
    @filter.command("鞭尸", "长期禁言被@的人（29天23小时59分）")
    async def whip_corpse_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        qq = self._extract_at_qq(raw)
        if not qq:
            yield event.plain_result("请 @要鞭尸的成员")
            return
        # 29天23小时59分 = 29*86400 + 23*3600 + 59*60 = 2591640 秒
        duration = 29 * 86400 + 23 * 3600 + 59 * 60
        ok = await self._mute_member(event, group_id, qq, duration)
        if ok:
            await self._record_mute_and_maybe_kick(event, group_id, qq, sender_id)
        if self._should_notify_mute(group_id, ok):
            yield event.plain_result("已鞭尸" if ok else "鞭尸失败")

    @filter.command("排名", "查看本群发言排名")
    async def rank_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        top_n = int(self.config.get("rank_top_n", 10))
        ranked = self.get_rank(group_id, top_n)
        if not ranked:
            yield event.plain_result("暂无发言数据")
            return
        lines = [f"{i+1}. {qq} - {cnt}条" for i, (qq, cnt) in enumerate(ranked)]
        yield event.plain_result(f"本群发言排名（Top {len(ranked)}）：\n" + "\n".join(lines))

    @filter.command("清除数据", "清除本群发言计数")
    async def clear_rank_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        self.reset_group_stats(group_id)
        yield event.plain_result("已清除本群发言数据，重新开始计数")

    # #21: 举报违规
    @filter.command("举报", "举报群成员违规行为（需要引用消息）")
    async def report_cmd(self, event: AstrMessageEvent, reason: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        reporter_id = str(raw.get("user_id"))
        target_qq = self._extract_at_qq(raw)
        if not target_qq:
            yield event.plain_result("请 @要举报的成员")
            return
        # #140: 群主豁免 — 群主不触发举报
        reporter_role = raw.get("sender", {}).get("role", "")
        if reporter_role == "owner":
            yield event.plain_result("群主无需举报")
            return
        reply_id = self._get_reply_id(event)
        record = {
            "group_id": group_id,
            "reporter_id": reporter_id,
            "target_qq": target_qq,
            "reason": reason or "（未提供原因）",
            "message_id": reply_id,
            "time": int(time.time()),
        }
        self.reports.setdefault("pending", []).append(record)
        self.save_reports()
        # #140: 按角色分级路由通知
        # 查被举报人角色
        target_role = ""
        info = await self._execute_action(event, "get_group_member_info",
                                          group_id=group_id, user_id=target_qq,
                                          return_raw=True, no_cache=True)
        if isinstance(info, dict):
            data = info.get("data") or info
            target_role = data.get("role", "")
        report_text = (f"[举报] 群 {group_id}\n"
                       f"举报人: {reporter_id}\n"
                       f"被举报: {target_qq}\n"
                       f"原因: {record['reason']}")
        # 被举报人是管理员 → 仅通知群主；普通成员 → 通知所有管理员 + 群主
        if target_role in ("admin", "owner"):
            # 仅通知群主
            owner_qq = await self._find_group_owner(event, group_id)
            if owner_qq:
                await self._send_private_msg(str(owner_qq), report_text)
        else:
            # 通知所有管理员 + 群主
            await self._notify_admins(report_text, group_id=group_id)
        yield event.plain_result("已提交举报，管理员会尽快处理")

    # ===================== 新增命令（#131 #135 #139 #146 #150 #151）=====================

    # #135: /禁言列表 — 查看本群当前被禁言成员
    @filter.command("禁言列表", "查看本群当前被禁言成员列表")
    async def mute_list_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        member_list = await self._execute_action(event, "get_group_member_list",
                                                  group_id=group_id, return_raw=True)
        muted = []
        if isinstance(member_list, dict):
            data = member_list.get("data") or member_list
            if isinstance(data, list):
                for m in data:
                    if not isinstance(m, dict):
                        continue
                    mute_left = m.get("mute_left", 0)
                    if isinstance(mute_left, (int, float)) and mute_left > 0:
                        uid = str(m.get("user_id", ""))
                        nick = m.get("nickname", "")
                        card = m.get("card", "") or ""
                        name = card if card else nick
                        minutes = max(1, int(mute_left / 60))
                        muted.append(f"{uid}（{name}）剩余 {minutes} 分钟")
        if not muted:
            yield event.plain_result("本群当前无被禁言成员")
            return
        text = f"本群禁言列表（{len(muted)} 人）：\n" + "\n".join(muted)
        yield event.plain_result(text)

    # #146: /给我头衔 — 普通成员自设群头衔
    @filter.command("给我头衔", "自设群头衔（/给我头衔 标题内容）")
    async def self_title_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        title = self._extract_text(raw).strip()
        for prefix in ("/给我头衔", "给我头衔"):
            if title.startswith(prefix):
                title = title[len(prefix):].lstrip()
                break
        import re as _re
        title = _re.sub(r"^@[\w（）()\d]+\s*", "", title)
        if not title:
            yield event.plain_result("请提供群头衔内容，例如 /给我头衔 传说")
            return
        if len(title) > 60:
            yield event.plain_result("头衔内容过长（最多60字符）")
            return
        ok = await self._set_group_title(event, group_id, sender_id, title)
        yield event.plain_result("设置头衔成功" if ok else "设置头衔失败（Bot可能无权限或头衔功能不可用）")

    # #131: /添加群待办 — 引用消息设为群待办（群管/群主）
    @filter.command("添加群待办", "引用消息设为群待办")
    async def add_group_todo_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条消息后发送此命令")
            return
        ok = await self._execute_action(event, "_set_group_todo",
                                        group_id=group_id, message_id=int(reply_id))
        if ok is None:
            ok = await self._execute_action(event, "set_group_todo",
                                            group_id=group_id, message_id=int(reply_id))
        yield event.plain_result("已设为群待办" if ok else "设置群待办失败（当前 OneBot 实现可能不支持此 API）")

    # #139: /取消群待办 — 引用消息取消群待办（群管/群主）
    @filter.command("取消群待办", "引用消息取消群待办")
    async def delete_group_todo_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条群待办消息后发送此命令")
            return
        ok = await self._execute_action(event, "_delete_group_todo",
                                        group_id=group_id, message_id=int(reply_id))
        if ok is None:
            ok = await self._execute_action(event, "delete_group_todo",
                                            group_id=group_id, message_id=int(reply_id))
        yield event.plain_result("已取消群待办" if ok else "取消群待办失败（当前 OneBot 实现可能不支持此 API）")

    # #150: /加群申请待处理 — 查看待处理加群申请（群管/群主）
    @filter.command("加群申请待处理", "查看本群未处理的加群申请列表")
    async def pending_join_requests_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        result = await self._execute_action(event, "get_group_apply_list",
                                            group_id=group_id, return_raw=True)
        if not result:
            yield event.plain_result("获取加群申请列表失败（当前 OneBot 实现可能不支持此 API）")
            return
        applies = []
        if isinstance(result, dict):
            data = result.get("data") or result
            if isinstance(data, list):
                for a in data:
                    if not isinstance(a, dict):
                        continue
                    # 只显示未处理的
                    sub_type = a.get("sub_type", "")
                    if sub_type not in ("add", ""):
                        continue
                    uid = str(a.get("user_id", ""))
                    nick = a.get("nickname", "")
                    comment = a.get("comment", "")
                    ts = a.get("time", 0)
                    time_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未知"
                    applies.append(f"{uid}（{nick}）| {time_str}\n验证消息: {comment or '无'}")
        if not applies:
            yield event.plain_result("本群当前无待处理的加群申请")
            return
        text = f"待处理加群申请（{len(applies)} 条）：\n\n" + "\n\n".join(applies)
        yield event.plain_result(text)

    # #151: /群信息 — 查看本群资料（任何成员可用）
    @filter.command("群信息", "查看本群资料（名称/号/标签/人数）")
    async def group_info_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        result = await self._execute_action(event, "get_group_info",
                                            group_id=group_id, return_raw=True)
        if not result:
            yield event.plain_result("获取群信息失败")
            return
        info = {}
        if isinstance(result, dict):
            data = result.get("data") or result
            if isinstance(data, dict):
                info = data
        name = info.get("group_name") or info.get("name") or "未知"
        gid = info.get("group_id") or group_id
        member_count = info.get("member_count") or info.get("member_count") or "?"
        tags = info.get("tags") or info.get("group_tags") or []
        tags_str = ", ".join(str(t) for t in tags) if tags else "无"
        text = f"群名称: {name}\n群号: {gid}\n群标签: {tags_str}\n成员数: {member_count}"
        yield event.plain_result(text)

# #164: /群相册 — 引用图片消息上传到群相册（群管/群主）
    @filter.command("群相册", "引用图片上传到群相册（/群相册 相册名）")
    async def group_album_upload_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条图片消息后发送此命令")
            return
        text = self._extract_text(raw).strip()
        # 用正则统一处理前缀，避免边界输入裁错
        text = re.sub(r"^[/]?群相册\s*", "", text)
        if not text:
            yield event.plain_result("请提供相册名，例如 /群相册 表情包")
            return
        # 提取被引用消息的图片 URL
        # 通过 get_msg API 拉取原消息段
        msg = await self._execute_action(event, "get_msg",
                                          message_id=int(reply_id), return_raw=True)
        if not msg:
            yield event.plain_result("无法获取引用消息内容")
            return
        msg_data = msg.get("data") if isinstance(msg, dict) else msg
        image_url = ""
        if isinstance(msg_data, dict):
            segs = msg_data.get("message") or []
            for seg in segs:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "image":
                    image_url = (seg.get("data") or {}).get("url", "") or (seg.get("data") or {}).get("file", "")
                    if image_url:
                        break
        if not image_url:
            yield event.plain_result("引用消息中未找到图片")
            return
        # 1) 尝试创建相册目录；失败时尝试 get_group_file_list 找已有目录
        folder_id = ""
        folder_result = await self._execute_action(event, "create_group_file_folder",
                                                    group_id=int(raw["group_id"]),
                                                    folder_name=text, return_raw=True)
        if isinstance(folder_result, dict):
            data = folder_result.get("data") or folder_result
            if isinstance(data, dict):
                folder_id = str(data.get("folder_id") or data.get("id") or "")
        if not folder_id:
            # 目录已存在时：列出根目录，匹配同名
            list_result = await self._execute_action(event, "get_group_file_list",
                                                     group_id=int(raw["group_id"]),
                                                     folder_id="", return_raw=True)
            if isinstance(list_result, dict):
                list_data = list_result.get("data") or list_result
                # 宽松兜底：尝试多种 OneBot 实现的文件夹列表字段
                items = (
                    list_data.get("folders")
                    or list_data.get("file_list")
                    or list_data.get("items")
                    or []
                )
                if not isinstance(items, list):
                    logger.warning(
                        f"gm: get_group_file_list 返回结构异常: {list_data}"
                    )
                    items = []
                for item in items:
                    if isinstance(item, dict) and str(item.get("folder_name", "")) == text:
                        folder_id = str(item.get("folder_id", ""))
                        break
        # 2) 上传文件到群文件
        # 截取 URL 文件名做默认显示名
        file_name = image_url.rsplit("/", 1)[-1].split("?")[0] or "image.jpg"
        ok = await self._execute_action(event, "upload_group_file",
                                         group_id=int(raw["group_id"]),
                                         file=image_url, name=file_name,
                                         folder_id=folder_id or "")
        if not ok:
            # 部分 OneBot 实现：folder_id 不允许空字符串
            ok = await self._execute_action(event, "upload_group_file",
                                             group_id=int(raw["group_id"]),
                                             file=image_url, name=file_name)
        yield event.plain_result(
            f"已上传到群相册「{text}」" if ok
            else "上传到群相册失败（当前 OneBot 实现可能不支持群相册 API）"
        )

# #166: /群名称 — 修改本群名（群管/群主）
    @filter.command("群名称", "修改本群名称（/群名称 新群名）")
    async def set_group_name_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        text = self._extract_text(raw).strip()
        for prefix in ("/群名称", "群名称"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        if not text:
            yield event.plain_result("请提供新群名，例如 /群名称 我的群")
            return
        if len(text) > 60:
            yield event.plain_result("群名过长（最多60字符）")
            return
        ok = await self._execute_action(event, "set_group_name",
                                        group_id=group_id, group_name=text)
        yield event.plain_result(f"已修改群名为「{text}」" if ok else "修改群名失败（当前 OneBot 实现可能不支持此 API，或机器人权限不足）")

    # #163: /群标签 — 添加群标签（群管/群主）
    @filter.command("群标签", "添加群标签（/群标签 标签名）")
    async def set_group_tag_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        text = self._extract_text(raw).strip()
        for prefix in ("/群标签", "群标签"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        if not text:
            yield event.plain_result("请提供标签内容，例如 /群标签 编程交流")
            return
        if len(text) > 20:
            yield event.plain_result("标签过长（最多20字符）")
            return
        ok = await self._execute_action(event, "set_group_tag",
                                        group_id=group_id, tag=text)
        yield event.plain_result(f"已添加群标签「{text}」" if ok else "添加群标签失败（当前 OneBot 实现可能不支持此 API）")

    # #162: /添加违禁图片 — 引用图片消息加入违禁图列表（群管/群主）
    @filter.command("添加违禁图片", "引用图片消息加入违禁图列表")
    async def add_banned_image_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请先在群聊发送图片，然后引用该图片回复 /添加违禁图片")
            return
        # 拉取被引用消息中的图片
        msg = await self._execute_action(event, "get_msg",
                                          message_id=int(reply_id), return_raw=True)
        if not msg:
            yield event.plain_result("无法获取引用消息内容")
            return
        msg_data = msg.get("data") if isinstance(msg, dict) else msg
        image_url = ""
        if isinstance(msg_data, dict):
            for seg in (msg_data.get("message") or []):
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "image":
                    image_url = (seg.get("data") or {}).get("url", "") or (seg.get("data") or {}).get("file", "")
                    if image_url:
                        break
        if not image_url:
            yield event.plain_result("引用消息中未找到图片")
            return
        md5, truncated = await self._compute_image_md5(image_url)
        if truncated:
            yield event.plain_result("图片过大（超过 10MB），无法加入违禁列表")
            return
        if not md5:
            yield event.plain_result("下载图片失败，无法计算 MD5")
            return
        # 写入本群覆盖（统一走 _get_group_override_list，与 /设置群配置 同一持久化入口）
        banned = self._get_group_override_list(group_id, "banned_images")
        if md5 in banned:
            yield event.plain_result("该图片已在本群违禁列表中")
            return
        banned.append(md5)
        try:
            self.save_config()
        except Exception as e:
            # 持久化失败时回滚内存态，避免用户下次被提示『已存在』
            banned.remove(md5)
            logger.error(f"保存违禁图片配置失败: {e}")
            yield event.plain_result("保存失败，未添加该违禁图片")
            return
        yield event.plain_result(f"已添加违禁图片（MD5: {md5[:8]}...），本群现有 {len(banned)} 张违禁图")

    # #162: /删除违禁图片 — 删除本群某张违禁图（群管/群主）
    @filter.command("删除违禁图片", "删除本群某张违禁图（/删除违禁图片 <md5前8位>）")
    async def remove_banned_image_cmd(self, event: AstrMessageEvent, md5_prefix: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        # #162 review: 校验 md5_prefix 为 hex 字符（仅 0-9a-f），避免与未来 sha256 等格式冲突
        md5_prefix = (md5_prefix or "").lower()
        if not re.fullmatch(r"[0-9a-f]{4,32}", md5_prefix):
            yield event.plain_result("MD5 前缀必须为 4-32 位 hex 字符（0-9a-f），例如 /删除违禁图片 1a2b3c4d")
            return
        gconf = self._get_group_override_list(group_id, "banned_images")
        banned = list(gconf)
        if not banned:
            yield event.plain_result("本群无违禁图片")
            return
        matches = [m for m in banned if str(m).startswith(md5_prefix)]
        if not matches:
            yield event.plain_result(f"未找到 MD5 前缀为 {md5_prefix} 的违禁图")
            return
        if len(matches) > 1:
            yield event.plain_result(f"匹配到多张（{len(matches)}），请用更长的前缀：\n" + "\n".join(matches))
            return
        target = matches[0]
        self._get_group_override_list(group_id, "banned_images").remove(target)
        try:
            self.save_config()
        except Exception as e:
            # 持久化失败时回滚内存态
            self._get_group_override_list(group_id, "banned_images").append(target)
            logger.error(f"保存违禁图片配置失败: {e}")
            yield event.plain_result("保存失败，未删除该违禁图片")
            return
        yield event.plain_result(f"已删除违禁图片 {target}，本群剩余 {len(banned) - 1} 张")

    # #162: /查看违禁图片 — 查看本群违禁图列表（群管/群主）
    @filter.command("查看违禁图片", "查看本群违禁图片列表")
    async def list_banned_images_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        banned = self._get_group_override_list(group_id, "banned_images")
        global_banned = self.config.get("banned_images", [])
        lines = []
        if banned:
            lines.append(f"本群违禁图（{len(banned)} 张）：")
            for m in banned:
                lines.append(f"  - {m}")
        if isinstance(global_banned, list) and global_banned:
            lines.append(f"\n全局违禁图（{len(global_banned)} 张）：")
            for m in global_banned:
                lines.append(f"  - {m}")
        if not lines:
            yield event.plain_result("本群与全局均无违禁图片")
            return
        yield event.plain_result("\n".join(lines))

    # ===================== 状态查看 =====================

    # #74: 设置群配置（仅插件管理员）
    @filter.command("设置群配置", "为本群覆盖插件配置项：/设置群配置 <key> <value>")
    async def set_group_config_cmd(self, event: AstrMessageEvent, key: str = "", value: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        if not key:
            yield event.plain_result(
                "用法：/设置群配置 <key> <value>\n"
                "示例：/设置群配置 show_recall_notice false\n"
                "      /设置群配置 rank_top_n 20\n"
                "\n"
                "支持 key（按群覆盖）：\n"
                "show_recall_notice（撤回消息发送提示，bool）\n"
                "mute_notice（禁言/解禁回复结果，bool）\n"
                "reject_re_add（踢人后拒绝再次加群，bool）\n"
                "auto_recall_keywords（Bot发言自动撤回关键词，list）\n"
                "auto_recall_enabled_groups（启用自动撤回的群ID，list 或 true 全启用）\n"
                "rank_top_n（发言排名显示人数，int）\n"
                "report_notify_admins（接收举报通知的QQ，list）\n"
                "join_approve_keywords（加群自动同意关键词，list）\n"
                "join_notify_admins（加群请求通知QQ，list）\n"
                "join_request_notify_in_group（群内提醒加群申请，bool）\n"
                "join_reject_reason（自动拒绝加群理由，string）\n"
                "join_audit_enabled（加群申请自动审核总开关，bool）\n"
                "enabled_groups（违规检测启用，bool/true/false）\n"
                "title_admins（可设置头衔的QQ，list）\n"
                "group_admin_admins（可设群管的QQ，list）\n"
                "kick_admins（可踢人的QQ，list）\n"
                "kick_recall_enabled（踢人撤回历史，bool）\n"
                "kick_recall_count（踢人撤回条数，int 1-50）\n"
                "max_message_history（撤回历史缓存条数，int）\n"
                "voice_check_enabled（语音违规检测开关，bool）\n"
                "whitelist_users（违规检测白名单，list）\n"
                "notify_on_violation（违规时群内通知，bool）\n"
                "\n详细列表与说明见 _conf_schema.json 中各字段的 description。"
            )
            return
        # 类型转换
        parsed_value: object = value
        if value.lower() in ("true", "false"):
            parsed_value = (value.lower() == "true")
        elif value.isdigit():
            parsed_value = int(value)
        elif value.startswith("[") and value.endswith("]"):
            try:
                parsed_value = json.loads(value)
            except Exception:
                parsed_value = [v.strip() for v in value.strip("[]").split(",") if v.strip()]
        overrides = self.config.setdefault("group_overrides", {})
        gconf = overrides.setdefault(group_id, {})
        gconf[key] = parsed_value
        self.save_config()
        yield event.plain_result(f"已为本群设置 {key} = {parsed_value}")

    @filter.command("查看群配置", "查看本群生效的配置覆盖")
    async def view_group_config_cmd(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        gconf = self.config.get("group_overrides", {}).get(group_id, {})
        if not gconf:
            yield event.plain_result("本群未设置任何覆盖（全部使用全局默认）")
            return
        lines = [f"{k}: {v}" for k, v in gconf.items()]
        yield event.plain_result(f"本群覆盖配置：\n" + "\n".join(lines))

    @filter.command("清除群配置", "清除本群所有覆盖")
    async def clear_group_config_cmd(self, event: AstrMessageEvent, key: str = ""):
        raw = self._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        gconf = self.config.get("group_overrides", {}).get(group_id, {})
        if key:
            if key not in gconf:
                yield event.plain_result(f"本群未设置 {key}")
                return
            del gconf[key]
            self.save_config()
            yield event.plain_result(f"已清除本群 {key} 覆盖")
        else:
            if group_id in self.config.get("group_overrides", {}):
                del self.config["group_overrides"][group_id]
                self.save_config()
            yield event.plain_result("已清除本群所有覆盖")

    @filter.command("status", "查看插件配置")
    async def status_cmd(self, event: AstrMessageEvent):
        c = self.config
        raw = self._get_raw_message(event)
        group_id = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        lines = [
            f"show_recall_notice: {c.get('show_recall_notice', True)}",
            f"reject_re_add: {c.get('reject_re_add', False)}",
            f"auto_recall_keywords: {c.get('auto_recall_keywords', [])}",
            f"violation_keywords: {len(c.get('violation_keywords', []))} 个",
            f"rank_top_n: {c.get('rank_top_n', 10)}",
        ]
        if group_id:
            overrides = self.config.get("group_overrides", {}).get(group_id, {})
            lines.extend([
                f"本群 title_admins: {', '.join(map(str, self.get_group_setting(group_id, 'title_admins', []))) or '空'}",
                f"本群 group_admin_admins: {', '.join(map(str, self.get_group_setting(group_id, 'group_admin_admins', []))) or '空'}",
                f"本群 kick_admins: {', '.join(map(str, self.get_group_setting(group_id, 'kick_admins', []))) or '空'}",
                f"本群 mute_kick_threshold: {self.get_group_setting(group_id, 'mute_kick_threshold', 0)}"
                f"{'（按群覆盖）' if 'mute_kick_threshold' in overrides else '（全局默认）'}",
            ])
        yield event.plain_result("插件配置：\n" + "\n".join(lines))

    # ===================== 全消息监听 =====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息：发言计数 + 违规检测。"""
        raw = self._get_raw_message(event)
        if not raw or not isinstance(raw, dict):
            return
        if raw.get("post_type") != "message":
            return
        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))
        # 跳过 bot 自身
        if str(raw.get("self_id", "")) == user_id:
            return

        # 发言计数（#29）
        self._increment_message_count(group_id, user_id)

        # 撤回消息历史：记录该群消息（插件指令消息不记录，避免编号偏移）
        self._record_message_to_history(group_id, raw)

        # 群违规检测（合并自参考插件，#19 + 图片/刷屏/骂人/广告/链接/群号推广）
        await self._moderation_dispatch(event, raw, group_id, user_id)

        # 加群申请引用回复处理（#57）
        reply_id = self._get_reply_id(event)
        has_permission = self._is_authorized(raw, user_id)
        if reply_id and has_permission:
            pending = self.config.get("pending_join_requests", {})
            info = pending.get(str(reply_id))
            if info:
                msg_text = self._extract_text(raw)
                if msg_text:
                    approve = "同意" in msg_text
                    deny = "拒绝" in msg_text
                    if approve or deny:
                        # #129: 拒绝时支持自定义理由（从 "拒绝 理由" 中提取）
                        reject_reason = "管理员审核"
                        if deny:
                            parts = msg_text.split("拒绝", 1)
                            custom = parts[1].strip() if len(parts) > 1 else ""
                            reject_reason = custom if custom else self.get_group_setting(
                                group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"
                        await self._handle_group_request(event, info["flag"], approve, reject_reason)
                        result = "同意" if approve else "拒绝"
                        # 清理已处理的记录
                        del pending[str(reply_id)]
                        self.save_config()
                        yield event.plain_result(f"已{result} {info['user_id']} 的加群申请")
                        return

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_event(self, event: AstrMessageEvent):
        raw = self._get_raw_message(event)
        if not raw or not isinstance(raw, dict):
            return

        # 禁言通知统计（#103）
        if raw.get("post_type") == "notice" and raw.get("notice_type") == "group_ban":
            group_id = str(raw.get("group_id"))
            target_id = str(raw.get("user_id", ""))
            operator_id = str(raw.get("operator_id", ""))
            try:
                duration = int(raw.get("duration", 0) or 0)
            except (TypeError, ValueError):
                duration = 0
            if target_id and duration > 0:
                await self._record_mute_and_maybe_kick(event, group_id, target_id, operator_id)
            return

        # 入群欢迎
        if raw.get("post_type") == "notice" and raw.get("notice_type") == "group_increase":
            group_id = str(raw.get("group_id"))
            if self.config.get("groups", {}).get(group_id, {}).get("welcome_enabled", False):
                welcome = self.config["groups"][group_id].get("welcome_message", "欢迎 {at} 加入本群！")
                content = welcome.replace("{at}", f"@{raw.get('user_id')}")
                await self._send(event, self._build_text(content))
            return

        # 加群请求处理（#27 合并 group_manager）
        if raw.get("post_type") == "request" and raw.get("request_type") == "group":
            group_id = str(raw.get("group_id"))
            user_id = str(raw.get("user_id"))
            flag = raw.get("flag", "")
            comment = raw.get("comment", "")
            # #155：审核总开关关闭后，全部自动审核逻辑都跳过
            audit_enabled = bool(self.get_group_setting(group_id, "join_audit_enabled", True))
            if not audit_enabled:
                return
            enabled_groups = self.get_group_setting(group_id, "violation_enabled_groups", [])
            violation_keywords = self.get_group_setting(group_id, "violation_keywords", [])
            join_approve_keywords = self.get_group_setting(group_id, "join_approve_keywords", [])
            enabled = enabled_groups and group_id in [str(x) for x in enabled_groups]

            # 命中违禁词：拒绝 + 通知管理员（#129 使用自定义拒绝理由；#159 优化提示）
            reject_reason = self.get_group_setting(group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"
            if enabled and violation_keywords and any(kw in comment for kw in violation_keywords):
                detail_reason = f"您的加群申请有词触碰到本群违禁词，自动拒绝（{reject_reason}）"
                await self._handle_group_request(event, flag, False, detail_reason)
                yield event.plain_result(f"已拒绝 {user_id} 的加群申请（含违禁词）")
                await self._notify_admins(
                    f"[加群请求] 已拒绝 {user_id}（群 {group_id}）\n"
                    f"验证消息: {comment}\n"
                    f"原因: 命中违禁词",
                    group_id=group_id,
                )
                return

            # 命中关键词：同意 + 通知管理员
            if enabled and join_approve_keywords and any(kw in comment for kw in join_approve_keywords):
                await self._handle_group_request(event, flag, True, "命中关键词自动同意")
                yield event.plain_result(f"已同意 {user_id} 的加群申请（命中关键词）")
                await self._notify_admins(
                    f"[加群请求] 已同意 {user_id}（群 {group_id}）\n"
                    f"验证消息: {comment}\n"
                    f"原因: 命中关键词",
                    group_id=group_id,
                )
                return

            # 群内提醒（#57）：发送申请消息到对应群聊，等待管理员引用回复同意/拒绝
            if self.get_group_setting(group_id, "join_request_notify_in_group", False):
                nickname = await self._get_user_nickname(event, user_id)
                notify_text = (
                    f"【有新人加群申请】\n"
                    f"qq昵称：{nickname}\n"
                    f"新人qq号：{user_id}\n"
                    f"加群验证消息：{comment or '（无）'}\n"
                    f"注：引用消息回复同意或拒绝"
                )
                # 暂存 flag 等待引用回复
                sent_id = await self._send_group_text(event, group_id, notify_text)
                if sent_id:
                    pending = self.config.setdefault("pending_join_requests", {})
                    pending[str(sent_id)] = {"flag": flag, "group_id": group_id, "user_id": user_id}
                    self.save_config()
                    await self._notify_admins(
                        f"[加群请求] {user_id} 申请加入群 {group_id}\n"
                        f"已在群内发送提醒，请管理员引用回复同意/拒绝",
                        group_id=group_id,
                    )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """Bot 自身发言后：若命中关键词配置则自动撤回（#46）。"""
        raw = self._get_raw_message(event)
        if not raw:
            return
        group_id = str(raw.get("group_id", ""))
        if not group_id:
            return

        # 撤回消息历史：记录 bot 自身发言，用于 /撤回自身 / /撤回 N（#117 #118 #122）。
        # 与自动关键词撤回无关，所有群组都需要写入。
        bot_msg_id = raw.get("message_id")
        bot_user_id = raw.get("user_id") or raw.get("self_id")
        if bot_msg_id and bot_user_id:
            content = self._extract_message_content_from_segments(raw.get("message") or [])
            sender = raw.get("sender") or {}
            bot_name = sender.get("card") or sender.get("nickname", "") or "机器人"
            self._add_message_to_history(
                group_id, bot_msg_id, content, str(bot_user_id), bot_name,
                is_bot=True, msg_time=raw.get("time"),
            )

        enabled = self.get_group_setting(group_id, "auto_recall_enabled_groups", [])
        keywords = self.get_group_setting(group_id, "auto_recall_keywords", [])
        # #170：兼容只配 keywords 未配 enabled_groups 的场景，配了关键词则默认全群启用
        if not enabled and keywords:
            enabled = ["*"]
        if not enabled:
            return
        if "*" not in [str(x) for x in enabled] and "all" not in [str(x) for x in enabled]:
            if group_id not in [str(x) for x in enabled]:
                return
        if not keywords:
            return
        msg_text = self._extract_text(raw)
        if not msg_text:
            return
        if any(kw in msg_text for kw in keywords):
            msg_id = raw.get("message_id")
            if msg_id:
                await self._recall_message(event, str(msg_id))

    def _extract_text(self, raw: dict) -> str:
        parts = []
        for seg in raw.get("message", []) or []:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(seg.get("data", {}).get("text", ""))
        return "".join(parts)

    def _should_notify_mute(self, group_id: str, ok: bool) -> bool:
        """判断禁言/解禁/宵禁/禁我 是否需要回复。
        配置 mute_notice=False 时只回复失败，成功静默。支持按群覆盖 (group_overrides)。"""
        if not ok:
            return True  # 失败总是提示
        return bool(self.get_group_setting(group_id, "mute_notice", self.config.get("mute_notice", True)))