# 项目记忆

累计反思 99 次

## 仓库背景

`mjy1113451/astrbot_plugin_gm` AstrBot 群管理插件。**5 种高频模式**：①权限重构（#130/#132/#139/#140）②引用消息触发（#131）③装饰字符 QQ（#134/#136）④举报路由（#140）⑤**动作联动型（#142/#143 新沉淀）**。其他：撤回/STT/头衔、禁言列表（#135）、解禁（#133）、命令别名（#138）。

- **`breaking-change` 4 档（#142）**：新增配置+默认 false ≤0.30 / 默认 true 0.55-0.65 / 修改既有 0.70-0.80 / 删除既有 0.90-0.95。
- **撤回缓存三层**：`recent_messages`→`message_history`→`get_group_msg_history` 兜底；复用 PR #123 必显式标注 merge/容量/跨群隔离。
- **跨适配器矩阵**：读类字段已建（#135/#136）；撤回 API 待建（#142/#143）：NapCat/Lagrange/go-cqhttp × `delete_msg` 限速 + `get_group_msg_history` 返回上限 + 分页。

## 动作联动型 Issue 模板（#142/#143 第 5 种模式，新沉淀）

- **触发**：动作 A 成功后自动触发动作 B（kick→clear-history / ban→notify / mute→log），共享配置与权限。**owner 追加副作用**是核心信号。
- **必查 10 条**：①事件钩子获取（`group_decrease`/适配器层/轮询）②副作用边界 ③隐私合规 ④跨适配器撤回权限 ⑤缓存链路复用 ⑥速率限制（QQ 风控+bot 封禁）⑦装饰字符防护 ⑧失败回滚（回执 N/M）⑨`group_overrides` 嵌套+生效顺序 ⑩命令命名空间冲突。
- **回执一致性硬约束（#142）**：必须区分"成功 N / 跳过 X 因时间窗 / 失败 Y 因限流"三类计数。
- **通知风暴（#143）**：OneBot 推送"XXX 撤回了一条消息"，批量 N 条会引发群内风暴。
- **去重视（#143）**：本地缓存+服务端历史重复 message_id，循环必须 `set` 去重。
- **撤回时限逐条检测（#142）**：2 分钟时间窗硬约束，helper 返回 `(message_id, time)` 元组。
- **节流方案（#142）**：`asyncio.sleep` 全局阻塞事件循环，推荐令牌桶/滑动窗口/专用节流队列。
- **必给标签**：`enhancement`(0.95)+`command`(0.85-0.95)+`configuration`(0.80-0.85)+**`group-management`(0.88-0.92 硬约束必给)**+`permission-model`(0.80-0.85)+动作 `kick`/`recall`(0.85-0.95)+`message-history`(0.75-0.90)+`auto-action`(0.75 新建)+`privacy`(0.70-0.80 新建)+`onebot`(0.80-0.90)+`onebot-extension`(0.55-0.65 新建)+**`compatibility`(0.55-0.75 硬约束必给)**+`external-reference`(0.85-0.90 参考借鉴) **或** `merge-request`+`external-repo`(0.90 真合入)+`license-check`+`throttling`/`pagination`/`partial-failure`(新建)+`breaking-change`(4 档)+`needs-discussion`(0.75-0.85)+**`needs-info`(≤0.30 双校准对冲必给)**。
- **优先级 medium 决策路径 5 条（#142 升级）**：①横切权限+breaking-change → 最低 medium ②owner+标准化 → 不升 high ③工作量可控+迁移明确 → 不升 high ④跨适配器风险有兜底 → 维持 medium ⑤**默认值决策（默认 false 维持；默认 true 升 high）**。
- **可行性分支**：A 80-200 行/1.5-2 天 / B 200-400 行/3-5 天 / C 400-600 行/5-10 天 / D 分阶段 4-6 天。
- **外部参考 vs 合入（#143 硬约束）**：`external-reference` 借鉴；`merge-request`+`external-repo`+`license-check` 仅真合入，**不可混用**。**必须先访问外部仓库验证实现路径**。

