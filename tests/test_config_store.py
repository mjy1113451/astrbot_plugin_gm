"""ConfigStore 按群覆盖的回归测试（PR #289 审查：config-override-data-loss /
config-override-cannot-clear）。

只依赖标准库 + cop 包本身，用最小桩替换 astrbot.api.logger。
"""

import copy
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

from unittest.mock import DEFAULT, patch  # noqa: E402
from collections import UserDict  # noqa: E402

from cop.config_store import ConfigRollbackError, ConfigStore  # noqa: E402
from cop.permissions import PermissionService  # noqa: E402


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


class _DropKeyFailingConfig(_FailingConfig):
    """保存时先删掉一个原有键再抛错：验证回滚会把被删的键补回来。"""

    def save_config(self):
        self.saves += 1
        self.pop("rank_top_n", None)
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


class _MigrationConfig(_FailingConfig):
    failure = ""
    failure_on_save = 1

    def __init__(self, config_path, **settings):
        super().__init__(**settings)
        self.save_snapshots = []
        self.disk_before_saves = []
        self.config_path = config_path
        config_path.write_bytes(
            json.dumps(self, indent=2).replace("\n", "\r\n").encode("utf-8-sig")
        )

    def __setitem__(self, key, value):
        if self.failure == "ignore_restore" and self.saves and key == "group_overrides_templates":
            return
        super().__setitem__(key, value)

    def save_config(self):
        self.saves += 1
        self.save_snapshots.append(copy.deepcopy(dict(self)))
        self.disk_before_saves.append(json.loads(self.config_path.read_bytes()))
        failure = self.failure if self.saves == self.failure_on_save else ""
        if failure == "silent":
            return
        if failure in ("partial", "ignore_restore"):
            rows = self.get("group_overrides_templates", [])
            rows[0]["blacklisted_users"].append("99999")
            self.pop("group_overrides_templates", None)
            self.config_path.write_bytes(b"{broken")
            raise OSError("config.json 部分写入后失败（模拟）")
        self.config_path.write_text(json.dumps(self, ensure_ascii=False), encoding="utf-8-sig")


