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

    def _action_result_explicit_success(self, result) -> bool:
        """响应是否带**明确**的成功信号（status=ok/async/success 或 retcode/code=0）。

        有副作用的 action 用宽松的 `_action_result_success` 判定就够了（含糊
        响应按成功处理可以避免重复执行）；但"能不能对外声称已经成功"必须区分：
        只有明确成功信号才能当作证据，含糊载荷（如 {"data": None}）只能算
        结果未知（PR #289 审查：ambiguous-success-response）。
        """
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            status = str(result.get("status", "")).strip().lower()
            if status in {"ok", "async", "success", "succeeded"}:
                return True
            for key in ("retcode", "code"):
                if key in result and result[key] is not None:
                    try:
                        return int(result[key]) == 0
                    except (TypeError, ValueError):
                        return False
        return False

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

    @staticmethod
    def _normalize_tags(items) -> list:
        tags = []
        for t in items or []:
            if isinstance(t, dict):
                t = t.get("tag") or t.get("name") or t.get("title") or ""
            t = str(t).strip()
            if t and t not in tags:
                tags.append(t)
        return tags

    @staticmethod
    def looks_like_join_request(item) -> bool:
        """条目是否带加群申请专属字段（flag / sub_type / request_type）。

        群系统消息里的普通条目（成员变动通知等）没有这些字段，用它来区分
        「真申请」与「系统消息」，避免诊断输出把系统消息误标成申请记录。
        不用 comment 判断：系统消息也可能带备注文本，会把非申请条目放进来
        （PR #289 审查：misleading-diagnostic-output）。
        """
        if not isinstance(item, dict):
            return False
        return bool(
            item.get("flag")
            or item.get("sub_type")
            or item.get("request_type")
        )

    @classmethod
    def collect_join_requests(cls, data, group_id: str, system_msg: bool = False):
        """从申请查询接口的响应里挑出加群申请，返回 (申请列表, 非申请条目数)。

        - system_msg=True（get_group_system_msg）：只有 ``join_requests`` 字段里的
          条目才归为申请，其它字段（如邀请记录、系统通知）一律计入非申请条目
          ——即使系统条目碰巧带了 flag/sub_type 字段也不算，避免诊断输出把
          系统消息误报成加群申请（PR #289 审查：misleading-diagnostic-output）；
        - system_msg=False（get_group_filtered_requests / get_group_ignore_add_request）：
          这类接口整个列表就是请求列表，列表内条目都算申请；
        - system_msg=True 且响应是裸数组（没有字段名可依）时，只有带 ``flag``
          的条目才算申请——flag 是执行同意/拒绝的必要标识，用它把系统通知挡掉；
        - 两种情况都只统计 group_id 与本群一致（或未提供群号）的条目。
        """
        requests, others = [], 0

        def _belongs(item) -> bool:
            gid = str(item.get("group_id", "") or "")
            return not gid or gid == str(group_id)

        if isinstance(data, dict):
            for key, value in data.items():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if system_msg:
                        # 系统消息接口只认 join_requests 这一个字段
                        is_req = key == "join_requests"
                    else:
                        is_req = True
                    if is_req:
                        if isinstance(item, dict) and _belongs(item):
                            requests.append(item)
                    else:
                        others += 1
        elif isinstance(data, list):
            for item in data:
                if system_msg:
                    # 裸数组没有字段名可依，只能按"带 flag"（可执行同意/拒绝的
                    # 申请标识）判定，避免把系统通知也当成申请
                    # （PR #289 审查：misleading-diagnostic-output）。
                    is_req = isinstance(item, dict) and bool(item.get("flag"))
                else:
                    is_req = True
                if is_req:
                    if isinstance(item, dict) and _belongs(item):
                        requests.append(item)
                else:
                    others += 1
        return requests, others

    def _tags_response_ok(self, raw) -> bool:
        """判断 get_group_tags 的字典响应是否**明确**成功。

        查询类响应不能用宽松的 `_action_result_success`：它把"既没有 status
        也没有 retcode、只带 data 的字典"当成功（有副作用 action 的语义），
        于是一份错误载荷（例如 {"error": "..."}、{"code": 1} 或干脆只有
        {"data": []}）会被读成"本群没有标签"，覆盖型回退便会拿它当基线把
        已有标签整体清空（PR #289 审查：misleading-empty-state）。

        这里要求显式成功信号：status 为 ok/async/success，或 retcode/code
        存在且为 0；同时显式失败信号优先——status 是失败态、success 为 False、
        带非空 error、retcode/code 非 0，一律判失败。
        """
        if not isinstance(raw, dict) or not raw:
            return False
        status = str(raw.get("status", "")).strip().lower()
        if status in {"failed", "fail", "failure", "error"}:
            return False
        if raw.get("success") is False:
            return False
        if raw.get("error"):
            return False
        codes = []
        for key in ("retcode", "code"):
            if key in raw and raw[key] is not None:
                try:
                    codes.append(int(raw[key]))
                except (TypeError, ValueError):
                    # 取不到数值的成功码无法确认结果，按失败处理
                    return False
        if any(code != 0 for code in codes):
            return False
        if codes:
            return True
        return status in {"ok", "async", "success", "succeeded"}

    async def _group_tags_read(self, event: AstrMessageEvent, group_id: str):
        """读取当前群标签，返回 (读取是否成功, 标签列表)。

        供 add/delete 的副作用确认使用：只有读取本身成功时调用方才应信任
        结果（协议端不支持 get_group_tags 时返回 (False, [])，不能当作
        "标签不存在"的证据）。

        兼容协议端三种返回形态：裸数组、{data: [...]}、{data: {tags: [...]}}。
        "响应本身就是列表"视为成功（没有任何状态字段可看，形态明确即证据）；
        字典响应则必须通过 `_tags_response_ok` 的显式成功判定。

        区分"成功且明确返回空列表"（(True, [])）与"成功但响应里找不到可识别的
        标签字段"（(False, []) 并记 warning）：后者多半是协议端响应结构不兼容，
        当成"没有标签"会让覆盖型回退把已有标签当基线清空
        （PR #289 审查：incompatible-response-shape）。
        """
        raw = await self._get_group_tags(event, group_id)
        if isinstance(raw, list):
            return True, self._normalize_tags(raw)
        if not self._tags_response_ok(raw):
            return False, []
        candidates = []
        data = raw.get("data")
        for source in (raw, data):
            if isinstance(source, dict):
                candidates.append(source.get("tags"))
                candidates.append(source.get("group_tags"))
        for candidate in (raw.get("data"), raw.get("tags"), raw.get("group_tags")):
            if isinstance(candidate, list):
                candidates.append(candidate)
        for candidate in candidates:
            if isinstance(candidate, list):
                return True, self._normalize_tags(candidate)
        # 成功但结构无法识别：按读取失败处理，不能当作"本群没有标签"
        logger.warning(f"[OneBot] get_group_tags 响应结构无法识别（按读取失败处理）: {raw}")
        return False, []

    async def _add_group_tag(self, event: AstrMessageEvent, group_id: str, tag: str):
        """添加群标签。返回 (是否成功, 最后一个原始响应, 告警码)。

        告警码："" 正常；"tags_may_be_lost" 表示覆盖型接口执行后本群原有标签
        可能已丢失（补回失败），调用方必须提示管理员人工核对，不能当普通成功/失败处理；
        "tags_altered" 表示覆盖型接口执行后出现了基线之外的标签（语义已偏离），
        需提示管理员核对清理；
        "unverified" 表示读接口不可用、响应又只是含糊成功，无法确认标签到底加没加上，
        调用方应提示管理员到客户端核对，而不是回执"添加失败"或"已添加"。

        #285：NapCat 等协议端统一命名为 add_group_tag，旧扩展名 set_group_tag
        （LLOneBot 等）保留为兼容回退；参数名逐候选尝试（tag= / tags=）。

        成功判定以**读回**为准：读回来确实有该标签才返回成功。协议端的成功响应
        只代表"请求被接受"，缺 status/retcode 的含糊载荷（如 {"data": None}）更
        无从判断，读接口不可用时不能凭它对外声称已添加
        （PR #289 审查：ambiguous-success-response）。

        PR #289 审查（duplicate-side-effect）：add 是有副作用的 action，且部分
        协议端的成功响应缺 status/retcode 会被误判为失败；重试下一个候选前先
        读回标签确认——已存在即幂等成功，避免同一标签被重复添加。

        读回只在"读得通"时才做，且每个候选最多读一次：协议端压根不支持
        get_group_tags 时首轮就读失败，后续候选不再重复查询，避免一次添加
        打出 4 次无效请求（PR #289 审查：duplicate-side-effect）。

        读接口不可用时只剩"按 action 结果判定"这一条路，此时重试门槛更高：
        只有响应明确指出 action/参数不被接受才换下一个变体，权限不足、群号
        错误、响应结构含糊（读不回来也无法确认是否已生效）一律停手，避免对
        同一个有副作用的 action 反复执行（PR #289 审查：duplicate-side-effect）。

        候选按副作用等级分开：`add_group_tag` 是纯新增语义，名称/参数变体都
        可以放心回退；`set_group_tag` 是覆盖型（可能整体改写标签集合），只在
        读接口可用**且确实遇到 action 名不被接受**时才作为兼容回退，权限不足/
        标签预置限制/群号错误等失败一律直接返回，执行后还要确认目标标签已加上、
        被挤掉的旧标签都已补回（PR #289 审查：unsafe-action-fallback /
        unsafe-overwrite-fallback / incomplete-tag-restoration）。
        """
        # 读接口能力先探一次：探不通就彻底不做读回，只按 action 结果判定
        baseline_ok, baseline_tags = await self._group_tags_read(event, group_id)
        if baseline_ok and tag in baseline_tags:
            # 标签本来就在：幂等成功，不再调用有副作用的 action
            return True, None, ""
        read_supported = baseline_ok
        candidates = [
            ("add_group_tag", {"tag": tag}),
            ("add_group_tag", {"tags": [tag]}),
        ]
        if read_supported:
            # 只有能读回标签集合时，才允许降级到覆盖型 set_group_tag：
            # 读不回来就无法发现"其它标签被覆盖没了"，也不能事后补回
            candidates.extend([
                ("set_group_tag", {"tag": tag}),
                ("set_group_tag", {"tags": [tag]}),
            ])
        last = None
        action_unsupported = False
        explicit_ok_absent = False
        for index, (action, extra) in enumerate(candidates):
            if action == "set_group_tag":
                # 覆盖型接口只是"协议端没有纯新增 action"时的兼容回退：读接口
                # 可用**且确实**遇到 action 名不被接受才进来。权限不足、标签
                # 预置限制、群号错误等失败一律不降级——为了加一个标签把本群
                # 已有标签整体改写，代价远大于加不上
                # （PR #289 审查：unsafe-overwrite-fallback）。
                if not read_supported or not action_unsupported:
                    break
            p = {"group_id": str(group_id)}
            p.update(extra)
            last = await self._call_onebot_raw(event, action, **p)
            if action == "set_group_tag":
                # 覆盖型调用后确认目标标签在、旧标签全在，缺什么补什么；
                # 补不回来必须让调用方提示管理员，不能当普通成功/失败
                state = await self._verify_and_restore_tags(
                    event, group_id, baseline_tags, tag)
                if state == "restore_failed":
                    logger.error(
                        f"[OneBot] 覆盖型群标签接口执行后本群原有标签可能已丢失，"
                        f"已停止后续回退（群 {group_id}）")
                    return False, last, "tags_may_be_lost"
                if state == "unexpected_tags":
                    logger.error(
                        f"[OneBot] 覆盖型群标签接口执行后出现预期之外的标签，"
                        f"请人工核对（群 {group_id}）")
                    return False, last, "tags_altered"
                if state == "target_missing":
                    logger.error(
                        f"[OneBot] 覆盖型群标签接口执行后目标标签仍未加入，"
                        f"本次添加未生效（群 {group_id}）")
                    return False, last, ""
                return True, last, ""
            if self._failure_says_action_unsupported(last):
                action_unsupported = True
            confirmed_absent = False
            if read_supported:
                # 读回才是可信证据：协议端的成功响应只代表"请求被接受"，
                # 缺 status/retcode 的含糊载荷更无从判断，读回来确实有该标签
                # 才能对外声称已添加（PR #289 审查：ambiguous-success-response）
                read_ok, tags = await self._group_tags_read(event, group_id)
                if read_ok:
                    if tag in tags:
                        return True, last, ""
                    confirmed_absent = True
                    if self._action_result_explicit_success(last):
                        # 明确成功却读不回该标签：不能直接断言失败（读回可能
                        # 滞后、操作其实已生效），但也绝不因此进入覆盖型接口；
                        # 最多在纯新增变体之间再试一次
                        # （PR #289 审查：unnecessary-side-effect-retry /
                        # premature-failure-after-explicit-success）
                        explicit_ok_absent = True
                else:
                    read_supported = False
            if index + 1 >= len(candidates):
                break
            # 明确成功过就只在纯新增变体之间再试一次，绝不进入覆盖型接口
            # （PR #289 审查：premature-failure-after-explicit-success）
            if explicit_ok_absent and candidates[index + 1][0] != "add_group_tag":
                break
            # 读回确认"确实没加上"时可以安全换变体（add 是纯新增语义）；
            # 读不回来时只有"action/参数不被接受"才换，含糊响应一律停手，
            # 避免重复执行有副作用的 action
            if not self._failure_invites_retry(
                    last, action,
                    ambiguous_retry=confirmed_absent or explicit_ok_absent):
                break
        if not read_supported and last is not None:
            if self._action_result_explicit_success(last):
                # 协议端明确回报成功、但读接口不可用：只能以 action 响应为唯一
                # 依据，并在日志里说明未经读回确认
                logger.warning(
                    f"[OneBot] 无法读回群标签，add_group_tag 结果仅按 action 响应判定"
                    f"（群 {group_id} 标签 {tag}）")
                return True, last, ""
            if self._action_result_success(last):
                # 含糊成功 + 读不回来：既不能报成功（可能压根没加上），也不能
                # 断言失败（可能已经加上了），交管理员到客户端核对
                return False, last, "unverified"
        if explicit_ok_absent:
            # 协议端明确成功、读回却始终没有该标签：读回可能滞后，也不能断言
            # 失败（PR #289 审查：premature-failure-after-explicit-success）
            return False, last, "unverified"
        return False, last, ""

    async def _verify_and_restore_tags(self, event: AstrMessageEvent, group_id: str,
                                       baseline_tags: list, tag: str) -> str:
        """覆盖型调用后确认"目标标签已加上"且"旧标签没被挤掉"，缺什么补什么。

        返回状态：
        - "verified"：目标标签在、基线完整、没有多余标签，不需要任何补救；
        - "restored"：目标标签或旧标签有缺失，但都已补回并用读回确认过；
        - "target_missing"：旧标签完整，但目标标签始终没加上（本次添加未生效）；
        - "unexpected_tags"：出现了基线之外、且不是目标标签的标签，语义已偏离
          "基线 + 目标"，需要人工核对；
        - "restore_failed"：有旧标签补不回来或无法确认，原标签可能已丢失
          （读不回标签集合时也按这个状态处理，宁可让管理员核对）。

        只补旧标签是不够的：覆盖型接口可能"保留旧标签但没把目标标签加上"，
        那样会回执"已添加"却查不到目标标签（PR #289 审查：incomplete-tag-restoration）。
        所以目标标签缺失时先用纯新增 action 补加，再恢复基线，最后统一读回确认。

        预期集合按"基线 ∪ 目标标签"校验：覆盖型接口的语义就是整体改写，多出来的
        标签既不是基线也不是目标，说明协议端行为偏离预期，不能默默接受
        （PR #289 审查：incomplete-overwrite-verification）。

        补加的每个 action 结果都要看：只要有一个没被协议端接受，就不能对外声称
        "已恢复"（PR #289 审查：ignored-side-effect-failure）。
        """
        read_ok, tags = await self._group_tags_read(event, group_id)
        if not read_ok:
            logger.warning(
                f"[OneBot] set_group_tag 后无法读取群标签，改写成败未知（群 {group_id}）")
            return "restore_failed"
        expected = set(baseline_tags) | {tag}
        extra = [t for t in tags if t not in expected]
        if extra:
            logger.error(
                f"[OneBot] 覆盖型群标签接口执行后出现预期之外的标签: {extra}（群 {group_id}）")
            return "unexpected_tags"
        missing = [t for t in baseline_tags if t not in tags and t != tag]
        target_ok = tag in tags
        if target_ok and not missing:
            return "verified"
        if not target_ok:
            logger.warning(
                f"[OneBot] 覆盖型群标签接口未加入目标标签，正在补加: {tag}（群 {group_id}）")
            if not await self._add_tag_quietly(event, group_id, tag):
                logger.error(
                    f"[OneBot] 目标标签 {tag} 补加未被协议端接受，以最终读回结果为准"
                    f"（群 {group_id}）")
        if missing:
            logger.warning(
                f"[OneBot] set_group_tag 覆盖了本群原有群标签，正在补回: {missing}（群 {group_id}）")
            for old_tag in missing:
                if not await self._add_tag_quietly(event, group_id, old_tag):
                    logger.error(
                        f"[OneBot] 旧标签 {old_tag} 补加未被协议端接受，以最终读回结果为准"
                        f"（群 {group_id}）")
        # 读回最终确认：目标标签与旧标签都要真的在，不能只看 action 有没有抛错
        final_ok, final_tags = await self._group_tags_read(event, group_id)
        if not final_ok:
            logger.error(f"[OneBot] 补回群标签后无法读回确认（群 {group_id}）")
            return "restore_failed"
        extra = [t for t in final_tags if t not in expected]
        if extra:
            logger.error(
                f"[OneBot] 群标签恢复后出现了预期之外的标签: {extra}（群 {group_id}）")
            return "unexpected_tags"
        still_missing = [t for t in baseline_tags if t not in final_tags and t != tag]
        if still_missing:
            logger.error(
                f"[OneBot] 群标签补回后仍缺失: {still_missing}（群 {group_id}）")
            return "restore_failed"
        if tag not in final_tags:
            logger.error(
                f"[OneBot] 覆盖型群标签接口执行后目标标签仍未加入（群 {group_id} 标签 {tag}）")
            return "target_missing"
        return "restored"

    async def _add_tag_quietly(self, event: AstrMessageEvent, group_id: str, tag: str) -> bool:
        """补加单个群标签并记录结果；返回 action 是否被协议端接受（不抛异常）。"""
        try:
            result = await self._call_onebot_raw(
                event, "add_group_tag", group_id=str(group_id), tag=tag)
        except Exception as e:
            logger.error(f"[OneBot] 补加群标签 {tag} 失败: {e}")
            return False
        if not self._action_result_success(result):
            logger.error(f"[OneBot] 补加群标签 {tag} 未被协议端接受: {result}")
            return False
        return True

    async def _delete_group_tag(self, event: AstrMessageEvent, group_id: str, tag: str):
        """删除群标签。#285：统一命名 delete_group_tag；参数名逐候选尝试。

        返回 (是否成功, 最后一个原始响应, 原因)。原因取值：
        "" / "not_found"（调用前标签就不存在）/ "still_present"（读回时标签仍在，
        删除没生效）/ "read_failed"（读回失败，无法确认）。

        PR #289 审查：所有候选失败后读回标签做副作用确认——但必须先确认
        标签在调用前确实存在，否则"本来就没有这个标签"会被误报成删除成功
        （false-success-delete）。

        成功判定同样以**读回**为准：明确成功响应只说明"请求被接受"，有些协议端
        删不掉也回 status=ok；读回确认标签确实不在才算删除成功，读回失败或标签
        仍在都返回失败/结果未知，不能直接报"已删除"
        （PR #289 审查：unverified-delete-success）。

        失败后换参数变体前同样先读回：标签已消失即视为成功立即返回；读不到
        标签列表时不再重试有副作用的调用；只有"参数/action 不被接受"或"读回
        确认标签仍在（说明上一次根本没生效）"才换下一个变体，权限不足等明确
        失败不重复执行（duplicate-side-effect）。
        """
        before_ok, before_tags = await self._group_tags_read(event, group_id)
        tag_exists = before_ok and tag in before_tags
        if before_ok and not tag_exists:
            # 删除前就不存在：不必发出任何删除请求
            return False, None, "not_found"

        candidates = (
            ("delete_group_tag", {"tag": tag}),
            ("delete_group_tag", {"tags": [tag]}),
        )
        last = None
        read_supported = before_ok
        after_ok, after_tags = before_ok, before_tags
        still_present = False
        for index, (action, extra) in enumerate(candidates):
            p = {"group_id": str(group_id)}
            p.update(extra)
            last = await self._call_onebot_raw(event, action, **p)
            still_present = False
            if read_supported:
                after_ok, after_tags = await self._group_tags_read(event, group_id)
                if after_ok:
                    if tag not in after_tags:
                        # 读回确认已删除（成功响应缺 status/retcode 的情况也覆盖）
                        return True, last, ""
                    # 标签还在：即便响应说成功也没真删掉
                    still_present = True
                else:
                    read_supported = False
                    if self._action_result_explicit_success(last):
                        logger.warning(
                            f"[OneBot] delete_group_tag 回报成功但读回群标签失败，"
                            f"删除结果未确认（群 {group_id} 标签 {tag}）")
                        return False, last, "read_failed"
            elif self._action_result_explicit_success(last):
                # 协议端不支持可靠读接口：只能以 action 响应为唯一依据，并记录说明
                logger.warning(
                    f"[OneBot] 无法读回群标签，delete_group_tag 结果仅按 action 响应判定"
                    f"（群 {group_id} 标签 {tag}）")
                return True, last, ""
            if index + 1 >= len(candidates):
                break
            if not self._failure_invites_retry(
                    last, action, ambiguous_retry=still_present):
                break
        if still_present:
            # 读回确认标签仍在：删除确实没生效（权限不足 / 参数不被接受 / 协议端假成功）
            return False, last, "still_present"
        # 读不到标签列表，无法确认"原本存在、现在消失"，保持失败语义
        return False, last, "read_failed"

    @staticmethod
    def _failure_says_action_unsupported(result) -> bool:
        """失败是否明确表示"这个 action 名/接口本身不被接受"。

        只用来决定能不能降级到覆盖型兼容接口：只有接口名压根不存在才需要换名字；
        参数名不对应该换参数变体，权限不足/标签预置限制/群号错误应该直接停手
        （PR #289 审查：unsafe-overwrite-fallback）。
        """
        if result is None:
            return False
        if isinstance(result, dict):
            text = " ".join(
                str(result.get(k, "")) for k in ("message", "msg", "wording", "error", "errmsg")
            ).lower()
            try:
                if result.get("retcode") is not None and int(result["retcode"]) == 10002:
                    return True
            except (TypeError, ValueError):
                pass
        else:
            text = str(result).lower()
        if not text.strip():
            return False
        blockers = ("权限", "permission", "forbidden", "denied", "not admin",
                    "群号", "group not", "tag not", "标签")
        if any(b in text for b in blockers):
            return False
        hints = ("unknown action", "unknown method", "no such action", "action not found",
                 "unimplemented", "not support", "unsupported", "不支持", "未实现")
        return any(h in text for h in hints)

    @staticmethod
    def _failure_invites_retry(result, action: str, ambiguous_retry: bool = True) -> bool:
        """失败是否属于"换个参数/action 还能成"的类型（决定要不要重试）。

        只有"接口/参数不被接受"才值得换变体；权限不足、群号错误等失败继续
        重试只会对同一个有副作用的 action 重复执行（PR #289 审查：
        duplicate-side-effect）。

        ambiguous_retry 控制"响应结构含糊（无 status/retcode 也没有错误文案）"
        是否算可重试：删除有读回兜底，可以再试一次变体；添加在读接口不可用时
        无法确认上一次到底生效没有，必须停手。

        result 为 None（无可用调用通路）时同样不可重试：那条通路本身就已经
        走不通，换 action 名也还是同一批通路；更重要的是无法确认上一次调用
        是否已经落到协议端，继续试就有重复执行副作用的可能
        （PR #289 审查：unsafe-action-fallback）。
        """
        if result is None:
            return False
        text = str(result).lower() if not isinstance(result, dict) else " ".join(
            str(result.get(k, "")) for k in ("message", "msg", "wording", "error", "errmsg")
        ).lower()
        if not text.strip():
            # 结构含糊（只有 retcode 或空响应）
            return bool(ambiguous_retry)
        hints = ("unknown action", "unknown method", "no such action", "not found",
                 "unsupported", "not support", "不支持", "参数", "param")
        blockers = ("权限", "permission", "forbidden", "denied", "not admin",
                    "群号", "group not", "retcode=1")
        if any(b in text for b in blockers):
            return False
        return any(h in text for h in hints)

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
        """向指定群发送纯文本消息，返回 (是否已发出, message_id)。

        message_id 用于后续引用回复关联；拿不到时为空串，由调用方按被引用
        消息内容兜底定位。

        #258：旧实现探测 self.context.send_group_msg——AstrBot Context 并不暴露
        该 OneBot action，探测必然失败，实际总走 _send 兜底并返回空串，导致
        加群申请通知的 message_id 记不进待处理表，管理员引用回复 /同意 时
        永远查不到记录。这里改为优先走 OneBot send_group_msg（event.bot 直调），
        从响应 data.message_id 拿真实消息 ID。

        #265：修复「发送成功但 message_id 提取失败后继续回退发送」造成
        【新人加群】通知等群通知重复发送两次的问题——只要判定发送成功，
        无论能否取到 message_id 都立即返回，不再进入任何回退通路。

        返回值第一项区分"发出去了但没拿到 ID"与"根本没发出去"：旧实现两者
        都返回空串，调用方无法判断，只能一律把待处理记录按 flag 暂存，管理员
        因此可能引用一条**从未发出**的通知去处理早已失效的申请
        （PR #289 审查：stale-pending-request）。
        """
        # #284：多群环境下部分群通知发不出来时，必须把失败原因与群号落到日志，
        # 否则只有「b 群发不了」的现象、没有任何线索。
        raw = await self._call_onebot_raw(event, "send_group_msg",
                                          group_id=str(group_id), message=text)
        if self._action_result_success(raw):
            mid = self._extract_message_id(raw)
            if mid:
                return True, mid
            # 消息已成功发出：拿不到 message_id 时只能返回空串（由调用方按被引用
            # 消息内容兜底定位），绝不能再次发送，否则同一段文本会重复发送（#265）
            logger.warning("[加群审核] send_group_msg 成功但未返回 message_id")
            return True, ""
        logger.warning(
            f"[加群审核] send_group_msg 直调失败 group={group_id}: "
            f"{self._describe_action_failure(raw, 'send_group_msg')}")
        # 仅在首次发送确实失败时，才尝试其它通路
        try:
            if hasattr(self.context, "send_group_msg"):
                result = await self.context.send_group_msg(group_id=int(group_id), message=text)
                mid = self._extract_message_id(result)
                if mid:
                    return True, mid
                if self._action_result_success(result):
                    logger.warning("[加群审核] context.send_group_msg 成功但未返回 message_id")
                    return True, ""
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
        return sent, ""

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
