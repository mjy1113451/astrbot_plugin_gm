"""L2 权限判定层：插件管理员 / 群主 / 群管理员 / 专项「设管理」名单等权限判定。

迁出自 main.py 的权限相关方法。依赖：
- L1 ConfigStore（构造注入）：读取本群统一名单及旧专项权限名单
- 同层 MessageParser（构造注入）：仅 _moderation_require_admin(_msg) 需要读 raw

依赖规则：不 import 同层模块，MessageParser 通过构造注入（TYPE_CHECKING + 字符串注解）。

注意：_moderation_require_admin_msg 必须保持为**普通 async 函数（非生成器）**，
18 个调用点依赖其 bool 返回值。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi


class PermissionService:
    """无状态（相对于插件类）的权限判定服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        message_parse: "MessageParser",
        onebot_api: "OneBotApi",
    ):
        self._store = config_store
        self._mp = message_parse
        # _moderation_require_admin_msg 需发提示（send），故同层注入 OneBotApi（不 import 模块）
        self._api = onebot_api

    # ---------- 基础角色判定 ----------

    def _is_authorized(self, raw: dict, user_id: str = "") -> bool:
        """只有当前群真实发送者可使用该群配置的插件管理权限。"""
        if self._is_group_admin_or_owner(raw):
            return True
        group_id = str(raw.get("group_id") or "")
        sender_id = str(raw.get("user_id") or "")
        if not group_id or not sender_id or (user_id and str(user_id) != sender_id):
            return False
        return sender_id in (self._store.get_plugin_admins(group_id) or [])

    def _is_bot_owner(self, event) -> bool:
        """#250：发送者是否为 AstrBot 主人（WebUI admins_id 配置的管理员）。

        优先用框架在唤醒阶段写好的 event.role（admins_id 命中即 "admin"），
        回退到直接比对 admins_id 配置，避免个别入口未赋 role 时漏判。
        """
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        if not sender_id:
            return False
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        try:
            admins = None
            cfg = getattr(self._api.context, "astrbot_config", None)
            if isinstance(cfg, dict):
                admins = cfg.get("admins_id")
            if admins is None:
                get_cfg = getattr(self._api.context, "get_config", None)
                if callable(get_cfg):
                    admins = get_cfg().get("admins_id")
            return sender_id in [str(x) for x in (admins or [])]
        except Exception:
            return False

    @staticmethod
    def _is_message_in_group(result, group_id: str) -> bool:
        if not group_id or not isinstance(result, dict):
            return False
        message = result.get("data") if isinstance(result.get("data"), dict) else result
        return message.get("message_type", "group") == "group" and str(message.get("group_id") or "") == group_id

    async def _may_act_on_quoted_message(self, event, raw: dict, reply_id: str) -> bool:
        if self._is_group_admin_or_owner(raw):
            return True
        result = await self._api._execute_action(event, "get_msg", message_id=str(reply_id), return_raw=True)
        return self._is_message_in_group(result, str(raw.get("group_id") or ""))

    async def _owner_may_recall_quoted(self, event, reply_id: str) -> bool:
        """#250：bot 主人未任群管时，是否允许撤回被引用消息。

        仅放宽到「引用撤回 bot 自身消息」：通过 get_msg 校验被引用消息的发送者
        是 bot 自己；拿不到被引用消息（个别实现不支持 get_msg）时放行，
        delete_msg 的最终权限由协议端校验（bot 非管理员时撤他人消息本就会失败）。
        """
        if not self._is_bot_owner(event):
            return False
        quoted = await self._api._execute_action(event, "get_msg",
                                                 message_id=str(reply_id), return_raw=True)
        qdata = None
        if isinstance(quoted, dict):
            qdata = quoted.get("data") if isinstance(quoted.get("data"), dict) else quoted
        sender = (qdata or {}).get("sender") or {}
        q_sender = str(sender.get("user_id", "") or "")
        self_id = self._api._get_self_id(event) or ""
        if q_sender and self_id:
            return q_sender == self_id
        logger.debug("[撤回] bot 主人引用撤回：无法确认被引用消息发送者，交由协议端校验")
        return True

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

    # ---------- 「设管理」专项名单 ----------

    def _effective_group_admin_admins(self, group_id: str) -> list:
        """本群生效的「设管理」专项名单：群内指令优先于 WebUI 按群名单。"""
        return self._store.get_group_override_list(group_id, "group_admin_admins")

    def _has_legacy_specialist_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        return (
            bool(group_id and user_id)
            and str(raw.get("group_id") or "") == str(group_id)
            and str(raw.get("user_id") or "") == str(user_id)
            and self._store.get_plugin_admins(group_id) is None
            and str(user_id) in self._effective_group_admin_admins(group_id)
        )

    def has_group_admin_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        """统一名单不允许改变 QQ 群角色，避免间接授予插件管理权限。"""
        return self._is_group_admin_or_owner(raw) or self._has_legacy_specialist_rights(user_id, group_id, raw)

    def has_group_nickname_rights(self, user_id: str, group_id: str, raw: dict) -> bool:
        """允许本群统一管理员；未配置统一名单时保留旧专项权限。"""
        return self._is_authorized(raw, user_id) or self._has_legacy_specialist_rights(user_id, group_id, raw)

    # ---------- 违规检测管理命令前置校验 ----------

    def _moderation_require_admin(self, event):
        """校验消息发送者是否为插件管理员。是则返回 user_id，否则返回 None。
        #132：群管理员与群主也视为插件管理员。"""
        raw = self._mp._get_raw_message(event)
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
        raw = self._mp._get_raw_message(event)
        if raw and not raw.get("group_id"):
            await self._api._send(event, self._mp._build_text("此指令只能在群聊中使用"))
            return False
        await self._api._send(event, self._mp._build_text("只有插件管理员可执行此操作"))
        return False
