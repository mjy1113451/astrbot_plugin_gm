"""L3b 群违规检测核心业务域：总开关/豁免/时长/违规处置/检测总入口。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _is_group_monitoring_enabled / _is_user_whitelisted
    _moderation_admin_bypass / _moderation_ban_duration
    _handle_violation / _moderation_dispatch

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / get_group_setting / runtime_map
- OneBotApi（L2）：_recall_message / _mute_member / _send
- StatsService（L3a）：_record_mute_and_maybe_kick / _record_violation
- PermissionService（L2）：保留注入（对齐分层约定 / 后续复用）
- MessageParser（L2）：_extract_text / _build_text / _extract_audio_urls
- RuntimeState（L2）：保留注入（对齐分层约定）
- TextModeration / ImageModeration / VoiceModeration（L3b 同层子模块，实例注入）

循环依赖说明：moderation 单向依赖 stats（_handle_violation → stats._record_mute_and_maybe_kick），
stats 不反向依赖 moderation，保证无环。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ..config_store import ConfigStore
    from ..message_parse import MessageParser
    from ..onebot_api import OneBotApi
    from ..permissions import PermissionService
    from ..runtime import RuntimeState
    from ..stats import StatsService
    from .image import ImageModeration
    from .text import TextModeration
    from .voice import VoiceModeration


class ModerationService:
    """群违规检测核心业务域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        onebot_api: "OneBotApi",
        stats: "StatsService",
        permissions: "PermissionService",
        message_parse: "MessageParser",
        runtime: "RuntimeState",
        text_moderation: "TextModeration",
        image_moderation: "ImageModeration",
        voice_moderation: "VoiceModeration",
    ):
        self._store = config_store
        self.config = config_store.config
        self._api = onebot_api
        self._stats = stats
        self._perms = permissions
        self._mp = message_parse
        self._runtime = runtime
        self._text = text_moderation
        self._image = image_moderation
        self._voice = voice_moderation

    # ===================== 群违规检测开关 / 豁免 =====================

    def _is_group_monitoring_enabled(self, group_id: str) -> bool:
        """群是否启用违规检测。
        优先级
        1. group_overrides[gid]["enabled_groups"] 为 bool 时，按 bool 决定
        2. top-level enabled_groups 列表：包含 * / all 表示全部；包含群号表示启用；
           非空但未命中表示不启用
        3. 兼容旧 violation_enabled_groups 列表：非空时按旧列表判定（老用户行为不漂移）
        4. #192 owner 拍板：两处均留空 = 全群启用
        """
        overrides = self._store.runtime_map("group_overrides").get(str(group_id), {})
        v = overrides.get("enabled_groups")
        if isinstance(v, bool):
            return v
        enabled = self.config.get("enabled_groups", []) or []
        if enabled:
            for x in enabled:
                sx = str(x).lower()
                if sx in ("*", "all"):
                    return True
                if str(x) == str(group_id):
                    return True
            return False
        legacy = self.config.get("violation_enabled_groups", []) or []
        if legacy:
            return str(group_id) in [str(x) for x in legacy]
        return True

    def _is_user_whitelisted(self, group_id: str, user_id: str) -> bool:
        whitelist = self._store.get_group_setting(group_id, "whitelist_users", []) or []
        return str(user_id) in [str(x) for x in whitelist]

    def _moderation_admin_bypass(self, group_id: str, raw: dict) -> bool:
        if not self._store.get_group_setting(group_id, "admin_bypass", True):
            return False
        role = raw.get("sender", {}).get("role", "") if isinstance(raw, dict) else ""
        return role in {"admin", "owner"}

    def _moderation_ban_duration(self, group_id: str, kind: str, severity: str = "") -> int:
        """按违规类型读取对应禁言时长（秒）。

        #243：AI 判骂人可给出严重程度（mild/medium/severe）。仅在开关
        `profanity_severity_enabled`（默认开）打开、且命中
        `profanity_ban_duration_severity` 分级表时按级别取时长；开关关闭、
        未给出严重度或表内没有该级别时，回退 `profanity_ban_duration`
        （开关关闭 = 完全按固定时长处理）。
        """
        key_map = {
            "image": "ban_duration",
            "spam": "spam_ban_duration",
            "profanity": "profanity_ban_duration",
            "ad": "ad_ban_duration",
            "link": "link_ban_duration",
            "group_promotion": "group_promotion_ban_duration",
            "unsafe_site": "link_safety_ban_duration",
        }
        key = key_map.get(kind, "ban_duration")
        default_map = {
            "image": 600, "spam": 600, "profanity": 600,
            "ad": 600, "link": 600, "group_promotion": 600,
            "banned_image": 600, "unsafe_site": 600,
        }
        if severity and key == "profanity_ban_duration" \
                and self._store.get_group_setting(group_id, "profanity_severity_enabled", True):
            table = self._store.get_group_setting(group_id, "profanity_ban_duration_severity", None)
            if isinstance(table, dict):
                raw_v = table.get(severity)
                try:
                    if raw_v is not None:
                        return max(1, int(raw_v))
                except (TypeError, ValueError):
                    pass
        try:
            v = int(self._store.get_group_setting(group_id, key, default_map.get(kind, 600)) or 600)
        except (TypeError, ValueError):
            v = default_map.get(kind, 600)
        return max(1, v)

    # ===================== 违规处置 / 检测总入口 =====================

    async def _handle_violation(
        self,
        event,
        kind: str,
        group_id: str,
        user_id: str,
        message_id: str,
        reason: str = "",
        severity: str = "",
    ) -> bool:
        """处理一条违规：撤回 + 按配置时长禁言 + 计数 + 通知。"""
        ok_any = False
        # 1. 撤回
        if message_id:
            recalled = await self._api._recall_message(event, str(message_id))
            ok_any = recalled
        # 2. 禁言
        duration = self._moderation_ban_duration(group_id, kind, severity)
        muted = await self._api._mute_member(event, group_id, user_id, duration)
        if muted:
            ok_any = True
            # 复用现有的 mute_kick_threshold 计数
            await self._stats._record_mute_and_maybe_kick(event, group_id, user_id, "moderation")
        # 3. 计数
        self._stats._record_violation(group_id, user_id, kind)
        # 4. 通知
        if self._store.get_group_setting(group_id, "notify_on_violation", True):
            label_map = {
                "image": "违规图片", "spam": "刷屏", "profanity": "骂人",
                "ad": "广告", "link": "链接", "group_promotion": "群号推广",
                "banned_image": "违禁图片", "unsafe_site": "不安全网站",
            }
            label = label_map.get(kind, "违规")
            note = f"检测到{label}行为"
            if reason:
                note += f"（{reason}）"
            note += f"，已撤回并禁言 {duration} 秒。"
            await self._api._send(event, self._mp._build_text(note))
        return ok_any

    async def _moderation_dispatch(self, event, raw, group_id: str, user_id: str) -> bool:
        """群消息违规检测总入口。"""
        if not self._is_group_monitoring_enabled(group_id):
            return False
        if self._is_user_whitelisted(group_id, user_id):
            return False
        if self._moderation_admin_bypass(group_id, raw):
            # #199：管理员/群主豁免为默认行为；记录 debug 便于排查"未撤回"工单
            logger.debug(f"[违规检测] 群 {group_id} 用户 {user_id} 命中管理员豁免，跳过检测")
            return False
        msg_text = self._mp._extract_text(raw) if isinstance(raw, dict) else ""
        # 1) 刷屏（不依赖文本；#260 传入文本/媒体标志以支持「排除纯媒体」）
        seg_types = [s.get("type") for s in (raw.get("message") or []) if isinstance(s, dict)] \
            if isinstance(raw, dict) else []
        has_media = any(t in ("image", "face") for t in seg_types)
        if await self._text._check_spam(group_id, user_id, bool(msg_text), has_media):
            mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
            await self._handle_violation(event, "spam", group_id, user_id, mid)
            return True
        # 2) 文本类检测
        if msg_text:
            # #204：涉政关键词硬清单（全局配置），命中即撤回+按涉政时长禁言+固定提示
            pol_kw = self._text._check_political(msg_text)
            if pol_kw:
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                if mid:
                    await self._api._recall_message(event, mid)
                minutes = max(1, int(self.config.get("political_ban_duration", 600) or 600))
                await self._api._mute_member(event, group_id, user_id, minutes * 60)
                self._stats._record_violation(group_id, user_id, "political")
                await self._api._send(event, self._mp._build_text(
                    f"你因触碰涉政关键词(词语∶{pol_kw})被禁言{minutes}分钟"))
                return True
            profanity_violated, profanity_severity = await self._text._check_profanity(
                msg_text, event, group_id, user_id)
            if profanity_violated:
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "profanity", group_id, user_id, mid,
                                             severity=profanity_severity)
                return True
            if await self._text._check_ad(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "ad", group_id, user_id, mid)
                return True
            if await self._text._check_link(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "link", group_id, user_id, mid)
                return True
            if await self._text._check_group_promotion(msg_text, event, group_id, user_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "group_promotion", group_id, user_id, mid)
                return True
            # #272：不安全网站检测（默认关，按群覆盖 /开关不安全网站检测）。
            # 置于文本类检测末位：先让刷屏/骂人/广告等本地判定生效，抓取兜底最后做。
            unsafe_hit, unsafe_reason = await self._text._check_link_safety(msg_text, group_id)
            if unsafe_hit:
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                logger.info(f"[群违规检测] 群 {group_id} 用户 {user_id} 命中不安全网站: {unsafe_reason}")
                await self._handle_violation(event, "unsafe_site", group_id, user_id, mid,
                                             unsafe_reason)
                return True
        # 3) 图片检测（#162 违禁图 MD5 比对先于 AI 鉴图）
        image_urls = self._image._collect_image_urls(raw)
        for url in image_urls:
            if await self._image._check_banned_image(url, group_id):
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                await self._handle_violation(event, "banned_image", group_id, user_id, mid, "图片命中违禁图")
                return True
            violated, reason = await self._image._check_image(url)
            if violated:
                mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                # #260：命中记录可诊断日志（含群号/用户/消息ID/原因），便于区分 AI 误判
                logger.info(f"[群违规检测] 群 {group_id} 用户 {user_id} 图片违规(消息 {mid}): {reason}")
                await self._handle_violation(event, "image", group_id, user_id, mid, reason)
                return True
            # #237：二维码内容识别（全局开关 qr_code_recall_enabled 默认关，
            # 按群覆盖指令 /开关二维码撤回）。普通二维码仅撤回、不禁言；
            # 拉群广告码按广告违规处置（ad 时长），黄赌毒码按违规图片处置
            # （image 时长），均撤回+禁言+计数。
            if self._store.get_group_setting(group_id, "qr_code_recall_enabled", False):
                qr_category, qr_reason = await self._image._check_qr_code(url)
                if qr_category in ("group_ad", "adult"):
                    mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                    kind = "ad" if qr_category == "group_ad" else "image"
                    if not qr_reason:
                        qr_reason = "二维码指向拉群广告" if qr_category == "group_ad" \
                            else "二维码涉及黄赌毒内容"
                    logger.info(f"[二维码检测] 群 {group_id} 用户 {user_id} 消息 {mid} "
                                f"命中二维码违规({qr_category}): {qr_reason}")
                    await self._handle_violation(event, kind, group_id, user_id, mid,
                                                 f"二维码内容违规({qr_reason})")
                    return True
                if qr_category == "plain":
                    mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                    if mid:
                        await self._api._recall_message(event, mid)
                    logger.info(f"[二维码检测] 群 {group_id} 用户 {user_id} 消息 {mid} 含二维码，已自动撤回")
                    await self._api._send(event, self._mp._build_text("检测到二维码图片，已自动撤回"))
                    return True
        # 4) 语音转文字检测（#128）
        if self._store.get_group_setting(group_id, "voice_check_enabled", False):
            audio_urls = self._mp._extract_audio_urls(raw.get("message") or []) if isinstance(raw, dict) else []
            for url in audio_urls:
                text = await self._voice._recognize_audio_url(event, url, group_id)
                if not text:
                    continue
                violated_kind = None
                profanity_hit, _severity = await self._text._check_profanity(text, event, group_id, user_id)
                if profanity_hit:
                    violated_kind = "profanity"
                elif await self._text._check_ad(text, event, group_id, user_id):
                    violated_kind = "ad"
                elif await self._text._check_link(text, event, group_id, user_id):
                    violated_kind = "link"
                elif await self._text._check_group_promotion(text, event, group_id, user_id):
                    violated_kind = "group_promotion"
                if violated_kind:
                    mid = str(raw.get("message_id", "")) if isinstance(raw, dict) else ""
                    await self._handle_violation(event, f"voice_{violated_kind}", group_id, user_id, mid,
                                                 f"语音内容: {text[:50]}")
                    return True
        return False


__all__ = ["ModerationService"]
