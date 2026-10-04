"""L2 运行时状态层：承载插件进程内可变状态（内存态 + 持久化数据的引用）。

迁出自 main.py 原 __init__ 中初始化的一批可变状态。RuntimeState 仅作为数据容器，
不持有任何业务方法；main.py 门面通过同名 property 转发到本对象（可写字段配 setter），
以保证全部既有调用点（self.stats / self.message_history / self.spam_records ...）零改动。

依赖规则：本模块不 import 任何 cop 同层 / 下层模块（stats / reports 的内容由门面
在构造时从磁盘加载后传入），从而保持「同层不互相 import」。
"""

from __future__ import annotations

from collections import defaultdict


class RuntimeState:
    """插件运行时可变状态容器（纯数据）。"""

    def __init__(self, *, stats, reports, max_history: int):
        # 统计（持久化；门面负责 save_stats）
        self.stats = stats
        # 举报待处理（持久化；门面负责 save_reports）
        self.reports = reports

        # 撤回消息历史（对齐 astrbot_plugin_batchrecall，修复 #117 #118 #122）：
        # 结构：message_history[group_id] -> list[(message_id, content_preview, timestamp,
        #       sender_id, sender_name, is_bot)]，最新消息在列表最前面（编号从 1 开始）。
        self.message_history: dict = {}
        self.max_history: int = max_history

        # #196：重复表情包检测的近期 _seen 缓存（内存态，不持久化）
        # key 为 (群号, 发送者QQ)，#239 起按发送者隔离，避免跨用户误撤回
        self._dup_face_seen: dict = {}
        # #238：每个群最近处理过的 message_id，用于丢弃重复投递的同一消息
        self._dup_face_last_mid: dict = {}

        # 群违规检测运行时状态（#109 PR #1）：spam_records[(group_id, user_id)] -> [timestamp, ...]
        # 仅内存，进程重启清空（与参考插件行为一致）
        self.spam_records = defaultdict(list)

        # #152：发言计数批量持久化计数器
        self._msg_save_counter: int = 0

        # #184：WebUI 上传违禁图片文件的 MD5 缓存
        # {相对路径: md5}；未命中缓存的路径在运行时懒计算并回填。
        self._banned_file_md5_cache: dict = {}

        # 群号存在性探测缓存（#267）：group_exists_cache[群号] -> (exists: bool, timestamp: float)
        # 仅内存，进程重启清空（探测结果有时效，不做持久化）。
        self.group_exists_cache: dict = {}

        # #241：群员邀请自动通过速率限制记录（内存态，进程重启清零）
        # 结构：invite_approve_records[group_id][inviter_user_id] -> [timestamp, ...]
        self.invite_approve_records: dict = {}
