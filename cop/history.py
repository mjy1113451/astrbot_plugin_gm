"""L3a 历史领域服务：撤回消息历史的增删查、API 兜底加载、最近消息撤回、通知开关判定。

迁出自 main.py 的以下方法（逐字保留逻辑等价）：
    _add_message_to_history / _remove_message_from_history / _record_message_to_history
    _load_history_from_api / _get_history_snapshot / _recall_user_recent_msgs
    _fetch_recent_bot_messages / _should_notify_mute / _should_notify_group_name

依赖（构造注入，L3 只依赖 L0/L1/L2，不 import 同层）：
- RuntimeState（L2）：message_history / max_history
- MessageParser（L2）：_extract_text / _extract_message_content_from_segments /
  _is_plugin_command / _parse_history_messages / _plain_text_from_segments 等
- OneBotApi（L2）：_get_self_id / _execute_action / _call_onebot_raw /
  _action_result_success / _describe_action_failure / _do_recall
- ConfigStore（L1）：get_group_setting

main.py 门面保留同名薄包装委托本服务，既有调用点零改动。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi
    from .runtime import RuntimeState


class HistoryService:
    """撤回消息历史与最近消息撤回领域服务。"""

    def __init__(
        self,
        *,
        runtime: "RuntimeState",
        message_parse: "MessageParser",
        onebot_api: "OneBotApi",
        config_store: "ConfigStore",
    ):
        self._runtime = runtime
        self._mp = message_parse
        self._api = onebot_api
        self._store = config_store

    # ===================== 撤回消息历史（对齐 astrbot_plugin_batchrecall，修复 #117 #118 #122） =====================

    def _history_limit(self, group_id: str) -> int:
        fallback = self._runtime.max_history
        value = self._store.get_group_setting(group_id, "max_message_history", fallback)
        try:
            return max(1, int(value or fallback))
        except (TypeError, ValueError):
            return fallback

    def _add_message_to_history(self, group_id: str, message_id, content: str,
                                sender_id: str, sender_name: str, is_bot: bool = False,
                                msg_time=None):
        """添加一条消息到历史。最新消息在列表最前面（编号 1 为最新）。"""
        if not group_id or not message_id or sender_id is None:
            return
        key = str(group_id)
        if key not in self._runtime.message_history:
            self._runtime.message_history[key] = []
        msg_id = str(message_id)
        # 去重：同一 message_id 只记录一次
        for existing in self._runtime.message_history[key]:
            if existing[0] == msg_id:
                return
        if msg_time is None:
            msg_time = int(time.time())
        self._runtime.message_history[key].insert(
            0, (msg_id, (content or "")[:100], int(msg_time), str(sender_id), sender_name or "未知", bool(is_bot))
        )
        del self._runtime.message_history[key][self._history_limit(key):]

    def _remove_message_from_history(self, group_id: str, message_id):
        """从历史中移除已撤回的消息。"""
        if not group_id or not message_id:
            return
        msg_id = str(message_id)
        key = str(group_id)
        if key in self._runtime.message_history:
            self._runtime.message_history[key] = [
                m for m in self._runtime.message_history[key] if m[0] != msg_id
            ]

    def _record_message_to_history(self, group_id: str, raw: dict):
        """记录一条群友消息到历史（bot 自身消息由 after_message_sent 记录）。"""
        if not group_id or not isinstance(raw, dict):
            return
        message_id = raw.get("message_id")
        if not message_id:
            return
        # 插件指令消息不记录（避免编号偏移）。用纯文本判断，避免 @ 段被标成 [@] 导致漏判
        if self._mp._is_plugin_command(self._mp._extract_text(raw)):
            return
        content = self._mp._extract_message_content_from_segments(raw.get("message") or [])
        sender = raw.get("sender") or {}
        sender_id = str(sender.get("user_id", "") or raw.get("user_id", "") or "")
        if not sender_id:
            return
        sender_name = sender.get("card") or sender.get("nickname", "未知")
        is_bot = bool(raw.get("self_id")) and str(raw.get("self_id")) == sender_id
        msg_time = raw.get("time", int(time.time()))
        self._add_message_to_history(group_id, message_id, content, sender_id, sender_name, is_bot, int(msg_time))

    async def _load_history_from_api(self, event, group_id: str):
        """从 OneBot get_group_msg_history 加载历史（本地历史为空时兜底），并替换本地历史。"""
        if not group_id:
            return
        try:
            limit = self._history_limit(group_id)
            fetch_count = min(limit * 2, 100)
            result = await self._api._execute_action(
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

            bot_self_id = self._api._get_self_id(event)
            history_list = []
            for msg in history_messages[:limit]:
                msg_id = msg.get("message_id")
                if not msg_id:
                    continue
                sender_info = msg.get("sender") or {}
                sender_id = str(sender_info.get("user_id", ""))
                raw_msg = msg.get("message") or []
                if isinstance(raw_msg, list):
                    content = self._mp._extract_message_content_from_segments(raw_msg)
                    # 跳过 API 返回历史中的插件指令消息（避免把历史中的"/撤回"等指令也编号）。
                    # 用纯文本判断，避免 @ 段被标成 [@] 导致漏判
                    if sender_id != bot_self_id and self._mp._is_plugin_command(self._mp._extract_text({"message": raw_msg})):
                        continue
                else:
                    content = str(raw_msg)[:50]
                sender_name = sender_info.get("card") or sender_info.get("nickname", "未知")
                msg_time = msg.get("time", int(time.time()))
                is_bot = bool(bot_self_id) and sender_id == bot_self_id
                history_list.append((str(msg_id), content, int(msg_time), sender_id, sender_name, is_bot))
            if history_list:
                self._runtime.message_history[str(group_id)] = history_list
        except Exception as exc:
            logger.error(f"从 API 获取消息历史失败: {exc}")

    async def _get_history_snapshot(self, event, group_id: str, current_msg_id=None) -> list:
        """返回该群的撤回用消息快照（本地历史优先，为空时从 API 加载）。
        排除当前指令消息；返回浅拷贝，避免并发修改影响编号映射。"""
        if not group_id:
            return []
        key = str(group_id)
        hist = self._runtime.message_history.get(key, [])
        if not hist:
            await self._load_history_from_api(event, group_id)
            hist = self._runtime.message_history.get(key, [])
        snapshot = hist[:self._history_limit(key)]
        if current_msg_id:
            cur = str(current_msg_id)
            return [m for m in snapshot if m[0] != cur]
        return snapshot

    async def _recall_user_recent_msgs(self, event, group_id: str, user_id: str, count: int) -> int:
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
                history = await self._api._execute_action(event, "get_group_msg_history", return_raw=True,
                                                          group_id=gid, count=min(self._history_limit(gid) * 2, 100))
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
            ok, err = await self._api._do_recall(event, m[0])
            if ok or "已撤回" in err:
                self._remove_message_from_history(gid, m[0])
                recalled += 1
            await asyncio.sleep(0.3)
        return recalled

    async def _fetch_recent_bot_messages(self, event, group_id: str, count: int = 10) -> list:
        """回查群消息历史中机器人自己最近发送的消息。

        AstrBot 的 after_message_sent 事件不提供已发送消息的 message_id
        （event.get_result() 只有消息链），故通过 OneBot get_group_msg_history 反查，
        用于写入撤回历史与自动撤回定位消息。这里用 _call_onebot_raw 调用，
        后端不支持该 API 时不会刷错误日志（ActionFailed 被收进返回值），仅返回空列表。

        返回按时间倒序的 [(message_id, 纯文本, 内容预览, 时间戳), ...]。
        """
        gid = str(group_id or "")
        bot_self_id = self._api._get_self_id(event)
        if not gid or not bot_self_id:
            return []
        result = await self._api._call_onebot_raw(
            event, "get_group_msg_history", group_id=gid, count=max(1, int(count)),
        )
        if not self._api._action_result_success(result):
            logger.debug(
                f"[自动撤回] get_group_msg_history 调用失败({gid})："
                f"{self._api._describe_action_failure(result, 'get_group_msg_history')}"
            )
            return []
        bot_messages = []
        for msg in self._mp._parse_history_messages(result):
            sender_id = str((msg.get("sender") or {}).get("user_id", "") or "")
            if sender_id != bot_self_id:
                continue
            msg_id = msg.get("message_id")
            if not msg_id:
                continue
            segments = msg.get("message") or []
            try:
                msg_time = int(msg.get("time", 0) or 0)
            except (TypeError, ValueError):
                msg_time = 0
            bot_messages.append((
                str(msg_id),
                self._mp._plain_text_from_segments(segments),
                self._mp._extract_message_content_from_segments(segments),
                msg_time,
            ))
        bot_messages.sort(key=lambda m: m[3], reverse=True)
        return bot_messages[:max(1, int(count))]

    def _should_notify_mute(self, group_id: str, ok: bool) -> bool:
        """判断禁言/解禁/宵禁/禁我 是否需要回复。
        配置 mute_notice=False 时只回复失败，成功静默。支持按群覆盖 (group_overrides)。"""
        if not ok:
            return True  # 失败总是提示
        return bool(self._store.get_group_setting(
            group_id, "mute_notice", self._store.config.get("mute_notice", True)))

    def _should_notify_group_name(self, group_id: str, ok: bool) -> bool:
        """判断修改群名是否需要回复（owner 09-18）。
        配置 group_name_notice=False 时只回复失败，成功静默。支持按群覆盖 (group_overrides)。"""
        if not ok:
            return True  # 失败总是提示
        return bool(self._store.get_group_setting(
            group_id, "group_name_notice", self._store.config.get("group_name_notice", True)))
