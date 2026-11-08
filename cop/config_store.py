"""L1 配置存储层：运行时映射 + 全局配置的读写与按群覆盖。

迁出自 main.py 原第 319-551 行。ConfigStore 持有：
    (config, runtime_path, data_dir, config_path, stats_path, reports_path, lock, json_store)

设计说明：
- 通过构造注入 JsonStore（L4 门面负责组装），避免 L1 内部同层互相 import。
- 可依赖 L0：从 constants 取 _RUNTIME_MAP_KEYS，从 text_utils 取 _parse_qq_list。
- 原 main.py 中的方法体逻辑逐字保留（含异常兜底、注释语义），仅参数名从 self.xxx 改为存储字段。
"""

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
        """
        with self._lock:
            self.save_json(self.runtime_path, self._runtime_maps)
            self.config.save_config()

    def mutate_group_list(self, group_id: str, key: str, op: str, value: str) -> tuple:
        """群 list 配置的原子读-改-写（review#192 race-condition）。

        取列表→增/删→落盘 全程在 _CFG_LOCK 内串行化，避免并发覆盖丢失更新；
        返回 (ok, 当前列表拷贝)。
        """
        with self._lock:
            lst = self.get_group_override_list(group_id, key, for_update=True)
            if op == "add":
                if any(str(x) == value for x in lst):
                    return False, list(lst)
                lst.append(value)
            else:
                target = next((x for x in list(lst) if str(x) == value), None)
                if target is None:
                    return False, list(lst)
                lst.remove(target)
            self.save_config()
            return True, list(lst)

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
        if key in overrides:
            return overrides[key]
        entry = self._template_group_entry(group_id)
        if entry is not None and key in self._template_field_defaults:
            value = entry.get(key, self._template_field_defaults[key])
            if value != self._template_field_defaults[key] or self._is_explicit_override(
                entry, key
            ):
                return value
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
            return {
                k: (v.get("default") if isinstance(v, dict) else None)
                for k, v in items.items()
                if k != "group_id"
            }
        except Exception as e:
            logger.warning(f"[群管插件] 读取按群配置模板默认值失败，模板覆盖不生效: {e}")
            return {}

    def _template_group_entry(self, group_id: str):
        """取本群在 WebUI 按群配置模板列表里的合并条目；无条目返回 None。

        template_list 保存后形如 [{"__template_key": "...", "group_id": "...", ...}]；
        同一群号出现多条时按顺序合并（后面的字段覆盖前面的），便于管理员
        拆多条配置同群的不同侧面。
        """
        entries = self.config.get("group_overrides_templates")
        if not isinstance(entries, list):
            return None
        gid = str(group_id).strip()
        if not gid:
            return None
        merged = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("group_id", "")).strip() != gid:
                continue
            if merged is None:
                merged = {}
            for k, v in entry.items():
                if k == "__template_key":
                    continue
                merged[k] = v
        return merged

    def _is_explicit_override(self, entry: dict, key: str) -> bool:
        """模板条目是否显式声明要覆盖 key（即使填写值等于模板默认值）。

        explicit_override_fields 支持列表或逗号/空格分隔的字符串两种写法，
        用于"本群要用空列表清空全局名单"这类无法靠默认值比较表达的意图。
        """
        raw = entry.get("explicit_override_fields")
        if isinstance(raw, (list, tuple)):
            names = [str(x) for x in raw]
        else:
            names = re.split(r"[,，;；\s]+", str(raw or ""))
        return key in {n.strip() for n in names if n and n.strip()}

    # ===================== 按群覆盖读写 =====================

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
        """取本群覆盖列表。

        - for_update=False（只读）：本群已有运行时覆盖时返回其本体，否则返回
          当前生效值（模板/全局）的拷贝——不创建运行时条目，避免"看一眼就
          把模板值冻结成本群快照"。
        - for_update=True（写入）：本群还没有运行时覆盖时，以当前生效值为基线
          创建条目，增删操作在其上增量进行，不会整体遮蔽 WebUI 按群模板里的
          列表（PR #289 审查：config-override-data-loss）。
        """
        # review#192 race-condition：首次创建也加锁，
        # 避免并发首建同 (group_id,key) 产生多个空 list 互相覆盖；RLock 可重入
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            gconf = overrides.get(str(group_id))
            value = gconf.get(key) if isinstance(gconf, dict) else None
            if isinstance(value, list):
                return value
            if not for_update:
                if value is None:
                    return self._effective_list(group_id, key)
                return [value] if value else []
            baseline = self._effective_list(group_id, key) if value is None else (
                [value] if value else []
            )
            gconf = overrides.setdefault(str(group_id), {})
            gconf[key] = baseline
            return baseline

    def runtime_group_list(self, group_id: str, key: str) -> list:
        """只取本群运行时覆盖列表（没有则空列表，不回退模板/全局）。

        用于「本群 XXX + 全局 XXX」同时展示的命令，避免本群段落把全局值
        再显示一遍。
        """
        overrides = self.runtime_map("group_overrides").get(str(group_id), {})
        value = overrides.get(key) if isinstance(overrides, dict) else None
        if isinstance(value, list):
            return value
        if value is None:
            return []
        return [value] if value else []

    def set_group_override(self, group_id: str, key: str, value) -> None:
        """按群覆盖写入单个配置项并持久化（#192 owner：管理指令直接按群生效）。"""
        with self._lock:
            overrides = self.runtime_map("group_overrides")
            overrides.setdefault(str(group_id), {})[key] = value
            self.save_config()

    def add_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        with self._lock:
            admins = self.get_group_override_list(group_id, key, for_update=True)
            added = []
            for qq in qq_list:
                qq = str(qq)
                if qq and qq not in [str(x) for x in admins]:
                    admins.append(qq)
                    added.append(qq)
            if added:
                self.save_config()
            return added

    def remove_group_override_admins(self, group_id: str, key: str, qq_list: list) -> list:
        with self._lock:
            admins = self.get_group_override_list(group_id, key, for_update=True)
            removed = []
            for qq in qq_list:
                qq = str(qq)
                for item in list(admins):
                    if str(item) == qq:
                        admins.remove(item)
                        removed.append(qq)
                        break
            if removed:
                self.save_config()
            return removed

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
