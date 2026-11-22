"""L3a 消息/指令公共辅助服务。

本模块承载命令层公共辅助逻辑，包括统一插件管理员名单和旧通知名单的管理。

去重收敛取舍说明（高风险，务必保持行为 100% 不变）：
1. 撤回主循环：recall_cmd（@用户分支 / 仅数量分支）与 recall_self_cmd 两份实现，
   差异点为：
     - recall_self_cmd 每轮 `if success >= count: break`（按成功数封顶），
       而 recall_cmd 按 candidates 切片封顶；
     - 失败收集：recall_self_cmd 收集 `f"{id}({err})"` 且只展示前 5 条，
       recall_cmd 只做 failed 计数不收集详情；
     - sleep：recall_cmd 每轮 asyncio.sleep(0.3)，recall_self_cmd 无 sleep；
     - 幽灵消息清理仅 recall_cmd 有。
   这些差异会导致输出/副作用不逐字等价，故**本阶段不强行统一**，两份实现
   各自保留在原命令方法内（留待阶段 4 视需要参数化抽取）。
2. 踢人：kick_cmd 与口语化 kick 分支文案不同（批量成功/失败汇总 vs 单人
   "已踢出 {target}"），不得改文案，故不合并文案；如需共享流程须以 notify
   文案参数区分，本阶段暂不抽取，避免任何行为漂移。
3. enabled_groups 判定统一读取模板开关，但只有加群审核兼容旧运行时列表覆盖。

依赖（构造注入，L3 只依赖 L0/L1/L2，不 import 同层）：
- MessageParser（L2）：_get_raw_message / _extract_at_qqs
- ConfigStore（L1）：add_group_override_admins / remove_group_override_admins / edit_legacy_notification_recipients
- PermissionService（L2）：_is_group_admin_or_owner
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from astrbot.api import logger

from .text_utils import normalize_qq_list

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .permissions import PermissionService


class MessagingService:
    """命令层公共辅助（统一插件管理员增删）。"""

    def __init__(
        self,
        *,
        message_parse: "MessageParser",
        config_store: "ConfigStore",
        permissions: "PermissionService",
    ):
        self._mp = message_parse
        self._store = config_store
        self._perms = permissions

    async def _manage_admin_list(self, event, target: str, *, add: bool | None, legacy_key: str = ""):
        raw = self._mp._get_raw_message(event)
        if not raw or not raw.get("group_id"):
            yield event.plain_result("此指令只能在群聊中使用")
            return
        group_id = str(raw["group_id"])
        if not self._perms._is_group_admin_or_owner(raw):
            yield event.plain_result("只有 QQ 群管理员或群主可管理本群管理员或旧通知名单")
            return

        if legacy_key and self._store.get_plugin_admins(group_id) is not None:
            yield event.plain_result("本群已配置统一插件管理员，旧通知名单不生效。请用 /查看插件管理员 查看。")
            return
        label = {"report_notify_admins": "举报通知QQ", "join_notify_admins": "加群通知QQ"}.get(legacy_key, "插件管理员")
        if add is None:
            configured = normalize_qq_list(self._store.get_group_setting(group_id, legacy_key, []))
            yield event.plain_result(
                f"本群旧{label}（独立配置）：{', '.join(configured) or '空'}（不授予管理权限）"
                + self._legacy_report_fallback_hint(group_id, legacy_key))
            return

        targets = self._mp._extract_at_qqs(raw) or re.split(r"[\s,，、]+", (target or "").strip())
        qq_list = list(dict.fromkeys(qq for qq in targets if re.fullmatch(r"[1-9][0-9]*", qq)))
        action = "添加" if add else "删除"
        if not qq_list:
            yield event.plain_result(f"请提供有效QQ号或 @成员，例如 /{action}{label} 123456")
            return

        try:
            if legacy_key:
                changed = self._store.edit_legacy_notification_recipients(group_id, legacy_key, qq_list, add=add)
                if changed is None:
                    yield event.plain_result("本群已配置统一插件管理员，旧通知名单不生效。")
                    return
            elif add:
                changed = self._store.add_group_override_admins(group_id, "plugin_admins", qq_list)
            else:
                changed = self._store.remove_group_override_admins(group_id, "plugin_admins", qq_list)
        except Exception as e:
            logger.error(f"{action}{label}失败（群 {group_id}）: {e}")
            yield event.plain_result("[错误] 保存失败，本次修改未生效，请稍后重试")
            return
        if not changed:
            detail = "所列QQ号均已存在" if add else f"所列QQ号不在本群{label}名单中"
            yield event.plain_result(detail + self._legacy_report_fallback_hint(group_id, legacy_key))
            return
        yield event.plain_result(f"本群已{action}{label}：{', '.join(changed)}")

    def _legacy_report_fallback_hint(self, group_id: str, key: str) -> str:
        if key == "report_notify_admins":
            _, source = self._store.get_admin_notification_recipients(group_id, key)
            if source == "join_notify_admins":
                return "\n举报私聊目前回退到旧加群名单，请用 /查看加群通知QQ 查看，或用 /添加举报通知QQ 配置独立收件人。"
        return ""
