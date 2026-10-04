"""L3b 加群审核域（#27 合并 group_manager + #57 引用回复审批）。

迁出自 main.py 的以下逻辑（逐字保留逻辑等价）：
    on_group_event 中的「入群欢迎」「加群请求自动审核」分支
    加群申请引用回复处理（#57，原 on_group_message 尾部代码块）

对外方法：
    _handle_group_increase_notice(event, raw)          —— 入群欢迎（notice.group_increase）
    _handle_group_join_request(event, raw)             —— 加群请求自动审核（request.group）
    _handle_group_request_reply(event, raw, gid, uid, reply_id) —— 引用回复同意/拒绝/拉黑

依赖（构造注入，L3b 只依赖 L0/L1/L2，不 import 同层）：
- ConfigStore（L1）：config / runtime_map / get_group_setting / get_group_override_list
  / save_config
- OneBotApi（L2）：_handle_group_request / _notify_admins / _send_group_text
  / _get_user_nickname
- RuntimeState（L2）：保留注入（pending_join_requests 经 ConfigStore.runtime_map 访问）
- PermissionService（L2）：_is_authorized
- MessageParser（L2）：_extract_text
- context：AstrBot Context（get_stranger_info，取 QQ 等级 #189）

enabled_groups 语义（与 moderation 不同，逐字保留本处原语义）：
    经 get_group_setting 合并按群覆盖；bool 直接采用；空列表回退旧 violation_enabled_groups，
    两者皆空 = 全群启用；非空列表按 * / all / 群号命中判定。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .onebot_api import OneBotApi
    from .permissions import PermissionService
    from .runtime import RuntimeState


class JoinReviewService:
    """加群审核与入群欢迎领域服务。"""

    def __init__(
        self,
        *,
        config_store: "ConfigStore",
        onebot_api: "OneBotApi",
        runtime: "RuntimeState",
        permissions: "PermissionService",
        message_parse: "MessageParser",
        context=None,
    ):
        self._store = config_store
        self.config = config_store.config
        self._api = onebot_api
        self._runtime = runtime
        self._perms = permissions
        self._mp = message_parse
        self.context = context

    async def _handle_group_increase_notice(self, event, raw: dict):
        """入群欢迎（原 on_group_event 的 notice.group_increase 分支）。"""
        group_id = str(raw.get("group_id"))
        welcome_conf = self._store.runtime_map("groups").get(group_id, {})
        if welcome_conf.get("welcome_enabled", False):
            welcome = welcome_conf.get("welcome_message", "欢迎 {at} 加入本群！")
            content = welcome.replace("{at}", f"@{raw.get('user_id')}")
            await self._api._send(event, self._mp._build_text(content))

    async def _handle_group_join_request(self, event, raw: dict):
        """加群请求处理（#27 合并 group_manager）。为 async generator（yield 审批结果）。

        #229：在违禁词判定之前新增「加群自动拒绝关键词」判定
        （join_reject_keywords：本群覆盖优先、否则用全局列表）——命中即自动拒绝
        并把申请人加入本群黑名单，此后其再次申请直接命中黑名单分支。
        """
        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))
        flag = raw.get("flag", "")
        comment = raw.get("comment", "")
        # #252：sub_type 用申请事件原值（add/invite），部分实现要求与 flag 匹配
        sub_type = str(raw.get("sub_type") or "add")
        # #155：审核总开关关闭后，全部自动审核逻辑都跳过
        audit_enabled = bool(self._store.get_group_setting(group_id, "join_audit_enabled", True))
        if not audit_enabled:
            return
        enabled_groups = self._store.get_group_setting(group_id, "enabled_groups", [])
        violation_keywords = self._store.get_group_setting(group_id, "violation_keywords", [])
        join_approve_keywords = self._store.get_group_setting(group_id, "join_approve_keywords", [])
        # #192 owner：留空 = 全群启用；迁移兼容：新列表为空回退旧 violation_enabled_groups；
        # 按群覆盖 bool 最高优先（可对单群显式关）
        if isinstance(enabled_groups, bool):
            enabled = enabled_groups
        elif not enabled_groups:
            legacy_groups = self.config.get("violation_enabled_groups", []) or []
            if legacy_groups:
                enabled = group_id in [str(x) for x in legacy_groups]
            else:
                enabled = True
        else:
            sx_list = [str(x).lower() for x in enabled_groups]
            enabled = ("*" in sx_list or "all" in sx_list
                       or group_id in [str(x) for x in enabled_groups])

        # #194：黑名单用户直接拒绝（无需检查关键词/门禁）
        bl_list = self._store.get_group_setting(group_id, "blacklisted_users", [])
        if bl_list and str(user_id) in [str(x) for x in bl_list]:
            handled = await self._api._handle_group_request(
                event, flag, False, "黑名单用户", sub_type=sub_type)
            yield event.plain_result(
                f"已拒绝 {user_id} 的加群申请（黑名单用户）" if handled else
                f"拒绝 {user_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {user_id}（群 {group_id}）\n"
                f"验证消息: {comment}\n"
                f"原因: 黑名单用户",
                group_id=group_id,
            )
            return

        # #241：群员邀请（invite）自动通过；黑名单在邀请场景同样拦截（见上方）。
        # 警告：invite_auto_approve 等同于豁免对邀请入群者的人工审核，请谨慎开启；
        # 如需限制同邀请人频率，建议配合 join_notify_admins 通知人工监督。
        if str(raw.get("sub_type") or "add") == "invite":
            auto_approve_invite = bool(
                self._store.get_group_setting(group_id, "invite_auto_approve", True))
            if auto_approve_invite:
                # 安全增强（#241 review 补充）：邀请场景同样检查 reject_keywords，避免
                # 恶意邀请人利用 invite 自动通过绕过关键词审核。
                reject_keywords = self._store.get_group_setting(group_id, "join_reject_keywords", [])
                if reject_keywords and any(str(kw) in comment for kw in reject_keywords):
                    # 邀请场景命中拒绝关键词 → 拒绝并加入黑名单（同普通申请处理）
                    hit_kw = next(str(kw) for kw in reject_keywords if str(kw) in comment)
                    bl = self._store.get_group_override_list(group_id, "blacklisted_users")
                    if str(user_id) not in [str(x) for x in bl]:
                        bl.append(str(user_id))
                        self._store.save_config()
                    detail_reason = f"邀请含有禁止词语，自动拒绝（{reject_reason}）"
                    handled = await self._api._handle_group_request(
                        event, flag, False, detail_reason, sub_type="invite")
                    logger.info(f"[加群审核] 群 {group_id} 邀请 {user_id} 命中拒绝关键词「{hit_kw}」，已拒绝并拉黑")
                    return
                # 速率限制：invite_auto_approve_window 秒内同邀请人超过阈值则跳过自动通过，交由后续人工审核
                inviter_key = f"{group_id}_{user_id}"
                try:
                    window_sec = max(0, int(self._store.get_group_setting(
                        group_id, "invite_auto_approve_window", 0) or 0))
                    max_per_window = max(0, int(self._store.get_group_setting(
                        group_id, "invite_auto_approve_max", 0) or 0))
                except (TypeError, ValueError):
                    window_sec, max_per_window = 0, 0
                if window_sec > 0 and max_per_window > 0:
                    now = time.time()
                    records = self._runtime.invite_approve_records.setdefault(group_id, {})
                    history = [t for t in records.get(inviter_key, []) if now - t < window_sec]
                    if len(history) >= max_per_window:
                        logger.info(f"[加群审核] 群 {group_id} 邀请人 {user_id} 速率超限（>{max_per_window}/{window_sec}s），跳过自动通过")
                        return  # 交由后续人工审核流程处理
                    history.append(now)
                    records[inviter_key] = history
                handled = await self._api._handle_group_request(
                    event, flag, True, "邀请入群自动通过", sub_type="invite")
                logger.info(f"[加群审核] 群 {group_id} 邀请申请 {user_id} 自动同意: "
                            f"{'成功' if handled else '失败（协议端）'}")
                return

        # 拒绝理由（#129 使用自定义拒绝理由；#159 优化提示）；#229 关键词自动拒绝复用同一理由
        reject_reason = self._store.get_group_setting(group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"

        # #229：命中「加群自动拒绝关键词」→ 自动拒绝 + 加入本群黑名单 + 通知管理员
        # （列表来源：本群覆盖非空优先，否则全局 join_reject_keywords；与违禁词/自动
        #   同意关键词一致，受加群审核总开关与启用群范围 enabled 控制）
        reject_keywords = self._store.get_group_setting(group_id, "join_reject_keywords", [])
        hit_keyword = None
        if enabled and reject_keywords:
            for kw in reject_keywords:
                if str(kw) and str(kw) in comment:
                    hit_keyword = str(kw)
                    break
        if hit_keyword is not None:
            # 先落黑名单再拒绝：即便协议端拒绝失败（如 flag 过期），后续申请也会被黑名单拦住
            bl = self._store.get_group_override_list(group_id, "blacklisted_users")
            if str(user_id) not in [str(x) for x in bl]:
                bl.append(str(user_id))
                self._store.save_config()
            detail_reason = f"您的加群申请含有本群禁止的词语，自动拒绝（{reject_reason}）"
            handled = await self._api._handle_group_request(
                event, flag, False, detail_reason, sub_type=sub_type)
            yield event.plain_result(
                f"已拒绝 {user_id} 的加群申请（命中自动拒绝关键词「{hit_keyword}」，已加入本群黑名单）" if handled else
                f"拒绝 {user_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志；该用户已加入本群黑名单）")
            if not handled:
                return
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {user_id}（群 {group_id}）\n"
                f"验证消息: {comment}\n"
                f"原因: 命中加群自动拒绝关键词「{hit_keyword}」，已加入本群黑名单",
                group_id=group_id,
            )
            return

        # 命中违禁词：拒绝 + 通知管理员（#129 使用自定义拒绝理由；#159 优化提示）
        if enabled and violation_keywords and any(kw in comment for kw in violation_keywords):
            detail_reason = f"您的加群申请有词触碰到本群违禁词，自动拒绝（{reject_reason}）"
            handled = await self._api._handle_group_request(
                event, flag, False, detail_reason, sub_type=sub_type)
            yield event.plain_result(
                f"已拒绝 {user_id} 的加群申请（含违禁词）" if handled else
                f"拒绝 {user_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {user_id}（群 {group_id}）\n"
                f"验证消息: {comment}\n"
                f"原因: 命中违禁词",
                group_id=group_id,
            )
            return

        # 命中关键词：同意 + 通知管理员 + 群内通知（#186）
        if enabled and join_approve_keywords and any(kw in comment for kw in join_approve_keywords):
            handled = await self._api._handle_group_request(
                event, flag, True, "命中关键词自动同意", sub_type=sub_type)
            yield event.plain_result(
                f"已同意 {user_id} 的加群申请（命中关键词）" if handled else
                f"同意 {user_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            # #229：原 join_request_notify_enabled（#205）全局开关已随配置去重移除，
            # 通知改为始终发送；需要彻底静音时把 join_notify_admins 留空即可
            await self._api._notify_admins(
                f"[加群请求] 已同意 {user_id}（群 {group_id}）\n"
                f"验证消息: {comment}\n"
                f"原因: 命中关键词",
                group_id=group_id,
            )
            # #186：命中加群审核通过关键词后，在该群发送通知
            await self._api._send_group_text(
                event, group_id,
                f"该用户触碰到加群审核通过词语，已自动同意！",
            )
            return

        # 群内提醒（#57）：发送申请消息到对应群聊，等待管理员引用回复同意/拒绝
        if self._store.get_group_setting(group_id, "join_request_notify_in_group", False):
            nickname = await self._api._get_user_nickname(event, user_id)
            # #189：补充 QQ 等级（协议端不支持时显示「未知」）
            level = await self._api._get_stranger_level(event, user_id) or "未知"
            # #261：昵称获取失败时明确标注，避免管理员误以为昵称就是 QQ 号
            nickname_display = nickname or f"获取失败({user_id})"
            notify_text = (
                f"【新人加群】通知\n"
                f"用户qq昵称：{nickname_display}\n"
                f"用户qq号：{user_id}\n"
                f"qq等级：{level}\n"
                f"加群验证消息：{comment or '无'}\n"
                f"回复 /同意 或 /拒绝 或 /拉黑（引用本消息）"
            )
            # 暂存 flag 等待引用回复
            sent_id = await self._api._send_group_text(event, group_id, notify_text)
            pending = self._store.runtime_map("pending_join_requests")
            record = {"flag": flag, "group_id": group_id, "user_id": user_id,
                      "sub_type": sub_type, "ts": int(time.time())}
            # #258：顺带清理超过 7 天的陈旧待处理记录，防止 runtime.json 无限膨胀
            stale = [k for k, v in pending.items()
                     if isinstance(v, dict) and int(v.get("ts", 0) or 0) < int(time.time()) - 7 * 86400]
            for k in stale:
                del pending[k]
            if sent_id:
                pending[str(sent_id)] = record
            else:
                # #258：拿不到通知消息 ID（协议端未回 message_id）时也要落记录，
                # 否则管理员引用通知回复 /同意 时无法定位该申请
                pending[f"flag:{flag}"] = record
                logger.warning("[加群审核] 未能获取申请通知的 message_id，"
                               "待处理记录改按 flag 暂存，引用回复时按内容兜底定位")
            self._store.save_config()
            # #229：原 join_request_notify_enabled（#205）全局开关已移除，通知改为始终发送
            await self._api._notify_admins(
                f"[加群请求] {user_id} 申请加入群 {group_id}\n"
                f"已在群内发送提醒，请管理员引用回复同意/拒绝",
                group_id=group_id,
            )
        else:
            # #226：未开启群内提醒时也必须通知管理员，否则普通加群申请不会有任何通知。
            # #229：随 join_request_notify_enabled 去重移除，这里不再有全局开关判定。
            nickname = await self._api._get_user_nickname(event, user_id)
            await self._api._notify_admins(
                f"[加群请求] {nickname}（{user_id}）申请加入群 {group_id}\n"
                f"验证消息: {comment or '无'}\n"
                f"可用 /加群申请待处理 查看，或在群内开启提醒后引用回复同意/拒绝",
                group_id=group_id,
            )

    async def _handle_group_request_reply(self, event, raw: dict, group_id: str, user_id: str, reply_id):
        """加群申请引用回复处理（#57，原 on_group_message 尾部代码块）。

        为 async generator：命中并处理后 yield 结果；未命中不 yield。
        """
        has_permission = self._perms._is_authorized(raw, user_id)
        if not (reply_id and has_permission):
            return
        pending = self._store.runtime_map("pending_join_requests")
        # #258：通知消息 ID 没记进待处理表时，按被引用消息内容里的
        # 「用户qq号」兜底定位同群待处理记录
        info = pending.get(str(reply_id)) or self._api._match_pending_by_quote(
            event, group_id, str(reply_id), pending)
        if not info:
            return
        msg_text = self._mp._extract_text(raw)
        if not msg_text:
            return
        approve = "同意" in msg_text
        deny = "拒绝" in msg_text
        blacklist = "拉黑" in msg_text
        if not (approve or deny or blacklist):
            return
        # #129: 拒绝时支持自定义理由；#194: 拉黑 = 拒绝 + 加入群黑名单
        reject_reason = "管理员审核"
        if blacklist:
            reject_reason = "拉黑"
            bl_list = self._store.get_group_override_list(group_id, "blacklisted_users")
            if info["user_id"] not in [str(x) for x in bl_list]:
                bl_list.append(info["user_id"])
        elif deny:
            parts = msg_text.split("拒绝", 1)
            custom = parts[1].strip() if len(parts) > 1 else ""
            reject_reason = custom if custom else self._store.get_group_setting(
                group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"
        # #228：接口调用失败时不能回复"已同意/已拒绝"
        # #252：sub_type 用申请事件原值，提高协议端兼容性
        handled = await self._api._handle_group_request(
            event, info["flag"], approve, reject_reason,
            sub_type=str(info.get("sub_type") or "add"))
        result = "拉黑" if blacklist else ("同意" if approve else "拒绝")
        if not handled:
            yield event.plain_result(
                f"处理 {info['user_id']} 的加群申请失败（协议端拒绝或接口不可用，"
                f"详见日志），请稍后重试")
            return
        # 清理已处理的记录（#258：兜底匹配可能存在同记录多键，全部清掉）
        for key in [k for k, v in pending.items() if v is info]:
            del pending[key]
        self._store.save_config()
        yield event.plain_result(f"已{result} {info['user_id']} 的加群申请")


__all__ = ["JoinReviewService"]