## 其他模式速查

- **命令集重构/双向复合（#139）**："删除命令 A+新增命令 B"对称对 → **必显式拆分两个 Issue** + 分支 A 必含七处同步（main.py/`_GM_COMMAND_NAMES`/README/帮助/CHANGELOG/metadata.yaml/公告）+ **对称权限不对称**（撤销群待办通常需群主 vs 添加侧群管）。
- **举报/通知路由（#140）**：`/举报`按举报人/被举报人角色分级路由（"群主豁免"），9 组合矩阵，必查通知风暴/扇出性能/隐私脱敏，medium。
- **装饰字符 QQ（#134/#136）**：Mathematical Alphanumeric Symbols（U+1D400-U+1D7FF）**不适用于 NFKC**——"加 NFKC 归一化"是错误技术建议。修复：`_extract_at_qq` 顶部加白名单 `\d{5,12}` 强校验。必给：`parser`(0.85)+`at-parse`(0.85)+`unicode-normalization`+`input-validation`(新建)。

## 反模式（P0 级硬约束，#143 第 N+1 次触发）

- **结构化输出校验失败 ≠ 信息不足**：字段校验失败应**仅修复字段输出**，**不得把所有判断退化为"无法评估"/`other`/空标签/无建议**。**Pre-check**：字段出现"无建议/无法评估/空/未检测到重复"前必先确认是否源于校验失败——**局部修复而非整体降级**。**校验失败短路器升为 P0 级前置自检门槛第一优先级，与"必给标签逐项核对"并列**。
- **校验失败短路器强制自检 4 步**（已第 N+1 次触发 #131-#140/#142/#143）：①扫描输出字段是否含"无/无法/空/未检测到重复" ②若含回溯字段层定位 ③实质性判断维持原始判断 ④禁止字段级失败传导整体降级。
- **重复检测措辞强制模板**：**结果行第一行必须写"暂未发现"**，"未检测到重复"/"无重复"/"可能是 #X 的重复"明令禁止。输出末加注"⚠️ 措辞核对"。**#143 第 N+1 次触犯**。
- **删除既有命令+新增对称命令必须显式拆分**（#139）：禁止混合 PR；**七处同步清单对"删除既有命令"强制适用**，纯减法也不能跳过。
- **owner-driven 纯减法应主动降 `low`**（#130/#132/#138）：显式说明"为何维持 medium/降 low"。
- **重复检测前置过滤**：主分类不同 → 上限 0.3；主分类同但 API 不同 → 上限 0.30。
- **同类 Issue 显式互引**：优先级章节对比"与 §X 同级，因 Y 原因定 medium"。**跨 Issue 决策路径同构（#142）**：决策路径同构时必显式互引。
- **必给标签逐项核对**（#136/#139/#140/#142/#143 硬约束）：模板清单**第一步逐项勾选**。**特别必给硬清单**：`group-management`(三次违反)、`needs-info`(≤0.30 双校准 两次违反)、`compatibility`(跨适配器)、`onebot-extension`(OneBot 协议扩展层)、`external-reference` 或 `merge-request`(参考/合入外部仓库时)。
- **可行性分支必须显式 A/B/C/D**（#133/#135/#139）：禁止只给范围估算。
- **优先级决策路径 5 条显式**（#133/#142）：#142 升级加第 5 条"默认值决策"。
- **模板对齐检查**：装饰字符/@解析/禁言/群待办/举报/动作联动类 Issue，**第一步对照模板逐项勾选**。
- **`ast.parse` ≠ 真实加载**：用 `python -m py_compile main.py`。
- **审查评分校准**：撤回/缓存核心回退路径 bug 影响"绝大多数未启用配置群组"时，评分上限 ≤5/10。
- **owner-driven ≠ 无决策**（#135/#140）：权限/字段差异/性能命令新增都有 2-3+ 决策点，`needs-discussion` ≥ 0.65-0.85。
- **标签误标识别**（#138）：`parser` 仅在 `_extract_at_qq`/`_get_reply_id`/装饰字符场景；`recall` 仅在撤回场景。两者均不涉及应**删除**。
- **标题字段禁止"无建议"**：清晰原标题应标"可保留"或给轻量规范化版。"。"等无意义标题必须改写。
- **#140/#142 红旗**：`needs-info` ≤0.30 与 `needs-discussion` ≥0.80 双校准对 owner-driven Issue 必同时满足；bot/被操作者角色查询时 `bot-role` 必给；OneBot API 调用时 `onebot`/`compatibility` 必给（≥0.55）。
- **PR 依赖状态必查证（#142）**：假设"复用 PR #X 骨架"必须显式标注"假设 #X 已 merge（待查证）"。

