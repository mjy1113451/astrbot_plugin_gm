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


if __name__ == "__main__":
    unittest.main()
