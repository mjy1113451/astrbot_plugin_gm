"""L3b 群消息即时处理域（重复表情包撤回 #196 + 口语化群管指令 #254）。

合并来源（同层 L3b、同属「on_group_message 触发的即时消息处理」，体量均小）：
    dup_face.py     → DupFaceService
    colloquial.py   → ColloquialService

两类均为「收到群消息后立即判断并处置」的域服务，行为逐字保留、对外类名不变。

依赖（构造注入，L3b 只依赖 L0/L1/L2 与 L3a，不 import 同层）：
- ConfigStore（L1）：config / runtime_map / get_group_setting
- OneBotApi（L2）：_do_recall / _get_self_id / _send / _mute_member / _unmute_member
  / _kick_member / _execute_action / _set_group_admin
- RuntimeState（L2）：_dup_face_seen / _dup_face_last_mid
- PermissionService（L2）：_is_authorized / has_group_admin_rights
- TargetResolver（L2）：_resolve_colloquial_target（构造注入）
- MessageParser（L2）：_message_at_bot / _extract_message_text_with_mentions / _build_text
- StatsService（L3a）：_record_mute_and_maybe_kick
- HistoryService（L3a）：_should_notify_mute / _recall_user_recent_msgs

L0：text_utils 的 _parse_duration_minutes / _format_minutes，
    constants 的 _MUTE_MIN_MINUTES / _MUTE_CLAMP_MAX_MINUTES。
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import _MUTE_CLAMP_MAX_MINUTES, _MUTE_MIN_MINUTES
from .text_utils import _format_minutes, _parse_duration_minutes

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .history import HistoryService
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi
    from .permissions import PermissionService
    from .runtime import RuntimeState
    from .stats import StatsService
    from .message_parse import TargetResolver


# ===================== 重复表情包撤回（#196，原 dup_face.py） =====================

# #273：同一发送者两次发出相同表情包之间，最多允许间隔的「其它消息」条数。
# 仅当相同表情在紧邻窗口内（中间不超过 _DUP_FACE_MAX_GAP 条消息）重发才判重复；
# 中间聊了若干条其它消息后再发同一表情属正常使用，不再撤回。
_DUP_FACE_MAX_GAP = 2
_DUP_FACE_WINDOW = 1800  # 指纹保留窗口（秒），沿用原 30 分钟
_DUP_FACE_MAX_KEYS = 200  # 单发送者缓存指纹上限，沿用原上限
_DUP_FACE_MID_CACHE = 50  # 每群最近处理过的 message_id 缓存条数（#282）

# #282：内容哈希指纹识别。
# - md5 字段：协议端直接给的图片内容哈希，最可靠；
# - file 字段：多数实现是 "<32位hex>.image/.jpg"，但部分实现给的是与内容
#   无关的序号文件名（如 "0.image"、"1.jpg"），拿它当指纹会让不同表情包
#   互相"撞指纹"，用户只发一次表情也会被当成重复而误撤回；
# - url 字段：QQ 图床 URL 路径里通常带 32 位文件哈希，可从中提取；
# 三者都拿不到内容哈希时宁可放弃该段的判重，也不能拿无关值误判。
_MD5_NAME_RE = re.compile(r"^([0-9a-fA-F]{32})(?:\.[A-Za-z0-9]+)?$")
_MD5_IN_URL_RE = re.compile(r"/([0-9a-fA-F]{32})(?:[./?]|$)")


def _image_fingerprint(data: dict) -> str:
    """从 image 段提取内容哈希指纹；拿不到真实哈希时返回空串（不判重）。"""
    md5 = str(data.get("md5") or "").strip().lower()
    if md5:
        return md5
    file_name = str(data.get("file") or "").strip()
    m = _MD5_NAME_RE.match(file_name)
    if m:
        return m.group(1).lower()
    url = str(data.get("url") or "").strip()
    if url:
        m = _MD5_IN_URL_RE.search(url)
        if m:
            return m.group(1).lower()
    return ""


class DupFaceService:
    """重复表情包检测与撤回服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        runtime: "RuntimeState",
        onebot_api: "OneBotApi",
    ):
        self._store = config_store
        self.config = config_store.config
        self._runtime = runtime
        self._api = onebot_api

    def _dup_face_enabled(self, group_id: str) -> bool:
        """#196：重复表情包撤回是否启用（按群 bool 覆盖 > 全局开关）。"""
        ov = (
            self._store.runtime_map("group_overrides")
            .get(str(group_id), {})
            .get("dup_face_recall_enabled")
        )
        if isinstance(ov, bool):
            return ov
        return bool(self.config.get("dup_face_recall_enabled", False))

    async def _dup_face_recall_check(self, event: AstrMessageEvent, raw: dict, group_id: str) -> None:
        """#196：检测群内重复表情包（face id / 图片指纹），命中则撤回新消息。

        仅撤回新发的重复消息（旧消息可能已超撤回窗口）；
        图片指纹优先 md5，回退 file/url（review#213 unstable-fingerprint）。

        #239：指纹按「群 + 发送者」隔离——旧实现只按群记录，导致
        B 发出与 A 相同的表情包会被当成 A 的重复而误撤回。
        #238：同一 message_id 只处理一次，避免消息被重复投递时把
        "自己"当成重复（仅发一次却被撤回）。
        #273：不再把 30 分钟窗口内出现过的相同表情一律判重。seen 改为
        {指纹: [时间戳, 间隔消息数]}，该发送者每发一条其它消息，所有缓存指纹的
        间隔数 +1；只有相同指纹间隔消息数 ≤ _DUP_FACE_MAX_GAP 时才判重复，
        「表情 → 几条聊天 → 同一表情」的正常使用不再被撤回。
        """
        if not self._dup_face_enabled(group_id):
            return
        now = time.time()
        keys = []
        for seg in raw.get("message") or []:
            if not isinstance(seg, dict):
                continue
            st = seg.get("type")
            d = seg.get("data") or {}
            if st == "face" and d.get("id"):
                keys.append(("face", str(d.get("id"))))
            elif st == "image":
                fp = _image_fingerprint(d)
                if fp:
                    keys.append(("image", fp))
        sender_id = str(raw.get("user_id", ""))
        mid = raw.get("message_id")
        # #238/#282：同一条消息只判定一次。旧实现只缓存「上一条 message_id」，
        # 协议端对同一消息的非相邻重复投递（中间隔了其它事件）会绕过该守卫，
        # 把"自己"当成重复导致仅发一次却被撤回；改为每群缓存最近 N 条已处理
        # message_id，重复投递无论隔多远都不会再被判重。
        if mid is not None:
            mid_cache = self._runtime._dup_face_mid_cache.get(group_id)
            if mid_cache is None:
                mid_cache = deque(maxlen=_DUP_FACE_MID_CACHE)
                self._runtime._dup_face_mid_cache[group_id] = mid_cache
            if str(mid) in mid_cache:
                return
            mid_cache.append(str(mid))
        # seen：{指纹: [上次出现时间戳, 距上次出现后该发送者发的其它消息数]}
        seen = self._runtime._dup_face_seen.setdefault((group_id, sender_id), {})
        # 清理超过时间窗口的陈旧指纹
        for fp in [k for k, v in seen.items() if now - v[0] > _DUP_FACE_WINDOW]:
            del seen[fp]
        if not keys:
            # 非表情包消息：所有指纹的间隔数 +1（#273：用于衡量是否"紧邻"）
            for v in seen.values():
                v[1] += 1
            return
        # 同一消息内的相同指纹去重，避免自身重复计入
        keys = list(dict.fromkeys(keys))
        # 命中条件：指纹出现过且中间间隔消息数未超阈值
        dup_fps = {k for k in keys if k in seen and seen[k][1] <= _DUP_FACE_MAX_GAP}
        # 本次未出现的指纹间隔 +1；本次出现的指纹随后重置为 [now, 0]
        for k, v in seen.items():
            if k not in keys:
                v[1] += 1
        for k in keys:
            seen[k] = [now, 0]
        if len(seen) > _DUP_FACE_MAX_KEYS:
            overflow = len(seen) - _DUP_FACE_MAX_KEYS
            oldest = sorted(seen, key=lambda fp: seen[fp][0])[:overflow]
            for old_fp in oldest:
                del seen[old_fp]
        if dup_fps and mid is not None:
            ok, err = await self._api._do_recall(event, mid)
            if ok:
                logger.info(f"[重复表情包] 群 {group_id} 用户 {sender_id} 撤回重复表情包消息 {mid}")
            else:
                logger.warning(f"[重复表情包] 群 {group_id} 撤回 {mid} 失败: {err}")


