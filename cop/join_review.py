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


def _sanitize(text, limit: int = 40) -> str:
    """#284：日志用单行截断，验证消息不刷屏也不丢换行信息。"""
    return str(text).replace("\n", " ")[:limit]


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
        # #271：用户已实际入群，清理其可能残留的待处理申请，
        # 避免「已入群却仍显示新人申请 / 待处理列表残留」
        new_uid = str(raw.get("user_id", ""))
        if new_uid:
            pending = self._store.runtime_map("pending_join_requests")
            stale_keys = [
                k for k, v in pending.items()
                if isinstance(v, dict)
                and str(v.get("group_id")) == group_id
                and str(v.get("user_id")) == new_uid
            ]
            for k in stale_keys:
                del pending[k]
            if stale_keys:
                self._store.save_config()
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

        #270/#271：区分 sub_type=add/invite——
        - 标准 OneBot 的 invite 表示「bot 被邀请入群」，发起人 user_id=邀请人，
          维持旧流程；
        - 部分协议端扩展的「A 邀请/直接拉入 B」事件中 user_id 是邀请人 A，
          被邀请人 B 取自 target_id/invited_user_id/invitee_id 扩展字段，
          审核对象与通知一律以 B 为准，邀请人仅作展示，避免把邀请人当申请人、
          甚至误拉黑邀请人；
        - 审核前复核被邀请人/申请人是否已是群成员（管理员直接拉入的场景），
          已入群则跳过审核与通知并清理残留待处理记录。

        #241：新增「第三方邀请入群自动通过」——第三方邀请场景
        （third_party_invite，A 邀请/拉 B 入群且 B 非 bot 自身）在
        invite_auto_approve 开启时直接同意，不再走审核与群内提醒；
        黑名单成员仍被拦截，bot 自己被邀请入群（标准 invite 语义）不受影响。
        全局配置默认关，按群覆盖指令 /邀请通过 on|off。
        """
        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))
        flag = raw.get("flag", "")
        comment = raw.get("comment", "")
        # #252：sub_type 用申请事件原值（add/invite），部分实现要求与 flag 匹配
        sub_type = str(raw.get("sub_type") or "add")
        # #284：每个加群请求的处理入口落一条 INFO 日志，多群环境下哪个群
        # 「没发通知」时可以立刻看出请求是否到达、走到了哪个分支
        logger.info(
            f"[加群审核] 收到加群请求 group={group_id} sub_type={sub_type} "
            f"user_id={user_id} comment={_sanitize(comment)}")
        # #288：记录请求事件到达时间，供 /加群申请通路检查 判断通路是否正常
        try:
            self._runtime._join_request_seen[group_id] = int(time.time())
        except Exception:
            pass

        # #155：审核总开关关闭后，全部自动审核逻辑都跳过。
        # PR #277 审查反馈：早退必须先于 inviter 解析 / _get_self_id /
        # _is_group_member 等调用，关闭审核时不产生任何额外 OneBot 请求开销。
        audit_enabled = bool(self._store.get_group_setting(group_id, "join_audit_enabled", True))
        if not audit_enabled:
            return

        # #270：解析邀请场景下的真实申请人（被邀请人）
        # #283：协议端（如 NapCat）对「A 邀请 B 入群」投递的 invite 事件只带
        # user_id=邀请人 A，没有被邀请人字段。旧实现拿不到扩展字段时把 A 当成
        # 申请人展示，命中关键词时还会把 A 拉黑。改为：invite 事件中 user_id
        # 一律按邀请人处理——只有显式携带被邀请人字段且非 bot 自身时才认定
        # third_party_invite（申请人是 B）；否则申请人为空，不做任何以"申请人"
        # 为对象的自动处置（黑名单/关键词自动拒绝等），审核交由管理员手动处理。
        self_id = str(self._api._get_self_id(event, raw) or "")
        inviter_id = ""
        applicant_id = user_id
        third_party_invite = False
        if sub_type == "invite":
            invitee = str(raw.get("target_id") or raw.get("invited_user_id")
                          or raw.get("invitee_id") or "")
            inviter_id = user_id
            if invitee and invitee != self_id:
                # 协议端扩展：A 邀请/拉入 B（非 bot 自身）
                third_party_invite = True
                applicant_id = invitee
            else:
                # 被邀请人未知（标准语义下为 bot 自身被邀请）：申请人为空
                applicant_id = ""

        # #271：审核对象已是群成员（管理员直接拉入 / 申请已被处理）→
        # 不再审批、不再发「新人加群」通知，并清理残留待处理记录。
        # 标准「bot 被邀请入群」时发起人本身就是群成员，该场景跳过复核。
        skip_member_check = (sub_type == "invite" and not third_party_invite) \
            or not applicant_id
        if applicant_id and not skip_member_check and await self._api._is_group_member(event, group_id, applicant_id):
            logger.info(f"[加群审核] 用户 {applicant_id} 已是群 {group_id} 成员"
                        f"（疑似管理员直接拉入），跳过审核与群内通知")
            pending = self._store.runtime_map("pending_join_requests")
            stale_keys = [k for k, v in pending.items()
                          if isinstance(v, dict)
                          and str(v.get("group_id")) == group_id
                          and str(v.get("user_id")) == applicant_id]
            for k in stale_keys:
                del pending[k]
            if stale_keys:
                self._store.save_config()
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
        # #270：invite 场景黑名单针对被邀请人 B，不针对邀请人 A
        bl_list = self._store.get_group_setting(group_id, "blacklisted_users", [])
        if applicant_id and bl_list and str(applicant_id) in [str(x) for x in bl_list]:
            handled = await self._api._handle_group_request(
                event, flag, False, "黑名单用户", sub_type=sub_type)
            yield event.plain_result(
                f"已拒绝 {applicant_id} 的加群申请（黑名单用户）" if handled else
                f"拒绝 {applicant_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {applicant_id}（群 {group_id}）\n"
                f"{invite_note}"
                f"验证消息: {comment}\n"
                f"原因: 黑名单用户",
                group_id=group_id,
            )
            return

        # #241：第三方邀请自动通过（全局配置 invite_auto_approve 默认关，
        # 按群覆盖指令 /邀请通过 on|off）。开启后此类申请不再走审核与群内提醒，
        # 直接同意并通知管理员；黑名单成员已在上方被拦截。
        # 闸门：仅在本群加群审核启用范围内（enabled）才放行——本群未启用
        # 违规检测/审核范围（enabled=False）时不自动通过，与 #192 语义一致。
        if third_party_invite and enabled \
                and self._store.get_group_setting(group_id, "invite_auto_approve", False):
            handled = await self._api._handle_group_request(
                event, flag, True, "第三方邀请自动通过", sub_type=sub_type)
            yield event.plain_result(
                f"已自动同意第三方邀请：{inviter_id} 邀请 {applicant_id} 加入本群" if handled else
                f"自动同意第三方邀请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            await self._api._notify_admins(
                f"[加群请求] 已自动同意第三方邀请（群 {group_id}）\n"
                f"邀请人QQ: {inviter_id}\n"
                f"被邀请人QQ: {applicant_id}",
                group_id=group_id,
            )
            return

        # 拒绝理由（#129 使用自定义拒绝理由；#159 优化提示）；#229 关键词自动拒绝复用同一理由
        reject_reason = self._store.get_group_setting(group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"

        # #229：命中「加群自动拒绝关键词」→ 自动拒绝 + 加入本群黑名单 + 通知管理员
        # （列表来源：本群覆盖非空优先，否则全局 join_reject_keywords；与违禁词/自动
        #   同意关键词一致，受加群审核总开关与启用群范围 enabled 控制）
        # #283：申请人为空（邀请入群且协议端未提供被邀请人）时不做关键词自动处置，
        # 避免把邀请人误当申请人拉黑，审核交由管理员手动处理
        reject_keywords = self._store.get_group_setting(group_id, "join_reject_keywords", [])
        hit_keyword = None
        if applicant_id and enabled and reject_keywords:
            for kw in reject_keywords:
                if str(kw) and str(kw) in comment:
                    hit_keyword = str(kw)
                    break
        if hit_keyword is not None:
            # 先落黑名单再拒绝：即便协议端拒绝失败（如 flag 过期），后续申请也会被黑名单拦住
            # #270：拉黑对象是申请人/被邀请人 B，不能误写邀请人 A
            # PR#289 审查（blacklist-update-not-persisted）：get_group_override_list
            # 返回的是拷贝，直接 append 不会落盘；统一走 mutate_group_list 原子写入
            bl_saved = True
            try:
                self._store.mutate_group_list(
                    group_id, "blacklisted_users", "add", str(applicant_id))
            except Exception as e:
                # 存储异常不能中断整条申请的处理：先完成拒绝（安全处置优先），
                # 再把"黑名单没保存"如实告知，避免管理员以为已经拉黑
                # （PR #289 审查：missing-error-handling）。
                bl_saved = False
                logger.error(f"[加群审核] 写黑名单失败（群 {group_id} 用户 {applicant_id}）: {e}")
            detail_reason = f"您的加群申请含有本群禁止的词语，自动拒绝（{reject_reason}）"
            handled = await self._api._handle_group_request(
                event, flag, False, detail_reason, sub_type=sub_type)
            if handled and bl_saved:
                yield event.plain_result(
                    f"已拒绝 {applicant_id} 的加群申请（命中自动拒绝关键词「{hit_keyword}」，已加入本群黑名单）")
            elif handled:
                yield event.plain_result(
                    f"已拒绝 {applicant_id} 的加群申请（命中自动拒绝关键词「{hit_keyword}」），"
                    "但黑名单保存失败，请稍后手动拉黑")
            elif bl_saved:
                yield event.plain_result(
                    f"拒绝 {applicant_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志；该用户已加入本群黑名单）")
            else:
                yield event.plain_result(
                    f"拒绝 {applicant_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志；黑名单也未保存成功）")
            if not handled:
                return
            invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
            bl_note = "已加入本群黑名单" if bl_saved else "【黑名单写入失败，请手动拉黑】"
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {applicant_id}（群 {group_id}）\n"
                f"{invite_note}"
                f"验证消息: {comment}\n"
                f"原因: 命中加群自动拒绝关键词「{hit_keyword}」，{bl_note}",
                group_id=group_id,
            )
            return

        # 命中违禁词：拒绝 + 通知管理员（#129 使用自定义拒绝理由；#159 优化提示）
        # #283：申请人为空时不自动拒绝（同上，避免误处置邀请人）
        if applicant_id and enabled and violation_keywords and any(kw in comment for kw in violation_keywords):
            detail_reason = f"您的加群申请有词触碰到本群违禁词，自动拒绝（{reject_reason}）"
            handled = await self._api._handle_group_request(
                event, flag, False, detail_reason, sub_type=sub_type)
            yield event.plain_result(
                f"已拒绝 {applicant_id} 的加群申请（含违禁词）" if handled else
                f"拒绝 {applicant_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
            await self._api._notify_admins(
                f"[加群请求] 已拒绝 {applicant_id}（群 {group_id}）\n"
                f"{invite_note}"
                f"验证消息: {comment}\n"
                f"原因: 命中违禁词",
                group_id=group_id,
            )
            return

        # 命中关键词：同意 + 通知管理员 + 群内通知（#186）
        # #283：申请人为空时不自动同意（被邀请人未知，无法核对黑名单）
        if applicant_id and enabled and join_approve_keywords and any(kw in comment for kw in join_approve_keywords):
            handled = await self._api._handle_group_request(
                event, flag, True, "命中关键词自动同意", sub_type=sub_type)
            yield event.plain_result(
                f"已同意 {applicant_id} 的加群申请（命中关键词）" if handled else
                f"同意 {applicant_id} 的加群申请失败（协议端拒绝或接口不可用，详见日志）")
            if not handled:
                return
            # #229：原 join_request_notify_enabled（#205）全局开关已随配置去重移除，
            # 通知改为始终发送；需要彻底静音时把 join_notify_admins 留空即可
            invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
            await self._api._notify_admins(
                f"[加群请求] 已同意 {applicant_id}（群 {group_id}）\n"
                f"{invite_note}"
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
            # #284：申请人的昵称/等级查询失败只影响展示，不能让整个通知流程挂掉——
            # 旧实现若此处抛出未捕获异常，该群的「新人加群」通知就静默消失。
            # #283：被邀请人未知时没有申请人可查，不能拿邀请人 A 的身份充数
            # （否则等级/昵称都是邀请人的，展示会误导管理员）。
            if applicant_id:
                nickname, level = await self._safe_applicant_profile(
                    event, applicant_id, group_id)
                # #261：昵称获取失败时明确标注，避免管理员误以为昵称就是 QQ 号
                nickname_display = nickname or f"获取失败({applicant_id})"
            else:
                nickname, level, nickname_display = "", "未知", ""
            # #270/#283：邀请入群时分行展示邀请人与被邀请人，
            # 不再把邀请人 A 的 QQ 标成「申请人」；协议端未提供被邀请人时明确标注未知
            invite_lines = ""
            if third_party_invite:
                invite_lines = (
                    f"类型：邀请入群\n"
                    f"邀请人QQ：{inviter_id}\n"
                    f"被邀请人QQ：{applicant_id}\n"
                )
            elif sub_type == "invite":
                invite_lines = (
                    f"类型：邀请入群\n"
                    f"邀请人QQ：{inviter_id}\n"
                    f"被邀请人：未知（协议端未提供，标准语义下为本 bot）\n"
                )
            if applicant_id:
                applicant_lines = (
                    f"用户qq昵称：{nickname_display}\n"
                    f"用户qq号：{applicant_id}\n"
                )
                identifier_lines = ""
            else:
                applicant_lines = ""
                # PR #289 审查：被邀请人未知时没有「用户qq号」行可供兜底匹配，
                # 补一行稳定唯一的申请标识（即请求 flag），管理员引用回复时
                # 即便通知 message_id 缺失也能按标识定位待处理记录
                identifier_lines = f"申请标识：{flag}\n"
            notify_text = (
                f"【新人加群】通知\n"
                f"{invite_lines}"
                f"{applicant_lines}"
                f"{identifier_lines}"
                f"qq等级：{level}\n"
                f"加群验证消息：{comment or '无'}\n"
                f"回复 /同意 或 /拒绝 或 /拉黑（引用本消息）"
            )
            # 暂存 flag 等待引用回复
            sent, sent_id = await self._api._send_group_text(event, group_id, notify_text)
            # #284：群内通知发送结果落日志，多群排查「b 群没发通知」的关键线索
            if sent_id:
                logger.info(f"[加群审核] 群 {group_id} 群内提醒已发送 message_id={sent_id}")
            elif sent:
                logger.warning(
                    f"[加群审核] 群 {group_id} 群内提醒发送后未拿到 message_id "
                    f"（协议端未返回 ID，详见上方 send_group_msg 日志）")
            else:
                logger.error(
                    f"[加群审核] 群 {group_id} 群内提醒所有通路均发送失败，"
                    f"本次不登记待处理记录")
            pending = self._store.runtime_map("pending_join_requests")
            # #270：user_id 一律存申请人/被邀请人；inviter_user_id 仅作记录
            record = {"flag": flag, "group_id": group_id, "user_id": applicant_id,
                      "inviter_user_id": inviter_id,
                      "sub_type": sub_type, "ts": int(time.time())}
            # #258：顺带清理超过 7 天的陈旧待处理记录，防止 runtime.json 无限膨胀
            stale = [k for k, v in pending.items()
                     if isinstance(v, dict) and int(v.get("ts", 0) or 0) < int(time.time()) - 7 * 86400]
            for k in stale:
                del pending[k]
            changed = bool(stale)
            if not sent:
                # 通知根本没发出去：不能登记待处理记录，否则管理员在群里看不到
                # 任何消息、记录却一直挂着，之后引用一条不存在的通知处理的是
                # 早已失效的申请（PR #289 审查：stale-pending-request）。
                # 只走下面的私聊通知，让管理员直接用 /加群申请待处理 处理。
                pass
            elif sent_id:
                pending[str(sent_id)] = record
                changed = True
            else:
                # #258：发送成功但协议端未回 message_id 时也要落记录，否则管理员
                # 引用通知回复 /同意 时无法定位该申请
                pending[f"flag:{flag}"] = record
                changed = True
                logger.warning("[加群审核] 未能获取申请通知的 message_id，"
                               "待处理记录改按 flag 暂存，引用回复时按内容兜底定位")
            if changed:
                self._store.save_config()
            # #229：原 join_request_notify_enabled（#205）全局开关已移除，通知改为始终发送
            # #283/PR#289：被邀请人未知的邀请入群请求，管理员通知沿用 invite 专用文案，
            # 不能拼成「[加群请求]  申请加入群 X」这种申请人为空的普通申请通知
            if not sent:
                # 群内提醒没发出去：私聊文案不能说"已在群内发送提醒"
                tail = "群内提醒发送失败，请直接用 /加群申请待处理 处理"
            else:
                tail = "已在群内发送提醒，请管理员引用回复同意/拒绝"
            if sub_type == "invite" and not applicant_id:
                admin_text = (
                    f"[加群请求] 收到群 {group_id} 的邀请入群请求\n"
                    f"类型：邀请入群\n"
                    f"邀请人QQ: {inviter_id}\n"
                    f"被邀请人：未知（协议端未提供，标准语义下为本 bot）\n"
                    f"申请标识：{flag}\n"
                    f"{tail}"
                )
            else:
                invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
                admin_text = (
                    f"[加群请求] {applicant_id} 申请加入群 {group_id}\n"
                    f"{invite_note}"
                    f"{tail}"
                )
            await self._api._notify_admins(admin_text, group_id=group_id)
        else:
            # #226：未开启群内提醒时也必须通知管理员，否则普通加群申请不会有任何通知。
            # #229：随 join_request_notify_enabled 去重移除，这里不再有全局开关判定。
            # #283：邀请入群且被邀请人未知时，明确展示「邀请入群」语义而非伪装成普通申请
            if sub_type == "invite" and not applicant_id:
                await self._api._notify_admins(
                    f"[加群请求] 收到群 {group_id} 的邀请入群请求\n"
                    f"类型：邀请入群\n"
                    f"邀请人QQ: {inviter_id}\n"
                    f"被邀请人：未知（协议端未提供，标准语义下为本 bot）\n"
                    f"申请标识：{flag}\n"
                    f"验证消息: {comment or '无'}\n"
                    f"可用 /加群申请待处理 查看，或在群内开启提醒后引用回复同意/拒绝",
                    group_id=group_id,
                )
            else:
                nickname, _level = await self._safe_applicant_profile(
                    event, applicant_id, group_id)
                # 昵称拿不到时只显示 QQ 号，不要拼成「获取失败(777)（777）」
                applicant_display = (
                    f"{nickname}（{applicant_id}）" if nickname else str(applicant_id)
                )
                invite_note = f"邀请人QQ: {inviter_id}\n" if third_party_invite else ""
                await self._api._notify_admins(
                    f"[加群请求] {applicant_display} 申请加入群 {group_id}\n"
                    f"{invite_note}"
                    f"验证消息: {comment or '无'}\n"
                    f"可用 /加群申请待处理 查看，或在群内开启提醒后引用回复同意/拒绝",
                    group_id=group_id,
                )

    async def _safe_applicant_profile(self, event, applicant_id: str, group_id: str):
        """查询申请人昵称与 QQ 等级；任何失败只降级展示，不中断通知流程。

        群内提醒与「未开启群内提醒时的私聊通知」两条分支共用这一个入口：
        以前私聊分支单独调接口、没有异常隔离，协议端一抛异常整条管理员通知
        就静默消失（PR #289 审查：missing-error-handling）。#189：等级取不到
        显示「未知」；#261：昵称取不到由调用方标注「获取失败(QQ)」。
        """
        nickname, level = "", "未知"
        if not applicant_id:
            return nickname, level
        try:
            nickname = await self._api._get_user_nickname(event, applicant_id)
        except Exception as e:
            logger.warning(f"[加群审核] 群 {group_id} 获取申请人昵称失败: {e}")
        try:
            level = await self._api._get_stranger_level(event, applicant_id) or "未知"
        except Exception as e:
            logger.warning(f"[加群审核] 群 {group_id} 获取申请人等级失败: {e}")
        return nickname, level

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
        if not info.get("user_id"):
            # #283/PR#289：被邀请人未知的邀请入群请求没有明确处理对象，
            # 同意/拒绝都可能作用到邀请人身上，禁止自动审批，交管理员人工处理
            # （unsafe-invite-approval）。
            yield event.plain_result(
                "这条邀请入群请求未提供被邀请人信息，无法自动同意/拒绝；"
                "请在 QQ 客户端人工处理（邀请人：" + str(info.get("inviter_user_id") or "未知") + "）")
            return
        # #129: 拒绝时支持自定义理由；#194: 拉黑 = 拒绝 + 加入群黑名单
        reject_reason = "管理员审核"
        if blacklist:
            reject_reason = "拉黑"
            # #283：被邀请人未知时没有可拉黑对象，只拒绝不写黑名单
            if info.get("user_id"):
                # PR#289 审查（blacklist-update-not-persisted）：返回的是拷贝，
                # 必须走 mutate_group_list 才会真正落盘
                try:
                    self._store.mutate_group_list(
                        group_id, "blacklisted_users", "add", str(info["user_id"]))
                except Exception as e:
                    # 写入失败就不能继续按"拉黑"处理，否则会回执"已拉黑"但实际没落盘
                    # （PR #289 审查：missing-error-handling）。
                    logger.error(
                        f"[加群审核] 写黑名单失败（群 {group_id} 用户 {info['user_id']}）: {e}")
                    yield event.plain_result(
                        f"拉黑 {info['user_id']} 失败（配置保存异常，详见日志），"
                        "该申请未处理，请稍后重试")
                    return
        elif deny:
            parts = msg_text.split("拒绝", 1)
            custom = parts[1].strip() if len(parts) > 1 else ""
            reject_reason = custom if custom else self._store.get_group_setting(
                group_id, "join_reject_reason", "不满足加群条件") or "不满足加群条件"
        # #228：接口调用失败时不能回复"已同意/已拒绝"
        # #252：sub_type 用申请事件原值，提高协议端兼容性
        # #283：邀请入群且被邀请人未知时，user_id 为空串，展示文案做兜底
        handled = await self._api._handle_group_request(
            event, info["flag"], approve, reject_reason,
            sub_type=str(info.get("sub_type") or "add"))
        result = "拉黑" if blacklist else ("同意" if approve else "拒绝")
        target_display = str(info.get("user_id") or "") or "该邀请入群请求"
        if not handled:
            yield event.plain_result(
                f"处理 {target_display} 的加群申请失败（协议端拒绝或接口不可用，"
                f"详见日志），请稍后重试")
            return
        # 清理已处理的记录（#258：兜底匹配可能存在同记录多键，全部清掉）
        for key in [k for k, v in pending.items() if v is info]:
            del pending[key]
        self._store.save_config()
        yield event.plain_result(f"已{result} {target_display} 的加群申请")


__all__ = ["JoinReviewService"]
