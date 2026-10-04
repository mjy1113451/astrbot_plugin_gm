from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain

import os
import re  # 用于群相册命令前缀解析、编号提取等
import time
import asyncio
from pathlib import Path

# ===================== cop 子包导入（阶段1：L0/L1） =====================
# 插件模块路径形如 data.plugins.<dir>.main（AstrBot star_manager 通过
# __import__(path, fromlist=[module_str]) 加载），故 main 所在包为
# data.plugins.<dir>，相对导入 .cop 可稳定解析；同时保留绝对导入 cop.* 兜底，
# 以兼容把插件目录直接加入 sys.path 的加载方式。
try:
    from .cop.compat import MessageChain, aiohttp, _CFG_LOCK
    from .cop.constants import (
        _DEFAULT_PROFANITY_PROMPT,
        _RUNTIME_MAP_KEYS,
        _MUTE_MIN_MINUTES,
        _MUTE_CLAMP_MAX_MINUTES,
        GM_COMMAND_NAMES as _GM_COMMAND_NAMES,
    )
    from .cop.text_utils import (
        _parse_qq_list,
        _parse_duration_minutes,
        _format_minutes,
    )
    from .cop.json_store import JsonStore
    from .cop.config_store import ConfigStore
    from .cop.runtime import RuntimeState
    from .cop.onebot_api import OneBotApi
    from .cop.message_parse import MessageParser, TargetResolver
    from .cop.permissions import PermissionService
    from .cop.history import HistoryService
    from .cop.stats import StatsService
    from .cop.messaging import MessagingService
    from .cop.moderation import (
        GroupExistsProbe,
        ModerationService,
        ImageModeration,
        TextModeration,
        VoiceModeration,
    )
    from .cop.conversational import ColloquialService, DupFaceService
    from .cop.join_review import JoinReviewService
except ImportError:  # 兜底：插件目录本身在 sys.path 上时的绝对导入
    from cop.compat import MessageChain, aiohttp, _CFG_LOCK
    from cop.constants import (
        _DEFAULT_PROFANITY_PROMPT,
        _RUNTIME_MAP_KEYS,
        _MUTE_MIN_MINUTES,
        _MUTE_CLAMP_MAX_MINUTES,
        GM_COMMAND_NAMES as _GM_COMMAND_NAMES,
    )
    from cop.text_utils import (
        _parse_qq_list,
        _parse_duration_minutes,
        _format_minutes,
    )
    from cop.json_store import JsonStore
    from cop.config_store import ConfigStore
    from cop.runtime import RuntimeState
    from cop.onebot_api import OneBotApi
    from cop.message_parse import MessageParser, TargetResolver
    from cop.permissions import PermissionService
    from cop.history import HistoryService
    from cop.stats import StatsService
    from cop.messaging import MessagingService
    from cop.moderation import (
        GroupExistsProbe,
        ModerationService,
        ImageModeration,
        TextModeration,
        VoiceModeration,
    )
    from cop.conversational import ColloquialService, DupFaceService
    from cop.join_review import JoinReviewService

# 迁移说明（阶段1 行为零变更）：
# - MessageChain / aiohttp / _CFG_LOCK 迁至 cop.compat；
# - _DEFAULT_PROFANITY_PROMPT / _RUNTIME_MAP_KEYS / _MUTE_* / _GM_COMMAND_NAMES 迁至 cop.constants；
# - _parse_qq_list / _cn_number_to_int / _parse_duration_minutes / _format_minutes 迁至 cop.text_utils；
# - load_json / save_json → cop.json_store.JsonStore，配置与运行时映射 → cop.config_store.ConfigStore。
#   上述名字在本模块内保持同名可用，所有既有调用点无需改动。


def _runtime_prop(name):
    """构造转发到 RuntimeState 同名字段的 property。

    get/set 语义与逐一手写的 @property/@x.setter 完全等价：读取返回容器本体
    （支持 self.xxx 原地修改），赋值为整体替换。用工厂收敛 9 组 getter/setter 样板。
    """
    return property(
        lambda self: getattr(self._runtime, name),
        lambda self, value: setattr(self._runtime, name, value),
    )


