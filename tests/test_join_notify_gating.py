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
from cop.config_store import ConfigStore
from cop.join_review import JoinReviewService  # noqa: E402
from cop.onebot_api import OneBotApi  # noqa: E402
from cop.text_utils import normalize_qq_list  # noqa: E402
from test_config_store import _Config, _template_entry, make_store  # noqa: E402


class _Store:
    """最小 ConfigStore 桩：按 key 返回预置的按群配置。"""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.config = {}
        self._maps = {}

    def get_group_setting(self, group_id, key, default=None):
        return self.settings.get(key, default)

    def get_plugin_admins(self, group_id):
        return self.settings.get("plugin_admins")

    get_admin_notification_recipients = ConfigStore.get_admin_notification_recipients

    def get_group_scope(self, group_id, key):
        return self.get_group_setting(group_id, key, [])

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
        self.group_destinations = []
        self.handled = []
        self.member_checks = []
        self.nickname_queries = []
        self.level_queries = []
        self.group_send_ok = True
        self.group_send_mid = "msg-1"

    def _get_self_id(self, event, raw=None):
        return "999000111"

    async def _is_group_member(self, event, group_id, user_id):
        self.member_checks.append(user_id)
        return False

    async def _get_user_nickname(self, event, user_id):
        return f"昵称{user_id}"

    async def _get_stranger_level(self, event, user_id):
        self.level_queries.append(user_id)
        return "12"

    async def _send_group_text(self, event, group_id, text):
        self.group_destinations.append(group_id)
        self.group_texts.append(text)
        if not self.group_send_ok:
            return False, ""
        return True, self.group_send_mid

    async def _notify_admins(self, text, group_id=None, purpose="join_request"):
        key = "report_notify_admins" if purpose == "report" else "join_notify_admins"
        store = getattr(self, "store", None)
        if store is not None and not store.get_admin_notification_recipients(group_id or "", key)[0]:
            return  # 与真实实现一致：没有收件人 = 没有发出去
        self.admin_texts.append(text)

    async def _handle_group_request(self, event, flag, approve, reason, sub_type=None):
        self.handled.append((flag, approve, reason))
        return True


class _Event:
    def plain_result(self, text):
        return ("text", text)


def _make_service(store, api):
    api.store = store
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

    async def test_webui_scope_gates_only_the_configured_groups_auto_actions(self):
        store = make_store(self, _Config(
            enabled_groups=["100"],
            join_approve_keywords=["你好"],
            join_request_notify_in_group=True,
            group_overrides_templates=[
                _template_entry("100", enabled_groups="off"),
                _template_entry("200", enabled_groups="on"),
            ],
        ))
        api = _Api()
        service = _make_service(store, api)
        await _drain(service._handle_group_join_request(_Event(), _add_raw(group_id=100, flag="f-100")))
        await _drain(service._handle_group_join_request(_Event(), _add_raw(group_id=200, flag="f-200")))

        self.assertEqual(api.handled, [("f-200", True, "命中关键词自动同意")])
        self.assertEqual(api.group_destinations, ["100", "200"])
        self.assertIn("【新人加群】通知", api.group_texts[0])
        self.assertEqual(store.runtime_map("pending_join_requests")["msg-1"]["group_id"], "100")

    async def test_group_increase_cleans_matching_pending_without_welcome(self):
        store = make_store(self, _Config(group_overrides_templates=[
            _template_entry("100", welcome_enabled=True, welcome_message="你好 {at}"),
        ]))
        store.runtime_map("groups")["200"] = {
            "welcome_enabled": True, "welcome_message": "旧欢迎语 {at}",
        }
        unrelated = {
            "other-user": {"group_id": "100", "user_id": "888"},
            "other-group": {"group_id": "200", "user_id": "777"},
        }
        pending = store.runtime_map("pending_join_requests")
        pending.update(unrelated)
        pending.update({
            "msg-1": {"group_id": "100", "user_id": "777"},
            "flag:f-1": {"group_id": 100, "user_id": 777},
        })
        api = _Api()
        api._send = mock.AsyncMock()
        service = _make_service(store, api)
        service._mp = SimpleNamespace(_build_text=str)

        with mock.patch.object(store, "save_config", wraps=store.save_config) as save:
            await service._handle_group_increase_notice(_Event(), _add_raw())
            self.assertEqual(pending, unrelated)
            await service._handle_group_increase_notice(_Event(), _add_raw(group_id=200, user_id="999"))
            save.assert_called_once_with()

        api._send.assert_not_called()
        self.assertEqual(api.group_texts, [])
        self.assertEqual(pending, unrelated)

    async def test_audit_off_still_sends_group_reminder(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
            "join_notify_admins": ["111"],
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
            "join_notify_admins": ["111"],
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
            "join_notify_admins": ["111"],
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


class AuditOffRequestCostTests(unittest.IsolatedAsyncioTestCase):
    """PR #292 审查（behavioral-compatibility-change）：关闭自动审核后的请求开销。

    完全没有通知渠道时应当与旧实现一样零额外请求；只要还会发通知，成员复核
    就保留（它同时防止对已入群成员重复发「新人加群」）。
    """

    async def test_audit_off_without_channels_makes_no_api_calls(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": False,
            "join_notify_admins": [],
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            _Event(), _add_raw()))

        self.assertEqual(api.member_checks, [])
        self.assertEqual(api.nickname_queries, [])
        self.assertEqual(api.level_queries, [])
        self.assertEqual(api.handled, [])
        self.assertEqual(api.group_texts, [])
        self.assertEqual(api.admin_texts, [])

    async def test_audit_off_with_notify_still_checks_membership(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": True,
        })
        api = _Api()
        await _drain(_make_service(store, api)._handle_group_join_request(
            _Event(), _add_raw()))

        self.assertEqual(api.member_checks, ["777"])
        self.assertEqual(len(api.group_texts), 1)


