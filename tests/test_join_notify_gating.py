"""加群申请通知的「按群生效条件」回归测试（#155 开关语义 + 静默丢弃告警）。

报告场景：多群里只有一个群收不到加群申请通知，其他群都正常，bot 在也都是管理员。

代码侧查到两处会让某个群「静默无任何通知」的问题：

1. ``join_audit_enabled``（自动审核总开关）关闭时直接 ``return``，把群内提醒、
   管理员私聊、待处理记录、黑名单自动拒绝一起跳过——与 README / 配置提示的
   「关闭后仅保留管理员手动审核」「黑名单用户始终直接拒绝」矛盾，且没有日志。
2. ``join_notify_admins`` 为空时 ``_notify_admins`` 循环零次静默返回，多群环境
   下表现为「这个群没有任何通知」却找不到线索。

这里分别钉住：关掉自动审核后通知与人工审核照常、自动处置不再执行、黑名单仍
然直接拒绝，以及无收件人时必须落告警。
"""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_astrbot_stubs():
    """补齐 join_review / onebot_api 需要的 astrbot 子模块（已存在则只补缺）。"""
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
    else:
        api = sys.modules["astrbot.api"]

    if "astrbot.api.event" not in sys.modules:
        event_mod = types.ModuleType("astrbot.api.event")
        event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
        sys.modules["astrbot.api.event"] = event_mod
        api.event = event_mod

    if "astrbot.api.message_components" not in sys.modules:
        comp_mod = types.ModuleType("astrbot.api.message_components")
        comp_mod.Plain = type("Plain", (), {})
        sys.modules["astrbot.api.message_components"] = comp_mod


_install_astrbot_stubs()

import cop.join_review as join_review_mod  # noqa: E402
import cop.onebot_api as onebot_api_mod  # noqa: E402
from cop.join_review import JoinReviewService  # noqa: E402
from cop.onebot_api import OneBotApi  # noqa: E402