## 分析经验要点

- **分类**：运行时报错/参数错误/`更新后仍存在`→ `bug`；权限/配置粒度调整→ `enhancement`；移除旧机制→评估 `breaking-change`。
- **优先级**：`medium`：核心命令局部不可用/权限影响多群但非阻断；启动失败/越权/误踢/误撤→ `high`。
- **`needs-info` vs `needs-discussion` 双校准**：owner-driven 缺决策非信息 → `needs-info` ≤0.30，`needs-discussion` ≥0.75-0.85。
- **"全员可 X"是重大权限变更信号**：显式对比"原本谁能做 vs 现在谁能做"，评估恶意滥用风险。
- **工作量下限**：小型修复 ≥1 天（#134）；动作联动型 A 分支 ≥1 天起步（#142/#143）。
- **本地缓存撤回模式骨架**：①按群隔离 ②`message_id+user_id` ③deque maxlen ④写入入口对称 ⑤回退标识 ⑥文档明示重启不可恢复 ⑦写入失败 try/except+debug ⑧提示语区分。
- **`@filter.command(...)` 启动 vs 运行**：AstrBot 提前转换参数，函数体内 `try/except int(count)` 无法兜底；复杂语法入口优先字符串/原始事件解析。
- **早退语句吞噬共享逻辑**：基础设施写入必须置于业务早退**之前**。
- **`defaultdict(factory)` 陷阱**：`.get()` 安全，但 `if k in d`/`dict(d)`/`copy.copy(d)`/`json.dumps` 会无差别创建空条目。**推荐 `{}` + 显式 `setdefault(key, factory())`**。
- **falsy 判空陷阱**：`duration` 必须 `is None`/`== -1`，严禁 `if not duration`（#133）。
- **`async def` 中 `yield` 即变 async generator**，不能 `return <value>`/`await func()`。
- **"返回成功但未生效"链路**：命令解析→权限判断→API 参数→适配器兼容→返回值→**状态回读**→用户提示。
- **"禁言/禁我"3 层语义**：①群管 API 禁言 ②"禁我"自怼 ③申请解禁工作流"待审批"。
- **权限 helper**：`plugin_admins`/`group_overrides`/`title_admins`/`group_admin_admins`/`kick_admins` 及 `has_*_admin_rights`。
- **PR 审查**：增量审查先识别"有价值的代码变更"与"chore 噪音"；PR 描述数字与实际 diff 不一致需主动指出；chore/reflection 批量 commit 标记为质量问题；命令 handler 可 `yield event.plain_result(...)`，普通 helper 统一 `_send`。
- **私聊申请与审批**：私聊事件无 `group_id` 是硬约束，必须要求用户提供群号或复用禁言记录；审批必须用申请 ID/引用回复/专用命令，不能只靠关键词。
- **合入外部仓库类**：`enhancement` + `external-reference`(参考借鉴) 或 `merge-request`+`external-repo`(真合入加 `license-check`)。**必须先访问外部仓库验证实现路径**（#143）。
- **行号引用**：无"已读取验证"说明则用"约 L2000-2060"模糊表述。