class NormalizeQqListTests(unittest.TestCase):
    """配置里的 QQ 名单必须先 strip 再丢空（#292 审查：inconsistent-recipient-normalization）。"""

    def test_strips_blank_and_drops_none(self):
        self.assertEqual(
            normalize_qq_list([" 111 ", "", None, "222", "   "]),
            ["111", "222"],
        )

    def test_none_input_is_empty(self):
        self.assertEqual(normalize_qq_list(None), [])
        self.assertEqual(normalize_qq_list([]), [])


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

    async def test_config_log_ignores_blank_and_none_recipients(self):
        store = _Store({
            "join_request_notify_in_group": True,
            "join_notify_admins": [" 111 ", "", None],
        })
        recorder = _RecordingLogger()
        with mock.patch.object(join_review_mod, "logger", recorder):
            await _drain(_make_service(store, _Api())._handle_group_join_request(
                _Event(), _add_raw()))

        lines = [msg for level, msg in recorder.records if "通知配置" in msg]
        self.assertEqual(len(lines), 1)
        self.assertIn("通知QQ=1人", lines[0])

    async def test_explicit_empty_unified_list_disables_legacy_private_lookup_and_delivery(self):
        store = _Store({
            "join_audit_enabled": False,
            "join_request_notify_in_group": False,
            "join_notify_admins": ["111"],
            "plugin_admins": [],
        })
        api = _Api()
        recorder = _RecordingLogger()
        with mock.patch.object(join_review_mod, "logger", recorder):
            await _drain(_make_service(store, api)._handle_group_join_request(_Event(), _add_raw()))
        self.assertEqual(api.nickname_queries, [])
        self.assertEqual(api.level_queries, [])
        self.assertEqual(api.admin_texts, [])
        self.assertTrue(any("通知QQ=0人" in msg for _, msg in recorder.records))

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

    async def test_recipients_are_stripped_and_none_dropped(self):
        sent = []
        api = self._api({"join_notify_admins": [" 111 ", "", None, "222"]}, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("通知正文", group_id="100")

        self.assertEqual(sent, [("111", "通知正文"), ("222", "通知正文")])
        self.assertEqual(recorder.records, [])

    async def test_report_purpose_falls_back_to_legacy_join_list(self):
        # 升级兼容：只配过 join_notify_admins 的用户不能丢举报通知
        sent = []
        api = self._api({
            "join_notify_admins": [" 111 "],
            "report_notify_admins": [],
        }, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("[举报] 群 100", group_id="100", purpose="report")

        self.assertEqual(sent, [("111", "[举报] 群 100")])
        warnings = [msg for level, msg in recorder.records if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("回退", warnings[0])
        self.assertIn("report_notify_admins", warnings[0])

    async def test_unified_recipients_replace_both_legacy_lists_even_when_empty(self):
        for admins in (["999"], []):
            with self.subTest(admins=admins):
                sent = []
                api = self._api({
                    "plugin_admins": admins, "join_notify_admins": ["111"],
                    "report_notify_admins": ["333"],
                }, sent)
                recorder = _RecordingLogger()
                with mock.patch.object(onebot_api_mod, "logger", recorder):
                    await api._notify_admins("加群", group_id="100", purpose="join_request")
                    await api._notify_admins("举报", group_id="100", purpose="report")
                self.assertEqual(sent, [(qq, text) for text in ("加群", "举报") for qq in admins])
                if not admins:
                    self.assertTrue(all("plugin_admins" in msg for _, msg in recorder.records))

    async def test_report_purpose_uses_report_notify_admins(self):
        sent = []
        api = self._api({
            "join_notify_admins": ["111"],
            "report_notify_admins": ["333"],
        }, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("[举报] 群 100", group_id="100", purpose="report")

        self.assertEqual(sent, [("333", "[举报] 群 100")])
        self.assertEqual(recorder.records, [])

    async def test_report_purpose_empty_list_warns_with_report_wording(self):
        sent = []
        api = self._api({"report_notify_admins": [], "join_notify_admins": []}, sent)
        recorder = _RecordingLogger()
        with mock.patch.object(onebot_api_mod, "logger", recorder):
            await api._notify_admins("[举报] 群 100", group_id="100", purpose="report")

        self.assertEqual(sent, [])
        warnings = [msg for level, msg in recorder.records if level == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("举报", warnings[0])
        self.assertIn("report_notify_admins", warnings[0])
        # 举报场景不应输出加群申请专属的「两个渠道都没开」诊断
        self.assertNotIn("两个渠道都没开", warnings[0])

    async def test_unknown_purpose_is_rejected(self):
        sent = []
        api = self._api({"join_notify_admins": ["111"]}, sent)
        with self.assertRaises(ValueError):
            await api._notify_admins("正文", group_id="100", purpose="mute")

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
