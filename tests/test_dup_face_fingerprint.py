"""重复表情包指纹提取的回归测试（PR #289 审查：weak-fingerprint-validation）。

md5 字段不是 32 位十六进制时（协议端塞了序号/短标识），不能拿它当内容哈希，
否则不同图片会撞指纹、只发一次的表情被误撤回。
"""

import sys
import types
import unittest
from pathlib import Path


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

from cop.conversational import _image_fingerprint  # noqa: E402


_HASH = "0123456789abcdef0123456789abcdef"


class ImageFingerprintTests(unittest.TestCase):
    def test_valid_md5_field_is_used(self):
        self.assertEqual(_image_fingerprint({"md5": _HASH.upper()}), _HASH)

    def test_non_hash_md5_field_is_ignored(self):
        for bad in ("0", "12", "abc", "0.image", _HASH + "ff"):
            with self.subTest(md5=bad):
                self.assertEqual(
                    _image_fingerprint({"md5": bad, "file": _HASH + ".image"}),
                    _HASH,
                )

    def test_invalid_md5_without_other_source_gives_empty(self):
        # 拿不到真实哈希时宁可不判重
        self.assertEqual(_image_fingerprint({"md5": "0"}), "")
        self.assertEqual(_image_fingerprint({"file": "0.image"}), "")

    def test_file_name_hash_is_used(self):
        self.assertEqual(_image_fingerprint({"file": _HASH + ".jpg"}), _HASH)

    def test_url_hash_is_used(self):
        self.assertEqual(
            _image_fingerprint({"url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-" + _HASH + "/0"}),
            _HASH,
        )

    def test_sequence_file_name_is_not_a_fingerprint(self):
        self.assertEqual(_image_fingerprint({"file": "1.image"}), "")


if __name__ == "__main__":
    unittest.main()
