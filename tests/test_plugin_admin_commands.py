"""Command-level checks for delegated versus native QQ administrator permissions."""

import importlib.util
import logging
import re
import sys
import tempfile
import unittest
from pathlib import Path
from threading import RLock
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_plugin():
    def passthrough(*args, **kwargs):
        return lambda func: func

    def command(name, description, *, alias=()):
        def register(func):
            func.command_names = {name, *alias}
            func.command_metadata = (name, description, set(alias))
            return func
        return register

    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("test_plugin_admin_commands")
    api.AstrBotConfig = dict
    event_api = ModuleType("astrbot.api.event")
    event_api.AstrMessageEvent = object
    event_api.filter = SimpleNamespace(
        command=command, event_message_type=passthrough, after_message_sent=passthrough,
        EventMessageType=SimpleNamespace(GROUP_MESSAGE=1, ALL=2),
    )
    star = ModuleType("astrbot.api.star")
    star.Star, star.Context, star.register = object, object, passthrough
    components = ModuleType("astrbot.api.message_components")
    components.At = components.AtAll = components.Plain = object
    astrbot = ModuleType("astrbot")
    astrbot.api = api
    with patch.dict(sys.modules, {
        "astrbot": astrbot, "astrbot.api": api, "astrbot.api.event": event_api,
        "astrbot.api.star": star, "astrbot.api.message_components": components,
    }):
        spec = importlib.util.spec_from_file_location("_plugin_admin_command_test", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class _Config(dict):
    def save_config(self):
        pass


class PluginAdminCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        plugin = self.module.GroupAdminPlugin.__new__(self.module.GroupAdminPlugin)
        plugin._store = self.module.ConfigStore(
            config=_Config(), runtime_path=root / "runtime.json", data_dir=root,
            config_path=root / "config.json", stats_path=root / "stats.json",
            reports_path=root / "reports.json", lock=RLock(), json_store=self.module.JsonStore(),
        )
        plugin._api = SimpleNamespace(
            _set_group_admin=AsyncMock(return_value=True),
            _execute_action=AsyncMock(),
            _find_group_owner=AsyncMock(return_value="88888"),
            _send_private_msg=AsyncMock(), _notify_admins=AsyncMock(),
        )
        plugin._mp = self.module.MessageParser(onebot_api=plugin._api)
        plugin._perms = self.module.PermissionService(
            config_store=plugin._store, message_parse=plugin._mp, onebot_api=plugin._api,
        )
        plugin._messaging = self.module.MessagingService(
            config_store=plugin._store, message_parse=plugin._mp, permissions=plugin._perms,
        )
        plugin._runtime = SimpleNamespace(reports={"pending": []})
        self.plugin = plugin

    @staticmethod
    def event(role="member", mentions=()):
        return SimpleNamespace(
            raw_message={
                "group_id": 12345, "user_id": 67890, "sender": {"role": role},
                "message": [{"type": "at", "data": {"qq": qq}} for qq in mentions],
            },
            plain_result=lambda text: text,
        )

    async def test_native_role_required_for_edits_and_single_or_bulk_clear(self):
        p = self.plugin
        native = self.event("admin", mentions=("all", "0", 67890, 67890))
        self.assertIn("67890", [msg async for msg in p.add_plugin_admin_cmd(native)][0])
        self.assertEqual(p._store.get_plugin_admins("12345"), ["67890"])
        p._store.set_group_override("12345", "group_admin_admins", ["55555"])

        delegated = self.event()
        self.assertTrue(p._perms._is_authorized(delegated.raw_message, "67890"))
        for command, args in (
            (p.add_plugin_admin_cmd, ("55555",)),
            (p.remove_plugin_admin_cmd, ("67890",)),
            (p.clear_group_config_cmd, ("plugin_admins",)),
            (p.clear_group_config_cmd, ("__list_ops__",)),
            (p.clear_group_config_cmd, ()),
        ):
            with self.subTest(command=command.__name__, args=args):
                self.assertIn("只有 QQ 群管理员或群主", [msg async for msg in command(delegated, *args)][0])
                self.assertEqual(p._store.get_plugin_admins("12345"), ["67890"])

        owner = self.event("owner")
        self.assertIn("67890", [msg async for msg in p.remove_plugin_admin_cmd(owner, "67890")][0])
        self.assertEqual(p._store.get_plugin_admins("12345"), [])
        self.assertIn("空（已配置）", [msg async for msg in p.view_plugin_admins_cmd(owner)][0])
        self.assertIn("已清除", [msg async for msg in p.clear_group_config_cmd(owner)][0])
        self.assertIsNone(p._store.get_plugin_admins("12345"))

    async def test_legacy_aliases_retire_without_writing_and_delegation_cannot_set_qq_role(self):
        p = self.plugin
        self.assertEqual(p.legacy_admin_list_cmd.command_names, {
            "添加插件管理", "删除插件管理", "添加管理管理", "删除管理管理",
        })
        self.assertIn("已停用", [msg async for msg in p.legacy_admin_list_cmd(self.event("owner"), "67890")][0])
        self.assertEqual(p._store.runtime_map("group_overrides"), {})

        p._store.set_group_override("12345", "group_admin_admins", ["67890"])
        self.assertIn("成功", [msg async for msg in p.set_group_admin_cmd(self.event(), "55555")][0])
        p._api._set_group_admin.reset_mock()
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        self.assertIn("没有设置", [msg async for msg in p.set_group_admin_cmd(self.event(), "55555")][0])
        p._api._set_group_admin.assert_not_awaited()

    async def test_legacy_notification_commands_only_change_recipients_while_unified_is_unset(self):
        p = self.plugin
        owner = self.event("owner")
        p._store.config["join_notify_admins"] = ["55555"]
        view = [msg async for msg in p.list_report_notify_cmd(owner)][0]
        self.assertNotIn("55555", view)
        self.assertIn("独立配置）：空", view)
        self.assertIn("/查看加群通知QQ", view)
        denied = [msg async for msg in p.remove_report_notify_cmd(self.event(), "55555")][0]
        self.assertIn("只有 QQ 群管理员或群主", denied)
        reply = [msg async for msg in p.remove_report_notify_cmd(owner, "55555")][0]
        self.assertIn("不在本群举报通知QQ名单", reply)
        self.assertIn("/添加举报通知QQ", reply)
        self.assertEqual(p._store.runtime_map("group_overrides"), {})
        self.assertEqual(p._store.get_admin_notification_recipients("12345", "report_notify_admins"),
                         (["55555"], "join_notify_admins"))
        self.assertIn("已添加", [msg async for msg in p.add_report_notify_cmd(owner, "66666")][0])
        self.assertEqual(p._store.get_admin_notification_recipients("12345", "report_notify_admins"),
                         (["66666"], "report_notify_admins"))
        self.assertIn("已删除", [msg async for msg in p.remove_report_notify_cmd(owner, "66666")][0])
        self.assertEqual(p._store.get_admin_notification_recipients("12345", "report_notify_admins"),
                         ([], "report_notify_admins"))
        self.assertIn("已添加", [msg async for msg in p.add_join_notify_cmd(owner, "67890")][0])
        self.assertFalse(p._perms._is_authorized(self.event().raw_message))
        self.assertIsNone(p._store.get_plugin_admins("12345"))
        p._store.set_group_override("12345", "plugin_admins", [])
        self.assertIn("旧通知名单不生效", [msg async for msg in p.add_report_notify_cmd(owner, "67890")][0])
        self.assertEqual(p._store.get_group_setting("12345", "report_notify_admins"), [])

    async def test_purge_preview_states_permissions_and_only_authorized_confirmation_kicks(self):
        p = self.plugin
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        p._stats = SimpleNamespace(get_last_active=lambda group_id: {})
        p._api._normalize_member_list = lambda response: [{"user_id": "55555", "role": "member"}]
        p._api._kick_member = AsyncMock(return_value=True)
        event = self.event()
        event.raw_message["message"] = [{"type": "text", "data": {"text": "/清人 4"}}]
        preview = [msg async for msg in p.purge_inactive_cmd(event)][0]
        self.assertIn("尚未踢人", preview)
        self.assertIn("仅 QQ 群管理员、群主或本群统一插件管理员可执行", preview)
        self.assertIn("/清人 4 确认", preview)
        p._api._kick_member.assert_not_awaited()

        event.raw_message["message"][0]["data"]["text"] = "/清人 4 确认"
        event.raw_message["group_id"] = 12346
        denied = [msg async for msg in p.purge_inactive_cmd(event)][0]
        self.assertIn("本群统一插件管理员", denied)
        p._api._kick_member.assert_not_awaited()
        event.raw_message["group_id"] = 12345
        self.assertIn("踢出 1 人", [msg async for msg in p.purge_inactive_cmd(event)][0])
        p._api._kick_member.assert_awaited_once_with(event, "12345", "55555")

    async def test_delegation_cannot_change_global_political_duration(self):
        p = self.plugin
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        denied = [msg async for msg in p.set_political_ban_duration_cmd(self.event(), 20)]
        self.assertIn("只有 QQ 群管理员或群主", denied[0])
        self.assertNotIn("political_ban_duration", p._store.config)
        allowed = [msg async for msg in p.set_political_ban_duration_cmd(self.event("admin"), 20)]
        self.assertIn("成功", allowed[0])
        self.assertEqual(p._store.config["political_ban_duration"], 20)

    async def test_pending_requests_only_show_records_explicitly_for_this_group(self):
        p = self.plugin
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        p._api._call_action_fallback = AsyncMock(return_value=(True, {"join_requests": [
            {"group_id": 12345, "comment": "current-group"},
            {"group_id": 12346, "comment": "other-group"},
            {"comment": "unknown-group"},
        ]}))
        reply = [msg async for msg in p.pending_join_requests_cmd(self.event())][0]
        self.assertIn("current-group", reply)
        self.assertNotIn("other-group", reply)
        self.assertNotIn("unknown-group", reply)

    def test_command_registry_and_snapshot_match_registered_names_and_aliases(self):
        commands = {
            method: func.command_metadata
            for method, func in vars(self.module.GroupAdminPlugin).items()
            if hasattr(func, "command_metadata")
        }
        names = [name for name, _, aliases in commands.values() for name in (name, *aliases)]
        self.assertEqual(len(names), len(set(names)))
        self.assertCountEqual(self.module._GM_COMMAND_NAMES, names)

        document = (ROOT / "docs" / "interfaces_snapshot.md").read_text(encoding="utf-8")
        command_section = document.split("## 1. 命令入口", 1)[1].split("## 2. 事件钩子", 1)[0]
        self.assertIn(f"**命令总数：{len(commands)}**", command_section)
        self.assertIn(f"**带别名命令统计**：{sum(bool(aliases) for _, _, aliases in commands.values())} 个", command_section)
        self.assertIn(f"共 {len(names)} 个名称", document.split("## 5. 校验结论", 1)[1])
        rows = re.findall(r"^\| \d+ \| `([^`]+)` \| (.+?) \| (.*?) \| `([^`]+)` \|$",
                          command_section, re.MULTILINE)
        self.assertCountEqual([method for _, _, _, method in rows], commands.keys())
        for name, description, aliases, method in rows:
            with self.subTest(command=name):
                self.assertEqual(
                    (name, description.replace(r"\|", "|"), set(re.findall(r"`([^`]+)`", aliases))),
                    commands[method],
                )

    async def test_unified_admin_can_change_nickname_only_in_their_group(self):
        p = self.plugin
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        p._api._set_group_card = AsyncMock(return_value=True)
        event = self.event(mentions=("55555",))
        event.raw_message["message"].append({"type": "text", "data": {"text": "/设群昵称 新昵称"}})
        reply = [msg async for msg in p.set_other_card_cmd(event)][0]
        self.assertIn("新昵称", reply)
        p._api._set_group_card.assert_awaited_once_with(event, "12345", "55555", "新昵称")

        p._api._set_group_card.reset_mock()
        event.raw_message["group_id"] = 12346
        reply = [msg async for msg in p.set_other_card_cmd(event)][0]
        self.assertIn("没有设置他人群昵称的权限", reply)
        p._api._set_group_card.assert_not_awaited()

    async def test_group_name_aliases_pass_only_the_new_name_to_onebot(self):
        p = self.plugin
        p._history = SimpleNamespace(_should_notify_group_name=lambda *args: True)
        p._api._execute_action.return_value = True
        event = self.event("admin")
        for name in sorted(p.set_group_name_cmd.command_names):
            with self.subTest(command=name):
                event.raw_message["message"] = [{"type": "text", "data": {"text": f"/{name} 新群名"}}]
                reply = [msg async for msg in p.set_group_name_cmd(event)][0]
                self.assertIn("已修改群名", reply)
                p._api._execute_action.assert_awaited_with(
                    event, "set_group_name", group_id="12345", group_name="新群名")

    async def test_delegated_essence_requires_confirmed_current_group(self):
        p = self.plugin
        p._store.set_group_override("12345", "plugin_admins", ["67890"])
        p._api._set_essence = AsyncMock(return_value=True)
        with patch.object(p._mp, "_get_reply_id", return_value="123"):
            for quoted in ({"group_id": 12346}, {}, {"group_id": 12345, "message_type": "private"}):
                p._api._execute_action.return_value = {"data": quoted}
                reply = [msg async for msg in p.essence_cmd(self.event())][0]
                self.assertIn("无法确认", reply)
            p._api._set_essence.assert_not_awaited()
            p._api._execute_action.return_value = {"group_id": 12345, "message_type": "group"}
            reply = [msg async for msg in p.essence_cmd(self.event())][0]
            self.assertIn("成功", reply)
            p._api._set_essence.assert_awaited_once()

    async def test_report_against_native_admin_or_unknown_role_only_notifies_owner(self):
        p = self.plugin
        event = self.event(mentions=("55555",))
        for role in ("admin", "owner", None, "member"):
            with self.subTest(role=role):
                p._api._execute_action.return_value = {"data": {"role": role}} if role else None
                p._api._send_private_msg.reset_mock()
                p._api._notify_admins.reset_mock()
                self.assertIn("已提交举报", [msg async for msg in p.report_cmd(event, "理由")][0])
                if role == "member":
                    p._api._notify_admins.assert_awaited_once()
                    p._api._send_private_msg.assert_not_awaited()
                else:
                    p._api._notify_admins.assert_not_awaited()
                    self.assertEqual(p._api._send_private_msg.await_args.args[0], "88888")
