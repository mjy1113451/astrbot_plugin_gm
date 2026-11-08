"""ConfigStore 按群覆盖的回归测试（PR #289 审查：config-override-data-loss /
config-override-cannot-clear）。

只依赖标准库 + cop 包本身，用最小桩替换 astrbot.api.logger。
"""

import json
import sys
import tempfile
import threading
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

from cop.config_store import ConfigStore  # noqa: E402


class _Config(dict):
    """最小配置桩：支持 dict 读取与 save_config()。"""

    def save_config(self):
        pass


class _JsonStore:
    def load_json(self, path, default):
        path = Path(path)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path, data):
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _template_entry(group_id, **fields):
    entry = {"__template_key": "group_config", "group_id": group_id}
    entry.update(fields)
    return entry


class GroupOverrideTests(unittest.TestCase):
    def make_store(self, config):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        base = Path(temp_dir.name)
        return ConfigStore(
            config=config,
            runtime_path=base / "runtime.json",
            data_dir=base,
            config_path=base / "config.json",
            stats_path=base / "stats.json",
            reports_path=base / "reports.json",
            lock=threading.RLock(),
            json_store=_JsonStore(),
        )

    def test_first_add_keeps_global_list_as_baseline(self):
        store = self.make_store(_Config(blacklisted_users=["global1", "global2"]))
        ok, current = store.mutate_group_list(
            "100", "blacklisted_users", "add", "new1"
        )
        self.assertTrue(ok)
        self.assertEqual(current, ["global1", "global2", "new1"])
        self.assertEqual(
            store.get_group_setting("100", "blacklisted_users"),
            ["global1", "global2", "new1"],
        )

    def test_first_add_keeps_template_list_as_baseline(self):
        store = self.make_store(
            _Config(
                blacklisted_users=["global1"],
                group_overrides_templates=[
                    _template_entry("100", blacklisted_users=["tpl1"])
                ],
            )
        )
        ok, current = store.mutate_group_list(
            "100", "blacklisted_users", "add", "new1"
        )
        self.assertTrue(ok)
        self.assertEqual(current, ["tpl1", "new1"])

    def test_remove_works_on_template_baseline(self):
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry("100", ad_keywords=["a", "b", "c"])
                ]
            )
        )
        ok, current = store.mutate_group_list("100", "ad_keywords", "remove", "b")
        self.assertTrue(ok)
        self.assertEqual(current, ["a", "c"])
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["a", "c"])

    def test_read_only_access_does_not_materialize_override(self):
        store = self.make_store(_Config(blacklisted_users=["global1"]))
        self.assertEqual(
            store.get_group_override_list("100", "blacklisted_users"), ["global1"]
        )
        self.assertNotIn("100", store.runtime_map("group_overrides"))

    def test_template_default_falls_back_to_global(self):
        store = self.make_store(
            _Config(
                blacklisted_users=["global1"],
                group_overrides_templates=[
                    _template_entry("100", blacklisted_users=[])
                ],
            )
        )
        self.assertEqual(
            store.get_group_setting("100", "blacklisted_users"), ["global1"]
        )

    def test_explicit_override_fields_allow_empty_list(self):
        store = self.make_store(
            _Config(
                blacklisted_users=["global1"],
                group_overrides_templates=[
                    _template_entry(
                        "100",
                        blacklisted_users=[],
                        explicit_override_fields="blacklisted_users",
                    )
                ],
            )
        )
        self.assertEqual(store.get_group_setting("100", "blacklisted_users"), [])

    def test_explicit_override_fields_accepts_list_and_separators(self):
        for raw in ("blacklisted_users, ad_keywords", ["blacklisted_users"], "blacklisted_users；ad_keywords"):
            with self.subTest(raw=raw):
                store = self.make_store(
                    _Config(
                        blacklisted_users=["global1"],
                        group_overrides_templates=[
                            _template_entry(
                                "100",
                                blacklisted_users=[],
                                explicit_override_fields=raw,
                            )
                        ],
                    )
                )
                self.assertEqual(
                    store.get_group_setting("100", "blacklisted_users"), []
                )

    def test_runtime_group_list_excludes_global_values(self):
        store = self.make_store(_Config(banned_images=["g1"]))
        self.assertEqual(store.runtime_group_list("100", "banned_images"), [])
        ok, current = store.mutate_group_list("100", "banned_images", "add", "m1")
        self.assertTrue(ok)
        self.assertEqual(current, ["g1", "m1"])
        self.assertEqual(
            store.runtime_group_list("100", "banned_images"), ["g1", "m1"]
        )

    def test_read_then_mutate_keeps_baseline(self):
        store = self.make_store(_Config(whitelist_users=["g1"]))
        # 先只读一次（不应固化快照），再写入仍以全局为基线。
        self.assertEqual(
            store.get_group_override_list("100", "whitelist_users"), ["g1"]
        )
        ok, current = store.mutate_group_list("100", "whitelist_users", "add", "u1")
        self.assertTrue(ok)
        self.assertEqual(current, ["g1", "u1"])

    def test_failed_add_does_not_materialize_override(self):
        store = self.make_store(_Config(whitelist_users=["g1"]))
        ok, current = store.mutate_group_list("100", "whitelist_users", "add", "g1")
        self.assertFalse(ok)
        self.assertEqual(current, ["g1"])
        self.assertNotIn("100", store.runtime_map("group_overrides"))

    def test_failed_remove_does_not_materialize_override(self):
        store = self.make_store(_Config(whitelist_users=["g1"]))
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "none")
        self.assertFalse(ok)
        self.assertNotIn("100", store.runtime_map("group_overrides"))

    def test_admin_add_without_change_does_not_materialize(self):
        store = self.make_store(_Config(group_admin_admins=["1"]))
        self.assertEqual(
            store.add_group_override_admins("100", "group_admin_admins", ["1"]), []
        )
        self.assertNotIn("100", store.runtime_map("group_overrides"))
        self.assertEqual(
            store.add_group_override_admins("100", "group_admin_admins", ["2"]), ["2"]
        )
        self.assertEqual(store.get_group_setting("100", "group_admin_admins"), ["1", "2"])

    def test_runtime_override_wins_over_template(self):
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry("100", mute_notice=False)
                ]
            )
        )
        self.assertFalse(store.get_group_setting("100", "mute_notice"))
        store.set_group_override("100", "mute_notice", True)
        self.assertTrue(store.get_group_setting("100", "mute_notice"))

    def test_other_group_not_affected(self):
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry("100", mute_notice=False)
                ]
            )
        )
        self.assertTrue(store.get_group_setting("200", "mute_notice", True))


if __name__ == "__main__":
    unittest.main()
