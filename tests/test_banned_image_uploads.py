"""Standalone tests for #296 WebUI-uploaded banned images."""

import hashlib
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger(__name__)
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from cop.moderation.image import ImageModeration  # noqa: E402
from cop.runtime import RuntimeState  # noqa: E402


GROUP_DIR = "files/banned_images_group_overrides/templates/group/files"
GLOBAL_DIR = "files/banned_image_files"


class _Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.config = {}
        self.group_files = {}
        self.group_md5s = {}

    def get_group_setting(self, group_id, key, default):
        assert key == "banned_images"
        return self.group_md5s.get(group_id, default)

    def get_group_banned_image_files(self, group_id):
        return list(self.group_files.get(group_id, []))


class BannedImageUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "plugin_data" / "astrbot_plugin_gm"
        store_dir = self.root / "group_admin"
        store_dir.mkdir(parents=True)
        self.store = _Store(store_dir)
        self.runtime = RuntimeState(stats={}, reports={}, max_history=1)
        self.image = ImageModeration(config_store=self.store, onebot_api=None, runtime=self.runtime)
        self.image._download_image = AsyncMock()

    def write_file(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    async def matches(self, group_id, payload, truncated=False):
        self.image._download_image.return_value = payload, truncated
        return await self.image._check_banned_image("ignored", group_id)

    async def test_group_uploads_are_additive_without_discovering_other_files(self):
        global_file = f"{GLOBAL_DIR}/global.png"
        group_file = f"{GROUP_DIR}/first.png"
        another_file = f"{GROUP_DIR}/second.JPEG"
        for rel, content in ((global_file, b"global"), (group_file, b"first"),
                             (another_file, b"second"), (f"{GROUP_DIR}/orphan.png", b"orphan")):
            self.write_file(rel, content)
        self.store.config["banned_image_files"] = [global_file]
        self.store.config["banned_images"] = [hashlib.md5(b"legacy global").hexdigest()]
        self.store.group_files["123"] = [group_file, another_file, group_file]
        self.store.group_md5s["123"] = [hashlib.md5(b"runtime or manual").hexdigest()]

        self.assertEqual(self.image._get_banned_file_md5s(), {hashlib.md5(b"global").hexdigest()})
        self.assertEqual(self.image._get_banned_file_md5s("123"), {
            hashlib.md5(b"first").hexdigest(), hashlib.md5(b"second").hexdigest(),
        })
        self.assertEqual(self.image._get_banned_file_md5s("456"), set())
        for payload in (b"global", b"legacy global", b"runtime or manual", b"first", b"second"):
            with self.subTest(payload=payload):
                self.assertTrue(await self.matches("123", payload))
        for payload in (b"runtime or manual", b"first", b"second", b"orphan"):
            with self.subTest(payload=payload):
                self.assertFalse(await self.matches("456", payload))
        self.assertTrue(await self.matches("456", b"global"))
        self.assertFalse(await self.matches("123", b"orphan"))
        self.assertFalse(await self.matches("123", b"first", truncated=True))
        self.assertFalse(await self.matches("123", None))

    async def test_same_path_can_be_replaced_or_removed_and_config_is_live(self):
        rel = f"{GROUP_DIR}/example.png"
        path = self.write_file(rel, b"old")
        self.store.group_files["123"] = [rel]
        self.assertTrue(await self.matches("123", b"old"))
        with patch.object(self.image, "_hash_banned_path", wraps=self.image._hash_banned_path) as hash_path:
            self.assertTrue(await self.matches("123", b"old"))
            hash_path.assert_not_called()

        replacement = self.write_file(f"{GROUP_DIR}/replacement.png", b"new")
        old_stat = path.stat()
        os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        replacement.replace(path)
        self.assertFalse(await self.matches("123", b"old"))
        self.assertTrue(await self.matches("123", b"new"))

        # 覆盖写入不会改变文件路径或 inode；同大小改写也需重新计算。
        path.write_bytes(b"edi")
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 2_000_000_000))
        self.assertFalse(await self.matches("123", b"new"))
        self.assertTrue(await self.matches("123", b"edi"))
        self.store.group_files["123"].clear()
        self.assertFalse(await self.matches("123", b"edi"))
        self.store.group_files["123"].append(rel)
        path.unlink()
        self.assertFalse(await self.matches("123", b"edi"))
        self.write_file(rel, b"recreated")
        self.assertTrue(await self.matches("123", b"recreated"))

    async def test_invalid_group_references_do_not_hash_and_global_legacy_still_works(self):
        invalid = f"{GROUP_DIR}/wrong.txt"
        valid = f"{GROUP_DIR}/valid.png"
        write_elsewhere = self.write_file("group_admin/legacy.dat", b"legacy")
        self.write_file(invalid, b"txt")
        self.write_file(valid, b"valid")
        self.store.group_files["123"] = [
            None, 123, "", "\x00", f"/{valid}", f"\\{valid}", f"C:/{valid}",
            f"{GROUP_DIR}/../files/valid.png", invalid, "group_admin/legacy.dat",
            str(write_elsewhere), f"{GROUP_DIR}/missing.png",
        ]
        self.assertEqual(self.image._banned_image_file_paths("123"), [])
        self.assertFalse(await self.matches("123", b"legacy"))
        self.store.config["banned_image_files"] = ["\\group_admin\\legacy.dat"]
        self.assertTrue(await self.matches("123", b"legacy"))

    def test_group_symlink_cannot_escape_upload_directory(self):
        rel = f"{GROUP_DIR}/link.png"
        wrong_dir = self.write_file("group_admin/hidden.png", b"hidden")
        link = self.root / rel
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(wrong_dir)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.store.group_files["123"] = [rel]
        self.assertEqual(self.image._get_banned_file_md5s("123"), set())

    async def test_oversized_and_unreadable_uploads_fail_open(self):
        large_rel = f"{GROUP_DIR}/large.png"
        empty_rel = f"{GROUP_DIR}/empty.png"
        broken_rel = f"{GROUP_DIR}/unreadable.png"
        valid_rel = f"{GROUP_DIR}/valid.png"
        self.write_file(large_rel, b"x" * 17)
        self.write_file(empty_rel, b"")
        self.write_file(broken_rel, b"unreadable")
        self.write_file(valid_rel, b"x" * 16)
        self.store.group_files["123"] = [large_rel, empty_rel, broken_rel, valid_rel]
        original_open = Path.open

        def open_file(path, *args, **kwargs):
            if path.name == "unreadable.png":
                raise PermissionError("not readable")
            if path.name in {"large.png", "empty.png"}:
                self.fail(f"should not open {path.name}")
            return original_open(path, *args, **kwargs)

        with (patch("cop.moderation.image._MAX_IMAGE_BYTES", 16),
              patch.object(Path, "open", open_file), patch("cop.moderation.image.logger") as logger):
            self.assertFalse(await self.matches("123", b"x" * 17))
            logger.error.assert_called_once()
            self.assertTrue(await self.matches("123", b"x" * 16))


if __name__ == "__main__":
    unittest.main()
