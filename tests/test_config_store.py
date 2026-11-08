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

    def test_multi_entry_merge_does_not_overwrite_with_defaults(self):
        # 同一群拆两条模板记录、字段互不重叠：后一条仍是默认值的字段
        # 不能把前一条已配置的值抹回默认（PR #289 审查：
        # template-merge-default-overwrite）。
        store = self.make_store(
            _Config(
                blacklisted_users=["global_black"],
                ad_keywords=["global_ad"],
                group_overrides_templates=[
                    _template_entry("100", blacklisted_users=["tpl_black"]),
                    _template_entry(
                        "100",
                        blacklisted_users=[],  # 仍是模板默认值 → 视为未覆盖
                        ad_keywords=["tpl_ad"],
                    ),
                ],
            )
        )
        self.assertEqual(
            store.get_group_setting("100", "blacklisted_users"), ["tpl_black"]
        )
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["tpl_ad"])

    def test_multi_entry_merge_keeps_explicit_override(self):
        # 前一条用 explicit_override_fields 声明的空列表意图，不能被后一条
        # 记录里的默认值/explicit_override_fields 冲掉。
        store = self.make_store(
            _Config(
                blacklisted_users=["global_black"],
                group_overrides_templates=[
                    _template_entry(
                        "100",
                        blacklisted_users=[],
                        explicit_override_fields="blacklisted_users",
                    ),
                    _template_entry("100", ad_keywords=["tpl_ad"]),
                ],
            )
        )
        self.assertEqual(store.get_group_setting("100", "blacklisted_users"), [])

    def test_later_explicit_declaration_without_value_clears_earlier(self):
        # 后一条只声明 explicit_override_fields、没给值时，代表"以后条为准"，
        # 前一条的值必须作废并回退全局
        # （PR #289 审查：template-explicit-override-lost）。
        store = self.make_store(
            _Config(
                ad_keywords=["global_ad"],
                group_overrides_templates=[
                    _template_entry("100", ad_keywords=["first"]),
                    _template_entry("100", explicit_override_fields="ad_keywords"),
                ],
            )
        )
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["global_ad"])

    def test_later_explicit_declaration_does_not_block_later_value(self):
        # 声明后又有值：仍以最后一个带值的条目为准。
        store = self.make_store(
            _Config(
                ad_keywords=["global_ad"],
                group_overrides_templates=[
                    _template_entry("100", ad_keywords=["first"]),
                    _template_entry("100", explicit_override_fields="ad_keywords"),
                    _template_entry("100", ad_keywords=["third"]),
                ],
            )
        )
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["third"])

    def test_present_fields_excludes_meta_keys(self):
        # 内部元数据不能混进"写过哪些字段"的记录里
        # （PR #289 审查：template-explicit-override-lost）。
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry(
                        "100", ad_keywords=["tpl_ad"], explicit_override_fields="ad_keywords"
                    )
                ],
            )
        )
        entry = store._template_group_entry("100")
        present = entry["__present_fields__"]
        self.assertEqual(present, ["ad_keywords"])
        for meta in ("group_id", "explicit_override_fields", "__template_key"):
            self.assertNotIn(meta, present)

    def test_multi_entry_merge_later_non_default_wins(self):
        # 两条都真正改了同一字段时，取后一条（保持“后面的覆盖前面的”语义）。
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry("100", ad_keywords=["first"]),
                    _template_entry("100", ad_keywords=["second"]),
                ]
            )
        )
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["second"])

    def test_merged_entry_tracks_present_fields(self):
        # 合并结果要能区分「条目里没写这个字段」与「写了但仍是默认值」
        store = self.make_store(
            _Config(
                group_overrides_templates=[
                    _template_entry("100", ad_keywords=["a"], mute_notice=True)
                ]
            )
        )
        entry = store._template_group_entry("100")
        self.assertIn("ad_keywords", entry)
        self.assertNotIn("mute_notice", entry)  # 等于默认值 → 不参与覆盖
        self.assertIn("mute_notice", entry["__present_fields__"])
        self.assertNotIn("blacklisted_users", entry["__present_fields__"])

    def test_runtime_list_follows_later_template_change(self):
        # 指令增删过一次之后，WebUI 模板后来的改动仍要生效
        # （PR #289 审查：config-override-precedence-loss）。
        config = _Config(
            whitelist_users=["g1"],
            group_overrides_templates=[_template_entry("100", whitelist_users=["t1"])],
        )
        store = self.make_store(config)
        ok, current = store.mutate_group_list("100", "whitelist_users", "add", "u1")
        self.assertTrue(ok)
        self.assertEqual(current, ["t1", "u1"])
        config["group_overrides_templates"] = [
            _template_entry("100", whitelist_users=["t1", "t2"])
        ]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["t1", "t2", "u1"]
        )
        self.assertEqual(
            store.runtime_group_list("100", "whitelist_users"), ["t1", "t2", "u1"]
        )

    def test_runtime_list_removal_survives_template_change(self):
        config = _Config(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, current = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        self.assertEqual(current, ["g2"])
        # 全局后来加了 g3：本群仍应保留删除意图，同时拿到新加的 g3
        config["whitelist_users"] = ["g1", "g2", "g3"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g2", "g3"]
        )

    def test_removal_of_baseline_item_survives_baseline_remove_and_readd(self):
        # 本群删的是基线里的项 → 之后全局删了又加回来，本群仍然保持删除
        # （PR #289 审查：stale-list-operation-conflict）。
        config = _Config(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        config["whitelist_users"] = ["g2"]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g2"])
        config["whitelist_users"] = ["g1", "g2"]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g2"])

    def test_removal_of_group_added_item_follows_later_baseline(self):
        # 本群自己加进去又删掉的项，记录的是"删除时基线里没有"，全局后来新增
        # 同名项时仍然生效，不会因为一次删除就永久屏蔽
        # （PR #289 审查：stale-list-operation-conflict）。
        config = _Config(whitelist_users=["g1"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "add", "x")
        self.assertTrue(ok)
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g1", "x"])
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "x")
        self.assertTrue(ok)
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g1"])
        config["whitelist_users"] = ["g1", "x"]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g1", "x"])

    def test_remove_readd_baseline_item_stays_present(self):
        # 删除基线项 → 重新添加 → 基线再删掉又加回来：按"最后一次操作优先"，
        # 该项仍然是本群显式加入的，最终只出现一次
        # （PR #289 审查：list-operation-state-loss）。
        config = _Config(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g2"])
        ok, _ = store.mutate_group_list("100", "whitelist_users", "add", "g1")
        self.assertTrue(ok)
        # 重新添加后 removed / removed_baseline 一起清掉，只剩 added
        ops = store._list_ops("100", "whitelist_users")
        self.assertEqual(ops["removed"], [])
        self.assertEqual(ops["removed_baseline"], {})
        self.assertEqual(ops["added"], ["g1"])
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1", "g2"]
        )
        # 基线删掉 g1：本群仍通过 added 保留它
        config["whitelist_users"] = ["g2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g2", "g1"]
        )
        # 基线再加回 g1：集合语义下只出现一次
        config["whitelist_users"] = ["g1", "g2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1", "g2"]
        )
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users").count("g1"), 1
        )

    def test_template_defaults_match_global_defaults(self):
        # 模板字段默认值必须与全局同名字段一致，否则"填了非默认值"会被误判成
        # "没覆盖"，或反之（PR #289 审查：template-default-mismatch）。
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        items = schema["group_overrides_templates"]["templates"]["group_config"]["items"]
        meta = {"group_id", "explicit_override_fields"}
        checked = 0
        for key, spec in items.items():
            if key in meta:
                continue
            with self.subTest(field=key):
                self.assertIn(key, schema, "模板字段在全局 schema 里不存在")
                self.assertEqual(
                    spec.get("default"),
                    schema[key].get("default"),
                    "模板字段默认值与全局不一致",
                )
                checked += 1
        self.assertGreater(checked, 40)

    def test_removed_baseline_flag_is_recorded(self):
        config = _Config(whitelist_users=["g1"])
        store = self.make_store(config)
        store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        ops = store._list_ops("100", "whitelist_users")
        self.assertEqual(ops["removed_baseline"], {"g1": True})

    def test_explicit_set_is_authoritative_snapshot(self):
        config = _Config(whitelist_users=["g1"])
        store = self.make_store(config)
        store.set_group_override("100", "whitelist_users", ["x"])
        config["whitelist_users"] = ["g1", "g2"]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["x"])

    def test_list_ops_dedupe_and_keep_order(self):
        # 名单按"值集合"语义：基线重复值只生效一份，添加保持出现顺序
        # （PR #289 审查：lossy-list-diff）。
        config = _Config(ad_keywords=["a", "a", "b"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "ad_keywords", "remove", "b")
        self.assertTrue(ok)
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["a"])
        ok, _ = store.mutate_group_list("100", "ad_keywords", "add", "c")
        self.assertTrue(ok)
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["a", "c"])

    def test_remove_then_readd_restores_baseline_position(self):
        config = _Config(whitelist_users=["a", "b"])
        store = self.make_store(config)
        store.mutate_group_list("100", "whitelist_users", "remove", "a")
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["b"])
        store.mutate_group_list("100", "whitelist_users", "add", "a")
        # 删除意图被撤销，该值回到基线里的位置；列表仍是基线顺序
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["a", "b"]
        )

    def test_list_ops_are_recorded_explicitly(self):
        store = self.make_store(_Config(whitelist_users=["a"]))
        store.mutate_group_list("100", "whitelist_users", "add", "x")
        store.mutate_group_list("100", "whitelist_users", "remove", "a")
        ops = store.runtime_map("group_overrides")["100"]["__list_ops__"]["whitelist_users"]
        self.assertEqual(ops["added"], ["x"])
        self.assertEqual(ops["removed"], ["a"])

    def test_save_failure_rolls_back_runtime_state(self):
        config = _Config(whitelist_users=["g1"])

        def _boom():
            raise RuntimeError("disk full")

        config.save_config = _boom
        store = self.make_store(config)
        with self.assertRaises(RuntimeError):
            store.mutate_group_list("100", "whitelist_users", "add", "x")
        # 落盘失败后运行时覆盖本体已回滚，不会残留"已添加"的假状态
        self.assertNotIn("100", store.runtime_map("group_overrides"))


if __name__ == "__main__":
    unittest.main()
