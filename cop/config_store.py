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

import re

from pathlib import Path

from astrbot.api import logger

from .constants import _RUNTIME_MAP_KEYS
from .text_utils import _parse_qq_list


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

    # ===================== 通用 IO（委托 JsonStore） =====================

    def load_json(self, path: Path, default):
        return self._json.load_json(path, default)

    def save_json(self, path: Path, data):
        return self._json.save_json(path, data)

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
        self.save_json(self.runtime_path, maps)
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
        """
        with self._lock:
            try:
                prev_runtime = self.runtime_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                prev_runtime = None
            except OSError as e:
                logger.warning(f"[群管插件] 读取 runtime.json 备份失败，跳过回滚保护: {e}")
                prev_runtime = None
            self.save_json(self.runtime_path, self._runtime_maps)
            try:
                self.config.save_config()
            except Exception:
                try:
                    if prev_runtime is None:
                        self.runtime_path.unlink(missing_ok=True)
                    else:
                        self.runtime_path.write_text(prev_runtime, encoding="utf-8")
                except OSError as e:
                    logger.error(
                        f"[群管插件] 保存框架配置失败后回滚 runtime.json 也失败，"
                        f"磁盘运行时数据可能领先于配置: {e}")
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

    @staticmethod
    def _as_str_list(value) -> list:
        """把任意配置值规整成去空白的字符串列表。"""
        if isinstance(value, (list, tuple)):
            return [str(x).strip() for x in value if str(x).strip()]
        if isinstance(value, str):
            return [x for x in re.split(r"[,，;；\s]+", value) if x]
        return [] if value is None else [str(value).strip()]

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
        """把增量操作套到最新的模板/全局基线上得到生效列表。"""
        ops = ops if ops is not None else self._list_ops(group_id, key)
        removed_baseline = ops.get("removed_baseline") or {}
        merged = []
        # 名单类配置按"值集合"语义生效：基线里的重复值只保留一份，
        # 添加也保持首次出现顺序（PR #289 审查：lossy-list-diff）。
        for item in self._as_str_list(self._template_or_global_setting(group_id, key, [])):
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

    def _apply_list_ops(self, group_id: str, key: str, add=(), remove=()) -> list:
        """记录一次增/删操作并物化生效列表（不做持久化，由调用方提交）。"""
        ops = self._list_ops(group_id, key)
        # 记录删除时的基线（模板/全局）内容，供后续区分"删的是谁的项"
        baseline = self._as_str_list(self._template_or_global_setting(group_id, key, []))
        for item in add:
            item = str(item).strip()
            if not item:
                continue
            # 重新添加 = 显式撤销此前的删除：added / removed / removed_baseline
            # 三处一起清掉再记一次添加。此后该项按"本群显式加入"生效，基线之后
            # 增删同名项都不再影响它——优先级固定为「最后一次操作优先」
            # （PR #289 审查：list-operation-state-loss）。
            ops["removed"] = [x for x in ops["removed"] if x != item]
            ops.get("removed_baseline", {}).pop(item, None)
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
        gconf[key] = merged
        self._store_list_ops(group_id, key, ops)
        return merged

    def _commit_list_change(self, group_id: str, key: str, add=(), remove=()) -> list:
        """执行列表增删并落盘；保存失败时回滚运行时对象再抛出。

        回滚直接改运行时覆盖本体（值 + 增量记录），不是改读出来的副本
        （PR #289 审查：rollback-not-persisted）。
        """
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            had_group = str(group_id) in overrides
            gconf = overrides.get(str(group_id))
            prev_value = copy.deepcopy(gconf.get(key)) if isinstance(gconf, dict) else None
            prev_ops = copy.deepcopy(gconf.get(self._LIST_OPS)) if isinstance(gconf, dict) else None
            merged = self._apply_list_ops(group_id, key, add, remove)
            try:
                self.save_config()
            except Exception:
                gconf = overrides.get(str(group_id)) or {}
                if prev_value is None:
                    gconf.pop(key, None)
                else:
                    gconf[key] = prev_value
                if prev_ops is None:
                    gconf.pop(self._LIST_OPS, None)
                else:
                    gconf[self._LIST_OPS] = prev_ops
                # 本次写入前本群没有任何覆盖时，回滚后不留空字典
                if not gconf and not had_group:
                    overrides.pop(str(group_id), None)
                raise
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

    def set_group_override(self, group_id: str, key: str, value) -> None:
        """按群覆盖写入单个配置项并持久化（#192 owner：管理指令直接按群生效）。

        列表值按"显式整体设置"处理：清掉该键的增量操作，此后以这份快照为准。
        """
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            overrides.setdefault(str(group_id), {})[key] = value
            self.drop_list_ops(group_id, key)
            self.save_config()

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