class _Store:
    """最小 ConfigStore 桩：按 key 返回预置的按群配置。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.config = {}
        self._maps = {}

    def get_group_setting(self, group_id, key, default=None):
        return self.settings.get(key, default)

    def runtime_map(self, name):
        return self._maps.setdefault(name, {})

    def save_config(self):
        pass

    def mutate_group_list(self, group_id, key, op, value):
        cur = [str(x) for x in (self.settings.get(key) or [])]
        value = str(value)
        if op == "add" and value not in cur:
            cur.append(value)
        elif op == "remove" and value in cur:
            cur.remove(value)
        else:
            return False, cur
        self.settings[key] = cur
        return True, list(cur)


class _Api:
    """最小 OneBotApi 桩：记录群内提醒 / 私聊通知 / 审批动作。"""

    def __init__(self):
        self.admin_texts = []
        self.group_texts = []
        self.handled = []
        self.group_send_ok = True
        self.group_send_mid = "msg-1"

    def _get_self_id(self, event, raw=None):
        return "999000111"

    async def _is_group_member(self, event, group_id, user_id):
        return False

    async def _get_user_nickname(self, event, user_id):
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


class _Event:
    def plain_result(self, text):
        return ("text", text)


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


def _add_raw(**overrides):
    raw = {
        "group_id": 100,
        "user_id": "777",
        "sub_type": "add",
        "flag": "f-1",
        "comment": "你好",
    }
    raw.update(overrides)
    return raw


class AuditSwitchOnlyGatesAutoActionsTests(unittest.IsolatedAsyncioTestCase):
    """关掉自动审核后，申请通知与人工审核链路必须照常。"""

    async def test_audit_off_still_sends_group_reminder(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))

        self.assertEqual(len(api.group_texts), 1)
        self.assertIn("【新人加群】通知", api.group_texts[0])
        self.assertIn("用户qq号：777", api.group_texts[0])
        # 通知真的发出去了才登记待处理记录，管理员才能引用回复
        self.assertEqual(list(store.runtime_map("pending_join_requests")), ["msg-1"])
        self.assertEqual(len(api.admin_texts), 1)

    async def test_audit_off_still_private_notifies_admins(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": False,
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))

        self.assertEqual(api.group_texts, [])
        self.assertEqual(len(api.admin_texts), 1)
        self.assertIn("申请加入群 100", api.admin_texts[0])

    async def test_audit_off_skips_keyword_auto_approve(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
            "join_approve_keywords": ["你好"],
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))

        self.assertEqual(api.handled, [])
        self.assertEqual(len(api.group_texts), 1)
        self.assertIn("【新人加群】通知", api.group_texts[0])
        for text in api.group_texts:
            self.assertNotIn("加群审核通过词语", text)

    async def test_audit_off_skips_keyword_auto_reject(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
            "join_reject_keywords": ["广告"],
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            _Event(), _add_raw(comment="低价广告推广")))

        self.assertEqual(api.handled, [])
        self.assertEqual(store.settings.get("blacklisted_users"), None)
        self.assertIn("【新人加群】通知", api.group_texts[0])

    async def test_audit_off_still_rejects_blacklisted_user(self):
        # README：黑名单用户始终直接拒绝，不受自动审核总开关影响
        store = _Store({
            "join_audit_enabled": False,
            "blacklisted_users": ["777"],
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))

        self.assertEqual(api.handled, [("f-1", False, "黑名单用户")])
        self.assertEqual(len(api.admin_texts), 1)
        self.assertIn("黑名单用户", api.admin_texts[0])

    async def test_audit_on_keyword_auto_approve_still_works(self):
        # 回归护栏：开着自动审核时关键词自动同意照旧
        store = _Store({
            "join_approve_keywords": ["你好"],
            "join_request_notify_in_group": True,
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))

        self.assertEqual(api.handled, [("f-1", True, "命中关键词自动同意")])
        self.assertIn("加群审核通过词语", api.group_texts[-1])


class _RecordingLogger:
    def __init__(self):
        self.records = []

    def _add(self, level, message):
        self.records.append((level, str(message)))

    def warning(self, message, *args, **kwargs):
        self._add("warning", message)

    def info(self, message, *args, **kwargs):
        self._add("info", message)

    def error(self, message, *args, **kwargs):
        self._add("error", message)

    def debug(self, message, *args, **kwargs):
        self._add("debug", message)


class RequestLogConfigTests(unittest.IsolatedAsyncioTestCase):
    """每次收到加群请求都要把本群生效通知配置打进日志（不用跑检查命令）。"""

    async def test_request_log_reports_effective_config(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
            "join_notify_admins": ["111", "222"],
        })
        recorder = _RecordingLogger()
        with mock.patch.object(join_review_mod, "logger", recorder):
            await _drain(_make_service(store, _Api())._handle_group_join_request(
                _Event(), _add_raw()))

        lines = [msg for level, msg in recorder.records if "通知配置" in msg]
        self.assertEqual(len(lines), 1)
        self.assertIn("自动审核=关", lines[0])
        self.assertIn("群内提醒=开", lines[0])
        self.assertIn("通知QQ=2人", lines[0])
        self.assertNotIn("两个通知渠道都关着", lines[0])

    async def test_request_log_flags_both_channels_off(self):
        store = _Store({
            "join_request_notify_in_group": False,
            "join_notify_admins": [],
        })
        recorder = _RecordingLogger()
        with mock.patch.object(join_review_mod, "logger", recorder):
            await _drain(_make_service(store, _Api())._handle_group_join_request(
                _Event(), _add_raw()))

        lines = [msg for level, msg in recorder.records if "通知配置" in msg]
        self.assertEqual(len(lines), 1)
        self.assertIn("群内提醒=关", lines[0])
        self.assertIn("通知QQ=0人", lines[0])
        self.assertIn("两个通知渠道都关着", lines[0])


class NotifyAdminsRecipientTests(unittest.IsolatedAsyncioTestCase):
    """收件人为空时必须落告警，不能静默丢弃。"""

    def _api(self, settings, sent):
        api = object.__new__(OneBotApi)
        api._store = _Store(settings)

        async def _send_private_msg(user_id, text):
            sent.append((user_id, text))

        api._send_private_msg = _send_private_msg
        return api

    async def test_empty_admin_list_warns_and_sends_nothing(self):
        sent = []
        api = self._api({"join_notify_admins": [], "join_request_notify_in_group": False}, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("[加群请求] 777 申请加入群 100", group_id="100")

        self.assertEqual(sent, [])
        warnings = [msg for level, msg in recorder.records if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("100", warnings[0])
        self.assertIn("join_notify_admins", warnings[0])
        self.assertIn("两个渠道都没开", warnings[0])

    async def test_group_reminder_on_does_not_claim_both_channels_off(self):
        sent = []
        api = self._api({"join_notify_admins": [], "join_request_notify_in_group": True}, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("[加群请求] 777 申请加入群 100", group_id="100")

        warnings = [msg for level, msg in recorder.records if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("两个渠道都没开", warnings[0])

    async def test_configured_admins_receive_private_messages(self):
        sent = []
        api = self._api({"join_notify_admins": ["111", "222"]}, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("通知正文", group_id="100")

        self.assertEqual(sent, [("111", "通知正文"), ("222", "通知正文")])
        self.assertEqual(recorder.records, [])


if __name__ == "__main__":
    unittest.main()
