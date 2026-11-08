"""L1 配置存储层：运行时映射 + 全局配置的读写与按群覆盖。

迁出自 main.py 原第 319-551 行。ConfigStore 持有：
    (config, runtime_path, data_dir, config_path, stats_path, reports_path, lock, json_store)

设计说明：
- 通过构造注入 JsonStore（L4 门面负责组装），避免 L1 内部同层互相 import。
- 可依赖 L0：从 constants 取 _RUNTIME_MAP_KEYS，从 text_utils 取 _parse_qq_list。
- 原 main.py 中的方法体逻辑逐字保留（含异常兜底、注释语义），仅参数名从 self.xxx 改为存储字段。
"""

import copy
import json
import os
import re
import tempfile

from pathlib import Path

from astrbot.api import logger

from .constants import _RUNTIME_MAP_KEYS
from .text_utils import _parse_qq_list


class ConfigRollbackError(RuntimeError):
    """保存失败后回滚不完整：当前进程的配置可能不可信，需要核对/重载。"""


class ConfigStore:
    """无状态（相对于插件类）的配置/运行时映射存储。"""

    def __init__(
        self,
        *,
        config,
        runtime_path: Path,
        data_dir: Path,
        config_path: Path,
        stats_path: Path,
        reports_path: Path,
        lock,
        json_store,
    ):
        self.config = config
        self.runtime_path = runtime_path
        self.data_dir = data_dir
        self.config_path = config_path
        self.stats_path = stats_path
        self.reports_path = reports_path
        self._lock = lock
        self._json = json_store
        # 运行时动态映射（group_overrides / groups / pending_join_requests）
        self._runtime_maps: dict = self.load_runtime_maps()
        # PR #290：WebUI 按群配置模板（template_list）的字段默认值缓存，
        # 用于区分"字段未动（回退全局）"与"显式覆盖"。
        self._template_field_defaults = self._load_template_field_defaults()
        # 本次保存事务里由插件显式改写的全局配置键：回滚时只删这些键，
        # 不做"当前键集合 − 快照"的差集删除（PR #289 审查：overbroad-memory-rollback）。
        self._touched_keys: tuple = ()
        # _conf_schema.json 声明的顶层键：回滚删除"本次新增键"时用来区分
        # "schema 内的配置键"与"框架注入的内部键"（PR #289 审查：
        # unregistered-global-write）。
        self._schema_keys = self._load_schema_keys()
        # set_global_setting 登记的单键写前值：save_config 的快照是在改动之后
        # 才拍的，靠它才能把回滚目标修正回真正的写前状态
        # （PR #289 审查：double-rollback-corruption）。
        self._touched_prev: dict = {}

    # ===================== 通用 IO（委托 JsonStore） =====================

    def load_json(self, path: Path, default):
        return self._json.load_json(path, default)

    def save_json(self, path: Path, data, strict: bool = False):
        return self._json.save_json(path, data, strict=strict)

    # ===================== 运行时映射 =====================

    @property
    def runtime_maps(self) -> dict:
        """对外暴露运行时映射字典本体（门面直接复用同一对象）。"""
        return self._runtime_maps

    @runtime_maps.setter
    def runtime_maps(self, value: dict) -> None:
        self._runtime_maps = value

    @staticmethod
    def extract_runtime_maps(source, target: dict) -> dict:
        """把 source 中属于运行时映射的键（dict 类型）合并进 target 并返回。"""
        if isinstance(source, dict):
            for key in _RUNTIME_MAP_KEYS:
                value = source.get(key)
                if isinstance(value, dict):
                    target[key] = value
        return target

    def load_runtime_maps(self) -> dict:
        """加载运行时动态映射（group_overrides / groups / pending_join_requests）。

        这些映射以群号 / 消息ID 为键、由群内指令动态增删，**不能**放进框架配置：
        AstrBotConfig 加载时会调用 check_config_integrity，把未在 _conf_schema.json
        声明的子键判为冗余并删除（内存与磁盘文件一起被清空）。
        因此单独存 data/plugin_data/group_admin/runtime.json。

        首次升级若不存在 runtime.json，则从旧版 config.json 迁移一次，并把旧文件
        备份为 .migrated.bak，避免过期数据在下次启动时反覆盖新数据。
        """
        # 迁移旧版 过几个版本记得删除
        maps = {key: {} for key in _RUNTIME_MAP_KEYS}
        if self.runtime_path.exists():
            return self.extract_runtime_maps(self.load_json(self.runtime_path, {}), maps)

        maps = self.extract_runtime_maps(self.load_json(self.config_path, {}), maps)
        try:
            # 迁移是"读旧文件 → 写新文件 → 备份旧文件"，写不成功就不能动旧文件，
            # 否则旧配置只剩 .bak、新配置又是空的（PR #289 审查：missing-error-handling）
            self.save_json(self.runtime_path, maps, strict=True)
        except Exception as e:
            logger.error(f"[群管插件] 迁移运行时配置到 runtime.json 失败，保留旧配置文件: {e}")
            return maps
        if self.config_path.exists():
            try:
                self.config_path.rename(
                    self.config_path.with_name(self.config_path.name + ".migrated.bak")
                )
                logger.info(
                    "[群管插件] 已把旧版 config.json 的运行时配置迁移到 runtime.json。"
                )
            except Exception as e:
                logger.error(f"备份旧配置文件失败: {e}")
        return maps

    def runtime_map(self, key: str) -> dict:
        """取运行时动态映射（缺键时懒创建）；改动后需调用 save_config() 落盘。"""
        value = self._runtime_maps.get(key)
        if not isinstance(value, dict):
            value = {}
            self._runtime_maps[key] = value
        return value

    def save_config(self) -> None:
        """保存配置（两个存储目标，缺一不可）。

        - 运行时动态映射（group_overrides / groups / pending_join_requests）→ runtime.json，
          原子写入见 save_json()；
        - 其余全局键 → 官方 AstrBotConfig.save_config()，与 WebUI 共用
          data/config/<插件>_config.json，重载/重启后依然生效。

        说明：框架在加载带 _conf_schema.json 的插件时必定以 config=<AstrBotConfig>
        注入（star_manager 中 AstrBotConfig(config_path=..., schema=...)），故直接调用
        官方接口，不再做「无 save_config() 时另存本地文件」的假落盘分支。

        两个目标分两步写，中间失败会留下"runtime.json 已更新、框架配置还是旧的"
        状态，因此这里先备份 runtime.json 原文：框架配置写入失败时把它恢复到
        写前内容，保证磁盘与内存一致（PR #289 审查：non-atomic-persistence）。

        runtime.json 本身用 strict 模式写：底层 save_json 默认只记日志不抛出，
        那样"运行时写失败"会被当成成功、命令回执已生效、重启后却丢数据
        （PR #289 审查：missing-error-handling）。

        回滚本身也可能失败：内存按"逐键比对后恢复"而不是 clear()+update()
        整体重写（后者中途报错会留下部分状态），且任何一步回滚失败都改抛
        ConfigRollbackError 说明配置可能不可信，不再让调用方以为只是一次普通的
        保存失败（PR #289 审查：incomplete-memory-rollback）。

        框架配置文件本身也做备份：save_config() 有可能"已经把文件写脏再抛错"
        （例如写完 flush/fsync 阶段失败），只恢复内存和 runtime.json 时磁盘上
        仍是一份坏配置。能拿到 config_path 时按原文原子写回；拿不到（自定义配置
        对象没有该属性）就只能保证内存与 runtime.json 一致，此时若框架已写盘，
        以框架文件为准需要管理员核对（PR #289 审查：incomplete-persistence-rollback）。
        """
        with self._lock:
            # 用哨兵区分"文件本来就不存在"（回滚时删除）与"读到了内容"（回滚时写回）。
            # 其它 OSError 直接抛出：连备份都读不到就无法在失败时恢复，宁可这次
            # 不写，也不能在之后的回滚里删掉一个原本存在的文件
            # （PR #289 审查：incorrect-rollback-deletion）。
            missing = object()
            try:
                prev_runtime = self.runtime_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                prev_runtime = missing
            # 框架配置对象也拍一份快照：save_config() 失败时进程内可能已经改了
            # 值，只恢复 runtime.json 会留下"内存是新的、磁盘是旧的"
            # （PR #289 审查：incomplete-persistence-rollback）。
            prev_global = copy.deepcopy(dict(self.config)) if isinstance(self.config, dict) else None
            prev_global_file = self._snapshot_framework_file()
            touched_keys = tuple(self._touched_keys)
            touched_prev = dict(self._touched_prev)
            self.save_json(self.runtime_path, self._runtime_maps, strict=True)
            try:
                self.config.save_config()
            except Exception as exc:
                failure = self._restore_framework_config(
                    prev_global, touched_keys, touched_prev)
                self._touched_keys = ()
                self._touched_prev = {}
                failure = failure or self._restore_framework_file(prev_global_file)
                try:
                    if prev_runtime is missing:
                        self.runtime_path.unlink(missing_ok=True)
                    else:
                        # 原子写回：临时文件 + os.replace，避免恢复过程中断电/异常
                        # 把备份内容再写坏一半
                        self._atomic_write_text(self.runtime_path, prev_runtime)
                except OSError as e:
                    logger.error(
                        f"[群管插件] 保存框架配置失败后回滚 runtime.json 也失败，"
                        f"磁盘运行时数据可能领先于配置: {e}")
                    failure = failure or e
                if failure is not None:
                    raise ConfigRollbackError(
                        f"保存配置失败且回滚不完整（{failure}），"
                        f"当前进程的配置可能不可信，请重启插件或到 WebUI 核对后重试"
                    ) from exc
                raise
            else:
                self._touched_keys = ()
                self._touched_prev = {}

    def set_global_setting(self, key: str, value) -> None:
        """写入单个全局配置项并持久化；保存失败时由 save_config 统一回滚。

        命令层直接 `self.config[key] = value` 再 save 的话，落盘失败后进程内会
        留下未落盘的新值；这里把"改了哪个键"及其写前值显式登记下来交给
        save_config——因为 save_config 的快照是在本次改动**之后**才拍的，
        自己拿不到旧值。

        回滚只发生在 save_config 里，这里不再二次写值：两层各改一遍很容易把
        已恢复的旧值又覆盖成新值（PR #289 审查：double-rollback-corruption）。
        """
        with self._lock:
            if isinstance(self.config, dict):
                self._touched_prev = {key: (copy.deepcopy(self.config.get(key)), key in self.config)}
            else:
                self._touched_prev = {}
            self._touched_keys = (key,)
            if isinstance(self.config, dict):
                self.config[key] = value
            try:
                self.save_config()
            except Exception:
                self._touched_keys = ()
                self._touched_prev = {}
                raise

    def _snapshot_framework_file(self):
        """备份框架配置文件原文（拿不到路径或读不到内容时返回 None）。"""
        path = getattr(self.config, "config_path", None)
        if not path:
            return None
        try:
            path = Path(path)
            return path, path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"[群管插件] 备份框架配置文件失败，回滚时无法恢复磁盘内容: {e}")
            return None

    def _restore_framework_file(self, snapshot):
        """把框架配置文件恢复成写前原文；返回恢复失败的异常，全部成功返回 None。"""
        if not snapshot:
            return None
        path, text = snapshot
        try:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                self._atomic_write_text(path, text)
        except OSError as e:
            logger.error(f"[群管插件] 回滚框架配置文件失败: {e}")
            return e
        return None

    def _restore_framework_config(self, prev_global, touched=None, touched_prev=None):
        """把框架配置对象恢复成写前快照；返回恢复失败的异常，全部成功返回 None。

        只做两件安全的事：把目标快照里的键改回原值；把"本次新增且可确认属于
        插件"的键删掉——可确认 = 通过 set_global_setting 显式登记，或在
        _conf_schema.json 里有声明（业务层直接改 `self.config` 绕过了登记时
        仍然能收拾掉）。框架自己注入的内部键既不在 touched 里也没有 schema
        声明，因此不会被误删（PR #289 审查：overbroad-memory-rollback /
        unregistered-global-write）。

        touched_prev 是 set_global_setting 记下的"这些键的写前值"：save_config
        的快照是在调用方改完之后才拍的，必须用它把目标快照修正回真正的写前状态，
        否则回滚会把新值当成旧值留下来（PR #289 审查：double-rollback-corruption）。
        """
        if prev_global is None or not isinstance(self.config, dict):
            return None
        target = dict(prev_global)
        for key, info in (touched_prev or {}).items():
            value, had_key = info
            if had_key:
                target[key] = value
            else:
                target.pop(key, None)
        failures = []
        for key, value in target.items():
            try:
                if key in self.config and self.config[key] == value:
                    continue
                self.config[key] = copy.deepcopy(value)
            except Exception as e:
                failures.append(e)
        try:
            new_keys = [k for k in self.config.keys() if k not in target]
        except Exception:
            new_keys = []
        removable = [
            k for k in new_keys
            if k in (touched or ()) or k in self._schema_keys
        ]
        for key in removable:
            try:
                self.config.pop(key, None)
            except Exception as e:
                failures.append(e)
        kept = [k for k in new_keys if k not in removable]
        if kept:
            # 不删：无法区分"我们的新增键"与"框架注入的内部键"
            logger.debug(f"[群管插件] 回滚后仍存在写前没有的配置键（保留未删）: {kept}")
        if failures:
            logger.error(f"[群管插件] 回滚框架配置内存值失败: {failures[0]}")
            return failures[0]
        return None

    @staticmethod
    def _load_schema_keys():
        """读取 _conf_schema.json 的顶层键集合（读不到时返回空集合）。"""
        schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            return frozenset(schema.keys()) if isinstance(schema, dict) else frozenset()
        except Exception as e:
            logger.warning(f"[群管插件] 读取 _conf_schema.json 键集合失败: {e}")
            return frozenset()

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """原子写文本：同目录临时文件写完 fsync 后 os.replace。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def mutate_group_list(self, group_id: str, key: str, op: str, value: str) -> tuple:
        """群 list 配置的原子读-改-写（review#192 race-condition）。

        读生效值 → 判断是否真的会变 → 记录增量操作 → 落盘，全程在 _CFG_LOCK
        内串行化，避免并发覆盖丢失更新；返回 (ok, 当前生效列表拷贝)。

        没有任何变化时不做写入，不留下 group_overrides 痕迹
        （PR #289 审查：unexpected-runtime-materialization）；
        落盘失败会回滚运行时对象（rollback-not-persisted）。
        """
        with self._lock:
            current = self.get_group_override_list(group_id, key)
            if op == "add":
                if any(str(x) == value for x in current):
                    return False, list(current)
                merged = self._commit_list_change(group_id, key, add=[value])
            else:
                if not any(str(x) == value for x in current):
                    return False, list(current)
                merged = self._commit_list_change(group_id, key, remove=[value])
            return True, list(merged)

    def mutate_group_list_batch(self, group_id: str, key: str, add=(), remove=()) -> tuple:
        """一次性增删多个值的原子版本（review#192 / PR#289 partial-batch-persistence）。

        批量名单命令逐项调用 mutate_group_list 时，中途落盘失败会留下"写了一半"
        的状态；这里先在锁内算出真正要变更的集合，再用一次 _commit_list_change
        落盘，失败整体回滚。返回 (是否有变更, 生效列表拷贝)。
        """
        with self._lock:
            current = [str(x) for x in self.get_group_override_list(group_id, key)]
            add_items, remove_items = [], []
            for item in add:
                item = str(item).strip()
                if item and item not in current and item not in add_items:
                    add_items.append(item)
            for item in remove:
                item = str(item).strip()
                if item and item in current and item not in remove_items:
                    remove_items.append(item)
            if not add_items and not remove_items:
                return False, current
            merged = self._commit_list_change(
                group_id, key, add=add_items, remove=remove_items)
            return True, list(merged)

    # ===================== stats / reports =====================

    def save_stats(self, stats) -> None:
        self.save_json(self.stats_path, stats)

    def save_reports(self, reports) -> None:
        self.save_json(self.reports_path, reports)

    # ===================== 配置读取 =====================

    def config_list(self, key: str) -> list:
        """读取配置中的列表项，统一兜底「键缺失 / 值为 None / 类型错误」→ 空列表。"""
        value = self.config.get(key)
        return value if isinstance(value, list) else []

    def warn_if_group_scope_empty(self) -> None:
        """启动告警：群范围类列表留空 = 在【全部群】生效（#192 语义变更）。

        `enabled_groups` 留空（且无历史 `violation_enabled_groups`）时，违规检测会在
        所有群开启；`auto_recall_enabled_groups` 留空时，Bot 发言自动撤回同样全群生效。
        这两点容易踩坑，故每次启动提示一次（最多两条，不会刷屏）。
        """
        enabled_groups = self.config_list("enabled_groups")
        legacy_violation_groups = self.config_list("violation_enabled_groups")
        auto_recall_groups = self.config_list("auto_recall_enabled_groups")

        if not enabled_groups and not legacy_violation_groups:
            logger.warning(
                "[IMPORTANT][群管插件] enabled_groups 为空：按 #192 新语义，违规检测"
                "（含刷屏/图片AI等）将在【全部群】启用。如需限定范围，请配置 "
                "enabled_groups 列表，或通过 group_overrides 将指定群 enabled_groups 设为 false。"
            )
        if not auto_recall_groups:
            logger.warning(
                "[IMPORTANT][群管插件] auto_recall_enabled_groups 为空：按 #192 新语义，"
                "Bot 发言自动撤回将在【全部群】生效（命中 auto_recall_keywords 时）。"
                "如需限定范围，请配置该列表。"
            )

    def get_group_setting(self, group_id: str, key: str, default=None):
        """按群读取配置项。

        优先级（PR #290）：
        1. 指令写入的运行时覆盖 group_overrides[群号][key]（绝对优先，语义明确）；
           列表项若记录了派生基线，会与最新的模板/全局值重新合并，避免"某次
           增删之后 WebUI 模板改动再也不生效"（PR #289 审查：
           config-override-precedence-loss）；
        2. WebUI 按群配置模板（_conf_schema.json 的 group_overrides_templates，
           AstrBot template_list）：本群条目里该字段的值 ≠ 模板默认值时视为
           显式覆盖——字段保持默认值意味着"未覆盖、回退全局"，这样管理员
           只需要填写真正想改的字段，也不用担心模板默认值冻结全局配置；
        3. 全局配置 / 默认值。

        默认值判定拿不到"字段是否被显式填写"的信息，所以模板条目里另有
        explicit_override_fields 用来声明"即使等于默认值也按覆盖处理"的字段
        （例如本群要用空列表清空全局名单），命中时第 2 步直接采用该值。
        """
        overrides = self.runtime_map("group_overrides").get(str(group_id), {})
        if isinstance(overrides, dict) and key in overrides:
            value = overrides[key]
            if isinstance(value, list):
                # 有增量操作时以最新模板/全局基线重算，否则按整体快照返回
                if self._has_list_ops(group_id, key):
                    return self._merge_list_ops(group_id, key)
                return self._as_str_list(value)
            return value
        return self._template_or_global_setting(group_id, key, default)

    def _template_or_global_setting(self, group_id: str, key: str, default=None):
        """模板层 → 全局层 的配置值（不含运行时覆盖）。

        get_group_setting 与列表基线重算共用这一个入口，保证两条路径
        对"模板条目算不算覆盖"的判定完全一致。
        """
        entry = self._template_group_entry(group_id)
        if entry is not None and key in self._template_field_defaults:
            default = self._template_field_defaults[key]
            if key in entry:
                value = entry[key]
                if value != default or self._is_explicit_override(entry, key):
                    return value
            elif key in (entry.get("__present_fields__") or []):
                # 条目里写过这个字段，但值仍等于模板默认值、也没写进显式覆盖字段
                # → 按「未覆盖、回退全局」处理（PR #289 审查：template-default-fallback）。
                logger.debug(
                    f"[群管插件] 群 {group_id} 的模板条目填写了 {key}，但值等于模板默认值，"
                    f"按未覆盖处理并回退全局配置；如需强制覆盖，请把 {key} 写入"
                    f" explicit_override_fields"
                )
            else:
                logger.debug(
                    f"[群管插件] 群 {group_id} 的模板条目未填写 {key}，按未覆盖处理并回退全局配置"
                )
        return self.config.get(key, default)

    # ===================== WebUI 按群配置模板（PR #290） =====================

    def _load_template_field_defaults(self) -> dict:
        """从 _conf_schema.json 读取 group_overrides_templates 模板字段的默认值。

        以 schema 文件为唯一事实来源，避免代码里再维护一份默认值副本；
        读取失败时返回空 dict（此时模板条目不参与覆盖判定，行为与旧版一致）。
        """
        schema_path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            field = schema.get("group_overrides_templates") or {}
            template = (field.get("templates") or {}).get("group_config") or {}
            items = template.get("items") or {}
            # 模板自身的管理字段不参与覆盖判定：group_id 是群号，
            # explicit_override_fields 是"显式覆盖声明"，都不是群配置项
            # （PR #289 审查：template-default-mismatch）。
            meta = {"group_id", "explicit_override_fields"}
            return {
                k: (v.get("default") if isinstance(v, dict) else None)
                for k, v in items.items()
                if k not in meta
            }
        except Exception as e:
            logger.warning(f"[群管插件] 读取按群配置模板默认值失败，模板覆盖不生效: {e}")
            return {}

    def _template_group_entry(self, group_id: str):
        """取本群在 WebUI 按群配置模板列表里的合并条目；无条目返回 None。

        template_list 保存后形如 [{"__template_key": "...", "group_id": "...", ...}]；
        同一群号出现多条时按顺序合并，便于管理员拆多条配置同群的不同侧面。
        合并只让"真正改过的字段"生效：后一条记录里仍是模板默认值的字段视为
        未覆盖，保留前一条已配置的值，否则拆多条配置时后一条会把前一条的
        设置整体抹回默认（PR #289 审查：template-merge-default-overwrite）。
        """
        entries = self.config.get("group_overrides_templates")
        if not isinstance(entries, list):
            return None
        gid = str(group_id).strip()
        if not gid:
            return None
        defaults = self._template_field_defaults
        merged = None
        present_fields = []
        # 不属于群配置本身的键：模板标识与群号不参与"写过哪些字段"的记录，
        # 否则 __present_fields__ 会把内部元数据混进真实配置字段里
        # （PR #289 审查：template-explicit-override-lost）。
        meta_keys = ("__template_key", "group_id", "explicit_override_fields", "__present_fields__")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("group_id", "")).strip() != gid:
                continue
            if merged is None:
                merged = {}
            # 本条显式声明要覆盖的字段：即使本条没给值，也代表"以后条为准"，
            # 必须清掉前面条目留下的值，让它回退全局
            # （PR #289 审查：template-explicit-override-lost）。
            # 作用域是单条记录级：声明只保护本条给出的值；同群后面另有一条
            # 记录给出非默认值时，按那条记录自己的规则判定（见
            # test_declaration_scope_is_per_record），不要理解成"声明会粘住
            # 后续所有记录"（PR #289 审查：template-declaration-scope-loss）。
            declared = self._explicit_field_names(entry.get("explicit_override_fields"))
            for k, v in entry.items():
                if k == "__template_key":
                    continue
                if k not in present_fields and k not in meta_keys:
                    present_fields.append(k)
                if k == "explicit_override_fields":
                    # 多条记录的显式覆盖声明要累加，不能被后一条覆盖掉。
                    merged[k] = self._merge_explicit_fields(merged.get(k), v)
                    continue
                if (
                    k in defaults
                    and v == defaults[k]
                    and not self._is_explicit_override(entry, k)
                ):
                    # 本条该字段仍是默认值且未声明显式覆盖 → 不覆盖前面条目的值。
                    continue
                merged[k] = v
            for name in declared:
                if name in entry or name in meta_keys:
                    continue
                # 声明了却没给值 → 前一条的值作废，回退全局配置
                merged.pop(name, None)
        if merged is not None:
            # 合并完再按最终结果重算显式覆盖声明：只保留"最终确实存在"的字段。
            # 后条声明的字段若没给值（前面已 pop 掉），它的声明也要一并去掉，
            # 否则条目会对外声称覆盖了一个自己并不存在的字段
            # （PR #289 审查：template-override-scope-loss）。
            declared = self._explicit_field_names(merged.get("explicit_override_fields"))
            kept = [name for name in declared if name in merged]
            if kept:
                merged["explicit_override_fields"] = kept
            else:
                merged.pop("explicit_override_fields", None)
            # 记录"条目里到底写过哪些字段"，用于区分「字段没写」与「写了但仍是
            # 默认值」两种都按未覆盖处理的情况，排查模板不生效时看 debug 日志
            # （PR #289 审查：template-default-fallback）。
            merged["__present_fields__"] = present_fields
        return merged

    @staticmethod
    def _explicit_field_names(raw) -> list:
        """把 explicit_override_fields 的两种写法（列表 / 分隔字符串）拆成名字列表。"""
        if isinstance(raw, (list, tuple)):
            names = [str(x) for x in raw]
        else:
            names = re.split(r"[,，;；\s]+", str(raw or ""))
        result = []
        for name in names:
            name = name.strip()
            if name and name not in result:
                result.append(name)
        return result

    @classmethod
    def _merge_explicit_fields(cls, prev, new) -> list:
        """合并多条模板记录的 explicit_override_fields（并集，保持出现顺序）。"""
        merged = cls._explicit_field_names(prev)
        for name in cls._explicit_field_names(new):
            if name not in merged:
                merged.append(name)
        return merged

    def _is_explicit_override(self, entry: dict, key: str) -> bool:
        """模板条目是否显式声明要覆盖 key（即使填写值等于模板默认值）。

        explicit_override_fields 支持列表或逗号/空格分隔的字符串两种写法，
        用于"本群要用空列表清空全局名单"这类无法靠默认值比较表达的意图。
        """
        return key in self._explicit_field_names(entry.get("explicit_override_fields"))

    # ===================== 按群覆盖读写 =====================

    # 运行时列表的增量操作记录：group_overrides[群号][__list_ops__][key]
    # = {"added": [...], "removed": [...]}，生效值 = 模板/全局基线 应用这些操作。
    # 不靠"最终列表减去基线"反推操作——那样无法表达重复项与顺序意图
    # （PR #289 审查：lossy-list-diff）。
    _LIST_OPS = "__list_ops__"

    # 整体快照标记：group_overrides[群号][__list_snapshot__][key] = True
    # 表示该键的值是"显式整体赋值"（set_group_override / WebUI 整体写），
    # 合并基线就是这份快照本身，不随模板/全局变化；没有这个标记的普通列表
    # 才是旧版残留快照，首次变更时转成增量（_adopt_snapshot）
    # （PR #289 审查：explicit-snapshot-semantics-loss）。
    _LIST_SNAPSHOT = "__list_snapshot__"

    @staticmethod
    def _as_str_list(value) -> list:
        """把任意配置值规整成去空白的字符串列表。"""
        if isinstance(value, (list, tuple)):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [x for x in re.split(r"[,，;；\s]+", value) if x]
        return [] if value is None else [str(value).strip()]

    def _is_explicit_snapshot(self, group_id: str, key: str) -> bool:
        """该键的运行时值是否为"显式整体快照"（不参与基线重算）。"""
        overrides = self.runtime_map("group_overrides").get(str(group_id), {})
        meta = overrides.get(self._LIST_SNAPSHOT) if isinstance(overrides, dict) else None
        return bool(isinstance(meta, dict) and meta.get(key))

    def _mark_explicit_snapshot(self, group_id: str, key: str, on: bool = True) -> None:
        overrides = self.runtime_map("group_overrides")
        gconf = overrides.get(str(group_id))
        if not isinstance(gconf, dict):
            if not on:
                return
            gconf = overrides.setdefault(str(group_id), {})
        meta = gconf.get(self._LIST_SNAPSHOT)
        if on:
            if not isinstance(meta, dict):
                meta = {}
                gconf[self._LIST_SNAPSHOT] = meta
            meta[key] = True
            return
        if isinstance(meta, dict):
            meta.pop(key, None)
            if not meta:
                gconf.pop(self._LIST_SNAPSHOT, None)

    def _list_baseline(self, group_id: str, key: str) -> list:
        """列表合并用的基线：显式整体快照 > 模板 > 全局。"""
        if self._is_explicit_snapshot(group_id, key):
            overrides = self.runtime_map("group_overrides").get(str(group_id), {})
            snapshot = overrides.get(key) if isinstance(overrides, dict) else None
            if isinstance(snapshot, list):
                return self._as_str_list(snapshot)
        return self._as_str_list(self._template_or_global_setting(group_id, key, []))

    def _list_ops(self, group_id: str, key: str) -> dict:
        """取某个列表键的增量操作（没有则返回空操作）。

        removed_baseline 记录"删除该项时基线里到底有没有它"，用于区分两种删除：
        - True：删的是模板/全局传下来的项 → 本群明确不要，以后基线再加也不生效；
        - False：删的是本群自己加进去的项 → 基线后续新增同名项仍然生效
        （PR #289 审查：stale-list-operation-conflict）。
        """
        overrides = self.runtime_map("group_overrides").get(str(group_id), {})
        meta = overrides.get(self._LIST_OPS) if isinstance(overrides, dict) else None
        ops = meta.get(key) if isinstance(meta, dict) else None
        if not isinstance(ops, dict):
            return {"added": [], "removed": [], "removed_baseline": {}}
        removed_baseline = ops.get("removed_baseline")
        return {
            "added": self._as_str_list(ops.get("added")),
            "removed": self._as_str_list(ops.get("removed")),
            "removed_baseline": {
                str(k): bool(v)
                for k, v in (removed_baseline or {}).items()
            }
            if isinstance(removed_baseline, dict)
            else {},
        }

    def _store_list_ops(self, group_id: str, key: str, ops: dict) -> None:
        overrides = self.runtime_map("group_overrides")
        gconf = overrides.setdefault(str(group_id), {})
        meta = gconf.get(self._LIST_OPS)
        if not isinstance(meta, dict):
            meta = {}
            gconf[self._LIST_OPS] = meta
        if ops["added"] or ops["removed"]:
            payload = {"added": list(ops["added"]), "removed": list(ops["removed"])}
            removed_baseline = {
                item: bool(ops.get("removed_baseline", {}).get(item, True))
                for item in ops["removed"]
            }
            if removed_baseline:
                payload["removed_baseline"] = removed_baseline
            meta[key] = payload
        else:
            meta.pop(key, None)
            if not meta:
                gconf.pop(self._LIST_OPS, None)

    def drop_list_ops(self, group_id: str, key: str) -> None:
        """丢弃某 key 的增量操作：此后的运行时列表视为完整快照。"""
        overrides = self.runtime_map("group_overrides")
        gconf = overrides.get(str(group_id))
        meta = gconf.get(self._LIST_OPS) if isinstance(gconf, dict) else None
        if isinstance(meta, dict):
            meta.pop(key, None)
            if not meta:
                gconf.pop(self._LIST_OPS, None)

    def _merge_list_ops(self, group_id: str, key: str, ops=None) -> list:
        """把增量操作套到基线上得到生效列表（基线见 _list_baseline）。"""
        ops = ops if ops is not None else self._list_ops(group_id, key)
        removed_baseline = ops.get("removed_baseline") or {}
        merged = []
        # 名单类配置按"值集合"语义生效：基线里的重复值只保留一份，
        # 添加也保持首次出现顺序（PR #289 审查：lossy-list-diff）。
        for item in self._list_baseline(group_id, key):
            if item in ops["removed"] and removed_baseline.get(item, True):
                # 只有"删的是基线里的项"才一直压住；本群自己加又删掉的项，
                # 基线后续新增时仍然生效（否则删一次等于永久屏蔽全局名单）
                continue
            if item not in merged:
                merged.append(item)
        for item in ops["added"]:
            if item not in merged:
                merged.append(item)
        return merged

    def _has_list_ops(self, group_id: str, key: str) -> bool:
        ops = self._list_ops(group_id, key)
        return bool(ops["added"] or ops["removed"])

    def _adopt_legacy_snapshot(self, group_id: str, key: str) -> bool:
        """把旧版"普通列表快照"直接认作显式整体快照，返回是否认领成功。

        老版本直接在 group_overrides 里写完整列表、没有 __list_ops__ 记录。
        若不做处理，第一次增删就会用"模板/全局基线 + 本次操作"覆盖掉快照，
        本群原有的名单条目静默消失（PR #289 审查：legacy-data-loss）。

        做法是给这份旧值补上 __list_snapshot__ 标记，让它成为该键的基线
        （与 set_group_override 的整体赋值同语义）：内容原样保留，后续增删在
        这份快照上叠加，不再假装是"管理员显式新增"的项——否则那些"全局没有"
        的历史项会被永久记成 added，全局以后删同名项时本群还留着
        （PR #289 审查：legacy-snapshot-added-semantics）。

        这也是旧版本的真实行为：旧版读取就是原样返回这份完整列表，不跟随
        模板/全局变化，认领成快照 = 保持升级前的表现。
        """
        overrides = self.runtime_map("group_overrides")
        gconf = overrides.get(str(group_id))
        if not isinstance(gconf, dict):
            return False
        if self._is_explicit_snapshot(group_id, key):
            return False
        if not isinstance(gconf.get(key), list):
            return False
        self._mark_explicit_snapshot(group_id, key, True)
        logger.debug(
            f"[群管插件] 群 {group_id} 的 {key} 是旧版整体列表快照，"
            f"已按显式快照保留（不跟随模板/全局变化）"
        )
        return True

    def _apply_list_ops(self, group_id: str, key: str, add=(), remove=()) -> list:
        """记录一次增/删操作并物化生效列表（不做持久化，由调用方提交）。

        第一次变更旧版普通列表快照时先把它认作显式快照（见
        _adopt_legacy_snapshot），再在它的基础上叠加本次操作。
        """
        ops = self._list_ops(group_id, key)
        explicit_snapshot = self._is_explicit_snapshot(group_id, key)
        if not (ops["added"] or ops["removed"]) and not explicit_snapshot:
            explicit_snapshot = self._adopt_legacy_snapshot(group_id, key)
        # 记录删除时的基线内容（显式快照 / 模板 / 全局），供后续区分"删的是谁的项"
        baseline = self._list_baseline(group_id, key)
        for item in add:
            item = str(item).strip()
            if not item:
                continue
            # 重新添加 = 显式撤销此前的删除：removed / removed_baseline 一起清掉。
            # 值本来就在基线里时不写 added——否则同一值既在基线又在 added，
            # 操作状态重复、优先级也变得含糊（PR #289 审查：duplicate-list-operation）。
            ops["removed"] = [x for x in ops["removed"] if x != item]
            ops.get("removed_baseline", {}).pop(item, None)
            if item in baseline:
                ops["added"] = [x for x in ops["added"] if x != item]
            else:
                ops["added"] = list(ops["added"]) + [item]
        for item in remove:
            item = str(item).strip()
            if not item:
                continue
            ops["added"] = [x for x in ops["added"] if x != item]
            ops["removed"] = [x for x in ops["removed"] if x != item] + [item]
            ops.setdefault("removed_baseline", {})[item] = item in baseline
        merged = self._merge_list_ops(group_id, key, ops)
        overrides = self.runtime_map("group_overrides")
        gconf = overrides.setdefault(str(group_id), {})
        self._store_list_ops(group_id, key, ops)
        if ops["added"] or ops["removed"] or explicit_snapshot:
            # 显式整体赋值即使增删互相抵消也保留快照：它的基线就是这份值，
            # 删掉就等于把本群配置改回跟随模板/全局
            gconf[key] = merged
        else:
            # 增删互相抵消后没有任何增量：删掉物化的运行时值，让读取重新
            # 按最新模板/全局基线推导。否则这份快照会把"抵消那一刻"的
            # 基线冻结下来，后续基线变化就再也传不到本群
            # （PR #289 审查：stale-materialized-snapshot）。
            gconf.pop(key, None)
            if not gconf:
                overrides.pop(str(group_id), None)
        return merged

    def _commit_runtime_change(self, group_id: str, apply) -> None:
        """在锁内执行一次运行时覆盖变更并落盘；保存失败整体回滚本群覆盖。

        回滚按"整群深拷贝快照"进行：值、增量记录（__list_ops__）、整体快照标记
        （__list_snapshot__）一次全恢复，本群原先不存在就把本轮创建的条目删掉。
        列表增删、整体赋值、清除覆盖三条写路径共用同一套失败处理，避免各写一份
        回滚逻辑时漏掉某个痕迹（PR #289 审查：missing-rollback）。
        """
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            gid = str(group_id)
            prev_group = copy.deepcopy(overrides.get(gid)) if gid in overrides else None
            try:
                apply()
                self.save_config()
            except Exception:
                if prev_group is None:
                    overrides.pop(gid, None)
                else:
                    overrides[gid] = prev_group
                raise

    def _commit_list_change(self, group_id: str, key: str, add=(), remove=()) -> list:
        """执行列表增删并落盘；保存失败时回滚运行时对象再抛出。

        回滚直接改运行时覆盖本体（值 + 增量记录），不是改读出来的副本
        （PR #289 审查：rollback-not-persisted）；具体回滚由
        _commit_runtime_change 统一负责。
        """
        merged = []

        def apply():
            nonlocal merged
            merged = self._apply_list_ops(group_id, key, add, remove)

        self._commit_runtime_change(group_id, apply)
        return merged

    def _effective_list(self, group_id: str, key: str) -> list:
        """当前生效的列表值（运行时 → 模板 → 全局），返回可安全改动的新列表。"""
        current = self.get_group_setting(group_id, key, [])
        if isinstance(current, (list, tuple)):
            return [str(x).strip() for x in current if str(x).strip()]
        if isinstance(current, str):
            return self.normalize_qq_list_value(current)
        return [] if current is None else [current]

    def get_group_override_list(
        self, group_id: str, key: str, for_update: bool = False
    ) -> list:
        """取本群覆盖列表（只读）。

        - 有增量操作记录：与最新模板/全局基线合并后返回；
        - 只有运行时快照（整体赋值 / 旧版本残留）：原样返回该快照；
        - 都没有：返回当前生效值（模板/全局）的拷贝。

        任何情况都不创建运行时条目——"看一眼"不该把模板值冻结成本群快照
        （PR #289 审查：unexpected-runtime-materialization）。
        for_update 参数保留以兼容旧调用点，语义上不再需要"可写本体"。
        """
        with self._lock:
            overrides = self.runtime_map("group_overrides").get(str(group_id), {})
            value = overrides.get(key) if isinstance(overrides, dict) else None
            if isinstance(value, list):
                if self._has_list_ops(group_id, key):
                    return self._merge_list_ops(group_id, key)
                return self._as_str_list(value)
            if value is None:
                return self._effective_list(group_id, key)
            return [value] if value else []

    def runtime_group_list(self, group_id: str, key: str) -> list:
        """只取本群运行时覆盖列表（没有则空列表，不回退模板/全局）。

        用于「本群 XXX + 全局 XXX」同时展示的命令，避免本群段落把全局值
        再显示一遍；有增量操作时展示与最新模板/全局合并后的结果。
        """
        overrides = self.runtime_map("group_overrides").get(str(group_id), {})
        value = overrides.get(key) if isinstance(overrides, dict) else None
        if isinstance(value, list):
            if self._has_list_ops(group_id, key):
                return self._merge_list_ops(group_id, key)
            return self._as_str_list(value)
        if value is None:
            return []
        return [value] if value else []

    def drop_group_override(self, group_id: str, key: str) -> bool:
        """清除本群某个配置项（连同增量操作与整体快照标记）并落盘。

        返回是否确实删掉了东西。三个痕迹要一起清：只删值会留下 __list_ops__
        反向"复活"本群配置，只删 ops 会留下 __list_snapshot__ 让后续读取
        继续把这份值当基线（PR #289 审查：explicit-snapshot-semantics-loss）。
        保存失败时整群覆盖回滚，本群条目不会被删掉一半。
        """
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            gconf = overrides.get(str(group_id))
            if not isinstance(gconf, dict) or key not in gconf:
                return False

        def apply():
            gconf = overrides.get(str(group_id))
            if not isinstance(gconf, dict):
                return
            gconf.pop(key, None)
            self.drop_list_ops(group_id, key)
            self._mark_explicit_snapshot(group_id, key, False)
            if not gconf:
                overrides.pop(str(group_id), None)

        self._commit_runtime_change(group_id, apply)
        return True

    def set_group_override(self, group_id: str, key: str, value) -> None:
        """按群覆盖写入单个配置项并持久化（#192 owner：管理指令直接按群生效）。

        列表值按"显式整体设置"处理：清掉该键的增量操作并打上整体快照标记，
        此后以这份快照为基线（不随模板/全局变化）。标记与"旧版残留列表"区分开，
        后者在首次增删时才转成增量操作
        （PR #289 审查：explicit-snapshot-semantics-loss）。

        保存失败时整群覆盖回滚到写前状态（含本轮新建的本群条目），不会留下
        "内存里改了、磁盘上没改"的残留配置（PR #289 审查：missing-rollback）。
        """
        def apply():
            overrides = self.runtime_map("group_overrides")
            overrides.setdefault(str(group_id), {})[key] = value
            self.drop_list_ops(group_id, key)
            self._mark_explicit_snapshot(group_id, key, isinstance(value, list))

        self._commit_runtime_change(group_id, apply)

    def add_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        with self._lock:
            # 只读判定先行：没有任何新增时不做任何写入。
            current = [str(x) for x in self.get_group_override_list(group_id, key)]
            pending = []
            for qq in qq_list:
                qq = str(qq)
                if qq and qq not in current and qq not in pending:
                    pending.append(qq)
            if not pending:
                return []
            self._commit_list_change(group_id, key, add=pending)
            return pending

    def remove_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        with self._lock:
            # 只读判定先行：没有任何删除时不做任何写入。
            current = [str(x) for x in self.get_group_override_list(group_id, key)]
            pending = [qq for qq in (str(x) for x in qq_list) if qq and qq in current]
            if not pending:
                return []
            self._commit_list_change(group_id, key, remove=pending)
            return pending

    def normalize_qq_list_value(self, value) -> list:
        """把 WebUI / 配置里各种写法的名单值归一化为 QQ 字符串列表。

        兼容：list（正常）、JSON 数组字符串（WebUI dict 编辑器里填 ["10001"]）、
        逗号/空格/顿号分隔的数字串（10001,10002），以及单个 QQ 号。
        """
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(x).strip() for x in value if str(x).strip()]
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
            if isinstance(parsed, (str, int)):
                text = str(parsed)
        except (ValueError, TypeError):
            pass
        return _parse_qq_list(text)
