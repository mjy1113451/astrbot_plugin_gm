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

from unittest.mock import patch  # noqa: E402

from cop.config_store import ConfigRollbackError, ConfigStore  # noqa: E402


class _Config(dict):
    """最小配置桩：支持 dict 读取与 save_config()。"""

    def save_config(self):
        pass


class _FailingConfig(_Config):
    """可切换失败的配置桩：用来验证"框架配置写失败"时的回滚。"""

    fail = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saves = 0

    def save_config(self):
        self.saves += 1
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _MutatingFailingConfig(_FailingConfig):
    """保存时先改内存值再抛错：模拟框架 save_config 半途失败留下的脏配置。"""

    def save_config(self):
        self.saves += 1
        self["dirty_key"] = "new"
        self["whitelist_users"] = ["corrupted"]
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _BrokenRollbackConfig(_MutatingFailingConfig):
    """失败后连赋值都写不进去：用来验证"回滚也失败"走独立异常。"""

    readonly = False

    def save_config(self):
        self["dirty_key"] = "new"
        self["whitelist_users"] = ["corrupted"]
        self.saves += 1
        # 先改脏内存，再变成不可写，最后抛错：回滚必然失败
        self.readonly = True
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")

    def __setitem__(self, key, value):
        if self.readonly and self.fail:
            raise RuntimeError("config 对象已不可写（模拟）")
        super().__setitem__(key, value)


class _WriteThenFailConfig(_FailingConfig):
    """模拟框架 save_config 已经把配置写脏再抛错（落盘/fsync 阶段失败）。"""

    def __init__(self, *args, **kwargs):
        self.config_path = kwargs.pop("config_path", None)
        super().__init__(*args, **kwargs)

    def save_config(self):
        self.saves += 1
        if self.config_path:
            Path(self.config_path).write_text('{"whitelist_users": ["dirty"]}', encoding="utf-8")
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _BypassWriteConfig(_FailingConfig):
    """模拟业务层绕过 set_global_setting 直接往配置对象里写键。

    一个写 schema 声明的键（rank_top_n），一个写框架内部风格的键
    （framework_helper_key），用来验证回滚只删前者。
    """

    def save_config(self):
        self.saves += 1
        self["rank_top_n"] = 99
        self["framework_helper_key"] = "内部键"
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _JsonStore:
    """最小 JSON 存储桩：可切换"写失败"，用来验证 strict 写入的传播。"""

    fail_saves = False

    def load_json(self, path, default):
        path = Path(path)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path, data, strict=False):
        if self.fail_saves:
            # 真实 JsonStore 在 strict=True 时把写入异常抛给调用方
            if strict:
                raise OSError("runtime.json 写入失败（模拟）")
            return
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _template_entry(group_id, **fields):
    entry = {"__template_key": "group_config", "group_id": group_id}
    entry.update(fields)
    return entry


