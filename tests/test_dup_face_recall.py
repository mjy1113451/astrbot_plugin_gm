"""重复表情包撤回的「紧邻重发」判定回归测试（#273 后续）。

报告场景：用户发一个表情包 → 发一条文字 → 再发同一个表情包，第二条表情
被当成「重复表情包」撤回。同样的表情只在「同一发送者、且中间没有任何其它
群消息」时才算重复刷屏（#273 的说法是「中间聊了若干条其它消息后再发同一
表情属正常使用」，其他人的发言同样算打断），因此这里按整群消息时间线钉住
判定：只有紧挨着的同人同表情才撤回。

不依赖 astrbot 运行时，L1/L2 依赖全部用最小桩件替换。
"""

import asyncio
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


_install_astrbot_stubs()

from cop.conversational import DupFaceService  # noqa: E402


_HASH_A = "0123456789abcdef0123456789abcdef"
_HASH_B = "fedcba9876543210fedcba9876543210"
_GROUP = "123456"
_SENDER = 10001
_OTHER = 10002


class _FakeStore:
    """ConfigStore 桩件：只提供 config 与 runtime_map。"""

    def __init__(self, enabled=True):
        self.config = {"dup_face_recall_enabled": enabled}
        self._maps = {"group_overrides": {}}

    def runtime_map(self, key):
        return self._maps.setdefault(key, {})


class _FakeApi:
    """OneBotApi 桩件：记录撤回过的 message_id。"""

    def __init__(self, fail=False):
        self.recalled = []
        self.fail = fail

    async def _do_recall(self, event, message_id):
        if self.fail:
            return False, "撤回失败: 模拟异常"
        self.recalled.append(str(message_id))
        return True, ""


def _image_seg(md5):
    return {"type": "image", "data": {"md5": md5, "file": md5 + ".image"}}


def _face_seg(face_id="178"):
    return {"type": "face", "data": {"id": face_id}}


def _text_seg(text="嗯"):
    return {"type": "text", "data": {"text": text}}


class _DupFaceHarness:
    """按群消息时间线依次喂消息，返回每条消息的 message_id。"""

    def __init__(self, enabled=True, fail=False):
        self.store = _FakeStore(enabled)
        self.api = _FakeApi(fail)
        self.runtime = SimpleNamespace(
            _dup_face_seen={},
            _dup_face_mid_cache={},
            _dup_face_seq={},
        )
        self.service = DupFaceService(
            config_store=self.store,
            runtime=self.runtime,
            onebot_api=self.api,
        )
        self.event = SimpleNamespace()
        self._next_mid = 1000

    def _raw(self, segments, user_id, group_id):
        self._next_mid += 1
        return {
            "post_type": "message",
            "message_type": "group",
            "group_id": group_id,
            "user_id": user_id,
            "message_id": self._next_mid,
            "message": segments,
        }

    def send(self, segments, user_id=_SENDER, group_id=_GROUP):
        raw = self._raw(segments, user_id, group_id)
        asyncio.run(self.service._dup_face_recall_check(self.event, raw, str(group_id)))
        return raw["message_id"]

    def deliver(self, raw, group_id=_GROUP):
        """重复投递同一条消息（message_id 不变）。"""
        asyncio.run(self.service._dup_face_recall_check(self.event, raw, str(group_id)))


class DupFaceAdjacencyTests(unittest.TestCase):
    def test_back_to_back_same_image_is_recalled(self):
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        second = case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [str(second)])

    def test_same_image_after_one_text_is_not_recalled(self):
        # 报告场景：表情包 → 一条文字 → 同一个表情包
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        case.send([_text_seg("哈哈")])
        second = case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [], f"消息 {second} 被误撤回")

    def test_same_image_after_other_members_chat_is_not_recalled(self):
        # #273 的口径：其他人的发言同样打断「连发」
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        case.send([_text_seg("不是吧")], user_id=_OTHER)
        case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])

    def test_same_image_after_different_image_is_not_recalled(self):
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        case.send([_image_seg(_HASH_B)])
        case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])

    def test_repeated_spam_keeps_being_recalled(self):
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        second = case.send([_image_seg(_HASH_A)])
        third = case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [str(second), str(third)])

    def test_same_face_after_one_text_is_not_recalled(self):
        case = _DupFaceHarness()
        case.send([_face_seg()])
        case.send([_text_seg("在吗")])
        case.send([_face_seg()])
        self.assertEqual(case.api.recalled, [])

    def test_back_to_back_same_face_is_recalled(self):
        case = _DupFaceHarness()
        case.send([_face_seg()])
        second = case.send([_face_seg()])
        self.assertEqual(case.api.recalled, [str(second)])

    def test_other_sender_same_image_is_not_recalled(self):
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A)])
        case.send([_image_seg(_HASH_A)], user_id=_OTHER)
        self.assertEqual(case.api.recalled, [])

    def test_same_message_redelivery_is_ignored(self):
        case = _DupFaceHarness()
        first = case._raw([_image_seg(_HASH_A)], _SENDER, _GROUP)
        case.deliver(first)
        case.deliver(first)
        self.assertEqual(case.api.recalled, [])

    def test_single_image_in_one_message_is_not_recalled(self):
        case = _DupFaceHarness()
        case.send([_image_seg(_HASH_A), _image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])

    def test_disabled_group_does_not_recall(self):
        case = _DupFaceHarness(enabled=False)
        case.send([_image_seg(_HASH_A)])
        case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])

    def test_recall_failure_is_swallowed(self):
        case = _DupFaceHarness(fail=True)
        case.send([_image_seg(_HASH_A)])
        case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])

    def test_stale_fingerprint_outside_window_is_not_recalled(self):
        case = _DupFaceHarness()
        with mock.patch("cop.conversational.time.time", return_value=1000.0):
            case.send([_image_seg(_HASH_A)])
        with mock.patch("cop.conversational.time.time", return_value=1000.0 + 1801):
            case.send([_image_seg(_HASH_A)])
        self.assertEqual(case.api.recalled, [])


if __name__ == "__main__":
    unittest.main()
