"""L2 OneBot API 封装层：调用各协议端 action、消息收发、撤回、管理操作等。

迁出自 main.py 的 OneBot API 封装方法。依赖：
- L0 compat：MessageChain（_send）
- L1 ConfigStore（构造注入）：get_group_setting（_notify_admins）
- astrbot Context（构造注入）：send_private_msg / send_group_msg / get_stranger_info 等
- AstrBot 消息组件 Plain（_send_group_text 回退）

依赖规则：不 import 同层模块。_recall_user_recent_msgs 属 L3，本阶段不迁移（留待阶段 3）。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

from .compat import MessageChain

if TYPE_CHECKING:
    from .config_store import ConfigStore

# PR #277 审查反馈：协议端错误文案可能回显请求参数（如 base64:// 全量图片
# 数据、带 token 的下载 URL），直接落日志会泄露内容。入库前统一截断。
_B64_PAYLOAD_RE = re.compile(r"base64://?[A-Za-z0-9+/=\s]{16,}")


def _sanitize_log_text(text, limit: int = 80) -> str:
    """日志文本脱敏：抹除 base64 载荷并按 limit 截断。"""
    t = _B64_PAYLOAD_RE.sub("base64://<已省略>", str(text))
    return t[:limit]


class OneBotApi:
    """OneBot action 调用与消息收发封装。"""

    def __init__(self, *, context, config_store: "ConfigStore"):
        self.context = context
        self._store = config_store

    # ---------- 基础发送 ----------

    async def _send(self, event: AstrMessageEvent, message_list):
        try:
            if hasattr(event, "send"):
                if MessageChain is not None:
                    await event.send(MessageChain(message_list))
                else:
                    # 理论上不会走到（依赖缺失），保留兜底并告警便于排查 #246
                    logger.warning("MessageChain 不可用，尝试以原始列表发送消息")
                    await event.send(message_list)
                return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
        return False

    # ---------- 结果判定 ----------

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
            # #268：无 status/retcode 时，只含错误描述字段（wording/msg/message/
            # error/errmsg）且不带 data 的响应是失败结构——部分协议端权限不足时
            # 返回 {"wording": "ERR..."}，旧逻辑 bool(dict) 误判为成功。
            # 含 data（含 data=null 之外的数据载荷）仍按旧语义 bool(result) 处理。
            error_keys = ("wording", "msg", "message", "error", "errmsg")
            if any(k in result for k in error_keys) and "data" not in result:
                logger.warning(f"[OneBot] 响应无 status/retcode 但含错误字段，判定为失败: {result}")
                return False
        return bool(result)

    def _describe_action_failure(self, result, action: str) -> str:
        """把 OneBot 失败响应解析为可读原因，区分未实现/未知 action/预置限制/权限不足/其他。"""
        if result is None:
            return f"当前 OneBot 实现可能不支持此 API（{action} 无可用调用通路）"
        if isinstance(result, str):
            return _sanitize_log_text(result.strip()
                                      or f"{action} 调用失败", 100)
        if isinstance(result, dict):
            retcode = result.get("retcode")
            message = str(result.get("message") or result.get("msg") or
                          result.get("wording") or "").strip()
            low = message.lower()
            # 无 bot 直调通路且其它调用方式都失败：等价于当前实现不支持此 API
            if "无可用调用通路" in message:
                return f"当前 OneBot 实现可能不支持此 API（{action} 无可用调用通路）"
            # #245：协议端没有这个 action（如 NapCat/Lagrange 返回 "unknown action"）
            if any(k in low for k in ("unknown action", "unknown method", "no such action",
                                      "action not found", "unimplemented")):
                return f"当前 OneBot 实现不支持 {action} 这个接口"
            if retcode == 10002 or any(
                    k in low for k in ("not support", "unsupported", "not implement",
                                       "未实现", "不支持")):
                return f"当前 OneBot 实现可能不支持此 API（{action}）"
            # 群标签类失败（协议端仅允许预置列表中的标签）
            if any(k in low for k in ("group tag", "tag not", "label")) or "标签" in message:
                return "当前 OneBot 实现仅允许添加预置列表中的群标签"
            if any(k in low for k in ("permission", "denied", "forbidden", "不允许")) \
                    or "权限" in message or retcode in (1200,):
                return f"权限不足（{action} 需要更高权限，如群主/管理员身份）"
            if message:
                return f"后端返回：{_sanitize_log_text(message)}"
            return f"调用失败(retcode={retcode})"
        return f"{action} 调用失败"

    # ---------- 通用 action 调用 ----------

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

    async def _call_onebot_raw(self, event: AstrMessageEvent, action: str, **params):
        """#242: 以原始响应调用 OneBot action，区分"调用失败"与"无可用通路"。
        aiocqhttp 的 call_action 成功时只返回 data 字段（set 类 action 的 data
        多为 null，即 None），失败时抛 ActionFailed（e.result 含 retcode/wording）；
        而 _execute_action 会吞掉异常并返回 None，失败与成功无法区分。这里直调
        bot.call_action 把失败响应收进字典；无 bot 直调通路时回退 _execute_action，
        此时返回 None 视为无可用调用通路。"""
        # 参数转换：group_id / user_id / message_id 转为 int（OneBot 要求）
        for k in ("group_id", "user_id", "message_id"):
            if k in params and isinstance(params[k], str) and params[k].isdigit():
                params[k] = int(params[k])

        bot = getattr(event, "bot", None)
        call = getattr(bot, "call_action", None)
        if callable(call):
            try:
                return await call(action, **params)
            except Exception as e:
                result = getattr(e, "result", None)
                if isinstance(result, dict):
                    return result
                return {"status": "failed", "retcode": -1, "wording": str(e)}
        # 无 bot 直调通路：回退 _execute_action，None 视为无可用调用通路
        result = await self._execute_action(event, action, return_raw=True, **params)
        if result is None:
            return {"status": "failed", "retcode": -1, "wording": "无可用调用通路"}
        return result

    async def _call_action_fallback(self, event: AstrMessageEvent, actions, **params):
        """按顺序尝试多个候选 action，返回 (是否成功, 最后一个原始响应)。

        #232：旧实现用「返回 None 才回退」，而 _execute_action 失败返回的是 False，
        回退分支永远不可达，导致始终提示「不支持此 API」。这里统一用原始响应判定。
        用于各家 OneBot 实现 action 名不一致的场景（set_group_todo / kick / 加群审核等）。
        """
        last = None
        for action in actions:
            last = await self._call_onebot_raw(event, action, **params)
            if self._action_result_success(last):
                return True, last
        return False, last

    # ---------- 具体 action 封装 ----------

    @staticmethod
    def _normalize_member_list(result):
        """把 get_group_member_list 的返回规整为成员 dict 列表；拿不到列表返回 None。

        #256：aiocqhttp 的 call_action 成功时直接返回 data 本身（即成员列表），
        部分框架再包一层 {data: [...]}；失败响应是含 status/retcode 的 dict 但没有
        data 列表。旧实现只认 dict 形态，列表形态会被误判为「无被禁言成员」。
        """
        if isinstance(result, list):
            return [m for m in result if isinstance(m, dict)]
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, list):
                return [m for m in data if isinstance(m, dict)]
        return None

    @staticmethod
    def _member_mute_remaining(m: dict) -> int:
        """读取单个成员的剩余禁言秒数（0=未禁言）。

        #256：不同 OneBot 实现字段不同——OneBot v11 标准成员对象用
        shut_up_timestamp（禁言到期 Unix 时间戳，0=未禁言），部分实现给
        mute_left 等剩余秒数，另有 ban_end_time / mute_end_time / mute_until
        等到期时间戳变体。旧实现只认 mute_left，导致多数协议端恒判「无人被禁言」。
        """
        now = int(time.time())
        for key in ("mute_left", "mute_time_remaining", "ban_left"):
            try:
                v = float(m.get(key))
            except (TypeError, ValueError):
                continue
            if v > 0:
                return int(v)
        for key in ("shut_up_timestamp", "ban_end_time", "mute_end_time", "mute_until"):
            try:
                v = float(m.get(key))
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
            if v >= 1_000_000_000:
                remaining = int(v) - now
                if remaining > 0:
                    return remaining
            else:
                return int(v)
        return 0

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

    # ---------- 群标签（#285：统一 add_group_tag / delete_group_tag / get_group_tags） ----------

    async def _add_group_tag(self, event: AstrMessageEvent, group_id: str, tag: str):
        """添加群标签。#285：NapCat 等协议端统一命名为 add_group_tag，
        旧扩展名 set_group_tag（LLOneBot 等）保留为兼容回退；参数名逐候选
        尝试（tag= / tags=）。返回 (是否成功, 最后一个原始响应)。"""
        last = None
        for action, extra in (
            ("add_group_tag", {"tag": tag}),
            ("add_group_tag", {"tags": [tag]}),
            ("set_group_tag", {"tag": tag}),
            ("set_group_tag", {"tags": [tag]}),
        ):
            p = {"group_id": str(group_id)}
            p.update(extra)
            last = await self._call_onebot_raw(event, action, **p)
            if self._action_result_success(last):
                return True, last
        return False, last

    async def _delete_group_tag(self, event: AstrMessageEvent, group_id: str, tag: str):
        """删除群标签。#285：统一命名 delete_group_tag；参数名逐候选尝试。
        返回 (是否成功, 最后一个原始响应)。"""
        last = None
        for action, extra in (
            ("delete_group_tag", {"tag": tag}),
            ("delete_group_tag", {"tags": [tag]}),
        ):
            p = {"group_id": str(group_id)}
            p.update(extra)
            last = await self._call_onebot_raw(event, action, **p)
            if self._action_result_success(last):
                return True, last
        return False, last

    async def _get_group_tags(self, event: AstrMessageEvent, group_id: str):
        """查询群标签。#285：统一命名 get_group_tags，返回原始响应由调用方解析。"""
        return await self._call_onebot_raw(event, "get_group_tags", group_id=str(group_id))

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
        """踢出群成员。OneBot v11 标准接口为 set_group_kick（#211：
        旧实现只调用的 kick 并非标准 action，协议端返回 unknown action 导致"踢出全部失败"）。"""
        ok, res = await self._call_action_fallback(
            event, ("set_group_kick", "kick"), group_id=group_id, user_id=qq)
        if not ok:
            logger.warning(f"[踢人] 失败 group={group_id} user={qq}: "
                           f"{self._describe_action_failure(res, 'set_group_kick')}")
        return ok

    async def _set_group_avatar(self, event: AstrMessageEvent, group_id: str, file: str):
        """修改群头像，file 可以是 URL 或本地路径或 base64。

        #268：改用 _call_onebot_raw 拿原始响应判定——ActionFailed 的
        retcode/wording 不再被吞掉，bot 无群管权限等失败不会再误报成功。
        """
        res = await self._call_onebot_raw(
            event, "set_group_portrait", group_id=group_id, file=file)
        ok = self._action_result_success(res)
        if not ok:
            logger.warning(f"[群头像] 设置失败 group={group_id}: "
                           f"{self._describe_action_failure(res, 'set_group_portrait')}")
        return ok

    async def _handle_group_request(self, event: AstrMessageEvent, flag: str, approve: bool,
                                    reason: str = "", sub_type: str = "add") -> bool:
        """同意/拒绝加群申请，返回是否真正成功（#228）。

        OneBot v11 标准接口为 set_group_add_request（需 flag + sub_type + approve），
        旧实现只调用的 handle_group_request 并非标准 action；且调用方从不校验返回值，
        导致协议端拒绝后仍回复"已同意"。这里按顺序回退并返回真实结果。

        #252：部分实现要求 sub_type 与申请事件一致（add/invite），改用申请事件
        自带的 sub_type 而非硬编码 "add"；个别实现拒收 sub_type 参数，追加一次
        不带 sub_type 的尝试；逐个记录真实失败原因，不再只报最后一个候选的
        「不支持接口」，避免 flag 过期等真实原因被掩盖。
        """
        params = {"flag": flag, "approve": approve}
        if not approve and reason:
            params["reason"] = reason
        attempts = []
        candidates = (
            ("set_group_add_request", {"sub_type": sub_type or "add"}),
            ("set_group_add_request", {}),
            ("handle_group_request", {}),
        )
        for action, extra in candidates:
            p = dict(params)
            p.update(extra)
            res = await self._call_onebot_raw(event, action, **p)
            if self._action_result_success(res):
                return True
            attempts.append(f"{action}{extra and list(extra.keys()) or ''}: "
                            f"{self._describe_action_failure(res, action)}")
        logger.warning(f"[加群审核] 处理失败 flag={flag} approve={approve}: " + "；".join(attempts))
        return False

    async def _match_pending_by_quote(self, event, group_id: str, reply_id: str,
                                      pending: dict) -> dict:
        """#258：被引用消息 ID 不在待处理表时的兜底定位。

        拉取被引用消息原文，若确为插件发的【新人加群】通知，则按其中
        「用户qq号：X」匹配同群待处理记录（多条例取最新一条）。被引用消息
        拉取失败或内容不含通知特征时不兜底——避免把普通聊天里的「同意」
        误处理成加群审核。
        """
        try:
            quoted = await self._execute_action(event, "get_msg",
                                                message_id=reply_id, return_raw=True)
        except Exception as exc:
            logger.debug(f"[加群审核] 兜底定位：get_msg 失败: {exc}")
            return {}
        qdata = None
        if isinstance(quoted, dict):
            qdata = quoted.get("data") if isinstance(quoted.get("data"), dict) else quoted
        if not isinstance(qdata, dict):
            return {}
        parts = []
        for seg in (qdata.get("message") or []):
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text", "")))
        qtext = "".join(parts)
        if "【新人加群】" not in qtext:
            return {}
        # #270：invite 通知同时含「邀请人QQ」行，必须优先匹配「被邀请人QQ」
        # （待处理记录 user_id 存的是被邀请人/申请人），普通申请仍匹配「用户qq号」
        m = re.search(r"(?:被邀请人QQ|用户qq号)[:：]\s*(\d{5,12})", qtext)
        if m:
            uid = m.group(1)
            matched = {}
            for rec in pending.values():
                if (isinstance(rec, dict) and str(rec.get("group_id")) == str(group_id)
                        and str(rec.get("user_id")) == uid):
                    matched = rec
            if matched:
                logger.info(f"[加群审核] 引用消息 {reply_id} 不在待处理表，"
                            f"已按通知内容定位到用户 {uid} 的待处理申请")
            return matched
        # PR #289 审查：被邀请人未知的邀请通知没有 QQ 号行，通知文本改为携带
        # 「申请标识：{flag}」；协议端发送成功但未返回 message_id 时待处理记录
        # 以 flag:<flag> 为键暂存，这里按该标识回查。绝不把邀请人 QQ 当申请人。
        m_flag = re.search(r"申请标识[:：]\s*([^\s，。；;]+)", qtext)
        if m_flag:
            rec = pending.get(f"flag:{m_flag.group(1)}")
            if (isinstance(rec, dict) and str(rec.get("group_id")) == str(group_id)):
                logger.info(f"[加群审核] 引用消息 {reply_id} 不在待处理表，"
                            f"已按申请标识定位被邀请人未知的邀请入群待处理记录")
                return rec
        return {}

    # ---------- 消息收发辅助 ----------

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
        for m in self._normalize_member_list(member_list) or []:
            if m.get("role") == "owner":
                return str(m.get("user_id", ""))
        return ""

    async def _is_group_member(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        """#271：查询用户是否已是群成员。

        管理员直接把人拉进群后，部分协议端仍会投递 group 请求/通知，
        用 get_group_member_info 复核可避免对已入群用户重复弹「新人加群」。
        协议端不支持该 action、查询失败或查无此人时一律返回 False，
        保证普通加群申请（申请人尚未入群）流程不受影响。
        """
        if not group_id or not user_id:
            return False
        try:
            ok, res = await self._call_action_fallback(
                event, ("get_group_member_info",), group_id=group_id, user_id=user_id)
        except Exception as exc:
            logger.debug(f"[加群审核] get_group_member_info 查询失败 "
                         f"group={group_id} user={user_id}: {exc}")
            return False
        if not ok:
            return False
        data = res.get("data") if isinstance(res, dict) and isinstance(res.get("data"), dict) else res
        return isinstance(data, dict) and data.get("user_id") is not None

    @staticmethod
    def _extract_message_id(result) -> str:
        """从 send 类 action 响应中提取 message_id（#265）。

        aiocqhttp 的 call_action 成功时直接返回 data 本身（send_group_msg 即
        `{"message_id": 123}`），部分框架再包一层 `{"status": "ok", "data": {...}}`，
        少数实现把 message_id 作为裸值返回。#258 只认「带 data 子字典」一种形态，
        发送成功却提取不到 message_id，于是被当成失败并回退 `_send`，
        导致同一段群通知被发送两次。
        """
        if result is None or isinstance(result, bool):
            return ""
        if isinstance(result, (int, str)):
            return str(result).strip()
        if isinstance(result, dict):
            for key in ("message_id", "messageId"):
                val = result.get(key)
                if val is not None and str(val).strip():
                    return str(val).strip()
            data = result.get("data")
            if isinstance(data, dict):
                for key in ("message_id", "messageId"):
                    val = data.get(key)
                    if val is not None and str(val).strip():
                        return str(val).strip()
            elif isinstance(data, (int, str)) and not isinstance(data, bool) and str(data).strip():
                return str(data).strip()
        return ""

    async def _send_group_text(self, event: AstrMessageEvent, group_id: str, text: str):
        """向指定群发送纯文本消息，返回 message_id（用于后续引用回复关联）。

        #258：旧实现探测 self.context.send_group_msg——AstrBot Context 并不暴露
        该 OneBot action，探测必然失败，实际总走 _send 兜底并返回空串，导致
        加群申请通知的 message_id 记不进待处理表，管理员引用回复 /同意 时
        永远查不到记录。这里改为优先走 OneBot send_group_msg（event.bot 直调），
        从响应 data.message_id 拿真实消息 ID。

        #265：修复「发送成功但 message_id 提取失败后继续回退发送」造成
        【新人加群】通知等群通知重复发送两次的问题——只要判定发送成功，
        无论能否取到 message_id 都立即返回，不再进入任何回退通路。
        """
        # #284：多群环境下部分群通知发不出来时，必须把失败原因与群号落到日志，
        # 否则只有「b 群发不了」的现象、没有任何线索。
        raw = await self._call_onebot_raw(event, "send_group_msg",
                                          group_id=str(group_id), message=text)
        if self._action_result_success(raw):
            mid = self._extract_message_id(raw)
            if mid:
                return mid
            # 消息已成功发出：拿不到 message_id 时只能返回空串（由调用方按被引用
            # 消息内容兜底定位），绝不能再次发送，否则同一段文本会重复发送（#265）
            logger.warning("[加群审核] send_group_msg 成功但未返回 message_id")
            return ""
        logger.warning(
            f"[加群审核] send_group_msg 直调失败 group={group_id}: "
            f"{self._describe_action_failure(raw, 'send_group_msg')}")
        # 仅在首次发送确实失败时，才尝试其它通路
        try:
            if hasattr(self.context, "send_group_msg"):
                result = await self.context.send_group_msg(group_id=int(group_id), message=text)
                mid = self._extract_message_id(result)
                if mid:
                    return mid
                if self._action_result_success(result):
                    logger.warning("[加群审核] context.send_group_msg 成功但未返回 message_id")
                    return ""
                logger.warning(
                    f"[加群审核] context.send_group_msg 失败 group={group_id}: {result}")
        except Exception as e:
            logger.error(f"发送群消息失败 group={group_id}: {e}")
        # 回退：使用 _send 但拿不到 message_id
        sent = await self._send(event, [Plain(text)])
        if not sent:
            logger.error(
                f"[加群审核] 群 {group_id} 的通知所有发送通路均失败，"
                f"请检查 bot 是否在目标群、协议端连接是否正常")
        return ""

    @staticmethod
    def _first_str_field(source: dict, keys) -> str:
        """从 dict 中按顺序返回首个非空字符串字段值（#261）。"""
        if not isinstance(source, dict):
            return ""
        for key in keys:
            val = source.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
        return ""

    async def _get_stranger_info(self, event: AstrMessageEvent, user_id: str) -> dict:
        """通过 OneBot get_stranger_info 获取陌生用户资料（昵称/等级等），失败返回 {}。

        #257：旧实现探测 self.context.get_stranger_info 属性——AstrBot Context
        不暴露 OneBot action，探测必然失败，昵称静默回退成 QQ 号、QQ 等级恒为
        「未知」。统一改走 _execute_action（event.bot.call_action 优先），并兼容
        「裸 data」与「{data: {...}}」两种返回形态。
        """
        raw = await self._execute_action(event, "get_stranger_info",
                                         user_id=str(user_id), return_raw=True)
        info = None
        if isinstance(raw, dict):
            info = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        return info if isinstance(info, dict) else {}

    async def _get_user_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称（OneBot get_stranger_info，多字段名兜底，#261）。

        部分协议端不返回 `nickname`，而是 `nick` / `card` / `name` 等字段，
        旧实现只读 `nickname` 导致昵称回落成 QQ 号。这里按候选字段顺序提取，
        全部缺失时记录 warning 便于排查，并返回空串（由调用方决定展示文案）。
        """
        try:
            info = await self._get_stranger_info(event, user_id)
            name = self._first_str_field(info, ("nickname", "nick", "card", "name"))
            if name:
                return name
            logger.warning(f"[加群通知] get_stranger_info 未返回昵称字段(user={user_id})：{info}")
        except Exception as e:
            logger.warning(f"[加群通知] 获取昵称失败(user={user_id}): {e}")
        return ""

    async def _get_stranger_level(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取陌生人 QQ 等级（get_stranger_info 的 level 字段，#189；不支持返回空串）。"""
        try:
            info = await self._get_stranger_info(event, user_id)
            level = self._first_str_field(info, ("level",))
            if level:
                return level
            logger.warning(f"[加群通知] get_stranger_info 未返回等级字段(user={user_id})")
        except Exception as e:
            logger.warning(f"[加群通知] 获取等级失败(user={user_id}): {e}")
        return ""

    async def _notify_admins(self, text: str, group_id: str = ""):
        """向 join_notify_admins 配置的管理员发送私聊通知。"""
        for admin_id in self._store.get_group_setting(group_id, "join_notify_admins", []) or []:
            await self._send_private_msg(str(admin_id), text)
