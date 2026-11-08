"""JoinReviewService 通知文案回归测试（PR #289 审查：inconsistent-invite-notification）。

只验证「被邀请人未知的邀请入群请求」在两条通知链路（群内提醒 / 仅私聊管理员）
上的文案一致性，不依赖 astrbot 运行时，用最小桩件替换 L1/L2 依赖。
"""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def warning(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from cop.join_review import JoinReviewService  # noqa: E402


class _Store:
    """最小 ConfigStore 桩：按 key 返回预置的按群配置。"""

    def __init__(self, settings=None):
        self.settings = settings or {}
        self.config = {}
        self._maps = {}
        self.mutations = []
        self.fail_mutation = False

    def get_group_setting(self, group_id, key, default=None):
        return self.settings.get(key, default)

    def get_group_override_list(self, group_id, key):
        return list(self.settings.get(key, []) or [])

    def mutate_group_list(self, group_id, key, op, value):
        """桩：模拟原子读写，记录调用并按 add/remove 真正改动 settings。"""
        self.mutations.append((group_id, key, op, value))
        if self.fail_mutation:
            raise OSError("config.json 写入失败（模拟）")
        current = [str(x) for x in (self.settings.get(key) or [])]
        value = str(value)
        if op == "add":
            if value in current:
                return False, current
            current.append(value)
        else:
            if value not in current:
                return False, current
            current.remove(value)
        self.settings[key] = current
        return True, list(current)

    def runtime_map(self, name):
        return self._maps.setdefault(name, {})

    def save_config(self):
        pass


class _Api:
    """最小 OneBotApi 桩：记录发出的通知文本。"""

    def __init__(self):
        self.admin_texts = []
        self.group_texts = []
        self.nickname_queries = []
        self.handled = []
        # 群内提醒发送结果：默认"成功且拿到 message_id"
        self.group_send_ok = True
        self.group_send_mid = "msg-1"

    def _get_self_id(self, event, raw=None):
        return "999000111"

    async def _is_group_member(self, event, group_id, user_id):
        return False

    async def _get_user_nickname(self, event, user_id):
        self.nickname_queries.append(user_id)
        return f"昵称{user_id}"

    async def _get_stranger_level(self, event, user_id):
        return "12"

    async def _send_group_text(self, event, group_id, text):
        self.group_texts.append(text)
        if not self.group_send_ok:
            return False, ""
        return True, self.group_send_mid

    async def _notify_admins(self, text, group_id=None):
        self.admin_texts.append(text)

    async def _handle_group_request(self, event, flag, approve, reason, sub_type=None):
        self.handled.append((flag, approve, reason))
        return True


def _make_service(store, api):
    return JoinReviewService(
        config_store=store,
        onebot_api=api,
        runtime=SimpleNamespace(_join_request_seen={}),
        permissions=object(),
        message_parse=object(),
    )


async def _drain(generator):
    return [item async for item in generator]


_UNKNOWN_INVITE_RAW = {
    "group_id": 100,
    "user_id": "555",  # invite 事件里 user_id 是邀请人
    "sub_type": "invite",
    "flag": "flag-abc",
    "comment": "",
}


class UnknownInviteeNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_notify_admin_message_uses_invite_wording(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            None, dict(_UNKNOWN_INVITE_RAW)))

        self.assertEqual(len(api.group_texts), 1)
        self.assertEqual(len(api.admin_texts), 1)
        admin_text = api.admin_texts[0]
        for expected in ("类型：邀请入群", "邀请人QQ: 555", "被邀请人：未知", "申请标识：flag-abc"):
            self.assertIn(expected, admin_text)
        # 不能再拼成「[加群请求]  申请加入群 X」这种申请人为空的普通申请通知
        self.assertNotIn("申请加入群", admin_text)
        self.assertNotIn("  ", admin_text.replace("\n", ""))

    async def test_group_notify_text_has_no_empty_applicant_line(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            None, dict(_UNKNOWN_INVITE_RAW)))

        group_text = api.group_texts[0]
        self.assertIn("类型：邀请入群", group_text)
        self.assertIn("被邀请人：未知", group_text)
        self.assertIn("申请标识：flag-abc", group_text)
        self.assertNotIn("用户qq号：", group_text)
        # 被邀请人未知时不能拿邀请人身份充数（昵称/等级都属于邀请人）
        self.assertEqual(api.nickname_queries, [])

    async def test_private_admin_notify_uses_same_invite_wording(self):
        store = _Store({"join_request_notify_in_group": False})
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            None, dict(_UNKNOWN_INVITE_RAW)))

        self.assertEqual(api.group_texts, [])
        admin_text = api.admin_texts[0]
        for expected in ("类型：邀请入群", "邀请人QQ: 555", "被邀请人：未知", "申请标识：flag-abc"):
            self.assertIn(expected, admin_text)

    async def test_known_applicant_keeps_plain_wording(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        raw = dict(_UNKNOWN_INVITE_RAW, user_id="777", sub_type="add")
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        admin_text = api.admin_texts[0]
        self.assertIn("777 申请加入群 100", admin_text)
        self.assertNotIn("类型：邀请入群", admin_text)

    async def test_private_notify_survives_profile_query_failure(self):
        # 未开启群内提醒时的私聊通知也要做异常隔离：协议端查询昵称/等级抛异常
        # 时，通知不能整条消失（PR #289 审查：missing-error-handling）。
        store = _Store({"join_request_notify_in_group": False})
        api = _Api()

        async def _boom(event, user_id):
            raise RuntimeError("协议端不支持 get_stranger_info")

        api._get_user_nickname = _boom
        api._get_stranger_level = _boom
        raw = dict(_UNKNOWN_INVITE_RAW, user_id="777", sub_type="add")
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        self.assertEqual(len(api.admin_texts), 1)
        # 昵称拿不到时只显示 QQ 号，通知照常发出
        self.assertIn("777 申请加入群 100", api.admin_texts[0])
        self.assertNotIn("获取失败(777)（777）", api.admin_texts[0])


class PendingRecordLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """待处理记录只在通知真的发出去后登记（PR #289 审查：stale-pending-request）。

    旧实现无论发送成功与否都写 pending_join_requests：通知全部通路失败时群内
    看不到任何消息，记录却一直挂着，管理员之后引用一条从未发出的通知，处理的是
    早已失效的申请。
    """

    async def test_no_pending_record_when_group_notify_fails(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        api.group_send_ok = False
        raw = dict(_UNKNOWN_INVITE_RAW, user_id="777", sub_type="add")
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        self.assertEqual(store.runtime_map("pending_join_requests"), {})
        # 私聊通知照发，但不能谎称"已在群内发送提醒"
        self.assertEqual(len(api.admin_texts), 1)
        self.assertNotIn("已在群内发送提醒", api.admin_texts[0])
        self.assertIn("发送失败", api.admin_texts[0])

    async def test_pending_record_uses_flag_when_message_id_missing(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        api.group_send_mid = ""
        raw = dict(_UNKNOWN_INVITE_RAW, user_id="777", sub_type="add")
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        pending = store.runtime_map("pending_join_requests")
        self.assertEqual(list(pending), ["flag:flag-abc"])
        self.assertIn("已在群内发送提醒", api.admin_texts[0])

    async def test_pending_record_keyed_by_message_id_on_success(self):
        store = _Store({"join_request_notify_in_group": True})
        api = _Api()
        raw = dict(_UNKNOWN_INVITE_RAW, user_id="777", sub_type="add")
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        self.assertEqual(list(store.runtime_map("pending_join_requests")), ["msg-1"])


class _Event:
    """最小事件桩：只提供 plain_result。"""

    def plain_result(self, text):
        return ("text", text)


class _Perms:
    def _is_authorized(self, raw, user_id):
        return True


class _Parser:
    def __init__(self, text="同意"):
        self.text = text

    def _extract_text(self, raw):
        return self.text


class UnknownInviteeReplyTests(unittest.IsolatedAsyncioTestCase):
    """被邀请人未知的邀请请求不能被引用回复直接同意/拒绝（unsafe-invite-approval）。"""

    def _service(self, record, text="同意"):
        store = _Store()
        store.runtime_map("pending_join_requests")["mid-1"] = record
        api = _Api()
        api.handled = []

        async def _handle(event, flag, approve, reason, sub_type=None):
            api.handled.append((flag, approve, reason))
            return True

        api._handle_group_request = _handle
        svc = JoinReviewService(
            config_store=store,
            onebot_api=api,
            runtime=SimpleNamespace(_join_request_seen={}),
            permissions=_Perms(),
            message_parse=_Parser(text),
        )
        return svc, api

    async def test_unknown_applicant_is_not_approved(self):
        record = {
            "flag": "f-unknown",
            "group_id": "100",
            "user_id": "",
            "inviter_user_id": "555",
            "sub_type": "invite",
            "ts": 1,
        }
        svc, api = self._service(record, "同意")
        out = await _drain(svc._handle_group_request_reply(_Event(), {}, "100", "1", "mid-1"))
        self.assertEqual(api.handled, [])
        self.assertEqual(len(out), 1)
        self.assertIn("人工处理", out[0][1] if isinstance(out[0], tuple) else str(out[0]))
        self.assertIn("555", str(out[0]))

    async def test_unknown_applicant_blacklist_also_blocked(self):
        record = {
            "flag": "f-unknown",
            "group_id": "100",
            "user_id": "",
            "inviter_user_id": "555",
            "sub_type": "invite",
            "ts": 1,
        }
        svc, api = self._service(record, "拉黑")
        await _drain(svc._handle_group_request_reply(_Event(), {}, "100", "1", "mid-1"))
        self.assertEqual(api.handled, [])

    async def test_known_applicant_still_handled(self):
        record = {
            "flag": "f-1",
            "group_id": "100",
            "user_id": "777",
            "inviter_user_id": "",
            "sub_type": "add",
            "ts": 1,
        }
        svc, api = self._service(record, "同意")
        out = await _drain(svc._handle_group_request_reply(_Event(), {}, "100", "1", "mid-1"))
        self.assertEqual(len(api.handled), 1)
        self.assertIn("已同意", str(out[0]))


class BlacklistPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """黑名单必须真正落盘（PR #289 审查：blacklist-update-not-persisted）。

    get_group_override_list 返回的是拷贝，直接 append 改不到运行时覆盖，
    所以代码必须走 mutate_group_list。
    """

    async def test_keyword_auto_reject_persists_blacklist(self):
        store = _Store({"join_reject_keywords": ["广告"], "blacklisted_users": []})
        api = _Api()
        raw = {
            "group_id": 100,
            "user_id": "777",
            "sub_type": "add",
            "flag": "f-kw",
            "comment": "低价广告推广",
        }
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), raw))

        self.assertIn(("100", "blacklisted_users", "add", "777"), store.mutations)
        self.assertEqual(store.settings["blacklisted_users"], ["777"])

    async def test_quote_reply_blacklist_persists(self):
        store = _Store()
        record = {
            "flag": "f-1",
            "group_id": "100",
            "user_id": "777",
            "inviter_user_id": "",
            "sub_type": "add",
            "ts": 1,
        }
        store.runtime_map("pending_join_requests")["mid-1"] = record
        api = _Api()
        svc = JoinReviewService(
            config_store=store,
            onebot_api=api,
            runtime=SimpleNamespace(_join_request_seen={}),
            permissions=_Perms(),
            message_parse=_Parser("拉黑"),
        )
        await _drain(svc._handle_group_request_reply(_Event(), {}, "100", "1", "mid-1"))

        self.assertIn(("100", "blacklisted_users", "add", "777"), store.mutations)
        self.assertEqual(store.settings["blacklisted_users"], ["777"])


class BlacklistErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    """黑名单写入失败必须被处理（PR #289 审查：missing-error-handling）。"""

    async def test_auto_reject_still_rejects_when_blacklist_write_fails(self):
        store = _Store({"join_reject_keywords": ["广告"], "blacklisted_users": []})
        store.fail_mutation = True
        api = _Api()
        raw = {
            "group_id": 100,
            "user_id": "777",
            "sub_type": "add",
            "flag": "f-kw",
            "comment": "低价广告推广",
        }
        out = await _drain(
            _make_service(store, api)._handle_group_join_request(_Event(), raw)
        )

        # 安全处置优先：拒绝照常执行
        self.assertEqual(len(api.handled), 1)
        self.assertFalse(api.handled[0][1])
        text = str(out[-1])
        self.assertIn("已拒绝", text)
        self.assertIn("黑名单保存失败", text)
        # 管理员通知里也要标明没写进去
        self.assertIn("黑名单写入失败", api.admin_texts[-1])

    async def test_quote_reply_blacklist_failure_is_not_reported_as_success(self):
        store = _Store()
        store.fail_mutation = True
        record = {
            "flag": "f-1",
            "group_id": "100",
            "user_id": "777",
            "inviter_user_id": "",
            "sub_type": "add",
            "ts": 1,
        }
        store.runtime_map("pending_join_requests")["mid-1"] = record
        api = _Api()
        svc = JoinReviewService(
            config_store=store,
            onebot_api=api,
            runtime=SimpleNamespace(_join_request_seen={}),
            permissions=_Perms(),
            message_parse=_Parser("拉黑"),
        )
        out = await _drain(svc._handle_group_request_reply(_Event(), {}, "100", "1", "mid-1"))

        # 没写进黑名单就不处理申请，也不能回执"已拉黑"
        self.assertEqual(api.handled, [])
        self.assertNotIn("已拉黑", str(out))
        self.assertIn("失败", str(out))


if __name__ == "__main__":
    unittest.main()