# ===================== 口语化群管指令（#254，原 colloquial.py） =====================

# 自禁言（口语化 /禁我）说法：
#  - 「禁我」「禁言我」要求出现在句首或标点/空白之后，排除
#    「别禁我」「为什么禁我」「你敢禁我」之类的疑问/反问句误触发；
#  - 「把我禁言」「给我禁言」「自我禁言」「自怼」为无歧义说法直接命中。
_SELF_MUTE_RE = re.compile(
    r"(?:^|[，。,.\s:：、])禁我"
    r"|(?:^|[，。,.\s:：、])禁言我(?:自己)?"
    r"|[把给将]我(?:给)?禁言"
    r"|自我禁言|自怼"
)


def _clamp_mute_minutes(parsed) -> int:
    """把时长解析结果夹取到合法分钟数，异常类型回退默认 10 分钟。

    _parse_duration_minutes 的契约是返回 int 或 None，但万一回归返回意外
    类型，int(parsed) 会抛 ValueError/TypeError 打断整条消息处理路径
    （PR #277 审查反馈），此处统一做类型守卫与夹取。
    """
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        if not (isinstance(parsed, str) and parsed.strip().isdigit()):
            return 10  # 缺省 10 分钟，与 /禁我 一致
    try:
        return max(_MUTE_MIN_MINUTES, min(int(parsed), _MUTE_CLAMP_MAX_MINUTES))
    except (TypeError, ValueError):
        return 10