@register(
    "group_admin",
    "YourName",
    "QQ群群管插件 - 禁言/踢人/头衔/精华/撤回/群公告/关键词撤回/违规检测/排名",
    "2.5.0",
    "https://github.com/mjy1113451/astrbot_plugin_gm"
)
class GroupAdminPlugin(Star):
    """QQ 群管插件门面（阶段5 收尾后）。

    职责边界：
    - 全部 @filter 入口（93 命令 + 2 事件钩子 + 1 after_message_sent）直接编排
      cop 子包中的 L3 服务实例（self._history/_stats/_api/_moderation/_colloquial/
      self._dup_face/_join_review 等），不再经过同名薄包装转发；
    - 仅保留极少量仍被引用的 glue：RuntimeState 状态 property 转发、
      self.load_json 存储委托、_moderation_require_admin_msg 静态语义（经 _perms）；
    - 服务组装顺序见 __init__：JsonStore/ConfigStore → OneBotApi → MessageParser →
      PermissionService/TargetResolver → RuntimeState → StatsService →
      HistoryService/MessagingService → ModerationService →
      ColloquialService/DupFaceService/JoinReviewService。
    """

    def __init__(self, context: Context, config=AstrBotConfig):
        super().__init__(context)
        try:
            from astrbot.api.star import StarTools
            self.data_dir = StarTools.get_data_dir() / "group_admin"
        except ImportError:
            self.data_dir = Path(os.getcwd()) / "data" / "group_admin"

        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        # 旧版本地配置路径：仅用于一次性迁移 / 框架未注入 AstrBotConfig 时降级落盘
        self.config_path = self.data_dir / "config.json"
        # 运行时动态映射（按群覆盖 / 欢迎语 / 待审申请）本地存储，见 _load_runtime_maps
        self.runtime_path = self.data_dir / "runtime.json"
        self.stats_path = self.data_dir / "stats.json"
        self.reports_path = self.data_dir / "reports.json"
        # 配置读取（修复 #180）：默认值统一由 _conf_schema.json 声明，AstrBot star_manager
        # 会以 config=<AstrBotConfig> 实例化插件并注入「schema 默认值 + WebUI 修改值」，
        # 保存统一走官方 self.config.save_config()。
        self.config = config
        # ---- 阶段1：L1 存储层组装（JsonStore 无状态；ConfigStore 持有运行时映射）----
        self._json_store = JsonStore()
        self._store = ConfigStore(
            config=self.config,
            runtime_path=self.runtime_path,
            data_dir=self.data_dir,
            config_path=self.config_path,
            stats_path=self.stats_path,
            reports_path=self.reports_path,
            lock=_CFG_LOCK,
            json_store=self._json_store,
        )
        # 动态映射不能放框架配置（框架加载时会清空这些嵌套数据），单独加载并镜像为属性
        self._runtime_maps = self._store.runtime_maps

        # ---- 阶段2：L2 服务层组装 ----
        # 依赖顺序：OneBotApi（L2）→ MessageParser（依赖 OneBotApi）→
        # PermissionService / TargetResolver（依赖前两者）。同层通过构造注入，不互相 import 模块。
        self._api = OneBotApi(context=self.context, config_store=self._store)
        self._mp = MessageParser(onebot_api=self._api)
        self._perms = PermissionService(
            config_store=self._store, message_parse=self._mp, onebot_api=self._api
        )
        self._resolver = TargetResolver(message_parse=self._mp, onebot_api=self._api)

        # ---- 阶段2：运行时可变状态迁至 RuntimeState；main 以 property 转发同名属性 ----
        # 说明：stats / reports 内容由门面在构造前从磁盘加载后传入；
        # message_history / max_history / spam_records / 各种缓存统一由 RuntimeState 承载，
        # 外部调用点（self.stats / self.message_history / self.spam_records ...）零改动。
        self._runtime = RuntimeState(
            stats=self._store.load_json(self.stats_path, {"groups": {}}),
            reports=self._store.load_json(self.reports_path, {"pending": []}),
            max_history=max(1, int(self.config.get("max_message_history", 50) or 50)),
        )

        # ---- 阶段3：L3a 领域服务组装 ----
        # 依赖注入：History / Stats / Messaging 只依赖 L0/L1/L2，同层不互相 import。
        # main.py 保留同名方法为薄包装，委托这些服务，既有调用点零改动。
        self._history = HistoryService(
            runtime=self._runtime,
            message_parse=self._mp,
            onebot_api=self._api,
            config_store=self._store,
        )
        self._stats = StatsService(
            runtime=self._runtime,
            config_store=self._store,
            onebot_api=self._api,
            message_parse=self._mp,
        )
        self._messaging = MessagingService(
            message_parse=self._mp,
            config_store=self._store,
            permissions=self._perms,
        )

        # ---- 阶段4：L3b 核心业务域组装 ----
        # 依赖顺序（关键）：stats 必须先于 moderation 构造——moderation._handle_violation
        # 单向调用 stats._record_mute_and_maybe_kick；同层不互相 import，只注入实例。
        # image/text/voice 为 moderation 同层子模块，先构造后注入。
        self._mod_image = ImageModeration(
            config_store=self._store,
            onebot_api=self._api,
            runtime=self._runtime,
        )
        self._mod_text = TextModeration(
            config_store=self._store,
            runtime=self._runtime,
        )
        self._mod_voice = VoiceModeration(
            config_store=self._store,
            context=self.context,
        )
        self._mod_group_exists = GroupExistsProbe(
            config_store=self._store,
            runtime=self._runtime,
        )
        self._moderation = ModerationService(
            config_store=self._store,
            onebot_api=self._api,
            stats=self._stats,
            permissions=self._perms,
            message_parse=self._mp,
            runtime=self._runtime,
            text_moderation=self._mod_text,
            image_moderation=self._mod_image,
            voice_moderation=self._mod_voice,
            group_exists_probe=self._mod_group_exists,
        )
        self._colloquial = ColloquialService(
            config_store=self._store,
            onebot_api=self._api,
            stats=self._stats,
            permissions=self._perms,
            target_resolver=self._resolver,
            message_parse=self._mp,
            history=self._history,
        )
        self._dup_face = DupFaceService(
            config_store=self._store,
            runtime=self._runtime,
            onebot_api=self._api,
        )
        self._join_review = JoinReviewService(
            config_store=self._store,
            onebot_api=self._api,
            runtime=self._runtime,
            permissions=self._perms,
            message_parse=self._mp,
            context=self.context,
        )

        # #192 review：留空=全群启用 语义变更的启动告警（详见 _warn_if_group_scope_empty）
        self._store.warn_if_group_scope_empty()

    # ===================== 阶段2：运行时状态 property 转发 =====================
    # RuntimeState 承载可变状态；以下 property 使 self.stats 等原调用点零改动。
    # stats / reports / message_history / spam_records / 各种 dict 需支持 self.xxx 原地修改，
    # 故只读引用返回容器本体（不可 setattr 整体替换，原代码亦无此用法）；
    # _msg_save_counter / max_history 为标量且有赋值，配 setter。

    # RuntimeState 承载可变状态；以下 property 转发使 self.stats 等原调用点零改动。
    # _runtime_prop 保持 get/set 语义与逐一手写等价（引用本体，支持原地修改）。
    stats = _runtime_prop("stats")
    reports = _runtime_prop("reports")
    message_history = _runtime_prop("message_history")
    max_history = _runtime_prop("max_history")
    spam_records = _runtime_prop("spam_records")
    _msg_save_counter = _runtime_prop("_msg_save_counter")
    _dup_face_seen = _runtime_prop("_dup_face_seen")
    _dup_face_last_mid = _runtime_prop("_dup_face_last_mid")
    _banned_file_md5_cache = _runtime_prop("_banned_file_md5_cache")
    group_exists_cache = _runtime_prop("group_exists_cache")

    # ===================== 通用 IO（阶段1：委托 cop 存储层） =====================
    # 仅保留 load_json 作为 cop 存储层委托（仍被门面内部读写明文 JSON 使用）。

    def load_json(self, path: Path, default):
        return self._store.load_json(path, default)

    # ----- 群违规检测管理命令（仅插件管理员） -----

    @filter.command("群违规检测状态", "查看群违规检测插件状态")
    async def moderation_status_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        api_type = self.config.get("api_type", "openai_vision")
        profanity_use_ai = self.config.get("profanity_use_ai", True)
        profanity_mode = "AI检测" if profanity_use_ai else "关键词检测"
        whitelist_users = self.config.get("whitelist_users", [])
        profanity_keywords = self.config.get("profanity_keywords", [])
        ad_keywords = self.config.get("ad_keywords", [])
        enabled_groups = self.config.get("enabled_groups", [])
        text = (
            "【群违规检测插件状态】\n"
            f"API 类型: {api_type}\n"
            f"API 站点: {self.config.get('api_endpoint', '') or '未配置'}\n"
            f"API Key: {'已配置' if self.config.get('api_key') else '未配置'}\n"
            f"模型: {self.config.get('model_name', 'gpt-4o')}\n"
            f"\n"
            f"监控群组: {enabled_groups if enabled_groups else '全部（留空=全群启用）'}\n"
            f"\n"
            f"【禁言时长（秒）】\n"
            f"图片: {self.config.get('ban_duration', 600)}\n"
            f"刷屏: {self.config.get('spam_ban_duration', 600)}\n"
            f"骂人: {self.config.get('profanity_ban_duration', 600)}\n"
            f"广告: {self.config.get('ad_ban_duration', 600)}\n"
            f"链接: {self.config.get('link_ban_duration', 600)}\n"
            f"群号推广: {self.config.get('group_promotion_ban_duration', 600)}\n"
            f"\n"
            f"【检测开关】\n"
            f"图片(色情/擦边): {self.config.get('check_porn', True)}/{self.config.get('check_sexy', True)}\n"
            f"刷屏: {self.config.get('spam_check_enabled', True)}（{self.config.get('spam_threshold', 5)} 条/{self.config.get('spam_time_window', 10)} 秒）\n"
            f"骂人: {self.config.get('profanity_check_enabled', True)}（{profanity_mode}, 关键词 {len(profanity_keywords)} 个）\n"
            f"广告: {self.config.get('ad_check_enabled', True)}（关键词 {len(ad_keywords)} 个）\n"
            f"链接: {self.config.get('link_check_enabled', False)}\n"
            f"群号推广: {self.config.get('group_promotion_check_enabled', True)}\n"
            f"\n"
            f"【其他】\n"
            f"白名单用户: {len(whitelist_users)} 人\n"
            f"管理员豁免: {self.config.get('admin_bypass', True)}\n"
            f"违规通知: {self.config.get('notify_on_violation', True)}\n"
            f"检测阈值: {self.config.get('threshold', 0.7)}"
        )
        yield event.plain_result(text)

    @filter.command("设置图片禁言时长", "设置图片违规禁言时长（秒，按群生效）")
    async def set_image_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "ban_duration", "图片违规"):
            yield r

    @filter.command("设置刷屏禁言时长", "设置刷屏禁言时长（秒，按群生效）")
    async def set_spam_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "spam_ban_duration", "刷屏"):
            yield r

    @filter.command("设置骂人禁言时长", "设置骂人禁言时长（秒，按群生效）")
    async def set_profanity_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "profanity_ban_duration", "骂人"):
            yield r

    @filter.command("设涉政禁言时长", "设置涉政禁言时长（分钟，全局配置，#204）")
    async def set_political_ban_duration_cmd(self, event: AstrMessageEvent, minutes: int = 0):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        if minutes <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0")
            return
        self.config["political_ban_duration"] = minutes
        self._store.save_config()
        yield event.plain_result(f"[成功] 涉政禁言时长已设置为 {minutes} 分钟")

    @filter.command("切换骂人检测模式", "切换 AI 检测 / 关键词检测（按群生效）")
    async def toggle_profanity_mode_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        cur = bool(self._store.get_group_setting(group_id, "profanity_use_ai", True))
        self._store.set_group_override(group_id, "profanity_use_ai", not cur)
        mode = "关键词检测" if cur else "AI检测"
        yield event.plain_result(f"[成功] 本群已切换为 {mode} 模式")

    # ===================== 加群审核通过关键词（#186，按群维度） =====================

    @filter.command("添加加群审核通过关键词", "添加加群审核自动通过关键词（按群生效）")
    async def add_join_approve_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self._store.get_group_override_list(group_id, "join_approve_keywords")
        if keyword in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        kws.append(keyword)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已添加加群审核通过关键词 '{keyword}'（本群当前 {len(kws)} 个）")

    @filter.command("删除加群审核通过关键词", "删除加群审核自动通过关键词（按群生效）")
    async def remove_join_approve_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self._store.get_group_override_list(group_id, "join_approve_keywords")
        if keyword not in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在（本群当前 {len(kws)} 个）")
            return
        kws.remove(keyword)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已删除加群审核通过关键词 '{keyword}'（本群当前 {len(kws)} 个）")

    @filter.command("查看加群审核通过关键词", "查看加群审核自动通过关键词列表（本群）")
    async def list_join_approve_keywords_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        kws = self._store.get_group_setting(group_id, "join_approve_keywords", [])
        if not kws:
            yield event.plain_result("本群当前没有设置加群审核通过关键词")
            return
        listing = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(kws)])
        yield event.plain_result(f"本群加群审核通过关键词（{len(kws)} 个）：\n{listing}")

    # ===================== 加群自动拒绝关键词（#229，全局配置 + 按群覆盖） =====================

    @filter.command("加群自动拒绝关键词", "加群申请命中关键词自动拒绝并拉黑（添加|删除|查看，按群覆盖）")
    async def join_reject_keywords_cmd(self, event: AstrMessageEvent, action: str = "", keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        act = (action or "").strip()
        kw = (keyword or "").strip()
        if act in ("添加", "add"):
            if not kw:
                yield event.plain_result("[错误] 用法：/加群自动拒绝关键词 添加 <关键词>")
                return
            ok, kws = self._store.mutate_group_list(group_id, "join_reject_keywords", "add", kw)
            if not ok:
                yield event.plain_result(f"[错误] 关键词 '{kw}' 已在本群列表中（当前 {len(kws)} 个）")
                return
            yield event.plain_result(
                f"[成功] 已添加本群加群自动拒绝关键词 '{kw}'（当前 {len(kws)} 个）\n"
                f"命中该词的加群申请将被自动拒绝，申请人同时加入本群黑名单")
            return
        if act in ("删除", "del", "remove"):
            if not kw:
                yield event.plain_result("[错误] 用法：/加群自动拒绝关键词 删除 <关键词>")
                return
            ok, kws = self._store.mutate_group_list(group_id, "join_reject_keywords", "remove", kw)
            if not ok:
                yield event.plain_result(f"[错误] 关键词 '{kw}' 不在本群列表中（当前 {len(kws)} 个）")
                return
            yield event.plain_result(f"[成功] 已删除本群加群自动拒绝关键词 '{kw}'（当前 {len(kws)} 个）")
            return
        if act in ("查看", "list", ""):
            # #229：本群覆盖用运行时原始值判断（不经 get_group_setting 回退），
            # 以便区分「本群列表」与「全局列表」并说明实际生效来源
            override = self._store.runtime_map("group_overrides").get(str(group_id), {})
            group_kws = override.get("join_reject_keywords")
            group_kws = group_kws if isinstance(group_kws, list) else []
            global_kws = self.config.get("join_reject_keywords", [])
            global_kws = global_kws if isinstance(global_kws, list) else []
            lines = []
            if group_kws:
                lines.append(f"本群加群自动拒绝关键词（{len(group_kws)} 个）：")
                lines.extend([f"  {i+1}. {k}" for i, k in enumerate(group_kws)])
            if global_kws:
                lines.append(f"全局加群自动拒绝关键词（{len(global_kws)} 个）：")
                lines.extend([f"  {i+1}. {k}" for i, k in enumerate(global_kws)])
            if not lines:
                yield event.plain_result("本群与全局均未设置加群自动拒绝关键词")
                return
            effective = "本群列表" if group_kws else "全局列表"
            yield event.plain_result(
                "\n".join(lines) +
                f"\n当前生效：{effective}（命中即自动拒绝申请，并把申请人加入本群黑名单）")
            return
        yield event.plain_result(
            "用法：/加群自动拒绝关键词 添加 <关键词> | 删除 <关键词> | 查看")

    @filter.command("添加白名单用户", "添加白名单用户（不受违规检测限制，按群生效）")
    async def add_whitelist_user_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        user_id = str(user_id).strip()
        if not user_id:
            yield event.plain_result("[错误] 请提供QQ号")
            return
        wl = self._store.get_group_override_list(group_id, "whitelist_users")
        if user_id in [str(x) for x in wl]:
            yield event.plain_result(f"[错误] 用户 {user_id} 已在本群白名单中")
            return
        wl.append(user_id)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已添加 {user_id} 到本群白名单（当前 {len(wl)} 人）")

    @filter.command("删除白名单用户", "从白名单移除用户（按群生效）")
    async def remove_whitelist_user_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        user_id = str(user_id).strip()
        if not user_id:
            yield event.plain_result("[错误] 请提供QQ号")
            return
        wl = self._store.get_group_override_list(group_id, "whitelist_users")
        if user_id not in [str(x) for x in wl]:
            yield event.plain_result(f"[错误] 用户 {user_id} 不在本群白名单中")
            return
        wl.remove(user_id)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已从本群白名单移除 {user_id}（当前 {len(wl)} 人）")

    @filter.command("查看白名单", "查看白名单用户列表（本群+全局）")
    async def list_whitelist_cmd(self, event: AstrMessageEvent):
        async for r in self._list_group_global_body(event, "whitelist_users", "白名单", "人"):
            yield r

    @filter.command("查看违规统计", "查看违规统计（默认全群；带 QQ 号查个人）")
    async def view_violation_stats_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        user_id = (user_id or "").strip()
        if user_id:
            g = self.stats.get("groups", {}).get(str(user_id), {})  # 简化：user_id 当群号查
            counts = g.get("violation_counts", {})
            total = sum(sum(b.values()) for b in counts.values())
            yield event.plain_result(
                f"群 {user_id} 违规统计:\n"
                f"图片: {sum(counts.get('image', {}).values())} 次\n"
                f"刷屏: {sum(counts.get('spam', {}).values())} 次\n"
                f"骂人: {sum(counts.get('profanity', {}).values())} 次\n"
                f"广告: {sum(counts.get('ad', {}).values())} 次\n"
                f"链接: {sum(counts.get('link', {}).values())} 次\n"
                f"群号推广: {sum(counts.get('group_promotion', {}).values())} 次\n"
                f"总计: {total} 次"
            )
        else:
            groups = self.stats.get("groups", {})
            total_users = 0
            total_violations = 0
            for g in groups.values():
                for bucket in g.get("violation_counts", {}).values():
                    total_users += len(bucket)
                    total_violations += sum(bucket.values())
            yield event.plain_result(
                f"违规统计概览:\n违规用户数: {total_users} 人\n总违规次数: {total_violations} 次"
            )

    @filter.command("设置广告禁言时长", "设置广告禁言时长（秒，按群生效）")
    async def set_ad_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "ad_ban_duration", "广告"):
            yield r

    @filter.command("设置链接禁言时长", "设置链接禁言时长（秒，按群生效）")
    async def set_link_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "link_ban_duration", "链接"):
            yield r

    @filter.command("设置群号推广禁言时长", "设置群号推广禁言时长（秒，按群生效）")
    async def set_group_promotion_ban_duration_cmd(self, event: AstrMessageEvent, seconds: int = 0):
        async for r in self._set_group_int_seconds_body(event, seconds, "group_promotion_ban_duration", "群号推广"):
            yield r

    @filter.command("添加广告关键词", "添加广告关键词（按群生效）")
    async def add_ad_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self._store.get_group_override_list(group_id, "ad_keywords")
        if keyword in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 已存在")
            return
        kws.append(keyword)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已添加本群广告关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("删除广告关键词", "删除广告关键词（按群生效）")
    async def remove_ad_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("[错误] 请提供关键词")
            return
        kws = self._store.get_group_override_list(group_id, "ad_keywords")
        if keyword not in kws:
            yield event.plain_result(f"[错误] 关键词 '{keyword}' 不存在（本群当前 {len(kws)} 个）")
            return
        kws.remove(keyword)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已删除本群广告关键词 '{keyword}'（当前 {len(kws)} 个）")

    @filter.command("查看广告关键词", "查看广告关键词列表（本群）")
    async def list_ad_keywords_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        kws = self._store.get_group_setting(group_id, "ad_keywords", [])
        if not kws:
            yield event.plain_result("本群当前没有设置广告关键词")
            return
        head = "\n".join([f"{i+1}. {kw}" for i, kw in enumerate(kws[:20])])
        more = f"\n…还有 {len(kws) - 20} 个" if len(kws) > 20 else ""
        yield event.plain_result(f"本群广告关键词（{len(kws)} 个）：\n{head}{more}")

    # ===================== 链接白名单（#195，按群） =====================

    @filter.command("开关链接检测", "开启/关闭本群链接检测撤回（/开关链接检测 on|off）")
    async def toggle_link_check_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        gid = self._mp._get_group_id_or_none(event)
        if not gid:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        v = (value or "").strip().lower()
        if v in ("on", "true", "开启"):
            enabled = True
        elif v in ("off", "false", "关闭"):
            enabled = False
        else:
            enabled = not bool(self._store.get_group_setting(gid, "link_check_enabled", False))
        self._store.set_group_override(gid, "link_check_enabled", enabled)
        yield event.plain_result(
            f"[成功] 本群链接检测已{'开启' if enabled else '关闭'}"
            "（#199：管理员/群主默认豁免，如需对其生效请设本群 admin_bypass false）"
        )

    @filter.command("添加链接白名单", "添加链接白名单域名（按群生效）")
    async def add_link_whitelist_cmd(self, event: AstrMessageEvent, domain: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        domain = (domain or "").strip().lower().split("/")[0].lstrip("www.")
        if not domain:
            yield event.plain_result("[错误] 请提供域名，例如：example.com")
            return
        wl = self._store.get_group_override_list(group_id, "link_whitelist")
        if domain in [str(x).lower().split("/")[0].lstrip("www.") for x in wl]:
            yield event.plain_result(f"[错误] 域名 '{domain}' 已在本群白名单中")
            return
        wl.append(domain)
        self._store.save_config()
        yield event.plain_result(f"[成功] 已添加本群链接白名单 '{domain}'（当前 {len(wl)} 个）")

    @filter.command("删除链接白名单", "从链接白名单移除域名（按群生效）")
    async def remove_link_whitelist_cmd(self, event: AstrMessageEvent, domain: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        domain = (domain or "").strip().lower().split("/")[0].lstrip("www.")
        if not domain:
            yield event.plain_result("[错误] 请提供域名")
            return
        wl = self._store.get_group_override_list(group_id, "link_whitelist")
        norm = [str(x).lower().split("/")[0].lstrip("www.") for x in wl]
        if domain not in norm:
            yield event.plain_result(f"[错误] 域名 '{domain}' 不在本群白名单中")
            return
        wl.remove(wl[norm.index(domain)])
        self._store.save_config()
        yield event.plain_result(f"[成功] 已从本群链接白名单移除 '{domain}'（当前 {len(wl)} 个）")

    @filter.command("查看链接白名单", "查看链接白名单（本群+全局）")
    async def list_link_whitelist_cmd(self, event: AstrMessageEvent):
        async for r in self._list_group_global_body(event, "link_whitelist", "链接白名单", "个"):
            yield r

    # ===================== 群黑名单（#194，按群） =====================

    @filter.command("添加黑名单", "将用户加入本群黑名单（拒绝加群申请）")
    async def add_blacklist_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "blacklisted_users", True,
            "[成功] 已拉黑 {n} 人到本群黑名单（当前 {total} 人）", "[提示] 所选用户已在本群黑名单中",
        ):
            yield r

    @filter.command("删除黑名单", "将用户从本群黑名单移除")
    async def remove_blacklist_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "blacklisted_users", False,
            "[成功] 已从本群黑名单移除 {n} 人", "[提示] 所选用户不在本群黑名单中",
        ):
            yield r

    @filter.command("查看黑名单", "查看本群黑名单")
    async def list_blacklist_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        bl = self._store.get_group_setting(group_id, "blacklisted_users", [])
        if not bl:
            yield event.plain_result("本群黑名单为空")
            return
        listing = "\n".join([f"{i+1}. {u}" for i, u in enumerate(bl)])
        yield event.plain_result(f"本群黑名单（{len(bl)} 人）：\n{listing}")

    # ===================== 群管指令 =====================

    @filter.command("添加插件管理", "按群添加专项权限管理员（兼容旧命令）")
    async def add_group_admin(self, event: AstrMessageEvent, target: str = ""):
        """兼容旧命令：按群添加插件管理员已废弃，改为按群添加专项权限管理员。"""
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        qq_list = list({str(x) for x in qq_list if x})
        if not qq_list:
            yield event.plain_result(
                "按群插件管理已改为设管理专项权限配置。\n"
                "请使用：/添加管理管理 QQ"
            )
            return
        added = self._store.add_group_override_admins(group_id, "group_admin_admins", qq_list)
        yield event.plain_result(
            "已按群添加专项权限管理员: " + (", ".join(sorted(set(added))) if added else "所列QQ号均已存在")
        )

    @filter.command("删除插件管理", "按群移除专项权限管理员（兼容旧命令）")
    async def remove_group_admin(self, event: AstrMessageEvent, target: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        qq_list = list({str(x) for x in qq_list if x})
        if not qq_list:
            yield event.plain_result(
                "按群插件管理已改为设管理专项权限配置。\n"
                "请使用：/删除管理管理 QQ"
            )
            return
        removed = self._store.remove_group_override_admins(group_id, "group_admin_admins", qq_list)
        yield event.plain_result(
            "已按群移除专项权限管理员: " + (", ".join(sorted(set(removed))) if removed else "所列QQ号均不存在")
        )

    @filter.command("添加管理管理", "按群添加可设置/取消群管理的专项管理员")
    async def add_group_admin_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._messaging._edit_special_admins(event, target, "group_admin_admins", "群管理", True):
            yield result

    @filter.command("删除管理管理", "按群移除可设置/取消群管理的专项管理员")
    async def remove_group_admin_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for result in self._messaging._edit_special_admins(event, target, "group_admin_admins", "群管理", False):
            yield result

    @filter.command("设管理", "设置群管理员（支持批量+@）")
    async def set_group_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._perms.has_group_admin_rights(str(raw.get("user_id")), group_id, raw):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标")
            return
        results = []
        for qq in qq_list:
            ok = await self._api._set_group_admin(event, group_id, qq, True)
            results.append((qq, ok))
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"设置群管理成功: {', '.join(ok_list)}" if ok_list else "设置群管理全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        yield event.plain_result(msg)

    @filter.command("取消管理", "取消群管理员（支持批量+@；管理员可取消自己）")
    async def unset_group_admin_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            qq_list = [sender_id]
        qq_list = list({str(x) for x in qq_list if x})
        self_cancel = len(qq_list) == 1 and qq_list[0] == sender_id and self._perms._is_sender_group_admin_only(raw)
        if not self._perms.has_group_admin_rights(sender_id, group_id, raw) and not self_cancel:
            yield event.plain_result("只有插件管理员、群管理员或被取消者本人可执行此操作")
            return
        results = []
        for qq in qq_list:
            ok = await self._api._set_group_admin(event, group_id, qq, False)
            results.append((qq, ok))
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"取消群管理成功: {', '.join(ok_list)}" if ok_list else "取消群管理全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        yield event.plain_result(msg)

    @filter.command("头衔", "设置群头衔（@某人 头衔内容）")
    async def set_group_title_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_group_admin_or_owner(raw):
            yield event.plain_result("只有插件管理员（群管理员/群主）可执行此操作")
            return
        target_qq = self._mp._extract_at_qq(raw)
        if not target_qq:
            target_qq = sender_id  # 操作对象为空时对自身（允许群管理员自设头衔）
        # 从 raw 消息提取所有 text 拼接，去掉命令前缀，得到完整头衔
        title = self._mp._extract_text(raw).strip()
        for prefix in ("/头衔", "头衔"):
            if title.startswith(prefix):
                title = title[len(prefix):].lstrip()
                break
        # 去掉开头的 @ 提及占位（如果 AstrBot 在 text 中保留了 @xxx）
        title = re.sub(r"^@[\w（）()\d]+\s*", "", title)
        if not title:
            yield event.plain_result("请提供群头衔内容")
            return
        ok = await self._api._set_group_title(event, group_id, target_qq, title)
        yield event.plain_result("设置头衔成功" if ok else "设置头衔失败")

    # #18: 群友昵称 - 设置他人群昵称（owner 09-14：合并为单条命令；普通群管理员即可，不必插件管理员）
    @filter.command("群友昵称", "设置他人群昵称（@某人 或 QQ号 + 新昵称；群管/群主/插件管理员）", alias={"别人昵称", "群昵称", "设群昵称", "设群友昵称"})
    async def set_other_card_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms.has_group_admin_rights(sender_id, group_id, raw):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        # 从原始消息提取所有 text 段拼接为 card（避免被 @ 组件挤掉）
        card = self._mp._extract_text(raw).strip()
        # 去掉开头的命令名（如果存在）
        for prefix in ("/群友昵称", "群友昵称", "/别人昵称", "别人昵称",
                       "/设群友昵称", "设群友昵称", "/群昵称", "群昵称",
                       "/设群昵称", "设群昵称"):
            if card.startswith(prefix):
                card = card[len(prefix):].lstrip()
                break
        target_qq = self._mp._extract_at_qq(raw)
        if not target_qq:
            # 支持直接给 QQ 号：首 token 为 QQ 则剥离，其余作为新昵称
            toks = card.split(None, 1)
            qq = self._mp._parse_qq(toks[0]) if toks else None
            if qq:
                target_qq = qq
                card = toks[1].strip() if len(toks) > 1 else ""
        if not target_qq:
            yield event.plain_result("请通过 @某人 或 QQ 号指定对象")
            return
        if not card:
            yield event.plain_result("请提供新昵称内容")
            return
        ok = await self._api._set_group_card(event, group_id, target_qq, card)
        yield event.plain_result(f"已将 {target_qq} 的群昵称设为 {card}" if ok else "设置群昵称失败")

    # #18: 改自己群昵称（owner 09-14：改自己昵称用「自己昵称」）
    @filter.command("自己昵称", "设置自己的群昵称", alias={"改群昵称", "改昵称"})
    async def set_self_card_cmd(self, event: AstrMessageEvent, card: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not card:
            yield event.plain_result("请提供新昵称内容")
            return
        ok = await self._api._set_group_card(event, group_id, sender_id, card)
        yield event.plain_result(f"已将你的群昵称设为 {card}" if ok else "设置群昵称失败")

    @filter.command("禁言", "禁言成员")
    async def mute_cmd(self, event: AstrMessageEvent, target: str = "", minutes: int = 10):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq = self._mp._extract_at_qq(raw)
        if not qq and target:
            # #136：仅在 target 为纯数字 QQ 号时 fallback，避免装饰字符误识别
            clean = re.sub(r"[^\d]", "", target)
            if clean and 5 <= len(clean) <= 12:
                qq = clean
        if not qq:
            yield event.plain_result("请指定要禁言的QQ号")
            return
        # #254：支持从整条消息解析时长（含中文时长），/禁言 @某人 30 与
        # /禁言 @某人 30分钟 / /禁言 @某人 半小时 均得到对应分钟数；解析失败回落默认 10。
        dur_text = self._mp._extract_command_tail(self._mp._extract_text(raw), ("禁言",)) or (target or "")
        dur_text = re.sub(r"@\d+", " ", dur_text)
        parsed = _parse_duration_minutes(dur_text)
        if parsed is not None:
            minutes = parsed
        else:
            target_stripped = (target or "").strip()
            if target_stripped.isdigit():
                try:
                    minutes = int(target_stripped)
                except ValueError:
                    pass
        minutes = max(_MUTE_MIN_MINUTES, min(int(minutes), _MUTE_CLAMP_MAX_MINUTES))
        ok = await self._api._mute_member(event, group_id, qq, minutes * 60)
        if ok:
            await self._stats._record_mute_and_maybe_kick(event, group_id, qq, sender_id)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result(f"禁言成功（{minutes}分钟）" if ok else "禁言失败")

    @filter.command("解禁", "解除禁言")
    async def unmute_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        qq = self._mp._extract_at_qq(raw)
        if not qq and target:
            # #136：仅在 target 为纯数字 QQ 号时 fallback，避免装饰字符 @用户名 误识别
            clean = re.sub(r"[^\d]", "", target)
            if clean and 5 <= len(clean) <= 12:
                qq = clean
        if not qq:
            return
        ok = await self._api._unmute_member(event, group_id, qq)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result("解禁成功" if ok else "解禁失败")

    @filter.command("踢", "踢出群成员（支持批量+@）")
    async def kick_cmd(self, event: AstrMessageEvent, target: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._perms._is_group_admin_or_owner(raw):
            yield event.plain_result("只有插件管理员（群管理员/群主）可执行此操作")
            return
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标")
            return
        results = []
        recalled_total = 0
        kick_recall_enabled = bool(self._store.get_group_setting(group_id, "kick_recall_enabled", False))
        kick_recall_count = max(1, min(int(self._store.get_group_setting(group_id, "kick_recall_count", 10) or 10), 50))
        for qq in qq_list:
            # #145：踢人前撤回该成员近期消息（踢出后无法再拉取其历史）
            recalled = 0
            if kick_recall_enabled:
                recalled = await self._history._recall_user_recent_msgs(event, group_id, qq, kick_recall_count)
                recalled_total += recalled
            ok = await self._api._kick_member(event, group_id, qq)
            if ok and self.config.get("reject_re_add", False):
                await self._api._execute_action(event, "reject_add", group_id=group_id, user_id=qq)
            results.append((qq, ok))
            if recalled:
                logger.info(f"踢人前撤回 {group_id}/{qq} 近期 {recalled} 条消息")
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        msg = f"踢出成功: {', '.join(ok_list)}" if ok_list else "踢出全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        if kick_recall_enabled and recalled_total > 0:
            msg += f"\n已撤回被踢成员近期消息 {recalled_total} 条"
        yield event.plain_result(msg)

    # #181: /清用户历史 已并入 /撤回 @用户 N（#110）；此处保留为向后兼容别名
    @filter.command("清用户历史", "撤回某用户在本群的最近 N 条消息（/清用户历史 @某人 [N]）")
    async def clear_user_history_cmd(self, event: AstrMessageEvent, target: str = ""):
        """手动撤回某用户在群内的最近 N 条消息（#145，对齐 zcj-ui/astrbot_plugin_group_guardian）。
        不踢人，仅撤回。已并入 /撤回 @用户 N（#181），保留旧命令作为兼容别名。"""
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        qq_list = self._mp._extract_at_qqs(raw) or _parse_qq_list(target)
        if not qq_list:
            yield event.plain_result("请通过 @某人 或QQ号指定目标，例如：/清用户历史 @某人 20")
            return
        tail_nums = re.findall(r"\d+", target or "")
        count = int(tail_nums[-1]) if tail_nums else 10
        count = max(1, min(count, 50))
        total = 0
        for qq in qq_list:
            recalled = await self._history._recall_user_recent_msgs(event, group_id, qq, count)
            total += recalled
        yield event.plain_result(f"已尝试撤回 {len(qq_list)} 个用户的最近消息，实际撤回 {total} 条（上限 {count} 条/人）")

    @filter.command("撤回", "撤回消息（/撤回 + 引用消息 / /撤回 @用户 N / /撤回 N）")
    async def recall_cmd(self, event: AstrMessageEvent):
        """统一分发器（#109 #110 #117 #118，修复 #122）：
        - 引用消息 -> 撤回引用消息
        - @用户 + N -> 撤回该用户最近 N 条
        - 仅有 N -> 撤回最近 N 条（不含指令本身，最多 50）
        """
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._mp._get_reply_id(event)
        if not self._perms._is_authorized(raw, str(raw.get("user_id", ""))):
            # #250：bot 主人（AstrBot admins_id）未任群管时，仅放宽「引用撤回
            # bot 自身消息」这一条路径；其余撤回（@用户 N / N 条）仍需群管权限
            if not (reply_id and await self._perms._owner_may_recall_quoted(event, reply_id)):
                yield event.plain_result("只有群管理员或群主可执行此操作（bot 主人可引用撤回 bot 自己的消息）")
                return
        target_qq = self._mp._extract_at_qq(raw)
        self_msg_id = str(raw.get("message_id", "")) if raw.get("message_id") else ""
        # 撤回成功提示：show_recall_notice 控制（全局或按群覆盖），关闭时成功静默；失败仍提示。
        recall_notice = bool(self._store.get_group_setting(
            group_id, "show_recall_notice", self.config.get("show_recall_notice", True)))

        # 参数文本：从 raw 文本段提取（@ 单独成段，避免 QQ 号混入数字）
        tail = self._mp._extract_command_tail(self._mp._extract_text(raw), ("撤回",))
        tail = re.sub(r"@\d+", "", tail)  # 去掉 @QQ 防止 QQ 号被当作编号
        all_numbers = [int(x) for x in re.findall(r"\d+", tail)]

        # 1) 引用消息优先（保持原有语义）
        if reply_id:
            ok = await self._api._recall_message(event, reply_id)
            if ok and recall_notice:
                yield event.plain_result("撤回成功")
            elif not ok:
                yield event.plain_result("撤回失败")
            return

        # 2) @用户 + N：撤回该用户最近 N 条（#110 #117）
        if target_qq:
            n = all_numbers[0] if all_numbers else 1
            n = max(1, min(n, 50))
            snapshot = await self._history._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result(
                    "当前 OneBot 实现不支持按用户撤回（缺少 get_group_msg_history，且插件本地历史为空）。\n"
                    "请使用 /撤回 + 引用消息 撤回指定消息。"
                )
                return
            if len([m for m in snapshot if m[3] == str(target_qq)]) < n:
                await self._history._load_history_from_api(event, group_id)
                snapshot = await self._history._get_history_snapshot(event, group_id, self_msg_id)
            candidates = [m for m in snapshot if m[3] == str(target_qq)][:n]
            # #240: 逐条撤回之间加间隔，避免高频 delete_msg 触发 OneBot 实现限流导致偶发少撤回
            recalled, expired, failed = 0, 0, 0
            for m in candidates:
                ok, err = await self._api._do_recall(event, m[0])
                if ok:
                    recalled += 1
                    self._history._remove_message_from_history(group_id, m[0])
                elif "已撤回" in err or "超时" in err:
                    # retcode=1200：消息已被他人撤回或超过 OneBot 约 2 分钟的撤回时限
                    expired += 1
                    self._history._remove_message_from_history(group_id, m[0])
                else:
                    failed += 1
                    # 消息已不存在（幽灵消息，重撤回返回"不存在"类错误）：清出本地历史，
                    # 避免后续候选/编号继续命中，造成少撤回
                    if any(k in err for k in ("不存在", "未找到", "没找到")) or any(
                            k in err.lower() for k in ("not found", "not exist", "no such")):
                        self._history._remove_message_from_history(group_id, m[0])
                await asyncio.sleep(0.3)
            if recalled:
                msg = f"撤回成功（{recalled} 条，用户 {target_qq}）"
                if expired:
                    msg += f"，{expired} 条已撤回或超过 2 分钟无法撤回"
                if failed:
                    msg += f"，{failed} 条撤回失败"
                if len(candidates) < n:
                    msg += f"，历史可撤回消息仅 {len(candidates)}/{n} 条"
                if recall_notice:
                    yield event.plain_result(msg)
            elif expired:
                yield event.plain_result(
                    f"撤回失败：找到 {len(candidates)} 条消息，均已被撤回或超过 2 分钟"
                    "（OneBot 撤回时限约 2 分钟）")
            elif candidates:
                yield event.plain_result(
                    f"撤回失败：找到 {len(candidates)} 条消息，均撤回失败")
            else:
                yield event.plain_result("撤回失败，未找到该用户的可撤回消息")
            return

        # 3) 仅数量：撤回最近 N 条（#109，不撤回指令本身；#118 本地历史兜底）
        if all_numbers:
            n = all_numbers[0]
            if n <= 0:
                yield event.plain_result("撤回数量必须为正整数。")
                return
            n = max(1, min(n, 50))
            snapshot = await self._history._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result(
                    "撤回失败：当前 OneBot 实现不支持 get_group_msg_history，且插件本地历史为空。\n"
                    "请使用 /撤回 + 引用消息 撤回指定消息。"
                )
                return
            if len(snapshot) < n:
                await self._history._load_history_from_api(event, group_id)
                snapshot = await self._history._get_history_snapshot(event, group_id, self_msg_id)
            if not snapshot:
                yield event.plain_result("撤回失败：本地历史为空，无可撤回消息。")
                return
            recalled, expired, failed = 0, 0, 0
            for m in snapshot[:n]:
                ok, err = await self._api._do_recall(event, m[0])
                if ok:
                    recalled += 1
                    self._history._remove_message_from_history(group_id, m[0])
                elif "已撤回" in err or "超时" in err:
                    # retcode=1200：消息已被他人撤回或超过 OneBot 约 2 分钟的撤回时限
                    expired += 1
                    self._history._remove_message_from_history(group_id, m[0])
                else:
                    failed += 1
                    # 幽灵消息清理：重撤回会返回"不存在"类错误，清出本地历史避免持续占用候选
                    if any(k in err for k in ("不存在", "未找到", "没找到")) or any(
                            k in err.lower() for k in ("not found", "not exist", "no such")):
                        self._history._remove_message_from_history(group_id, m[0])
                await asyncio.sleep(0.3)
            if recalled:
                msg = f"撤回成功（{recalled} 条）"
                if expired:
                    msg += f"，{expired} 条已撤回或超过 2 分钟无法撤回"
                if failed:
                    msg += f"，{failed} 条撤回失败"
                if len(snapshot) < n:
                    msg += f"，历史可撤回消息仅 {len(snapshot)}/{n} 条"
                if recall_notice:
                    yield event.plain_result(msg)
            elif expired:
                yield event.plain_result(
                    f"撤回失败：找到 {len(snapshot[:n])} 条消息，均已被撤回或超过 2 分钟"
                    "（OneBot 撤回时限约 2 分钟）")
            elif snapshot[:n]:
                yield event.plain_result(
                    f"撤回失败：找到 {len(snapshot[:n])} 条消息，均撤回失败")
            else:
                yield event.plain_result("撤回失败，未找到可撤回消息")
            return

        # 4) 用法提示
        yield event.plain_result(
            "用法：\n"
            "/撤回 + 引用消息：撤回引用消息\n"
            "/撤回 @用户 N：撤回该用户最近 N 条\n"
            "/撤回 N：撤回最近 N 条（最多 50，不含指令本身）"
        )

    @filter.command("撤回自身", "撤回机器人最近发送的消息（/撤回自身 N）")
    async def recall_self_cmd(self, event: AstrMessageEvent):
        """撤回机器人自身发送的消息（修复 #122）。"""
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, str(raw.get("user_id", ""))):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return

        tail = self._mp._extract_command_tail(self._mp._extract_text(raw), ("撤回自身",))
        nums = re.findall(r"\d+", tail)
        if not nums:
            yield event.plain_result("请在指令后填写需要撤回的数量，例如：撤回自身 5")
            return
        count = int(nums[-1])
        if count <= 0:
            yield event.plain_result("撤回数量必须为正整数。")
            return
        count = max(1, min(count, 50))

        current_msg_id = raw.get("message_id")
        snapshot = await self._history._get_history_snapshot(event, group_id, current_msg_id)
        bot_messages = [m for m in snapshot if m[5]]  # is_bot=True
        if not bot_messages:
            yield event.plain_result("未找到可撤回的机器人消息。")
            return
        success = 0
        failed_msgs = []
        for m in bot_messages:
            if success >= count:
                break
            ok, err = await self._api._do_recall(event, m[0])
            if ok:
                success += 1
                self._history._remove_message_from_history(group_id, m[0])
            elif "已撤回" not in err:
                failed_msgs.append(f"{m[0]}({err})")
            else:
                self._history._remove_message_from_history(group_id, m[0])
        msg = f"已尝试撤回机器人最近 {success} 条消息。"
        if failed_msgs:
            msg += f"\n失败: {', '.join(failed_msgs[:5])}"
        yield event.plain_result(msg)

    @filter.command("设精", "设置精华消息")
    async def essence_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._perms._is_group_admin(raw) and not self._perms._is_group_owner(raw):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条消息后使用该指令")
            return
        ok = await self._api._set_essence(event, reply_id, group_id=str(raw.get("group_id")))
        yield event.plain_result("设精成功" if ok else "设精失败")

    @filter.command("取消设精", "取消精华消息")
    async def cancel_essence_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._perms._is_group_admin(raw) and not self._perms._is_group_owner(raw):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条精华消息后使用该指令")
            return
        ok = await self._api._delete_essence(event, reply_id, group_id=str(raw.get("group_id")))
        yield event.plain_result("取消设精成功" if ok else "取消设精失败")

    # #24: 改群头像；#251：补「设群头像/设置群头像」别名，防止未注册指令被
    # AstrBot 核心 LLM 兜底接管（报「未找到任何可用的对话模型」）
    @filter.command("改群头像", "引用图片回复即可修改群头像",
                    alias={"设群头像", "设置群头像"})
    async def set_group_avatar_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        image_url = self._mp._extract_image_url(event)
        if not image_url:
            # #230：图片在被引用的那条消息里，从引用消息中补取
            image_url = await self._mp._extract_reply_image_url(event)
        if not image_url:
            yield event.plain_result("请引用一条图片消息，或在消息中附带图片")
            return
        ok = await self._api._set_group_avatar(event, group_id, image_url)
        yield event.plain_result("群头像已更新" if ok else "修改群头像失败")

    # #79: 宵禁 - 全体禁言
    @filter.command("宵禁", "开启全群禁言")
    async def whole_ban_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        ok = await self._api._execute_action(event, "set_group_whole_ban",
                                        group_id=group_id, enable=True)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result("已开启全群禁言" if ok else "开启失败")

    # #79: 解除宵禁 - 解除全体禁言
    @filter.command("解除宵禁", "关闭全群禁言")
    async def unwhole_ban_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        ok = await self._api._execute_action(event, "set_group_whole_ban",
                                        group_id=group_id, enable=False)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result("已解除全群禁言" if ok else "解除失败")

    # #75: 禁我 [分钟] - 任意成员禁言自己
    @filter.command("禁我", "禁言自己，格式：/禁我 [分钟]，默认10分钟")
    async def mute_self_cmd(self, event: AstrMessageEvent, minutes: int = 10):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        minutes = max(1, min(int(minutes), 43200))  # 限制 1 分钟 ~ 30 天
        ok = await self._api._mute_member(event, group_id, sender_id, minutes * 60)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result(f"已禁言自己 {minutes} 分钟" if ok else "禁言失败")

    # #16: 群公告
    @filter.command("发群公告", "发送群公告")
    async def send_group_notice_cmd(self, event: AstrMessageEvent, content: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        if not content:
            yield event.plain_result("请提供公告内容")
            return
        # 尝试通过 send_group_notice / _send_group_notice API
        ok = await self._api._execute_action(event, "_send_group_notice",
                                        group_id=group_id, content=content)
        if not ok:
            ok = await self._api._execute_action(event, "send_group_notice",
                                            group_id=group_id, content=content)
        if ok:
            # #233：公告发布后按配置发送群内通知提醒成员查看
            if self._store.get_group_setting(group_id, "announce_notify", True):
                await self._api._send(event, self._mp._build_text(
                    f"📢 管理员已发布群公告，请各位成员注意查看"))
            yield event.plain_result("群公告已发布")
        else:
            # 退化为普通消息提示
            await self._api._send(event, [Plain(f"[群公告] {content}")])
            yield event.plain_result("当前框架不支持发群公告，已以普通消息发送")

    # #29: 鞭尸禁言 + 发言排名
    @filter.command("鞭尸", "长期禁言被@的人（29天23小时59分）")
    async def whip_corpse_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        qq = self._mp._extract_at_qq(raw)
        if not qq:
            yield event.plain_result("请 @要鞭尸的成员")
            return
        # 29天23小时59分 = 29*86400 + 23*3600 + 59*60 = 2591640 秒
        duration = 29 * 86400 + 23 * 3600 + 59 * 60
        ok = await self._api._mute_member(event, group_id, qq, duration)
        if ok:
            await self._stats._record_mute_and_maybe_kick(event, group_id, qq, sender_id)
        if self._history._should_notify_mute(group_id, ok):
            yield event.plain_result("已鞭尸" if ok else "鞭尸失败")

    @filter.command("排名", "查看本群发言排名")
    async def rank_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        top_n = int(self.config.get("rank_top_n", 10))
        ranked = self._stats.get_rank(group_id, top_n)
        if not ranked:
            yield event.plain_result("暂无发言数据")
            return
        lines = [f"{i+1}. {qq} - {cnt}条" for i, (qq, cnt) in enumerate(ranked)]
        yield event.plain_result(f"本群发言排名（Top {len(ranked)}）：\n" + "\n".join(lines))

    @filter.command("清除数据", "清除本群发言计数")
    async def clear_rank_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或插件管理员可执行此操作")
            return
        self._stats.reset_group_stats(group_id)
        yield event.plain_result("已清除本群发言数据，重新开始计数")

    # #225：清人
    @filter.command("清人", "清理 N 天未发言的成员（/清人 N）")
    async def clean_inactive_cmd(self, event: AstrMessageEvent, value: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        if not await self._perms._moderation_require_admin_msg(event):
            return
        if not value:
            yield event.plain_result("请提供天数，例如：/清人 4")
            return
        try:
            days = int(value)
        except (ValueError, TypeError):
            yield event.plain_result("天数必须是整数")
            return
        if days < 1:
            yield event.plain_result("天数必须大于 0")
            return
        yield event.plain_result(f"正在清理 {days} 天未发言的成员，请稍候...")
        kicked_ok, kicked_fail, skipped, capable = await self._stats.clean_inactive_members(
            event, group_id, days)
        if not capable:
            yield event.plain_result(
                f"无法执行（当前协议端不支持成员发言时间查询，请确认 OneBot 实现是否提供 last_sent_time 字段）")
            return
        parts = []
        if kicked_ok > 0:
            parts.append(f"已踢出 {kicked_ok} 人")
        if kicked_fail > 0:
            parts.append(f"踢出失败 {kicked_fail} 人")
        if skipped:
            parts.append(f"跳过 {len(skipped)} 人（群主/管理员）")
        if not parts:
            yield event.plain_result(f"近 {days} 天内无未发言的普通成员")
            return
        yield event.plain_result("，".join(parts))

    # #21: 举报违规
    @filter.command("举报", "举报群成员违规行为（@成员或引用其消息）")
    async def report_cmd(self, event: AstrMessageEvent, reason: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        reporter_id = str(raw.get("user_id"))
        # #227：优先 @ 成员；未 @ 时通过「引用（回复）被举报成员的消息」定位目标
        target_qq = self._mp._extract_at_qq(raw)
        if not target_qq:
            _quoted_name, target_qq = await self._resolver._resolve_quoted_sender(event, raw)
        if not target_qq:
            yield event.plain_result("请 @要举报的成员，或引用（回复）其消息后再发送 举报")
            return
        # #140: 群主豁免 — 群主不触发举报
        reporter_role = raw.get("sender", {}).get("role", "")
        if reporter_role == "owner":
            yield event.plain_result("群主无需举报")
            return
        reply_id = self._mp._get_reply_id(event)
        record = {
            "group_id": group_id,
            "reporter_id": reporter_id,
            "target_qq": target_qq,
            "reason": reason or "（未提供原因）",
            "message_id": reply_id,
            "time": int(time.time()),
        }
        self.reports.setdefault("pending", []).append(record)
        self._store.save_reports()
        # #140: 按角色分级路由通知
        # 查被举报人角色
        target_role = ""
        info = await self._api._execute_action(event, "get_group_member_info",
                                          group_id=group_id, user_id=target_qq,
                                          return_raw=True, no_cache=True)
        if isinstance(info, dict):
            data = info.get("data") or info
            target_role = data.get("role", "")
        report_text = (f"[举报] 群 {group_id}\n"
                       f"举报人: {reporter_id}\n"
                       f"被举报: {target_qq}\n"
                       f"原因: {record['reason']}")
        # 被举报人是管理员 → 仅通知群主；普通成员 → 通知所有管理员 + 群主
        if target_role in ("admin", "owner"):
            # 仅通知群主
            owner_qq = await self._api._find_group_owner(event, group_id)
            if owner_qq:
                await self._api._send_private_msg(str(owner_qq), report_text)
        else:
            # 通知所有管理员 + 群主
            await self._api._notify_admins(report_text, group_id=group_id)
        yield event.plain_result("已提交举报，管理员会尽快处理")

    # ===================== 新增命令（#131 #135 #139 #146 #150 #151）=====================

    # #135: /禁言列表 — 查看本群当前被禁言成员
    @filter.command("禁言列表", "查看本群当前被禁言成员列表")
    async def mute_list_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有插件管理员或群管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        member_list = await self._api._execute_action(event, "get_group_member_list",
                                                      group_id=group_id, return_raw=True)
        members = self._api._normalize_member_list(member_list)
        if members is None:
            # #256：列表获取失败与「无人被禁言」分开提示，避免误判
            yield event.plain_result(
                "获取群成员列表失败（当前 OneBot 实现可能不支持 get_group_member_list），"
                "无法查询禁言列表")
            return
        muted = []
        for m in members:
            remaining = self._api._member_mute_remaining(m)
            if remaining > 0:
                uid = str(m.get("user_id", ""))
                nick = m.get("nickname", "")
                card = m.get("card", "") or ""
                name = card if card else nick
                minutes = max(1, remaining // 60)
                muted.append(f"{uid}（{name}）剩余 {minutes} 分钟")
        if not muted:
            yield event.plain_result("本群当前无被禁言成员")
            return
        text = f"本群禁言列表（{len(muted)} 人）：\n" + "\n".join(muted)
        yield event.plain_result(text)

    # #146: /给我头衔 — 普通成员自设群头衔
    @filter.command("给我头衔", "自设群头衔（/给我头衔 标题内容）")
    async def self_title_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        group_id = str(raw.get("group_id"))
        title = self._mp._extract_text(raw).strip()
        for prefix in ("/给我头衔", "给我头衔"):
            if title.startswith(prefix):
                title = title[len(prefix):].lstrip()
                break
        title = re.sub(r"^@[\w（）()\d]+\s*", "", title)
        if not title:
            yield event.plain_result("请提供群头衔内容，例如 /给我头衔 传说")
            return
        if len(title) > 60:
            yield event.plain_result("头衔内容过长（最多60字符）")
            return
        ok = await self._api._set_group_title(event, group_id, sender_id, title)
        yield event.plain_result("设置头衔成功" if ok else "设置头衔失败（Bot可能无权限或头衔功能不可用）")

    # #131: /添加群待办 — 引用消息设为群待办（群管/群主）
    @filter.command("添加群待办", "引用消息设为群待办")
    async def add_group_todo_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条消息后发送此命令")
            return
        ok, res = await self._api._call_action_fallback(
            event, ("_set_group_todo", "set_group_todo"),
            group_id=group_id, message_id=int(reply_id))
        if ok:
            yield event.plain_result("已设为群待办")
        else:
            yield event.plain_result(
                f"设置群待办失败：{self._api._describe_action_failure(res, 'set_group_todo')}")

    # #139: /取消群待办 — 引用消息取消群待办（群管/群主）
    @filter.command("取消群待办", "引用消息取消群待办")
    async def delete_group_todo_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条群待办消息后发送此命令")
            return
        ok, res = await self._api._call_action_fallback(
            event, ("_delete_group_todo", "delete_group_todo"),
            group_id=group_id, message_id=int(reply_id))
        if ok:
            yield event.plain_result("已取消群待办")
        else:
            yield event.plain_result(
                f"取消群待办失败：{self._api._describe_action_failure(res, 'delete_group_todo')}")

    # #150: /加群申请待处理 — 查看待处理加群申请（群管/群主）
    @filter.command("加群申请待处理", "查看本群未处理的加群申请列表")
    async def pending_join_requests_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        # #235：get_group_apply_list 并非 OneBot v11 标准接口，多数协议端返回 unknown action。
        # 标准接口为 get_group_system_msg（返回 join_requests），部分实现另提供
        # get_group_apply_list / get_group_add_request_list，按顺序回退。
        ok, result = await self._api._call_action_fallback(
            event, ("get_group_system_msg", "get_group_apply_list",
                    "get_group_add_request_list"))
        if not ok:
            yield event.plain_result(
                f"获取加群申请列表失败：{self._api._describe_action_failure(result, 'get_group_system_msg')}")
            return
        # aiocqhttp 的 call_action 成功时只返回 data 本身，部分框架会再包一层 data
        if isinstance(result, dict) and isinstance(result.get("data"), (dict, list)):
            data = result["data"]
        else:
            data = result if isinstance(result, (dict, list)) else {}
        # 标准接口把待处理申请放在 join_requests；兼容直接返回列表的实现
        raw_list = data.get("join_requests") if isinstance(data, dict) else data
        if not isinstance(raw_list, list):
            raw_list = []
        applies = []
        for a in raw_list:
            if not isinstance(a, dict):
                continue
            # 各家字段名不同：go-cqhttp 用 requester_uin/message，其它实现用 user_id/comment
            if a.get("checked"):
                continue
            item_gid = str(a.get("group_id", "") or "")
            if item_gid and item_gid != group_id:
                continue
            sub_type = a.get("sub_type", "")
            if sub_type not in ("add", ""):
                continue
            uid = str(a.get("user_id") or a.get("requester_uin") or "")
            nick = a.get("nickname") or a.get("requester_nick") or ""
            comment = a.get("comment") or a.get("message") or ""
            ts = a.get("time", 0)
            time_str = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未知"
            applies.append(f"{uid}（{nick}）| {time_str}\n验证消息: {comment or '无'}")
        if not applies:
            yield event.plain_result("本群当前无待处理的加群申请")
            return
        text = f"待处理加群申请（{len(applies)} 条）：\n\n" + "\n\n".join(applies)
        yield event.plain_result(text)

    # #151: /群信息 — 查看本群资料（任何成员可用）
    @filter.command("群信息", "查看本群资料（名称/号/标签/人数）")
    async def group_info_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        result = await self._api._execute_action(event, "get_group_info",
                                            group_id=group_id, return_raw=True)
        if not result:
            yield event.plain_result("获取群信息失败")
            return
        info = {}
        if isinstance(result, dict):
            data = result.get("data") or result
            if isinstance(data, dict):
                info = data
        name = info.get("group_name") or info.get("name") or "未知"
        gid = info.get("group_id") or group_id
        member_count = info.get("member_count") or info.get("member_count") or "?"
        tags = info.get("tags") or info.get("group_tags") or []
        tags_str = ", ".join(str(t) for t in tags) if tags else "无"
        text = f"群名称: {name}\n群号: {gid}\n群标签: {tags_str}\n成员数: {member_count}"
        yield event.plain_result(text)

# #164: /群相册 — 引用图片消息上传到群相册（群管/群主）
    @filter.command("群相册", "引用图片上传到群相册（/群相册 相册名）")
    async def group_album_upload_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请引用一条图片消息后发送此命令")
            return
        text = self._mp._extract_text(raw).strip()
        # 用正则统一处理前缀，避免边界输入裁错
        text = re.sub(r"^[/]?群相册\s*", "", text)
        if not text:
            yield event.plain_result("请提供相册名，例如 /群相册 表情包")
            return
        # 提取被引用消息的图片 URL
        # 通过 get_msg API 拉取原消息段
        msg = await self._api._execute_action(event, "get_msg",
                                          message_id=int(reply_id), return_raw=True)
        if not msg:
            yield event.plain_result("无法获取引用消息内容")
            return
        msg_data = msg.get("data") if isinstance(msg, dict) else msg
        image_url = ""
        segs = msg_data.get("message") if isinstance(msg_data, dict) else None
        if isinstance(segs, str):
            # #206：部分 OneBot 实现 get_msg 返回 CQ 字符串而非段列表
            m = re.search(r"\[CQ:image[^\]]*?url=([^\],]+)", segs) or \
                re.search(r"\[CQ:image[^\]]*?file=([^\],]+)", segs)
            if m:
                image_url = m.group(1)
            segs = None
        for seg in segs or []:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "image":
                d = seg.get("data") or {}
                image_url = d.get("url") or d.get("file") or d.get("file_id") or ""
                if image_url:
                    break
        if not image_url and isinstance(msg_data, dict):
            # #206：兜底解析 raw_message 中的 CQ image
            rawmsg = str(msg_data.get("raw_message") or "")
            m = re.search(r"\[CQ:image[^\]]*?url=([^\],]+)", rawmsg) or \
                re.search(r"\[CQ:image[^\]]*?file=([^\],]+)", rawmsg)
            if m:
                image_url = m.group(1)
        if not image_url:
            # #206：兜底从当前事件消息链的引用组件中取 Image
            for comp in getattr(event.message_obj, "message", []) or []:
                if comp.__class__.__name__ == "Image":
                    image_url = getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                    if image_url:
                        break
        if not image_url:
            yield event.plain_result("引用消息中未找到图片")
            return
        # 1) 尝试创建相册目录；失败时尝试 get_group_file_list 找已有目录
        folder_id = ""
        folder_result = await self._api._execute_action(event, "create_group_file_folder",
                                                    group_id=int(raw["group_id"]),
                                                    folder_name=text, return_raw=True)
        if isinstance(folder_result, dict):
            data = folder_result.get("data") or folder_result
            if isinstance(data, dict):
                folder_id = str(data.get("folder_id") or data.get("id") or "")
        if not folder_id:
            # 目录已存在时：列出根目录，匹配同名
            list_result = await self._api._execute_action(event, "get_group_file_list",
                                                     group_id=int(raw["group_id"]),
                                                     folder_id="", return_raw=True)
            if isinstance(list_result, dict):
                list_data = list_result.get("data") or list_result
                # 宽松兜底：尝试多种 OneBot 实现的文件夹列表字段
                items = (
                    list_data.get("folders")
                    or list_data.get("file_list")
                    or list_data.get("items")
                    or []
                )
                if not isinstance(items, list):
                    logger.warning(
                        f"gm: get_group_file_list 返回结构异常: {list_data}"
                    )
                    items = []
                for item in items:
                    if isinstance(item, dict) and str(item.get("folder_name", "")) == text:
                        folder_id = str(item.get("folder_id", ""))
                        break
        # 2) 上传文件到群文件
        # 截取 URL 文件名做默认显示名
        file_name = image_url.rsplit("/", 1)[-1].split("?")[0] or "image.jpg"
        ok = await self._api._execute_action(event, "upload_group_file",
                                         group_id=int(raw["group_id"]),
                                         file=image_url, name=file_name,
                                         folder_id=folder_id or "")
        if not ok:
            # 部分 OneBot 实现：folder_id 不允许空字符串
            ok = await self._api._execute_action(event, "upload_group_file",
                                             group_id=int(raw["group_id"]),
                                             file=image_url, name=file_name)
        yield event.plain_result(
            f"已上传到群相册「{text}」" if ok
            else "上传到群相册失败（当前 OneBot 实现可能不支持群相册 API）"
        )

# #166: /群名称 — 修改本群名（群管/群主）
    @filter.command("群名", "修改本群名称（/群名 新群名）", alias={"群名称", "改群名", "修改群名"})
    async def set_group_name_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        text = self._mp._extract_text(raw).strip()
        for prefix in ("/群名称", "/群名", "群名称", "群名"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        if not text:
            yield event.plain_result("请提供新群名，例如 /群名 我的群")
            return
        if len(text) > 60:
            yield event.plain_result("群名过长（最多60字符）")
            return
        ok = await self._api._execute_action(event, "set_group_name",
                                        group_id=group_id, group_name=text)
        if self._history._should_notify_group_name(group_id, ok):
            yield event.plain_result(f"已修改群名为「{text}」" if ok else "修改群名失败（当前 OneBot 实现可能不支持此 API，或机器人权限不足）")

    # #163: /群标签 — 添加群标签（群管/群主）
    @filter.command("群标签", "添加群标签（/群标签 标签名）")
    async def set_group_tag_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        text = self._mp._extract_text(raw).strip()
        for prefix in ("/群标签", "群标签"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        if not text:
            yield event.plain_result("请提供标签内容，例如 /群标签 编程交流")
            return
        if len(text) > 20:
            yield event.plain_result("标签过长（最多20字符）")
            return
        # #242: 不同 OneBot 实现的参数名不一致（tag= 或 tags=），先按 tag= 调用，
        # 失败再用 tags=[text] 兼容重试，并拆分失败原因便于定位
        result = await self._api._call_onebot_raw(event, "set_group_tag",
                                             group_id=group_id, tag=text)
        if not self._api._action_result_success(result):
            retry = await self._api._call_onebot_raw(event, "set_group_tag",
                                                group_id=group_id, tags=[text])
            if self._api._action_result_success(retry):
                result = retry
        if self._api._action_result_success(result):
            yield event.plain_result(f"已添加群标签「{text}」")
            return
        logger.warning(f"添加群标签失败: group={group_id} tag={text} result={result}")
        detail = self._api._describe_action_failure(result, 'set_group_tag')
        # #249：set_group_tag 是 LLOneBot 等个别协议端的扩展接口，并非 OneBot v11
        # 标准 action，go-cqhttp / NapCat / Lagrange 均未提供，无跨实现候选可回退；
        # 失败时把这一点讲清楚，避免用户误以为是插件或权限问题
        if ("不支持" in detail) or ("无可用调用通路" in detail):
            detail += ("。set_group_tag 并非 OneBot v11 标准接口，仅部分协议端"
                       "（如 LLOneBot）提供，当前协议端不支持且无替代接口；"
                       "请更换/升级协议端，或在 QQ 客户端手动设置群标签")
        yield event.plain_result(f"添加群标签失败：{detail}")

    # #162: /添加违禁图片 — 引用图片消息加入违禁图列表（群管/群主）
    @filter.command("添加违禁图片", "引用图片消息加入违禁图列表")
    async def add_banned_image_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        reply_id = self._mp._get_reply_id(event)
        if not reply_id:
            yield event.plain_result("请先在群聊发送图片，然后引用该图片回复 /添加违禁图片")
            return
        # 拉取被引用消息中的图片
        msg = await self._api._execute_action(event, "get_msg",
                                          message_id=int(reply_id), return_raw=True)
        if not msg:
            yield event.plain_result("无法获取引用消息内容")
            return
        msg_data = msg.get("data") if isinstance(msg, dict) else msg
        image_url = ""
        if isinstance(msg_data, dict):
            for seg in (msg_data.get("message") or []):
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "image":
                    image_url = (seg.get("data") or {}).get("url", "") or (seg.get("data") or {}).get("file", "")
                    if image_url:
                        break
        if not image_url:
            yield event.plain_result("引用消息中未找到图片")
            return
        md5, truncated = await self._mod_image._compute_image_md5(image_url)
        if truncated:
            yield event.plain_result("图片过大（超过 10MB），无法加入违禁列表")
            return
        if not md5:
            yield event.plain_result("下载图片失败，无法计算 MD5")
            return
        # 写入本群覆盖（统一走 _get_group_override_list）
        banned = self._store.get_group_override_list(group_id, "banned_images")
        if md5 in banned:
            yield event.plain_result("该图片已在本群违禁列表中")
            return
        banned.append(md5)
        try:
            self._store.save_config()
        except Exception as e:
            # 持久化失败时回滚内存态，避免用户下次被提示『已存在』
            banned.remove(md5)
            logger.error(f"保存违禁图片配置失败: {e}")
            yield event.plain_result("保存失败，未添加该违禁图片")
            return
        yield event.plain_result(f"已添加违禁图片（MD5: {md5[:8]}...），本群现有 {len(banned)} 张违禁图")

    # #162: /删除违禁图片 — 删除本群某张违禁图（群管/群主）
    @filter.command("删除违禁图片", "删除本群某张违禁图（/删除违禁图片 <md5前8位>）")
    async def remove_banned_image_cmd(self, event: AstrMessageEvent, md5_prefix: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        # #162 review: 校验 md5_prefix 为 hex 字符（仅 0-9a-f），避免与未来 sha256 等格式冲突
        md5_prefix = (md5_prefix or "").lower()
        if not re.fullmatch(r"[0-9a-f]{4,32}", md5_prefix):
            yield event.plain_result("MD5 前缀必须为 4-32 位 hex 字符（0-9a-f），例如 /删除违禁图片 1a2b3c4d")
            return
        gconf = self._store.get_group_override_list(group_id, "banned_images")
        banned = list(gconf)
        if not banned:
            yield event.plain_result("本群无违禁图片")
            return
        matches = [m for m in banned if str(m).startswith(md5_prefix)]
        if not matches:
            yield event.plain_result(f"未找到 MD5 前缀为 {md5_prefix} 的违禁图")
            return
        if len(matches) > 1:
            yield event.plain_result(f"匹配到多张（{len(matches)}），请用更长的前缀：\n" + "\n".join(matches))
            return
        target = matches[0]
        self._store.get_group_override_list(group_id, "banned_images").remove(target)
        try:
            self._store.save_config()
        except Exception as e:
            # 持久化失败时回滚内存态
            self._store.get_group_override_list(group_id, "banned_images").append(target)
            logger.error(f"保存违禁图片配置失败: {e}")
            yield event.plain_result("保存失败，未删除该违禁图片")
            return
        yield event.plain_result(f"已删除违禁图片 {target}，本群剩余 {len(banned) - 1} 张")

    # #162: /查看违禁图片 — 查看本群违禁图列表（群管/群主）
    @filter.command("查看违禁图片", "查看本群违禁图片列表")
    async def list_banned_images_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        sender_id = str(raw.get("user_id"))
        if not self._perms._is_authorized(raw, sender_id):
            yield event.plain_result("只有群管理员或群主可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        banned = self._store.get_group_override_list(group_id, "banned_images")
        global_banned = self.config.get("banned_images", [])
        lines = []
        if banned:
            lines.append(f"本群违禁图（{len(banned)} 张）：")
            for m in banned:
                lines.append(f"  - {m}")
        if isinstance(global_banned, list) and global_banned:
            lines.append(f"\n全局违禁图（{len(global_banned)} 张）：")
            for m in global_banned:
                lines.append(f"  - {m}")
        # #184：展示 WebUI 上传的违禁图片文件
        uploaded = self.config.get("banned_image_files", []) or []
        if uploaded:
            lines.append(f"\nWebUI 上传违禁图（{len(uploaded)} 张，自动计算 MD5）：")
            for rel in uploaded:
                lines.append(f"  - {rel}")
        if not lines:
            yield event.plain_result("本群与全局均无违禁图片")
            return
        yield event.plain_result("\n".join(lines))

    # ===================== 状态查看（#193：独立指令替代 /设置群配置） =====================

    async def _toggle_body(self, event, value, key, label):
        """Bool 开关命令公共体（generator；各命令方法转发其 yield，行为逐条等价）。"""
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        val = value.lower() not in ("off", "false", "0", "关", "关闭")
        self._store.set_group_override(gid, key, val)
        yield event.plain_result(f"[成功] 本群{label}已设为 {'开启' if val else '关闭'}")

    async def _set_group_int_seconds_body(self, event, seconds, key, label):
        """按群「禁言时长（秒）」设置命令公共体（generator；各命令转发其 yield，行为逐条等价）。
        覆盖 图片/刷屏/骂人/广告/链接/群号推广 六个同构命令。"""
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if seconds <= 0:
            yield event.plain_result("[错误] 禁言时长必须大于0秒")
            return
        self._store.set_group_override(group_id, key, seconds)
        yield event.plain_result(f"[成功] 本群{label}禁言时长已设置为 {seconds} 秒")

    async def _list_group_global_body(self, event, key, label, unit):
        """「本群 + 全局」列表查看命令公共体（generator；行为逐条等价）。
        覆盖 白名单（unit=人）/ 链接白名单（unit=个）。"""
        if not await self._perms._moderation_require_admin_msg(event):
            return
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_wl = self._store.get_group_setting(group_id, key, [])
        global_wl = self.config.get(key, [])
        lines = []
        if group_wl:
            lines.append(f"本群{label}（{len(group_wl)} {unit}）：")
            lines.extend([f"  {i+1}. {x}" for i, x in enumerate(group_wl)])
        if isinstance(global_wl, list) and global_wl:
            lines.append(f"全局{label}（{len(global_wl)} {unit}）：")
            lines.extend([f"  {i+1}. {x}" for i, x in enumerate(global_wl)])
        if not lines:
            yield event.plain_result(f"本群与全局{label}均为空")
            return
        yield event.plain_result("\n".join(lines))

    async def _batch_group_admin(self, event, group_id, qq_list, enable):
        """批量设置/取消群管理的公共文案（行为逐条等价）。enable=True 设置，False 取消。"""
        results = []
        for qq in qq_list:
            ok = await self._api._set_group_admin(event, group_id, qq, enable)
            results.append((qq, ok))
        ok_list = [q for q, ok in results if ok]
        bad_list = [q for q, ok in results if not ok]
        verb = "设置群管理" if enable else "取消群管理"
        msg = f"{verb}成功: {', '.join(ok_list)}" if ok_list else f"{verb}全部失败"
        if bad_list:
            msg += f"\n失败: {', '.join(bad_list)}"
        return msg

    async def _edit_group_override_qqs(self, event, target, key, add, ok_tpl, empty_msg):
        """「@目标增删到本群覆盖列表」命令公共体（generator；行为逐条等价）。
        覆盖 黑名单 / 举报通知QQ / 加群通知QQ 三组增删命令；仅结果文案与列表 key 不同。"""
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        raw = self._mp._get_raw_message(event)
        qq_list = self._mp._extract_at_qqs(raw if isinstance(raw, dict) else {}) or _parse_qq_list(target)
        if not qq_list: yield event.plain_result("请通过 @某人 或QQ号指定目标"); return
        lst = self._store.get_group_override_list(gid, key)
        if add:
            changed = [q for q in qq_list if q not in [str(x) for x in lst] and (lst.append(q) or True)]
        else:
            changed = [q for q in qq_list if q in [str(x) for x in lst] and (lst.remove(q) or True)]
        self._store.save_config()
        yield event.plain_result(ok_tpl.format(n=len(changed), total=len(lst)) if changed else empty_msg)

    # --- Bool 开关指令 ---
    @filter.command("开关撤回提示", "开关撤回消息提示（on/off，按群生效）")
    async def toggle_recall_notice_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "show_recall_notice", "撤回提示"):
            yield r

    @filter.command("开关禁言提示", "开关禁言/解禁回复结果（on/off，按群生效）")
    async def toggle_mute_notice_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "mute_notice", "禁言提示"):
            yield r

    @filter.command("开关群名提示", "开关修改群名结果回复（on/off，按群生效）")
    async def toggle_group_name_notice_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "group_name_notice", "群名提示"):
            yield r

    @filter.command("开关踢人拒加", "开关踢人后拒绝重新加群（on/off，按群生效）")
    async def toggle_reject_re_add_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "reject_re_add", "踢人拒加"):
            yield r

    @filter.command("开关管理员豁免", "开关管理员/群主跳过违规检测（on/off，按群生效）")
    async def toggle_admin_bypass_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "admin_bypass", "管理员豁免"):
            yield r

    @filter.command("开关违规通知", "开关违规时群内通知（on/off，按群生效）")
    async def toggle_notify_on_violation_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "notify_on_violation", "违规通知"):
            yield r

    @filter.command("开关加群申请提醒", "开关加群申请群内通知提醒（on/off，按群生效）")
    async def toggle_join_notify_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "join_request_notify_in_group", "加群申请提醒"):
            yield r

    @filter.command("开关加群自动审核", "开关加群申请自动审核总开关（on/off，按群生效）")
    async def toggle_join_audit_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "join_audit_enabled", "加群自动审核"):
            yield r

    @filter.command("开关踢人清历史", "开关踢人时自动撤回消息（on/off，按群生效）")
    async def toggle_kick_recall_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "kick_recall_enabled", "踢人清历史"):
            yield r

    @filter.command("开关公告通知", "开关发群公告后发送群内提醒（on/off，按群生效）")
    async def toggle_announce_notify_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "announce_notify", "公告通知"):
            yield r

    @filter.command("开关二维码检测", "开关图片二维码检测（on/off，按群生效）")
    async def toggle_qr_check_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "qr_check_enabled", "二维码检测"):
            yield r

    @filter.command("开关语音检测", "开关语音消息转文字违规检测（on/off，按群生效）")
    async def toggle_voice_check_cmd(self, event: AstrMessageEvent, value: str = ""):
        async for r in self._toggle_body(event, value, "voice_check_enabled", "语音检测"):
            yield r

    # --- Int 设置指令 ---
    @filter.command("设置排名人数", "设置发言排名榜显示人数（按群生效）")
    async def set_rank_top_n_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        try: n = int(value)
        except (ValueError, TypeError): yield event.plain_result("[错误] 请提供数字，例如：/设置排名人数 10"); return
        if n < 1: yield event.plain_result("[错误] 必须大于0"); return
        self._store.set_group_override(gid, "rank_top_n", n)
        yield event.plain_result(f"[成功] 本群排名人数已设为 {n}")

    @filter.command("设置踢人阈值", "设置禁言次数达阈值自动踢出（0=关闭，按群生效）")
    async def set_mute_kick_threshold_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        try: n = int(value)
        except (ValueError, TypeError): yield event.plain_result("[错误] 请提供数字，例如：/设置踢人阈值 3"); return
        if n < 0: yield event.plain_result("[错误] 必须>=0"); return
        self._store.set_group_override(gid, "mute_kick_threshold", n)
        yield event.plain_result(f"[成功] 本群踢人阈值已设为 {'关闭' if n == 0 else f'{n}次'}")

    @filter.command("设置消息历史条数", "设置撤回消息历史缓存条数（按群生效）")
    async def set_max_history_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        try: n = int(value)
        except (ValueError, TypeError): yield event.plain_result("[错误] 请提供数字，例如：/设置消息历史条数 50"); return
        if n < 10: yield event.plain_result("[错误] 必须>=10"); return
        self._store.set_group_override(gid, "max_message_history", n)
        yield event.plain_result(f"[成功] 本群消息历史条数已设为 {n}")

    @filter.command("设置踢人清条数", "设置踢人时自动撤回消息条数（按群生效）")
    async def set_kick_recall_count_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        try: n = int(value)
        except (ValueError, TypeError): yield event.plain_result("[错误] 请提供数字，例如：/设置踢人清条数 10"); return
        if n < 1 or n > 50: yield event.plain_result("[错误] 范围 1-50"); return
        self._store.set_group_override(gid, "kick_recall_count", n)
        yield event.plain_result(f"[成功] 本群踢人清条数已设为 {n}")

    # --- String 设置指令 ---
    @filter.command("设置拒绝理由", "设置加群申请自动拒绝理由（按群生效）")
    async def set_reject_reason_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: return
        if not value: yield event.plain_result("[错误] 请提供理由，例如：/设置拒绝理由 请填写真实信息"); return
        self._store.set_group_override(gid, "join_reject_reason", value)
        yield event.plain_result(f"[成功] 本群拒绝理由已设为「{value}」")

    # --- List 增删查指令 ---
    @filter.command("添加自动撤回关键词", "添加Bot发言自动撤回关键词（按群生效）")
    async def add_auto_recall_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        kw = (keyword or "").strip()
        if not kw: yield event.plain_result("[错误] 请提供关键词"); return
        ok, kws = self._store.mutate_group_list(gid, "auto_recall_keywords", "add", kw)
        if not ok: yield event.plain_result(f"[错误] 关键词 '{kw}' 已存在"); return
        yield event.plain_result(f"[成功] 已添加本群自动撤回关键词 '{kw}'（当前 {len(kws)} 个）")

    @filter.command("删除自动撤回关键词", "删除Bot发言自动撤回关键词（按群生效）")
    async def remove_auto_recall_keyword_cmd(self, event: AstrMessageEvent, keyword: str = ""):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        kw = (keyword or "").strip()
        if not kw: yield event.plain_result("[错误] 请提供关键词"); return
        ok, kws = self._store.mutate_group_list(gid, "auto_recall_keywords", "remove", kw)
        if not ok: yield event.plain_result(f"[错误] 关键词 '{kw}' 不存在"); return
        yield event.plain_result(f"[成功] 已删除本群自动撤回关键词 '{kw}'（当前 {len(kws)} 个）")

    @filter.command("查看自动撤回关键词", "查看Bot发言自动撤回关键词列表（本群+全局）")
    async def list_auto_recall_keywords_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        g = self._store.get_group_setting(gid, "auto_recall_keywords", [])
        gl = self.config.get("auto_recall_keywords", [])
        lines = []
        if g: lines.extend([f"本群（{len(g)} 个）："] + [f"  {i+1}. {kw}" for i, kw in enumerate(g)])
        if isinstance(gl, list) and gl: lines.extend([f"全局（{len(gl)} 个）："] + [f"  {i+1}. {kw}" for i, kw in enumerate(gl)])
        yield event.plain_result("\n".join(lines) if lines else "本群与全局均无自动撤回关键词")

    @filter.command("添加举报通知QQ", "添加接收举报通知的管理员QQ（按群生效）")
    async def add_report_notify_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "report_notify_admins", True,
            "[成功] 已添加 {n} 人（当前 {total} 人）", "[提示] 所选QQ已在列表中",
        ):
            yield r

    @filter.command("删除举报通知QQ", "删除接收举报通知的管理员QQ（按群生效）")
    async def remove_report_notify_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "report_notify_admins", False,
            "[成功] 已删除 {n} 人", "[提示] 所选QQ不在列表中",
        ):
            yield r

    @filter.command("查看举报通知QQ", "查看本群接收举报通知的管理员QQ列表")
    async def list_report_notify_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        ql = self._store.get_group_setting(gid, "report_notify_admins", [])
        yield event.plain_result(f"本群举报通知QQ：{', '.join(ql) if ql else '空'}")

    @filter.command("添加加群通知QQ", "添加加群请求通知管理员QQ（按群生效）")
    async def add_join_notify_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "join_notify_admins", True,
            "[成功] 已添加 {n} 人（当前 {total} 人）", "[提示] 所选QQ已在列表中",
        ):
            yield r

    @filter.command("删除加群通知QQ", "删除加群请求通知管理员QQ（按群生效）")
    async def remove_join_notify_cmd(self, event: AstrMessageEvent, target: str = ""):
        async for r in self._edit_group_override_qqs(
            event, target, "join_notify_admins", False,
            "[成功] 已删除 {n} 人", "[提示] 所选QQ不在列表中",
        ):
            yield r

    @filter.command("查看加群通知QQ", "查看本群加群请求通知管理员QQ列表")
    async def list_join_notify_cmd(self, event: AstrMessageEvent):
        if not await self._perms._moderation_require_admin_msg(event): return
        gid = self._mp._get_group_id_or_none(event)
        if not gid: yield event.plain_result("此指令只能在群聊中使用"); return
        ql = self._store.get_group_setting(gid, "join_notify_admins", [])
        yield event.plain_result(f"本群加群通知QQ：{', '.join(ql) if ql else '空'}")

    @filter.command("查看群配置", "查看本群生效的配置覆盖")
    async def view_group_config_cmd(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw.get("group_id"))
        gconf = self._store.runtime_map("group_overrides").get(group_id, {})
        if not gconf:
            yield event.plain_result("本群未设置任何覆盖（全部使用全局默认）")
            return
        lines = [f"{k}: {v}" for k, v in gconf.items()]
        yield event.plain_result(f"本群覆盖配置：\n" + "\n".join(lines))

    @filter.command("清除群配置", "清除本群所有覆盖")
    async def clear_group_config_cmd(self, event: AstrMessageEvent, key: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        if not self._perms._is_authorized(raw, str(raw.get("user_id"))):
            yield event.plain_result("只有插件管理员可执行此操作")
            return
        group_id = str(raw.get("group_id"))
        gconf = self._store.runtime_map("group_overrides").get(group_id, {})
        if key:
            if key not in gconf:
                yield event.plain_result(f"本群未设置 {key}")
                return
            del gconf[key]
            self._store.save_config()
            yield event.plain_result(f"已清除本群 {key} 覆盖")
        else:
            overrides = self._store.runtime_map("group_overrides")
            if group_id in overrides:
                del overrides[group_id]
                self._store.save_config()
            yield event.plain_result("已清除本群所有覆盖")

    @filter.command("status", "查看插件配置")
    async def status_cmd(self, event: AstrMessageEvent):
        c = self.config
        raw = self._mp._get_raw_message(event)
        group_id = str(raw.get("group_id", "")) if isinstance(raw, dict) else ""
        lines = [
            f"show_recall_notice: {c.get('show_recall_notice', True)}",
            f"reject_re_add: {c.get('reject_re_add', False)}",
            f"auto_recall_keywords: {c.get('auto_recall_keywords', [])}",
            f"violation_keywords: {len(c.get('violation_keywords', []))} 个",
            f"rank_top_n: {c.get('rank_top_n', 10)}",
        ]
        if group_id:
            overrides = self._store.runtime_map("group_overrides").get(group_id, {})
            lines.extend([
                f"本群 group_admin_admins: {', '.join(self._perms._effective_group_admin_admins(group_id)) or '空'}",
                f"本群 mute_kick_threshold: {self._store.get_group_setting(group_id, 'mute_kick_threshold', 0)}"
                f"{'（按群覆盖）' if 'mute_kick_threshold' in overrides else '（全局默认）'}",
            ])
        yield event.plain_result("插件配置：\n" + "\n".join(lines))

    # ===================== 全消息监听 =====================

    @filter.command("重复表情包撤回", "按群覆盖重复表情包自动撤回（开/关，#196）")
    async def toggle_dup_face_recall_cmd(self, event: AstrMessageEvent, value: str = ""):
        if not await self._perms._moderation_require_admin_msg(event):
            return
        gid = self._mp._get_group_id_or_none(event)
        if not gid:
            yield event.plain_result("此指令只能在群聊中使用")
            return
        v = (value or "").strip().lower()
        if v in ("开", "on", "true", "开启"):
            enabled = True
        elif v in ("关", "off", "false", "关闭"):
            enabled = False
        else:
            enabled = not self._dup_face._dup_face_enabled(gid)
        self._store.set_group_override(gid, "dup_face_recall_enabled", enabled)
        yield event.plain_result(f"[成功] 本群重复表情包撤回已{'开启' if enabled else '关闭'}")

    # ===================== #254 口语化群管指令 =====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息：发言计数 + 违规检测。"""
        raw = self._mp._get_raw_message(event)
        if not raw or not isinstance(raw, dict):
            return
        if raw.get("post_type") != "message":
            return
        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))
        # 跳过 bot 自身
        if str(raw.get("self_id", "")) == user_id:
            return

        # #254：@bot + 口语化群管请求优先处理，命中即返回（避免重复计数/历史污染）
        if await self._colloquial._handle_colloquial_admin_request(event, raw, group_id, user_id):
            return

        # 发言计数（#29）
        self._stats._increment_message_count(group_id, user_id)

        # 撤回消息历史：记录该群消息（插件指令消息不记录，避免编号偏移）
        self._history._record_message_to_history(group_id, raw)

        # 群违规检测（合并自参考插件，#19 + 图片/刷屏/骂人/广告/链接/群号推广）
        await self._moderation._moderation_dispatch(event, raw, group_id, user_id)

        # #196：重复表情包自动撤回（全局开关 + 按群覆盖）
        await self._dup_face._dup_face_recall_check(event, raw, group_id)

        # 加群申请引用回复处理（#57）—— 阶段4 委托 JoinReviewService（原逻辑迁至 cop/join_review.py）
        reply_id = self._mp._get_reply_id(event)
        async for _reply_result in self._join_review._handle_group_request_reply(
                event, raw, group_id, user_id, reply_id):
            yield _reply_result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_event(self, event: AstrMessageEvent):
        raw = self._mp._get_raw_message(event)
        if not raw or not isinstance(raw, dict):
            return

        # 禁言通知统计（#103）
        if raw.get("post_type") == "notice" and raw.get("notice_type") == "group_ban":
            group_id = str(raw.get("group_id"))
            target_id = str(raw.get("user_id", ""))
            operator_id = str(raw.get("operator_id", ""))
            try:
                duration = int(raw.get("duration", 0) or 0)
            except (TypeError, ValueError):
                duration = 0
            if target_id and duration > 0:
                await self._stats._record_mute_and_maybe_kick(event, group_id, target_id, operator_id)
            return

        # 入群欢迎 —— 阶段4 委托 JoinReviewService（原逻辑迁至 cop/join_review.py）
        if raw.get("post_type") == "notice" and raw.get("notice_type") == "group_increase":
            await self._join_review._handle_group_increase_notice(event, raw)
            return

        # 加群请求处理（#27 合并 group_manager）—— 阶段4 委托 JoinReviewService
        if raw.get("post_type") == "request" and raw.get("request_type") == "group":
            async for _join_result in self._join_review._handle_group_join_request(event, raw):
                yield _join_result

    # #229：原 /新人加群申请通知（join_request_notify_enabled，#205 全局开关）已随
    # 「两项加群通知配置略微重复，删除一项」的清理一并移除。群内提醒开关统一由
    # /开关加群申请提醒（join_request_notify_in_group，按群生效）负责；如需彻底
    # 静音，把 join_notify_admins / /查看加群通知QQ 的列表留空即可（无收件人即不发送）。

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """Bot 自身发言后：记录撤回历史，并在命中关键词时自动撤回（#46）。

        ⚠️ 关键点：本钩子回调时 event 的 raw_message 依旧是【用户发来的那条消息】，
        并不包含 bot 刚发出的消息内容与 ID。因此：
          1. 判断 bot 发言必须取 event.get_result().chain（respond 阶段在调用本钩子
             之后才 clear_result），否则关键词永远匹配不到 bot 的发言；
          2. 撤回所需的 message_id 需回查 OneBot get_group_msg_history 中发送者为
             自身的最新消息（框架不会把已发送消息的 ID 回传给插件）。
        """
        group_id = self._mp._get_group_id_or_none(event)
        if not group_id:
            return

        # 1) 本次实际发送的消息链 → 纯文本（用于关键词匹配）
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        bot_text = MessageParser._extract_text_from_chain(chain)

        # 2) 回查机器人刚发送的消息，拿到真实 message_id
        bot_messages = await self._history._fetch_recent_bot_messages(event, group_id)
        if bot_messages:
            newest_id, newest_text, newest_preview, newest_time = bot_messages[0]
            # 撤回消息历史：记录 bot 自身发言，用于 /撤回自身 / /撤回 N（#117 #118 #122）
            self._history._add_message_to_history(
                group_id, newest_id,
                newest_preview or newest_text or "[无文本内容]",
                self._api._get_self_id(event) or "bot", "机器人",
                is_bot=True, msg_time=newest_time,
            )
        elif bot_text:
            logger.debug(
                "[自动撤回] 未能从群消息历史定位机器人刚发送的消息 ID"
                "（当前 OneBot 实现可能不在 get_group_msg_history 中返回机器人自身消息）"
            )

        # 3) 关键词自动撤回（#46）
        # #260：只用「本次发送链的可见文本」判定——纯图片/表情包消息 bot_text 为空，
        # 直接返回，不会再用图片文件名/URL 等回退文本匹配关键词而被误撤回。
        enabled = self._store.get_group_setting(group_id, "auto_recall_enabled_groups", [])
        keywords = self._store.get_group_setting(group_id, "auto_recall_keywords", [])
        # #192 owner 拍板：留空 = 全群启用（keywords 为空时本就无命中，不产生实际撤回）；
        # 按群覆盖 bool 可单群显式开/关；非空列表按 * / all / 群号判定
        if isinstance(enabled, bool):
            if not enabled:
                return
        elif enabled and "*" not in [str(x) for x in enabled] \
                and "all" not in [str(x) for x in enabled] and group_id not in [str(x) for x in enabled]:
            return
        if not keywords or not bot_text:
            return
        if not any(kw in bot_text for kw in keywords):
            return

        # 只撤回本次发送窗口内的 bot 消息，避免误伤更早的历史发言；
        # 命中判定同样只用纯文本（不拿预览/文件名匹配），避免表情包被误撤回（#260）
        now = int(time.time())
        recent = [m for m in bot_messages if not m[3] or now - m[3] <= 30]
        matched = [m for m in recent if m[1] and any(kw in m[1] for kw in keywords)]
        targets = matched[:3] if matched else recent[:1]
        if not targets:
            logger.warning(
                "[自动撤回] 已命中关键词，但未能定位机器人刚发送的消息 ID，撤回失败："
                "当前 OneBot 实现可能不在 get_group_msg_history 中返回机器人自身消息"
            )
            return
        logger.debug(f"[自动撤回] 群 {group_id} 命中关键词，目标消息 {[m[0] for m in targets]}")
        for msg_id, _text, _preview, _t in targets:
            # #202：记录撤回结果，失败时给出原因，避免静默
            ok, err = await self._api._do_recall(event, msg_id)
            if ok:
                self._history._remove_message_from_history(group_id, msg_id)
                logger.info(f"[自动撤回] 命中关键词，已撤回 bot 消息 {msg_id}")
            elif "已撤回" in err or "超时" in err:
                self._history._remove_message_from_history(group_id, msg_id)
            else:
                logger.warning(f"[自动撤回] 撤回 bot 消息 {msg_id} 失败: {err}")
