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

    async def test_success_with_empty_list_is_trusted(self):
        # 明确返回空列表 = 本群确实没有标签，可以当可信基线
        self.assertEqual(await self.read({"status": "ok", "data": []}), (True, []))
        self.assertEqual(
            await self.read({"status": "ok", "data": {"tags": []}}), (True, [])
        )

    async def test_success_without_recognized_tags_field_is_failure(self):
        # 成功但响应里找不到可识别的标签字段：按读取失败处理，绝不能当成
        # "本群没有标签"，否则覆盖型回退会把已有标签做基线清空
        # （PR #289 审查：incompatible-response-shape）。
        self.assertEqual(await self.read({"status": "ok", "data": {}}), (False, []))
        self.assertEqual(
            await self.read({"status": "ok", "result": {"tags": ["A"]}}), (False, [])
        )

    async def test_response_without_explicit_success_is_not_trusted(self):
        # 查询类响应必须带显式成功信号（status=ok/async 或 retcode/code=0）：
        # 只有 data 的字典无法区分"正常空列表"与"错误载荷"，一律按读取失败，
        # 免得覆盖型回退拿它当基线把已有标签清空
        # （PR #289 审查：misleading-empty-state）。
        self.assertEqual(await self.read({"data": []}), (False, []))
        self.assertEqual(await self.read({"data": {"tags": []}}), (False, []))

    async def test_error_payload_is_not_trusted(self):
        # 错误载荷可能也带 data/tags，绝不能读成"本群没有标签"
        self.assertEqual(await self.read({"error": "internal error", "data": []}), (False, []))
        self.assertEqual(await self.read({"code": 1, "data": []}), (False, []))
        self.assertEqual(await self.read({"retcode": 100, "data": []}), (False, []))
        self.assertEqual(await self.read({"success": False, "data": []}), (False, []))

    async def test_explicit_zero_code_is_success(self):
        # 没有 status 但 retcode/code 明确为 0：可以信任（含空列表）
        self.assertEqual(await self.read({"retcode": 0, "data": []}), (True, []))
        self.assertEqual(await self.read({"code": 0, "data": ["A"]}), (True, ["A"]))

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
        ok, _, _warn = await api._add_group_tag(None, "100", "标签A")
        self.assertFalse(ok)
        # 只探一次读能力；读不回来时无法确认是否已生效，只打一个最匹配的
        # action 就停手，不再拿含糊失败去试其余变体（duplicate-side-effect）
        self.assertEqual(self.read_calls, 1)
        self.assertEqual(len(self.action_calls), 1)
        self.assertEqual(self.action_calls[0][0], "add_group_tag")

    async def test_unsupported_action_still_falls_back_when_read_unavailable(self):
        # 明确"不认识这个 action"时换参数变体是安全的：上一次根本没被执行。
        # 但不会降级到覆盖型 set_group_tag——读不回来就无法确认它有没有把
        # 本群其它标签整体改写掉（PR #289 审查：unsafe-action-fallback）。
        api = self.make_api(None, {"status": "failed", "wording": "unknown action"})
        ok, _, _warn = await api._add_group_tag(None, "100", "标签A")
        self.assertFalse(ok)
        self.assertEqual(
            [c[0] for c in self.action_calls],
            ["add_group_tag", "add_group_tag"],
        )

    async def test_ambiguous_success_without_read_back_stops_after_one_call(self):
        # 读接口不可用 + 首个 action 回应含糊（有载荷但没有 status/retcode）：
        # 按成功处理并立即返回，不再重复调用其余有副作用的变体
        api = self.make_api(None, {"data": None})
        ok, _, _warn = await api._add_group_tag(None, "100", "标签B")
        self.assertTrue(ok)
        self.assertEqual(len(self.action_calls), 1)

    async def test_preexisting_tag_skips_side_effect(self):
        api = self.make_api(["标签A"], {"status": "ok"})
        ok, result, _warn = await api._add_group_tag(None, "100", "标签A")
        self.assertTrue(ok)
        self.assertIsNone(result)
        self.assertEqual(self.action_calls, [])
        self.assertEqual(self.read_calls, 1)

    async def test_success_response_without_status_returns_early(self):
        api = self.make_api([], {"data": None})
        ok, _, _warn = await api._add_group_tag(None, "100", "标签B")
        self.assertTrue(ok)
        # 首个候选即成功，不再尝试其余变体
        self.assertEqual(len(self.action_calls), 1)

    async def test_no_retry_on_unavailable_path(self):
        # 读接口不可用 + 首个 action 明确失败：不再换变体重复执行有副作用的调用
        # （PR #289 审查：unsafe-action-fallback）。
        api = self.make_api(None, {"status": "failed", "wording": "权限不足"})
        ok, _, warn = await api._add_group_tag(None, "100", "标签D")
        self.assertFalse(ok)
        self.assertEqual(warn, "")
        self.assertEqual(len(self.action_calls), 1)

    def test_none_result_does_not_invite_retry(self):
        # 无可用调用通路（None）不能当作"可以换变体重试"：通路本身走不通，
        # 也无法确认上一次是否已经落到协议端
        self.assertFalse(OneBotApi._failure_invites_retry(None, "add_group_tag"))

    async def test_missing_tag_after_all_candidates_fails(self):
        api = self.make_api([], {"status": "failed", "retcode": -1})
        ok, _, _warn = await api._add_group_tag(None, "100", "标签C")
        self.assertFalse(ok)
        self.assertEqual(len(self.action_calls), 4)


