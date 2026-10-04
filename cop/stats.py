from __future__ import annotations

import time

"""L3a 统计领域服务：发言计数、排名、重置、禁言计数与阈值踢人、违规计数。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _increment_message_count / get_rank / reset_group_stats
    _record_mute_and_maybe_kick / _record_violation / clean_inactive_members

依赖（构造注入，L3 只依赖 L0/L1/L2，不 import 同层）：
- RuntimeState（L2）：stats / _msg_save_counter
- ConfigStore（L1）：get_group_setting / save_stats
- OneBotApi（L2）：_kick_member / _send / _execute_action / _normalize_member_list
- MessageParser（L2）：_build_text（_record_mute_and_maybe_kick 的踢出提示）

main.py 门面保留同名薄包装委托本服务，既有调用点零改动。
"""

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi
    from .runtime import RuntimeState


class StatsService:
    """发言统计 / 排名 / 禁言与违规计数领域服务。"""

    def __init__(
        self,
        *,
        runtime: "RuntimeState",
        config_store: "ConfigStore",
        onebot_api: "OneBotApi",
        message_parse: "MessageParser",
    ):
        self._runtime = runtime
        self._store = config_store
        self._api = onebot_api
        self._mp = message_parse

    # ===================== 计数统计（#29） =====================

    def _increment_message_count(self, group_id: str, user_id: str):
        groups = self._runtime.stats.setdefault("groups", {})
        g = groups.setdefault(str(group_id), {"messages": {}})
        msgs = g.setdefault("messages", {})
        msgs[str(user_id)] = msgs.get(str(user_id), 0) + 1
        # #152：每50条消息批量写入磁盘，避免重启丢数据
        self._runtime._msg_save_counter += 1
        if self._runtime._msg_save_counter >= 50:
            self._store.save_stats(self._runtime.stats)
            self._runtime._msg_save_counter = 0

    def get_rank(self, group_id: str, top_n: int) -> list:
        groups = self._runtime.stats.get("groups", {})
        msgs = groups.get(str(group_id), {}).get("messages", {})
        ranked = sorted(msgs.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_n]

    def reset_group_stats(self, group_id: str):
        self._runtime.stats.setdefault("groups", {})[str(group_id)] = {"messages": {}}
        self._store.save_stats(self._runtime.stats)

    async def _record_mute_and_maybe_kick(self, event: AstrMessageEvent, group_id: str, user_id: str, operator_id: str = ""):
        """记录被禁言次数，达到阈值后自动踢出。阈值为 0/空则关闭。"""
        try:
            threshold = int(self._store.get_group_setting(group_id, "mute_kick_threshold", 0) or 0)
        except (TypeError, ValueError):
            threshold = 0
        if threshold <= 0:
            return
        group_key = str(group_id)
        user_key = str(user_id)
        groups = self._runtime.stats.setdefault("groups", {})
        g = groups.setdefault(group_key, {"messages": {}})
        counts = g.setdefault("mute_counts", {})
        counts[user_key] = int(counts.get(user_key, 0)) + 1
        self._store.save_stats(self._runtime.stats)
        if counts[user_key] >= threshold:
            ok = await self._api._kick_member(event, group_id, user_id)
            if ok:
                counts[user_key] = 0
                self._store.save_stats(self._runtime.stats)
                await self._api._send(event, self._mp._build_text(
                    f"{user_id} 禁言次数达到 {threshold} 次，已自动踢出"))

    # ===================== 群违规检测（合并自 astrbot_plugin_group_moderation） =====================

    def _record_violation(self, group_id: str, user_id: str, kind: str):
        """把一次违规记录到 stats 中（持久化）。"""
        g = self._runtime.stats.setdefault("groups", {}).setdefault(str(group_id), {"messages": {}})
        counts = g.setdefault("violation_counts", {})
        bucket = counts.setdefault(kind, {})
        bucket[str(user_id)] = int(bucket.get(str(user_id), 0)) + 1
        self._store.save_stats(self._runtime.stats)

    # ===================== 清理未发言成员（#225） =====================

    async def clean_inactive_members(
        self, event, group_id: str, days: int,
    ) -> tuple[int, int, list[str], bool]:
        """踢出 N 天内未发言的群成员。

        返回 (成功踢出数, 失败数, 被跳过的不应踢成员 QQ 列表, capability)。
        capability=False 表示协议端不支持 last_sent_time，无法执行；
        capability=True 表示正常执行（即使成功/失败均为 0 表示确实无人）。
        群主与管理员始终跳过（不受发言时间影响）。
        """
        if days < 1:
            return 0, 0, [], True
        cutoff = time.time() - days * 86400
        member_list_raw = await self._api._execute_action(
            event, "get_group_member_list", group_id=group_id, return_raw=True)
        member_list = self._api._normalize_member_list(member_list_raw)
        if member_list is None or not member_list:
            return 0, 0, [], False
        # 检查首成员是否有 last_sent_time 字段（协议端是否支持）
        if not any(k in member_list[0] for k in ("last_sent_time", "last_message_time")):
            return 0, 0, [], False
        skipped: list[str] = []
        kicked_ok, kicked_fail = 0, 0
        for m in member_list:
            role = m.get("role", "") if isinstance(m, dict) else ""
            if role in ("owner", "admin"):
                skipped.append(str(m.get("user_id", "")))
                continue
            try:
                last = float(m.get("last_sent_time") or m.get("last_message_time") or 0)
            except (TypeError, ValueError):
                continue
            if 0 < last < cutoff:
                ok = await self._api._kick_member(event, group_id, str(m.get("user_id", "")))
                if ok:
                    kicked_ok += 1
                else:
                    kicked_fail += 1
        return kicked_ok, kicked_fail, skipped, True