class GroupOverrideTests(unittest.TestCase):
    def make_store(self, config, json_store=None):
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
            json_store=json_store or _JsonStore(),
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

    def test_declaration_scope_is_per_record(self):
        # 声明的作用域是"单条记录级"：它只保护自己这条记录里的值。
        # 前条给值 → 中间只声明不给值（清掉前条、回退全局）→ 后条给默认值
        # （没声明，按未覆盖处理）仍回退全局，不会被中间那条的声明"粘"成覆盖
        # （PR #289 审查：template-declaration-scope-loss）。
        # 用 whitelist_users 做样本：它的模板默认值就是空列表，便于验证
        # "写了但等于默认值 → 仍算未覆盖"这条规则。
        store = self.make_store(
            _Config(
                whitelist_users=["global1"],
                group_overrides_templates=[
                    _template_entry("100", whitelist_users=["first"]),
                    _template_entry("100", explicit_override_fields="whitelist_users"),
                    _template_entry("100", whitelist_users=[]),
                ],
            )
        )
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["global1"]
        )
        # 最终确实没覆盖这个字段，声明也不该留在合并结果里对外声称覆盖了
        entry = store._template_group_entry("100")
        self.assertNotIn("whitelist_users", entry)
        self.assertNotIn("explicit_override_fields", entry)

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
        # 重新添加基线里已有的值 = 撤销删除、恢复基线继承：不写 added，
        # 避免同一值既在基线又在 added（PR #289 审查：duplicate-list-operation）。
        ops = store._list_ops("100", "whitelist_users")
        self.assertEqual(ops["removed"], [])
        self.assertEqual(ops["removed_baseline"], {})
        self.assertEqual(ops["added"], [])
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1", "g2"]
        )
        # 恢复继承后基线删掉 g1：本群也随之下线（不再固定保留）
        config["whitelist_users"] = ["g2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g2"]
        )

    def test_cancelled_list_ops_leave_no_materialized_snapshot(self):
        # 删掉再加回同一基线项后增量记录清零；此时运行时里不能留下物化快照，
        # 否则这份快照会把"抵消那一刻"的基线冻结下来，之后基线怎么变都传不到
        # 本群（PR #289 审查：stale-materialized-snapshot）。
        config = _Config(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        store.mutate_group_list("100", "whitelist_users", "add", "g1")
        runtime = store.runtime_map("group_overrides")
        self.assertNotIn("100", runtime)
        # 落盘内容同样干净，重启后不会读到冻结快照
        saved = json.loads(Path(store.runtime_path).read_text(encoding="utf-8"))
        self.assertNotIn("100", saved.get("group_overrides", {}))
        # 基线新增的值直接生效，说明读取走的是实时基线而不是快照
        config["whitelist_users"] = ["g1", "g2", "g3"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1", "g2", "g3"]
        )
        # 基线再加回 g1：本群重新拿到它，且只出现一次
        config["whitelist_users"] = ["g1", "g2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1", "g2"]
        )
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users").count("g1"), 1
        )

    def test_explicit_snapshot_is_not_reattached_to_baseline(self):
        # set_group_override 是显式整体赋值：之后模板/全局怎么变都以这份快照为
        # 基线，增删只在快照上叠加，不能被重新挂回模板/全局
        # （PR #289 审查：explicit-snapshot-semantics-loss）。
        config = _Config(whitelist_users=["g1"])
        store = self.make_store(config)
        store.set_group_override("100", "whitelist_users", ["s1", "s2"])
        config["whitelist_users"] = ["g1", "g2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["s1", "s2"]
        )
        ok, current = store.mutate_group_list("100", "whitelist_users", "add", "s3")
        self.assertTrue(ok)
        self.assertEqual(current, ["s1", "s2", "s3"])
        # 全局再改也不影响本群：基线是快照而不是全局名单
        config["whitelist_users"] = ["g3"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["s1", "s2", "s3"]
        )
        store.mutate_group_list("100", "whitelist_users", "remove", "s1")
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["s2", "s3"]
        )
        # 清除覆盖（值 + 增量 + 快照标记）后回到跟随全局
        self.assertTrue(store.drop_group_override("100", "whitelist_users"))
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g3"]
        )

    def test_legacy_plain_snapshot_survives_first_add(self):
        # 升级前 runtime.json 里的普通列表快照（没有 __list_ops__）首次被增删时
        # 认作显式整体快照继承下来，不能用"模板/全局基线 + 本次新增"把它覆盖掉
        # （PR #289 审查：legacy-data-loss）。
        config = _Config(whitelist_users=["global1"])
        store = self.make_store(config)
        store.runtime_map("group_overrides")["100"] = {
            "whitelist_users": ["old1", "old2"]
        }
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["old1", "old2"]
        )
        ok, current = store.mutate_group_list("100", "whitelist_users", "add", "new1")
        self.assertTrue(ok)
        self.assertEqual(current, ["old1", "old2", "new1"])
        # 旧快照里没有的全局项不会随迁移冒出来
        self.assertNotIn("global1", current)
        ops = store._list_ops("100", "whitelist_users")
        # 历史项不算"管理员显式新增"，只有本次真正新增的才记 added
        # （PR #289 审查：legacy-snapshot-added-semantics）
        self.assertEqual(ops["added"], ["new1"])
        self.assertEqual(ops["removed"], [])
        self.assertTrue(store._is_explicit_snapshot("100", "whitelist_users"))

    def test_legacy_snapshot_stays_independent_of_later_baseline(self):
        # 迁移后的旧快照是"本群自己的完整列表"（旧版本行为）：全局以后删掉
        # 同名项、或新增别的项，都不会改变本群这份列表
        # （PR #289 审查：legacy-snapshot-added-semantics）。
        config = _Config(whitelist_users=["global1", "shared"])
        store = self.make_store(config)
        store.runtime_map("group_overrides")["100"] = {
            "whitelist_users": ["shared", "old1"]
        }
        store.mutate_group_list("100", "whitelist_users", "add", "new1")
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"),
            ["shared", "old1", "new1"],
        )
        # 全局删掉 shared、新增 global2：本群列表原样不动
        config["whitelist_users"] = ["global2"]
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"),
            ["shared", "old1", "new1"],
        )

    def test_legacy_plain_snapshot_survives_first_remove(self):
        config = _Config(whitelist_users=["global1", "global2"])
        store = self.make_store(config)
        store.runtime_map("group_overrides")["100"] = {
            "whitelist_users": ["global1", "global2", "old1"]
        }
        ok, current = store.mutate_group_list(
            "100", "whitelist_users", "remove", "global2"
        )
        self.assertTrue(ok)
        self.assertEqual(current, ["global1", "old1"])
        # 快照中的自定义项仍然在，只有被点名删除的项消失
        self.assertIn("old1", current)

    def test_runtime_write_failure_aborts_without_touching_config(self):
        # runtime.json 写失败必须被感知：不能回执已生效、重启后丢数据
        # （PR #289 审查：missing-error-handling）。
        config = _FailingConfig(whitelist_users=["g1"])
        json_store = _JsonStore()
        json_store.fail_saves = True
        store = self.make_store(config, json_store=json_store)
        with self.assertRaises(OSError):
            store.mutate_group_list("100", "whitelist_users", "add", "new1")
        # 框架配置没有被提交，运行时覆盖也回滚了
        self.assertEqual(config.saves, 0)
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1"]
        )

    def test_save_config_failure_restores_runtime_file(self):
        # 框架配置写失败时必须把已落盘的 runtime.json 恢复原样，否则磁盘上
        # 运行时数据领先于配置（PR #289 审查：non-atomic-persistence）。
        config = _FailingConfig(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        before = store.runtime_path.read_text(encoding="utf-8")
        config.fail = True
        with self.assertRaises(OSError):
            store.mutate_group_list("100", "whitelist_users", "remove", "g2")
        self.assertEqual(store.runtime_path.read_text(encoding="utf-8"), before)

    def test_save_config_read_failure_aborts_without_touching_file(self):
        # 备份都读不到时不能继续写：否则之后的回滚会删掉一个原本存在的文件
        # （PR #289 审查：incorrect-rollback-deletion）。
        config = _FailingConfig(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        before = store.runtime_path.read_text(encoding="utf-8")
        config.fail = True
        with patch.object(Path, "read_text", side_effect=OSError("磁盘读取失败")):
            with self.assertRaises(OSError):
                store.mutate_group_list("100", "whitelist_users", "remove", "g2")
        self.assertTrue(store.runtime_path.exists())
        self.assertEqual(store.runtime_path.read_text(encoding="utf-8"), before)

    def test_batch_add_saves_once(self):
        config = _FailingConfig(whitelist_users=[])
        store = self.make_store(config)
        changed, latest = store.mutate_group_list_batch(
            "100", "whitelist_users", add=["a", "b", "c"])
        self.assertTrue(changed)
        self.assertEqual(latest, ["a", "b", "c"])
        self.assertEqual(config.saves, 1)

    def test_batch_failure_rolls_back_everything(self):
        # 批量变更必须整体成功或整体失败，不能留下"写了一半"
        # （PR #289 审查：partial-batch-persistence）。
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        config.fail = True
        with self.assertRaises(OSError):
            store.mutate_group_list_batch("100", "whitelist_users", add=["a", "b"])
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["g1"])

    def test_batch_without_changes_does_not_write(self):
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        changed, latest = store.mutate_group_list_batch(
            "100", "whitelist_users", add=["g1"], remove=["nope"])
        self.assertFalse(changed)
        self.assertEqual(latest, ["g1"])
        self.assertEqual(config.saves, 0)

    def test_save_config_success_keeps_runtime_file_updated(self):
        config = _FailingConfig(whitelist_users=["g1", "g2"])
        store = self.make_store(config)
        ok, _ = store.mutate_group_list("100", "whitelist_users", "remove", "g1")
        self.assertTrue(ok)
        self.assertIn("100", store.runtime_path.read_text(encoding="utf-8"))

    def test_framework_config_memory_is_restored_key_by_key(self):
        # 框架 save_config 半途改了内存值再抛错：被改的键要恢复成写前值；
        # 写前不存在的键不能盲删——它可能是框架自己注入的内部/辅助键
        # （PR #289 审查：overbroad-memory-rollback）。
        config = _MutatingFailingConfig(whitelist_users=["g1"])
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(OSError):
            store.save_config()
        self.assertEqual(config["whitelist_users"], ["g1"])
        self.assertIn("dirty_key", config)

    def test_declared_new_key_is_removed_on_failure(self):
        # 插件自己新增的键通过 set_global_setting 显式登记，失败时会被删掉，
        # 不会留下"内存有、磁盘没有"的配置项
        # （PR #289 审查：overbroad-memory-rollback）。
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        config.fail = True
        with self.assertRaises(OSError):
            store.set_global_setting("brand_new_key", 5)
        self.assertNotIn("brand_new_key", config)

    def test_declared_existing_key_is_restored_on_failure(self):
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        config.fail = True
        with self.assertRaises(OSError):
            store.set_global_setting("whitelist_users", ["changed"])
        self.assertEqual(config["whitelist_users"], ["g1"])

    def test_unregistered_schema_key_is_removed_on_failure(self):
        # 业务层绕过 set_global_setting 直接写配置时，回滚仍能收拾"schema 内的
        # 新增键"，但不会误删框架注入的内部键
        # （PR #289 审查：unregistered-global-write）。
        config = _BypassWriteConfig(whitelist_users=["g1"])
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(OSError):
            store.save_config()
        self.assertNotIn("rank_top_n", config)
        self.assertIn("framework_helper_key", config)

    def test_rollback_failure_raises_dedicated_error(self):
        # 连回滚都失败时不能只记日志：调用方必须知道"配置可能不可信"
        # （PR #289 审查：incomplete-memory-rollback）。
        config = _BrokenRollbackConfig(whitelist_users=["g1"])
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(ConfigRollbackError):
            store.save_config()

    def test_set_group_override_rolls_back_on_save_failure(self):
        # 整体赋值写入后保存失败：内存里的运行时覆盖必须回滚，本群新建的条目
        # 也要删掉，否则命令报错但进程内配置已经变了
        # （PR #289 审查：missing-rollback）。
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        config.fail = True
        with self.assertRaises(OSError):
            store.set_group_override("100", "whitelist_users", ["s1"])
        self.assertNotIn("100", store.runtime_map("group_overrides"))
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["g1"]
        )

    def test_drop_group_override_rolls_back_on_save_failure(self):
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        store.set_group_override("100", "whitelist_users", ["s1"])
        config.fail = True
        with self.assertRaises(OSError):
            store.drop_group_override("100", "whitelist_users")
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["s1"]
        )

    def test_framework_config_file_restored_after_partial_write(self):
        # 框架 save_config 可能已经把文件写脏再抛错：只恢复内存不算完，
        # 磁盘上的框架配置也要按原文写回
        # （PR #289 审查：incomplete-persistence-rollback）。
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        original = '{"whitelist_users": ["g1"]}'
        path.write_text(original, encoding="utf-8")
        config = _WriteThenFailConfig(whitelist_users=["g1"], config_path=path)
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(OSError):
            store.save_config()
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_later_declaration_without_value_drops_declaration(self):
        # 后条声明了字段却没给值：字段值与声明都要去掉，条目不再声称覆盖它
        # （PR #289 审查：template-override-scope-loss）。
        store = self.make_store(
            _Config(
                ad_keywords=["global_ad"],
                group_overrides_templates=[
                    _template_entry(
                        "100",
                        ad_keywords=["first"],
                        explicit_override_fields="ad_keywords",
                    ),
                    _template_entry("100", explicit_override_fields="ad_keywords"),
                ],
            )
        )
        entry = store._template_group_entry("100")
        self.assertNotIn("ad_keywords", entry)
        self.assertNotIn("explicit_override_fields", entry)
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["global_ad"])

    def test_third_entry_after_empty_declaration_wins(self):
        # 前条有值并声明 → 后条只声明无值 → 第三条给值：最终取第三条
        store = self.make_store(
            _Config(
                ad_keywords=["global_ad"],
                group_overrides_templates=[
                    _template_entry(
                        "100",
                        ad_keywords=["first"],
                        explicit_override_fields="ad_keywords",
                    ),
                    _template_entry("100", explicit_override_fields="ad_keywords"),
                    _template_entry("100", ad_keywords=["third"]),
                ],
            )
        )
        self.assertEqual(store.get_group_setting("100", "ad_keywords"), ["third"])
        entry = store._template_group_entry("100")
        self.assertEqual(entry["ad_keywords"], ["third"])

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