class AddGroupTagOverwriteFallbackTests(unittest.IsolatedAsyncioTestCase):
    """覆盖型 set_group_tag 只能"读得通"时才用，且要补回被挤掉的旧标签。

    PR #289 审查：unsafe-action-fallback——set_group_tag 可能整体改写标签集合，
    读接口不可用时无法发现副作用，也不该自动降级过去；
    ignored-side-effect-failure——补回旧标签时不能只看有没有抛异常。
    """

    def make_api(self, tags, read_ok=True, restore_add_works=True):
        api = OneBotApi.__new__(OneBotApi)
        self.tags = list(tags)
        self.baseline = list(tags)
        self.calls = []
        self.read_ok = read_ok
        # 补回旧标签时 add_group_tag 是否真的生效（模拟协议端接受/拒绝）
        self.restore_add_works = restore_add_works

        async def fake_get_tags(event, group_id):
            if not self.read_ok:
                return None
            return list(self.tags)

        async def fake_call(event, action, **params):
            self.calls.append((action, params))
            tag = params.get("tag") or (params.get("tags") or [""])[0]
            if action == "set_group_tag":
                # 覆盖语义：整体改写，原有标签全部丢失
                self.tags[:] = [tag]
                return {"status": "ok"}
            if action == "add_group_tag":
                # 只让"补回原有标签"的调用生效，新增的标签一律当"action 不支持"
                if self.restore_add_works and tag in self.baseline:
                    if tag not in self.tags:
                        self.tags.append(tag)
                    return {"status": "ok"}
                return {"status": "failed", "wording": "unknown action"}
            return {"status": "failed", "wording": "unknown action"}

        api._get_group_tags = fake_get_tags
        api._call_onebot_raw = fake_call
        return api

    async def test_overwrite_action_restores_displaced_tags(self):
        api = self.make_api(["旧标签1", "旧标签2"], restore_add_works=True)
        ok, _, warn = await api._add_group_tag(None, "100", "新标签")
        self.assertTrue(ok)
        self.assertEqual(warn, "")
        # 走的是覆盖型接口，且把被挤掉的两个旧标签补了回来
        self.assertIn("set_group_tag", [a for a, _ in self.calls])
        self.assertEqual(sorted(self.tags), sorted(["旧标签1", "旧标签2", "新标签"]))

    async def test_restore_failure_is_reported(self):
        # 补回的 action 返回 failed 但没有异常：不能当成"已恢复"，
        # 必须回报 tags_may_be_lost 让管理员人工核对
        # （PR #289 审查：ignored-side-effect-failure）。
        api = self.make_api(["旧标签1", "旧标签2"], restore_add_works=False)
        ok, _, warn = await api._add_group_tag(None, "100", "新标签")
        self.assertFalse(ok)
        self.assertEqual(warn, "tags_may_be_lost")

    async def test_overwrite_action_not_used_without_read(self):
        api = self.make_api([], read_ok=False)
        ok, _, warn = await api._add_group_tag(None, "100", "新标签")
        self.assertFalse(ok)
        self.assertEqual(warn, "")
        self.assertEqual({a for a, _ in self.calls}, {"add_group_tag"})


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

    def test_bare_list_system_entries_need_flag(self):
        # 裸数组没有字段名可依：只有带 flag 的条目才算申请
        data = [
            {"group_id": "100", "flag": "f1", "sub_type": "add"},
            {"group_id": "100", "sub_type": "invite", "type": "系统通知"},
        ]
        requests, others = OneBotApi.collect_join_requests(data, "100", system_msg=True)
        self.assertEqual(len(requests), 1)
        self.assertEqual(others, 1)

    def test_plain_system_entries_are_not_requests(self):
        self.assertFalse(OneBotApi.looks_like_join_request({"type": "member_increase"}))
        self.assertTrue(OneBotApi.looks_like_join_request({"request_type": "add"}))
        # comment 系统消息也可能带，不能作为申请判据
        self.assertFalse(OneBotApi.looks_like_join_request({"comment": "想进群"}))

    def test_system_entries_with_request_fields_still_not_counted(self):
        # 系统消息条目即使带 flag/sub_type 也不算申请：get_group_system_msg
        # 只认 join_requests 这一个字段，否则诊断输出仍会误报
        # （PR #289 审查：misleading-diagnostic-output）。
        data = {
            "join_requests": [{"group_id": "100", "flag": "f1"}],
            "system_msg": [
                {"group_id": "100", "flag": "f2", "sub_type": "invite"},
                {"group_id": "100", "request_type": "add"},
            ],
        }
        requests, others = OneBotApi.collect_join_requests(data, "100", system_msg=True)
        self.assertEqual(requests, [{"group_id": "100", "flag": "f1"}])
        self.assertEqual(others, 2)


if __name__ == "__main__":
    unittest.main()