class ColloquialService:
    """@bot + 口语化禁言/踢人/设管理请求处理服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        onebot_api: "OneBotApi",
        stats: "StatsService",
        permissions: "PermissionService",
        target_resolver: "TargetResolver",
        message_parse: "MessageParser",
        history: "HistoryService",
    ):
        self._store = config_store
        self.config = config_store.config
        self._api = onebot_api
        self._stats = stats
        self._perms = permissions
        self._resolver = target_resolver
        self._mp = message_parse
        self._history = history

    def _detect_colloquial_intent(self, text: str):
        """从口语化文本识别意图：mute/unmute/kick/admin/unadmin。无命中返回 None。"""
        if not text:
            return None
        # 精度优先：先识别「取消/解除」类，再识别「设置/禁言」类，避免被正向词覆盖
        if re.search(r"解除.{0,3}禁言|解禁|取消.{0,3}禁言|撤销.{0,3}禁言|别禁言|解封|unban|解ban", text, re.IGNORECASE):
            return "unmute"
        if re.search(r"取消.{0,3}管理|撤(?:销|掉|除|下)?管理|下管理|卸任管理|去掉.{0,3}管理", text):
            return "unadmin"
        # 正向：设管理 > 禁言 > 踢人（避免「给管理」中的字误判）
        if re.search(r"设(?:为|成|置)?(?:群)?管理|上管理|给.{0,4}管理|加管理|升管理", text):
            return "admin"
        # ban 为常见网络用语，用前后非英文字母边界避免误命中 banana/urban 等
        # 「禁我」「自怼」为自禁言说法（交由下方自禁言分支处理，任意成员可用）
        if re.search(r"禁言|禁我|自怼|闭嘴|封口|静音|沉默|封禁|(?<![a-zA-Z])ban(?![a-zA-Z])", text, re.IGNORECASE):
            return "mute"
        if re.search(r"踢|移出群|清出群", text):
            return "kick"
        return None

    async def _handle_colloquial_admin_request(self, event, raw: dict, group_id: str, user_id: str) -> bool:
        """#254：处理 @bot + 口语化禁言/踢人/设管理请求。

        命中并已回复返回 True（调用方 return，避免重复计数与历史污染）；
        未命中或不适用返回 False，让消息走原有流程。
        """
        if not group_id:
            return False
        # 口语化总开关（全局配置 + 按群覆盖 group_overrides）
        if not self._store.get_group_setting(
            group_id, "colloquial_enabled", self.config.get("colloquial_enabled", True)
        ):
            return False
        self_id = self._api._get_self_id(event, raw)
        if not self_id or not self._mp._message_at_bot(raw, self_id):
            return False

        # 提取口语文本（跳过 @bot 自身，保留 @目标 段用于目标解析）
        text = self._mp._extract_message_text_with_mentions(raw, skip_qq=self_id)
        intent = self._detect_colloquial_intent(text)
        if not intent:
            return False

        # 自禁言（「禁我 / 自怼 / 把我禁言」）：任意成员可用，权限与 /禁我 命令一致，
        # 必须在管理员权限校验之前处理，否则普通成员的自禁请求会被直接拒绝。
        # 命中后就地处置并返回，消息不再下沉到 AstrBot 核心 LLM
        # （否则会被模型兜底回复成 "You have no egg" 之类无关内容）。
        if intent == "mute" and _SELF_MUTE_RE.search(text):
            minutes = _clamp_mute_minutes(_parse_duration_minutes(text))
            ok = await self._api._mute_member(event, group_id, user_id, minutes * 60)
            if self._history._should_notify_mute(group_id, ok):
                msg = f"已禁言自己 {_format_minutes(minutes)}" if ok else \
                    "禁言自己失败（请确认 bot 有管理员权限，且你不是群主/管理员）"
                await self._api._send(event, self._mp._build_text(msg))
            return True

        # 权限校验：非插件管理员直接拒绝（与命令语义一致）
        if not self._perms._is_authorized(raw, user_id):
            await self._api._send(event, self._mp._build_text("只有插件管理员或群管理员可执行此操作"))
            return True

        target = await self._resolver._resolve_colloquial_target(event, raw, text)
        if not target:
            await self._api._send(event, self._mp._build_text(
                "没有找到要操作的目标，请 @ 某人、回复他的消息，或直接给出 QQ 号"))
            return True
        if str(target) == str(self_id):
            await self._api._send(event, self._mp._build_text("不能对自己执行该操作"))
            return True

        if intent == "mute":
            minutes = _clamp_mute_minutes(_parse_duration_minutes(text))
            ok = await self._api._mute_member(event, group_id, target, minutes * 60)
            if ok:
                await self._stats._record_mute_and_maybe_kick(event, group_id, target, user_id)
            msg = f"已禁言 {target} {_format_minutes(minutes)}" if ok else \
                f"禁言 {target} 失败（请确认 bot 有管理员权限且对方不是群主/管理员）"
            if self._history._should_notify_mute(group_id, ok):
                await self._api._send(event, self._mp._build_text(msg))
            return True

        if intent == "unmute":
            ok = await self._api._unmute_member(event, group_id, target)
            msg = f"已解除 {target} 的禁言" if ok else \
                f"解除 {target} 禁言失败（请确认 bot 有管理员权限，且对方当前处于禁言状态）"
            if self._history._should_notify_mute(group_id, ok):
                await self._api._send(event, self._mp._build_text(msg))
            return True

        if intent == "kick":
            recalled = 0
            if self._store.get_group_setting(group_id, "kick_recall_enabled", False):
                recalled = await self._history._recall_user_recent_msgs(
                    event, group_id, target,
                    max(1, min(int(self._store.get_group_setting(group_id, "kick_recall_count", 10) or 10), 50)))
            ok = await self._api._kick_member(event, group_id, target)
            if ok and self.config.get("reject_re_add", False):
                await self._api._execute_action(event, "reject_add", group_id=group_id, user_id=target)
            msg = f"已踢出 {target}" if ok else f"踢出 {target} 失败（请确认 bot 有管理员权限，且对方不是群主）"
            if recalled:
                msg += f"\n已撤回其近期消息 {recalled} 条"
            await self._api._send(event, self._mp._build_text(msg))
            return True

        if intent == "unadmin":
            if not self._perms.has_group_admin_rights(user_id, group_id, raw):
                await self._api._send(event, self._mp._build_text("只有插件管理员或群管理员可取消群管理"))
                return True
            ok = await self._api._set_group_admin(event, group_id, target, False)
            msg = f"已取消 {target} 的群管理" if ok else \
                f"取消 {target} 群管理失败（请确认 bot 有管理员权限，且对方当前是群管理员）"
            await self._api._send(event, self._mp._build_text(msg))
            return True

        # intent == "admin"
        if not self._perms.has_group_admin_rights(user_id, group_id, raw):
            await self._api._send(event, self._mp._build_text("只有插件管理员或群管理员可授予群管理"))
            return True
        ok = await self._api._set_group_admin(event, group_id, target, True)
        msg = f"已将 {target} 设为群管理" if ok else \
            f"设置 {target} 为群管理失败（请确认 bot 有管理员权限，且对方不是群主）"
        await self._api._send(event, self._mp._build_text(msg))
        return True


__all__ = ["DupFaceService", "ColloquialService"]
