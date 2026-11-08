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

    def get_group_setting(self, group_id, key, default=None):
        return self.settings.get(key, default)

    def get_group_override_list(self, group_id, key):
        return list(self.settings.get(key, []) or [])

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
        return "msg-1"

    async def _notify_admins(self, text, group_id=None):
        self.admin_texts.append(text)

    async def _handle_group_request(self, event, flag, approve, reason, sub_type=None):
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
        await _drain(_make_service(store, api)._handle_group_join_request(None, raw))

        admin_text = api.admin_texts[0]
        self.assertIn("777 申请加入群 100", admin_text)
        self.assertNotIn("类型：邀请入群", admin_text)


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


if __name__ == "__main__":
    unittest.main()