class _BypassWriteConfig(_FailingConfig):
    """模拟框架在 save_config() 执行过程中往配置对象里注入键。

    一个注入 schema 声明过的键（rank_top_n），一个注入框架内部风格的键
    （framework_helper_key）：两者都必须保留——回滚只删插件显式登记过的
    新增键（PR #289 审查：overbroad-schema-rollback）。
    """

    def save_config(self):
        self.saves += 1
        self["rank_top_n"] = 99
        self["framework_helper_key"] = "内部键"
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _CountingRollbackConfig(_FailingConfig):
    """记录每次赋值，用来验证同一个键在回滚里只被写一次。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.writes = []

    def __setitem__(self, key, value):
        self.writes.append((key, copy.deepcopy(value)))
        super().__setitem__(key, value)


class _MappingConfig(UserDict):
    """非 dict 的 Mapping 配置桩：验证按 Mapping 协议写入与回滚。"""

    fail = False

    def save_config(self):
        if self.fail:
            raise OSError("config.json 写入失败（模拟）")


class _NonMappingConfig:
    """不支持 Mapping 协议的配置对象：写入必须显式报错，不能静默跳过。"""

    def save_config(self):
        pass


class _JsonStore:
    """最小 JSON 存储桩：可切换"写失败"，用来验证 strict 写入的传播。"""

    fail_saves = False
    saves = 0

    def load_json(self, path, default):
        path = Path(path)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, path, data, strict=False):
        self.saves += 1
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


def make_store(test, config, json_store=None):
    temp_dir = tempfile.TemporaryDirectory()
    test.addCleanup(temp_dir.cleanup)
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


class GroupOverrideTests(unittest.TestCase):
    make_store = make_store

    def test_global_write_failure_before_framework_save_keeps_memory_unchanged(self):
        json_store = _JsonStore()
        config = _FailingConfig(rank_top_n=10)
        store = self.make_store(config, json_store=json_store)
        json_store.fail_saves = True
        with self.assertRaises(OSError):
            store.set_global_setting("rank_top_n", 20)
        self.assertEqual(config["rank_top_n"], 10)
        self.assertEqual(config.saves, 0)

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
        store = self.make_store(_Config(group_overrides_templates=[
            _template_entry("100", group_admin_admins=["1"]),
        ]))
        self.assertEqual(
            store.add_group_override_admins("100", "group_admin_admins", ["1"]), []
        )
        self.assertNotIn("100", store.runtime_map("group_overrides"))
        self.assertEqual(
            store.add_group_override_admins("100", "group_admin_admins", ["2"]), ["2"]
        )
        self.assertEqual(store.get_group_setting("100", "group_admin_admins"), ["1", "2"])

    def test_migrate_legacy_group_admins_without_overwriting_existing_template(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        config = _MigrationConfig(
            Path(temp_dir.name) / "framework.json",
            group_admin_admins_by_group={
                "100": [10001, " 10002 ", "", None],
                "200": "10003,10004",
                "300": '["10005"]',
                "400": ["10006"],
                "500": ["10007"],
                "600": ["10009"],
            },
            group_overrides_templates=[
                _template_entry("100", mute_notice=False, enabled_groups="off", auto_recall_enabled_groups="on"),
                _template_entry("100", group_admin_admins=[], enabled_groups="inherit"),
                _template_entry("400", group_admin_admins=["10008"]),
                _template_entry("500", group_admin_admins=[], explicit_override_fields="group_admin_admins"),
            ],
        )
        json_store = _JsonStore()
        store = self.make_store(config, json_store=json_store)
        self.assertEqual(json_store.saves, 1)
        self.assertEqual(config.saves, 3)
        first = config.save_snapshots[0]
        self.assertEqual(first["group_admin_admins_by_group"]["600"], ["10009"])
        self.assertEqual(config.disk_before_saves[1], first)
        self.assertEqual(config["group_admin_admins_by_group"], {})
        self.assertEqual(config["group_overrides_templates"], [])
        self.assertEqual(len(config["group_admin_admins_group_overrides"]), 6)
        self.assertEqual(json.loads(config.config_path.read_bytes()), config)
        self.assertEqual(store.get_group_scope("100", "enabled_groups"), [])
        self.assertIs(store.get_group_scope("100", "auto_recall_enabled_groups"), True)
        self.assertEqual(store.get_group_override_list("100", "group_admin_admins"), ["10001", "10002"])
        self.assertFalse(store.get_group_setting("100", "mute_notice"))
        self.assertCountEqual(store.get_group_override_list("200", "group_admin_admins"), ["10003", "10004"])
        self.assertEqual(store.get_group_override_list("300", "group_admin_admins"), ["10005"])
        self.assertEqual(store.get_group_override_list("400", "group_admin_admins"), ["10008"])
        self.assertEqual(store.get_group_override_list("500", "group_admin_admins"), [])
        self.assertEqual(store.get_group_override_list("600", "group_admin_admins"), ["10009"])
        self.make_store(config)
        self.assertEqual(config.saves, 3)
        self.assertEqual(config["group_overrides_templates"], [])

    def test_admin_migration_preserves_legacy_when_template_has_no_effective_list(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        key = "group_admin_admins"
        for fields in (
            {},
            {key: []},
            {key: None},
            {key: [None, " ", "not-a-qq"]},
            {key: {"qq": "10002"}},
            {key: [" "], "explicit_override_fields": key},
            {"explicit_override_fields": key},
        ):
            with self.subTest(fields=fields):
                entries = [_template_entry("100", mute_notice=False, **fields)]
                config = _MigrationConfig(path, group_admin_admins_by_group={"100": ["10001"]},
                                          group_overrides_templates=entries)
                store = self.make_store(config)
                saved = json.loads(path.read_bytes())
                self.assertEqual(saved["group_admin_admins_by_group"], {})
                self.assertEqual(saved["group_overrides_templates"], [])
                self.assertEqual(saved[f"{key}_group_overrides"][0]["value"], ["10001"])
                self.assertEqual(store.get_group_override_list("100", key), ["10001"])
                self.assertFalse(store.get_group_setting("100", "mute_notice"))

    def test_admin_migration_uses_final_template_value_not_earlier_row(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        key = "group_admin_admins"
        config = _MigrationConfig(
            Path(temp_dir.name) / "framework.json",
            group_admin_admins_by_group={
                "100": ["10001"], "200": ["20001"], "300": ["30001"], "400": ["40001"],
            },
            group_overrides_templates=[
                _template_entry("100", group_admin_admins=["10002"]),
                _template_entry("100", explicit_override_fields=key),
                _template_entry("200", group_admin_admins=[" 20002 ", None]),
                _template_entry("200", group_admin_admins=[" "]),
                _template_entry("300", group_admin_admins=["30002"]),
                _template_entry("300", group_admin_admins=[], explicit_override_fields=key),
                _template_entry("400", group_admin_admins='["40002"]'),
            ],
        )
        store = self.make_store(config)
        self.assertEqual(store.get_group_override_list("100", key), ["10001"])
        self.assertEqual(store.get_group_override_list("200", key), ["20002"])
        self.assertEqual(store.get_group_override_list("300", key), [])
        self.assertEqual(store.get_group_override_list("400", key), ["40002"])
        self.assertEqual(config["group_admin_admins_by_group"], {})

    def test_admin_migration_without_framework_file_does_not_write(self):
        legacy = {"100": ["10001"], "200": ["20001"]}
        entries = [
            _template_entry("100", group_admin_admins=None),
            _template_entry("200", group_admin_admins=[], explicit_override_fields="group_admin_admins"),
        ]
        config = _FailingConfig(group_admin_admins_by_group=legacy, group_overrides_templates=entries)
        store = self.make_store(config)
        self.assertEqual(config.saves, 0)
        self.assertEqual(config["group_admin_admins_by_group"], legacy)
        self.assertEqual(config["group_overrides_templates"], entries)
        self.assertEqual(store.get_group_override_list("100", "group_admin_admins"), ["10001"])
        self.assertEqual(store.get_group_override_list("200", "group_admin_admins"), [])

    def test_admin_migration_does_not_persist_when_input_cannot_be_validated(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        for legacy, entries, invalid_value in (
            ({"not-a-group": ["10001"]}, [], None),
            ({"100": ["10001"]}, ["invalid-row"], None),
            ({"100": ["10001"]}, [], object()),
        ):
            with self.subTest(legacy=legacy, entries=entries, invalid_value=invalid_value):
                config = _MigrationConfig(path, group_admin_admins_by_group=legacy, group_overrides_templates=entries)
                config["validation_test"] = invalid_value
                original = path.read_bytes()
                store = self.make_store(config)
                self.assertEqual(config.saves, 0)
                self.assertEqual(config["group_admin_admins_by_group"], legacy)
                self.assertEqual(config["group_overrides_templates"], entries)
                self.assertEqual(path.read_bytes(), original)
                group_id = next(iter(legacy))
                self.assertEqual(store.get_group_override_list(group_id, "group_admin_admins"), ["10001"])

    def test_admin_migration_save_failure_restores_both_keys_and_original_file(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        for failure in ("partial", "silent"):
            with self.subTest(failure=failure):
                entries = [_template_entry("100", blacklisted_users=["10002"])]
                config = _MigrationConfig(path, group_admin_admins_by_group={"100": ["10001"]},
                                          group_overrides_templates=entries)
                config.failure = failure
                original = path.read_bytes()
                before = copy.deepcopy(dict(config))
                store = self.make_store(config)
                self.assertEqual(config.saves, 1)
                self.assertEqual(config, before)
                self.assertEqual(entries, before["group_overrides_templates"])
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.get_group_override_list("100", "group_admin_admins"), ["10001"])

    def test_admin_migration_cleanup_failure_preserves_verified_template_and_legacy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        key = "group_admin_admins"
        legacy = {"100": ["10001"]}
        for failure in ("partial", "silent"):
            with self.subTest(failure=failure):
                config = _MigrationConfig(path, group_admin_admins_by_group=legacy,
                                          group_overrides_templates=[_template_entry("100", blacklisted_users=["10002"])])
                config.failure = failure
                config.failure_on_save = 2
                with patch("cop.config_store.logger.warning") as warning:
                    store = self.make_store(config)
                warning.assert_called_once()
                self.assertIn("回滚已验证，保留模板和旧名单并继续加载", warning.call_args.args[0])
                self.assertEqual(config.saves, 2)
                self.assertEqual(config["group_admin_admins_by_group"], legacy)
                self.assertEqual(config, config.save_snapshots[0])
                self.assertEqual(json.loads(path.read_bytes()), config)
                self.assertEqual(store.get_group_override_list("100", key), ["10001"])
                self.make_store(config)
                self.assertEqual(config.saves, 4)
                self.assertEqual(config["group_admin_admins_by_group"], {})
                self.assertEqual(config["group_overrides_templates"], [])
                self.assertEqual(len(config[f"{key}_group_overrides"]), 1)
                self.assertEqual(json.loads(path.read_bytes()), config)

    def test_admin_cleanup_continues_if_restore_raises_after_restoring_file(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        config = _MigrationConfig(path, group_admin_admins_by_group={"100": ["10001"]},
                                  group_overrides_templates=[_template_entry("100", blacklisted_users=["10002"])])
        config.failure = "partial"
        config.failure_on_save = 2
        restore_file = ConfigStore._restore_framework_file

        def restore_then_raise(store, snapshot):
            restore_file(store, snapshot)
            raise RuntimeError("恢复文件后抛出异常（模拟）")

        with patch.object(ConfigStore, "_restore_framework_file", restore_then_raise):
            store = self.make_store(config)
        self.assertEqual(config, config.save_snapshots[0])
        self.assertEqual(json.loads(path.read_bytes()), config)
        self.assertEqual(store.get_group_override_list("100", "group_admin_admins"), ["10001"])

    def test_admin_cleanup_stops_loading_when_either_snapshot_cannot_be_verified(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        for target in ("memory", "disk", "readback"):
            with self.subTest(target=target):
                config = _MigrationConfig(
                    path, group_admin_admins_by_group={"100": ["10001"]},
                    group_overrides_templates=[_template_entry("100", blacklisted_users=["10002"])],
                )
                config.failure = "ignore_restore" if target == "memory" else "partial"
                config.failure_on_save = 2
                read_bytes = Path.read_bytes

                def fail_verification(file_path):
                    data = read_bytes(file_path)
                    if target == "readback" and file_path == path and config.saves == 2 and data != b"{broken":
                        raise OSError("恢复后无法读回（模拟）")
                    return data

                def write_file(file_path, data):
                    if target == "disk" and file_path == path:
                        raise RuntimeError("恢复写入异常（模拟）")
                    return DEFAULT

                with (
                    patch.object(Path, "read_bytes", fail_verification),
                    patch.object(ConfigStore, "_atomic_write_bytes", wraps=ConfigStore._atomic_write_bytes,
                                 side_effect=write_file),
                    patch("cop.config_store.logger.warning") as warning,
                    patch("cop.config_store.logger.error") as error,
                    self.assertRaisesRegex(ConfigRollbackError, "清理旧专项管理员名单失败且无法确认回滚"),
                ):
                    self.make_store(config)
                self.assertEqual(config.saves, 2)
                self.assertIn("已停止加载插件", error.call_args.args[0])
                warning.assert_not_called()
                if target != "memory":
                    self.assertEqual(config, config.save_snapshots[0])
                if target != "disk":
                    self.assertEqual(json.loads(path.read_bytes()), config.save_snapshots[0])

    def test_admin_migration_stops_loading_if_rollback_cannot_be_verified(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        config = _MigrationConfig(path, group_admin_admins_by_group={"100": ["10001"]},
                                  group_overrides_templates=[_template_entry("100", blacklisted_users=["10002"])])
        config.failure = "ignore_restore"
        original = path.read_bytes()
        with self.assertRaises(ConfigRollbackError):
            self.make_store(config)
        self.assertEqual(path.read_bytes(), original)

    def test_admin_migration_stops_loading_if_file_rollback_fails(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        config = _MigrationConfig(path, group_admin_admins_by_group={"100": ["10001"]},
                                  group_overrides_templates=[_template_entry("100", blacklisted_users=["10002"])])
        config.failure = "partial"
        before = copy.deepcopy(dict(config))
        with patch.object(ConfigStore, "_atomic_write_bytes", side_effect=OSError("恢复写入失败")):
            with self.assertRaises(ConfigRollbackError):
                self.make_store(config)
        self.assertEqual(config, before)

    def test_special_admin_permission_and_list_edits_stay_in_their_group(self):
        store = self.make_store(_Config(group_overrides_templates=[
            _template_entry("100", group_admin_admins=["10001"]),
            _template_entry("200", join_notify_admins=["10001"], group_admin_admins=["10002"]),
        ]))
        perms = PermissionService(config_store=store, message_parse=None, onebot_api=None)
        def raw(gid, uid):
            return {"group_id": gid, "user_id": uid, "sender": {"role": "member"}}
        store.runtime_map("group_overrides")["100"] = {"group_admin_admins": []}
        self.assertTrue(perms.has_group_admin_rights("10001", "100", raw("100", "10001")))
        self.assertFalse(perms.has_group_admin_rights("10001", "200", raw("200", "10001")))
        self.assertEqual(store.add_group_override_admins("100", "group_admin_admins", ["10003"]), ["10003"])
        self.assertTrue(perms.has_group_admin_rights("10001", "100", raw("100", "10001")))
        self.assertEqual(store.remove_group_override_admins("100", "group_admin_admins", ["10001", "10003"]),
                         ["10001", "10003"])
        self.assertFalse(perms.has_group_admin_rights("10001", "100", raw("100", "10001")))
        self.assertTrue(perms.has_group_admin_rights("10002", "200", raw("200", "10002")))
        self.assertTrue(store.drop_group_override("100", "group_admin_admins"))
        self.assertTrue(perms.has_group_admin_rights("10001", "100", raw("100", "10001")))

    def test_unified_admins_are_scoped_and_never_promote_legacy_lists(self):
        def row(group_id, value):
            return {"__template_key": "group", "group_id": group_id, "value": value}

        store = self.make_store(_Config(
            plugin_admins_group_overrides=[row("100", ["111", "111"]), row("100", ["invalid"])],
            group_admin_admins_group_overrides=[row("100", ["222"]), row("200", ["222"])],
            join_notify_admins=["333"], report_notify_admins=["444"],
        ))
        perms = PermissionService(config_store=store, message_parse=None, onebot_api=None)

        def raw(gid, uid, role="member"):
            return {"group_id": gid, "user_id": uid, "sender": {"role": role}}

        self.assertEqual(store.get_plugin_admins("100"), ["111"])
        self.assertIsNone(store.get_plugin_admins("200"))
        self.assertTrue(perms._is_authorized(raw("100", "111"), "111"))
        self.assertFalse(perms._is_authorized(raw("200", "111"), "111"))
        self.assertFalse(perms._is_authorized(raw("100", "999"), "111"))
        self.assertFalse(perms._is_authorized(raw("", "111")))
        self.assertFalse(perms._is_authorized(raw("100", "333")))
        self.assertFalse(perms._is_authorized(raw("200", "444")))
        self.assertTrue(perms._is_authorized(raw("200", "999", "admin")))
        self.assertTrue(perms._is_authorized(raw("200", "999", "owner")))
        self.assertTrue(perms.has_group_admin_rights("222", "200", raw("200", "222")))
        self.assertFalse(perms.has_group_admin_rights("222", "200", raw("100", "222")))
        self.assertFalse(perms.has_group_admin_rights("222", "100", raw("100", "222")))
        self.assertFalse(perms.has_group_admin_rights("111", "100", raw("100", "111")))
        self.assertTrue(perms.has_group_nickname_rights("111", "100", raw("100", "111")))
        self.assertFalse(perms.has_group_nickname_rights("222", "100", raw("100", "222")))
        self.assertEqual(store.get_admin_notification_recipients("100", "report_notify_admins"),
                         (["111"], "plugin_admins"))
        self.assertEqual(store.get_admin_notification_recipients("200", "report_notify_admins"),
                         (["444"], "report_notify_admins"))
        self.assertEqual(store.remove_group_override_admins("100", "plugin_admins", ["111"]), ["111"])
        self.assertEqual(store.get_plugin_admins("100"), [])
        self.assertFalse(perms._is_authorized(raw("100", "111")))
        self.assertEqual(store.get_admin_notification_recipients("100", "join_notify_admins"),
                         ([], "plugin_admins"))
        self.assertTrue(store.drop_group_override("100", "plugin_admins"))
        self.assertEqual(store.get_plugin_admins("100"), ["111"])

    def test_group_image_file_rows_are_additive_without_replacing_manual_md5s(self):
        digest = "0" * 32
        def row(group_id, **fields):
            return {"__template_key": "group", "group_id": group_id, **fields}

        store = self.make_store(_Config(banned_images_group_overrides=[
            row("100", value=[digest], files=["one.png"]),
            row("100", files=["one.png", "two.png", 123]),
            row("100", value=[], files=["three.png"]),
            row("200", value=[], files=["other.png"]),
        ]))
        self.assertEqual(store.get_group_banned_image_files("100"), ["one.png", "two.png", "three.png"])
        self.assertEqual(store.get_group_banned_image_files("200"), ["other.png"])
        self.assertEqual(store.get_group_banned_image_files("300"), [])
        self.assertEqual(store.get_group_setting("100", "banned_images"), [digest])
        store.set_group_override("100", "banned_images", [])
        self.assertEqual(store.get_group_setting("100", "banned_images"), [])
        self.assertEqual(store.get_group_banned_image_files("100"), ["one.png", "two.png", "three.png"])

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
        with patch.object(Path, "read_bytes", side_effect=OSError("磁盘读取失败")):
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

    def test_global_setting_rolls_back_only_once(self):
        # set_global_setting 不再自己回滚，全部交给 save_config：同一个键只写回
        # 一次旧值，避免两层回滚把已恢复的旧值又覆盖回新值
        # （PR #289 审查：double-rollback-corruption）。
        config = _CountingRollbackConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        config.fail = True
        with self.assertRaises(OSError):
            store.set_global_setting("whitelist_users", ["changed"])
        writes = [v for k, v in config.writes if k == "whitelist_users"]
        self.assertEqual(writes, [["changed"], ["g1"]])

    def test_keys_injected_during_save_are_kept(self):
        # 保存过程中才出现的键必然是框架自己注入的（业务层的写入发生在快照之前，
        # 已随快照恢复），哪怕它在 _conf_schema.json 里有声明也不能删
        # （PR #289 审查：overbroad-schema-rollback）。
        config = _BypassWriteConfig(whitelist_users=["g1"])
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(OSError):
            store.save_config()
        self.assertIn("rank_top_n", config)
        self.assertIn("framework_helper_key", config)

    def test_mapping_config_write_and_rollback(self):
        # 非 dict 的 Mapping（如 UserDict）同样要能写入、建快照并在失败后回滚
        # （PR #289 审查：unsupported-config-type）。
        config = _MappingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        store.set_global_setting("rank_top_n", 30)
        self.assertEqual(config["rank_top_n"], 30)
        config.fail = True
        with self.assertRaises(OSError):
            store.set_global_setting("whitelist_users", ["changed"])
        self.assertEqual(config["whitelist_users"], ["g1"])

    def test_non_mapping_config_raises_instead_of_silent_success(self):
        config = _NonMappingConfig()
        store = self.make_store(config)
        with self.assertRaises(TypeError):
            store.set_global_setting("rank_top_n", 30)

    def test_key_deleted_during_save_is_restored(self):
        # 框架保存过程中删掉一个原有键再抛错：回滚要在 config 里把缺的键补回来，
        # 不能只恢复"还在但值变了"的键，否则内存里会少一个配置项
        # （PR #289 审查：incomplete-memory-rollback）。
        config = _DropKeyFailingConfig(whitelist_users=["g1"], rank_top_n=20)
        config.fail = True
        store = self.make_store(config)
        with self.assertRaises(OSError):
            store.save_config()
        self.assertIn("rank_top_n", config)
        self.assertEqual(config["rank_top_n"], 20)
        self.assertEqual(config["whitelist_users"], ["g1"])

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

    def test_drop_group_overrides_rolls_back_on_save_failure(self):
        # 整群清除也是事务：保存失败必须把本群覆盖整体恢复，否则内存里已经
        # 没有本群配置、磁盘上还在，命令却报"已清除"
        # （PR #289 审查：missing-rollback）。
        config = _FailingConfig(whitelist_users=["g1"])
        store = self.make_store(config)
        store.set_group_override("100", "whitelist_users", ["s1"])
        config.fail = True
        with self.assertRaises(OSError):
            store.drop_group_overrides("100")
        self.assertIn("100", store.runtime_map("group_overrides"))
        self.assertEqual(
            store.get_group_setting("100", "whitelist_users"), ["s1"]
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

    def test_template_schema_covers_group_settings_only(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        template_list = schema["group_overrides_templates"]
        self.assertEqual(template_list["type"], "template_list")
        self.assertEqual(template_list["default"], [])
        self.assertTrue(schema["group_admin_admins_by_group"]["invisible"])
        items = template_list["templates"]["group_config"]["items"]
        self.assertEqual(items["group_id"]["type"], "string")
        dedicated_defaults = {
            "enabled_groups": "", "auto_recall_enabled_groups": "",
            "group_admin_admins": [], "mute_kick_threshold": 0, "violation_keywords": [],
            "banned_images": [], "profanity_keywords": [],
        }
        global_only = {
            "group_admin_admins_by_group", "api_type", "api_endpoint", "api_key",
            "model_name", "detection_prompt", "threshold", "check_porn", "check_sexy",
            "political_keywords", "political_ban_duration", "banned_image_files",
            "voice_asr_endpoint", "voice_asr_api_key", "voice_asr_model", "voice_check_timeout",
            "group_overrides_templates",
        }
        suffix = "_group_overrides"
        lists = {key.removesuffix(suffix): spec for key, spec in schema.items() if key.endswith(suffix)}
        self.assertTrue(template_list["invisible"])
        self.assertEqual(lists.keys(), (items.keys() - {"group_id", "explicit_override_fields"}) | {"plugin_admins"})
        for key in ("welcome_enabled", "welcome_message"):
            self.assertNotIn(key, lists)
            self.assertNotIn(key, items)
        for key in ("join_notify_admins", "report_notify_admins", "group_admin_admins"):
            self.assertTrue(lists[key]["invisible"])
            if key in schema:
                self.assertTrue(schema[key]["invisible"])
        self.assertFalse(lists["plugin_admins"].get("invisible", False))
        ordered_keys = list(schema)
        for key, spec in lists.items():
            with self.subTest(setting=key):
                self.assertEqual(spec["type"], "template_list")
                self.assertEqual(spec["default"], [])
                row = spec["templates"]["group"]
                self.assertEqual(row["display_item"], "group_id")
                expected_fields = {"group_id", "value"}
                if key == "banned_images":
                    expected_fields.add("files")
                    self.assertEqual(row["items"]["files"]["type"], "file")
                self.assertEqual(row["items"].keys(), expected_fields)
                self.assertEqual(row["items"]["group_id"]["type"], "string")
                self.assertEqual(row["items"]["value"]["type"], "list" if key == "plugin_admins" else items[key]["type"])
                if key in schema:
                    self.assertEqual(ordered_keys[ordered_keys.index(key) + 1], key + suffix)
        shared = schema.keys() - global_only - dedicated_defaults.keys() - {key + suffix for key in lists}
        self.assertEqual(set(items), shared | dedicated_defaults.keys() | {"group_id", "explicit_override_fields"})
        for key in shared:
            with self.subTest(field=key):
                self.assertEqual(items[key]["default"], schema[key]["default"])
                self.assertEqual(items[key]["type"], schema[key]["type"])
        for key, default in dedicated_defaults.items():
            with self.subTest(field=key):
                self.assertEqual(items[key]["default"], default)
                if key in ("enabled_groups", "auto_recall_enabled_groups"):
                    self.assertEqual(items[key]["options"], ["", "inherit", "on", "off"])

    def test_per_setting_templates_keep_explicit_values_and_command_precedence(self):
        def row(value, gid="100"):
            return {"__template_key": "group", "group_id": gid, "value": value}

        config = _Config(
            rank_top_n=10, mute_notice=True, mute_kick_threshold=5,
            whitelist_users=["10001"], profanity_ban_duration_severity={"mild": 180},
            rank_top_n_group_overrides=[row(20), row(5, "200"), row(30), row(True)],
            mute_notice_group_overrides=[row(False)],
            mute_kick_threshold_group_overrides=[row(0)],
            whitelist_users_group_overrides=[row([])],
            profanity_ban_duration_severity_group_overrides=[row({})],
            group_admin_admins_group_overrides=[row(["10001"]), row([]), row([" "])],
            voice_check_provider_id="global", voice_check_provider_id_group_overrides=[row("")],
        )
        store = self.make_store(config)
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 30)
        self.assertEqual(store.get_group_setting("200", "rank_top_n"), 5)
        self.assertEqual(store.get_group_setting("300", "rank_top_n"), 10)
        for key, value in {"mute_notice": False, "mute_kick_threshold": 0, "whitelist_users": [],
                           "profanity_ban_duration_severity": {}, "voice_check_provider_id": "",
                           "group_admin_admins": []}.items():
            self.assertEqual(store.get_group_setting("100", key), value)
            self.assertIn(key, store.group_override_keys("100"))
        store.set_group_override("100", "rank_top_n", 12)
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 12)
        store.drop_group_override("100", "rank_top_n")
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 30)
        store.mutate_group_list("100", "whitelist_users", "add", "10002")
        config["whitelist_users_group_overrides"] = [row(["10003"])]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["10003", "10002"])

    def test_migrate_per_setting_templates_and_delete_without_reviving_legacy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        config = _MigrationConfig(
            path, rank_top_n=8, mute_notice=False, enabled_groups=["200"],
            group_overrides_templates=[
                _template_entry("100", rank_top_n=20, mute_notice=True, enabled_groups="off",
                                whitelist_users=[], explicit_override_fields="whitelist_users"),
                _template_entry("100", rank_top_n=10, enabled_groups="inherit"),
                _template_entry("200", rank_top_n=25, mute_notice=True),
            ],
            rank_top_n_group_overrides=[{"__template_key": "group", "group_id": "200", "value": 5}],
        )
        store = self.make_store(config)
        self.assertEqual(config["group_overrides_templates"], [])
        self.assertEqual(config, json.loads(path.read_bytes()))
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 20)
        self.assertEqual(store.get_group_setting("200", "rank_top_n"), 5)
        self.assertIs(store.get_group_setting("100", "mute_notice"), False)
        self.assertNotIn("mute_notice_group_overrides", config)
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), [])
        self.assertEqual(store.get_group_scope("100", "enabled_groups"), ["200"])
        config["rank_top_n_group_overrides"] = []
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 8)
        self.assertEqual(store.get_group_setting("200", "rank_top_n"), 8)
        self.make_store(config)
        self.assertEqual(config.saves, 1)

    def test_per_setting_migration_keeps_explicit_values_equal_to_legacy_defaults(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        defaults = {
            "rank_top_n": 10, "dup_face_recall_enabled": False, "mute_kick_threshold": 0,
            "voice_check_provider_id": "", "whitelist_users": [],
            "profanity_ban_duration_severity": {"mild": 180, "medium": 600, "severe": 1800},
        }
        config = _MigrationConfig(
            Path(temp_dir.name) / "framework.json",
            rank_top_n=25, dup_face_recall_enabled=True, mute_kick_threshold=3,
            voice_check_provider_id="global", whitelist_users=["10001"],
            profanity_ban_duration_severity={"mild": 999},
            group_overrides_templates=[
                _template_entry("100", rank_top_n=30),
                _template_entry("100", **defaults, explicit_override_fields=",".join(defaults)),
                _template_entry("100", **defaults),
            ],
        )
        store = self.make_store(config)
        saved = json.loads(config.config_path.read_bytes())
        self.assertEqual(saved["group_overrides_templates"], [])
        reloaded = self.make_store(_Config(saved))
        for key, value in defaults.items():
            with self.subTest(key=key):
                self.assertEqual(saved[f"{key}_group_overrides"][0]["value"], value)
                self.assertEqual(store.get_group_setting("100", key), value)
                self.assertEqual(reloaded.get_group_setting("100", key), value)
                self.assertEqual(reloaded.get_group_setting("200", key), config[key])

    def test_invalid_per_setting_rows_do_not_mask_valid_values_or_defaults(self):
        def row(value, gid="100"):
            return {"__template_key": "group", "group_id": gid, "value": value}

        config = _Config(rank_top_n=10, kick_recall_count=10, link_fetch_timeout=5,
                         whitelist_users=["10001"], ad_keywords=["全局广告词"])
        cases = {
            "rank_top_n": (20, [0, -1, True, 1.5]),
            "kick_recall_count": (50, [0, 51]),
            "link_fetch_timeout": (15, [0, 16]),
            "mute_kick_threshold": (0, [-1]),
            "whitelist_users": (["20001"], [["20001", "bad"], [None], [True], [0]]),
            "ad_keywords": (["推广"], [[" ", "推广"], [{"text": "推广"}]]),
            "banned_images": (["a" * 32], [["abc"], [["a" * 32]]]),
            "link_whitelist": (["*.example.com"], [["https://example.com"], ["bad..com"], ["*."]]),
            "profanity_ban_duration_severity": ({"mild": 180}, [{"mild": 0}, {"severe": True}, {"other": 3}]),
            "enabled_groups": ("off", ["other", "", None]),
        }
        for key, (valid, invalid) in cases.items():
            config[f"{key}_group_overrides"] = [row(valid), *(row(value) for value in invalid)]
        config["rank_top_n_group_overrides"] += [row(77, gid) for gid in (100, "0", "-100", "１００", "01")]
        store = self.make_store(config)
        for key, (valid, _) in cases.items():
            with self.subTest(key=key):
                self.assertEqual(store.get_group_setting("100", key), False if key == "enabled_groups" else valid)
        for gid in ("0", "-100", "１００", "01", "200"):
            self.assertEqual(store.get_group_setting(gid, "rank_top_n"), 10)
        config["whitelist_users_group_overrides"] = [row([None])]
        self.assertEqual(store.get_group_setting("100", "whitelist_users"), ["10001"])

    def test_per_setting_migration_preserves_old_file_when_a_value_cannot_be_converted(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        config = _MigrationConfig(path, group_overrides_templates=[
            _template_entry("100", rank_top_n=20, kick_recall_count=51),
        ])
        original = path.read_bytes()
        store = self.make_store(config)
        self.assertEqual(config.saves, 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("rank_top_n_group_overrides", config)
        self.assertEqual(store.get_group_setting("100", "rank_top_n"), 20)

    def test_per_setting_migration_restores_original_on_failed_save(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "framework.json"
        for failure in ("silent", "partial"):
            with self.subTest(failure=failure):
                config = _MigrationConfig(path, rank_top_n=8,
                                          group_overrides_templates=[_template_entry("100", rank_top_n=20)])
                original = path.read_bytes()
                before = copy.deepcopy(dict(config))

                def fail_save():
                    if failure == "partial":
                        path.write_bytes(b"{broken")
                        raise OSError("save failed")

                with patch.object(config, "save_config", side_effect=fail_save):
                    store = self.make_store(config)
                self.assertEqual(config, before)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(store.get_group_setting("100", "rank_top_n"), 20)

    def test_webui_row_defaults_do_not_override_existing_config(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        items = schema["group_overrides_templates"]["templates"]["group_config"]["items"]
        entry = {key: spec["default"] for key, spec in items.items()}
        entry.update(__template_key="group_config", group_id="100")
        store = self.make_store(_Config(
            enabled_groups=["200"], profanity_keywords=["全局关键词"], admin_bypass=False,
            group_overrides_templates=[_template_entry("100", enabled_groups="off"), entry],
        ))
        self.assertIs(store.get_group_scope("100", "enabled_groups"), False)
        self.assertEqual(store.get_group_setting("100", "profanity_keywords"), ["全局关键词"])
        self.assertFalse(store.get_group_setting("100", "admin_bypass"))
        self.assertEqual(store.group_override_keys("100"), ["enabled_groups"])

    def test_scope_dropdown_uses_last_valid_state_and_preserves_legacy_overrides(self):
        for key in ("enabled_groups", "auto_recall_enabled_groups"):
            with self.subTest(field=key):
                config = _Config({
                    key: ["200"],
                    "group_overrides_templates": [
                        _template_entry("100", **{key: "on"}),
                        _template_entry("100", **{key: "inherit"}),
                        _template_entry("100", **{key: "invalid"}),
                        _template_entry("100", **{key: ""}, explicit_override_fields=key),
                        _template_entry("100", explicit_override_fields=key),
                        _template_entry("200", **{key: "inherit"}),
                        _template_entry("200", **{key: "off"}),
                        _template_entry("300"),
                    ],
                })
                store = self.make_store(config)
                self.assertEqual(store._template_group_entry("100")[key], "inherit")
                self.assertNotIn(key, store._template_group_entry("300"))
                self.assertEqual(store.get_group_scope("100", key), ["200"])
                self.assertIs(store.get_group_scope("200", key), False)
                self.assertEqual(store.get_group_scope("300", key), ["200"])
                self.assertEqual(store.group_override_keys("100"), [])
                for global_range in (["*"], ["all"], []):
                    config[key] = global_range
                    self.assertEqual(store.get_group_scope("100", key), global_range)
                config[key] = ["200"]
                store.set_group_override("100", key, False)
                self.assertIs(store.get_group_scope("100", key), False)
                store.set_group_override("100", key, ["400"])
                self.assertEqual(store.get_group_scope("100", key), ["400"])
                self.assertEqual(store.get_group_scope("100", key, allow_runtime_list=False), ["200"])
                self.assertEqual(store.group_override_keys("100"), [key])

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
