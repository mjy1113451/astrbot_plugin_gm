"""群标签删除的成功判定回归测试（PR #289 审查：false-success-delete）。

只验证「删除前后读回标签」的判定逻辑，用最小桩替换 OneBot 调用。
"""

import sys
import types
import unittest
from pathlib import Path


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

if "astrbot.api.event" not in sys.modules:
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    sys.modules["astrbot.api.event"] = event_mod

if "astrbot.api.message_components" not in sys.modules:
    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Plain = type("Plain", (), {})
    comp_mod.At = type("At", (), {})
    comp_mod.AtAll = type("AtAll", (), {})
    sys.modules["astrbot.api.message_components"] = comp_mod

from cop.onebot_api import OneBotApi  # noqa: E402


class _TagApi(OneBotApi):
    """只实现标签相关调用的 OneBotApi 桩。"""

    def __init__(self, before, after, raw_result=None, delete_ok=False):
        self.tags_before = before
        self.tags_after = after
        self.raw_result = raw_result
        self.delete_ok = delete_ok
        self.delete_calls = 0
        self.read_calls = 0

    async def _group_tags_read(self, event, group_id):
        self.read_calls += 1
        tags = self.tags_before if self.read_calls == 1 else self.tags_after
        if tags is None:
            return False, []
        return True, list(tags)

    async def _call_onebot_raw(self, event, action, **params):
        self.delete_calls += 1
        return self.raw_result


def _fail_payload():
    # 无 status/retcode 也不含成功载荷 → _action_result_success 判失败
    return {"wording": "接口不支持"}


class DeleteGroupTagTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_when_tag_existed_before_and_gone_after(self):
        api = _TagApi(before=["老标签"], after=[], raw_result=_fail_payload())
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    async def test_not_found_is_not_reported_as_success(self):
        # 标签在调用前就不存在：响应含糊不能当成删除成功
        api = _TagApi(before=["别的标签"], after=["别的标签"], raw_result=_fail_payload())
        ok, _, reason = await api._delete_group_tag(None, "100", "不存在的标签")
        self.assertFalse(ok)
        self.assertEqual(reason, "not_found")

    async def test_read_failure_keeps_failure(self):
        api = _TagApi(before=None, after=[], raw_result=_fail_payload())
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertFalse(ok)
        self.assertEqual(reason, "read_failed")

    async def test_tag_still_present_after_delete_is_failure(self):
        api = _TagApi(before=["老标签"], after=["老标签"], raw_result=_fail_payload())
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertFalse(ok)
        self.assertEqual(reason, "still_present")

    async def test_explicit_success_but_tag_still_present_is_failure(self):
        # 协议端回 status=ok 却根本没删掉：读回标签仍在，不能报"已删除"
        # （PR #289 审查：unverified-delete-success）。
        api = _TagApi(
            before=["老标签"], after=["老标签"], raw_result={"status": "ok", "retcode": 0},
        )
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertFalse(ok)
        self.assertEqual(reason, "still_present")

    async def test_explicit_success_is_confirmed_by_readback(self):
        api = _TagApi(
            before=["老标签"], after=[], raw_result={"status": "ok", "retcode": 0},
        )
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    async def test_explicit_success_with_unreadable_tags_keeps_failure(self):
        # 明确成功但读回失败：无法确认到底删没删，按失败/结果未知上报
        api = _TagApi(before=["老标签"], after=None, raw_result={"status": "ok"})
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertFalse(ok)
        self.assertEqual(reason, "read_failed")

    async def test_explicit_success_without_read_interface_is_trusted(self):
        # 协议端压根不提供读接口：只能以 action 响应为唯一依据
        api = _TagApi(before=None, after=None, raw_result={"status": "ok", "retcode": 0})
        ok, _, reason = await api._delete_group_tag(None, "100", "老标签")
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
