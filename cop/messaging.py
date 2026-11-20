"""L3a 消息/指令公共辅助服务。

本模块承载与「命令层公共辅助 / 去重收敛」相关、且能证明行为逐字等价的逻辑。
迁出自 main.py 的以下方法：
    _edit_special_admins（专项管理员增删公共实现）

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
- ConfigStore（L1）：add_group_override_admins / remove_group_override_admins
- PermissionService（L2）：_is_authorized
- L0 text_utils：_parse_qq_list

main.py 门面保留同名薄包装委托本服务。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .text_utils import _parse_qq_list

if TYPE_CHECKING:
    from .config_store import ConfigStore
    from .message_parse import MessageParser
    from .permissions import PermissionService


class MessagingService:
    """命令层公共辅助（专项管理员增删等）。"""

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

    async def _edit_special_admins(self, event, target: str, key: str, label: str, add: bool):
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
            action = "添加" if add else "删除"
            yield event.plain_result(f"请提供QQ号，例如 /{action}{label}管理 123456")
            return
        if add:
            changed = self._store.add_group_override_admins(group_id, key, qq_list)
            verb = "添加"
            empty = "所列QQ号均已存在"
        else:
            changed = self._store.remove_group_override_admins(group_id, key, qq_list)
            verb = "移除"
            empty = "所列QQ号均不存在"
        detail = ", ".join(changed) if changed else empty
        yield event.plain_result(f"已为群 {group_id} {verb}{label}专项管理员: {detail}")
