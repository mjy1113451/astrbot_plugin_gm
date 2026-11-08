"""群标签读取解析的回归测试（PR #289 审查：incompatible-response-shape）。

协议端返回形态不统一：裸数组 / {data: [...]} / {data: {tags: [...]}} 都要能读。
"""

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_astrbot_stubs():
    """补齐 onebot_api 需要的 astrbot 子模块（已存在则只补缺）。"""
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

from cop.onebot_api import OneBotApi  # noqa: E402


class GroupTagsStateContractTests(unittest.IsolatedAsyncioTestCase):
    """区分"本群没有标签"与"查询失败"（PR #289 审查：misleading-empty-state）。

    命令层（/群标签、/删群标签）依赖这个 (ok, tags) 契约来决定提示文案，
    所以这里把它固定成回归测试。
    """

    async def read(self, raw):
        api = OneBotApi.__new__(OneBotApi)

        async def fake_get_tags(event, group_id):
            return raw

        api._get_group_tags = fake_get_tags
        return await api._group_tags_read(None, "100")

    async def test_empty_success_is_not_a_failure(self):
        self.assertEqual(await self.read([]), (True, []))
        self.assertEqual(await self.read({"status": "ok", "data": []}), (True, []))

    async def test_missing_api_is_a_failure(self):
        # None = 没有可用调用通路（协议端不支持 get_group_tags）
        self.assertEqual(await self.read(None), (False, []))

    async def test_failed_response_is_a_failure_not_empty(self):
        self.assertEqual(await self.read({"status": "failed"}), (False, []))


class GroupTagsReadTests(unittest.IsolatedAsyncioTestCase):
    def make_api(self, raw):
        api = OneBotApi.__new__(OneBotApi)

        async def fake_get_tags(event, group_id):
            return raw

        api._get_group_tags = fake_get_tags
        return api

    async def read(self, raw):
        api = self.make_api(raw)
        return await api._group_tags_read(None, "100")

    async def test_bare_list_response(self):
        self.assertEqual(await self.read(["标签A", "标签B"]), (True, ["标签A", "标签B"]))

    async def test_data_list_response(self):
        self.assertEqual(await self.read({"status": "ok", "data": ["A"]}), (True, ["A"]))

    async def test_data_tags_dict_response(self):
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": {"tags": [{"tag": "x"}, {"name": "y"}]},
        }
        self.assertEqual(await self.read(payload), (True, ["x", "y"]))

    async def test_top_level_tags_response(self):
        self.assertEqual(await self.read({"status": "ok", "tags": ["z"]}), (True, ["z"]))

    async def test_failed_response_is_not_trusted(self):
        self.assertEqual(
            await self.read({"status": "failed", "retcode": -1}), (False, [])
        )
        self.assertEqual(await self.read(None), (False, []))
        self.assertEqual(await self.read("bad"), (False, []))

    async def test_success_without_tags_field_is_empty(self):
        self.assertEqual(await self.read({"status": "ok", "data": {}}), (True, []))

    async def test_tags_are_deduplicated(self):
        self.assertEqual(
            await self.read(["A", "A", {"tag": "A"}]), (True, ["A"])
        )


class AddGroupTagReadBudgetTests(unittest.IsolatedAsyncioTestCase):
    """添加群标签时的读回预算（PR #289 审查：duplicate-side-effect）。

    读接口不可用时不能每个候选都重查一遍，否则一次添加会打出 4 次无效请求。
    """

    def make_api(self, read_raw, action_result):
        api = OneBotApi.__new__(OneBotApi)
        self.read_calls = 0
        self.action_calls = []

        async def fake_get_tags(event, group_id):
            self.read_calls += 1
            return read_raw

        async def fake_call(event, action, **params):
            self.action_calls.append((action, params))
            return action_result

        api._get_group_tags = fake_get_tags
        api._call_onebot_raw = fake_call
        return api

    async def test_read_unsupported_does_not_requery_per_candidate(self):
        api = self.make_api(None, {"status": "failed", "retcode": -1})
        ok, _ = await api._add_group_tag(None, "100", "标签A")
        self.assertFalse(ok)
        # 只探一次读能力，其余 4 个候选都不再读回
        self.assertEqual(self.read_calls, 1)
        self.assertEqual(len(self.action_calls), 4)

    async def test_preexisting_tag_skips_side_effect(self):
        api = self.make_api(["标签A"], {"status": "ok"})
        ok, result = await api._add_group_tag(None, "100", "标签A")
        self.assertTrue(ok)
        self.assertIsNone(result)
        self.assertEqual(self.action_calls, [])
        self.assertEqual(self.read_calls, 1)

    async def test_success_response_without_status_returns_early(self):
        api = self.make_api([], {"data": None})
        ok, _ = await api._add_group_tag(None, "100", "标签B")
        self.assertTrue(ok)
        # 首个候选即成功，不再尝试其余变体
        self.assertEqual(len(self.action_calls), 1)

    async def test_missing_tag_after_all_candidates_fails(self):
        api = self.make_api([], {"status": "failed", "retcode": -1})
        ok, _ = await api._add_group_tag(None, "100", "标签C")
        self.assertFalse(ok)
        self.assertEqual(len(self.action_calls), 4)


class JoinRequestCollectionTests(unittest.TestCase):
    """申请查询结果解析（PR #289 审查：misleading-diagnostic-output）。"""

    def test_system_msg_only_join_requests_count(self):
        # get_group_system_msg 里其它字段是系统消息，不能算申请
        data = {
            "join_requests": [{"group_id": "100", "flag": "f1", "sub_type": "add"}],
            "invited_requests": [{"group_id": "100", "invitor_uin": "1"}],
            "system_msg": [{"type": "member_increase"}],
        }
        requests, others = OneBotApi.collect_join_requests(data, "100", system_msg=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(others, 2)

    def test_request_interface_counts_all_list_items(self):
        data = [{"user_id": "777", "flag": "f1"}, {"user_id": "888"}]
        requests, others = OneBotApi.collect_join_requests(data, "100", system_msg=False)
        self.assertEqual(len(requests), 2)
        self.assertEqual(others, 0)

    def test_other_group_requests_are_skipped(self):
        data = {"join_requests": [{"group_id": "999", "flag": "f1"}]}
        requests, others = OneBotApi.collect_join_requests(data, "100", system_msg=True)
        self.assertEqual(requests, [])
        self.assertEqual(others, 0)

    def test_plain_system_entries_are_not_requests(self):
        self.assertFalse(OneBotApi.looks_like_join_request({"type": "member_increase"}))
        self.assertTrue(OneBotApi.looks_like_join_request({"request_type": "add"}))
        self.assertTrue(OneBotApi.looks_like_join_request({"comment": "想进群"}))


if __name__ == "__main__":
    unittest.main()
