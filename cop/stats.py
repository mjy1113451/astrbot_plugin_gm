"""L3a 统计领域服务：发言计数、排名、重置、禁言计数与阈值踢人、违规计数。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _increment_message_count / get_rank / reset_group_stats
    _record_mute_and_maybe_kick / _record_violation

依赖（构造注入，L3 只依赖 L0/L1/L2，不 import 同层）：
- RuntimeState（L2）：stats / _msg_save_counter
- ConfigStore（L1）：get_group_setting / save_stats
- OneBotApi（L2）：_kick_member / _send
- MessageParser（L2）：_build_text（_record_mute_and_maybe_kick 的踢出提示）

main.py 门面保留同名薄包装委托本服务，既有调用点零改动。
"""

from __future__ import annotations

import time
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

    def get_rank(self, group_id: str) -> list:
        try:
            top_n = max(1, int(self._store.get_group_setting(group_id, "rank_top_n", 10)))
        except (TypeError, ValueError):
            top_n = 10
        groups = self._runtime.stats.get("groups", {})
        msgs = groups.get(str(group_id), {}).get("messages", {})
        ranked = sorted(msgs.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_n]

    # ===================== 成员最后发言时间（#225 /清人 依据） =====================

    def _record_user_last_active(self, group_id: str, user_id: str):
        """#225：记录成员最后发言时间（/清人 N 判定依据）。

        仅更新内存，复用发言计数的每 50 条批量落盘节奏（_increment_message_count），
        避免每条消息写盘。
        """
        g = self._runtime.stats.setdefault("groups", {}).setdefault(str(group_id), {})
        last_active = g.setdefault("last_active", {})
        last_active[str(user_id)] = int(time.time())

    def get_last_active(self, group_id: str) -> dict:
        """#225：返回本群成员最后发言时间映射 {user_id: ts}（无记录返回空 dict）。"""
        groups = self._runtime.stats.get("groups", {})
        last_active = groups.get(str(group_id), {}).get("last_active", {})
        return last_active if isinstance(last_active, dict) else {}

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
