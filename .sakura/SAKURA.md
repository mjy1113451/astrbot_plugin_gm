# 项目概述：mjy1113451/astrbot_plugin_gm

> 累计反思 99 次

## 1. 项目简介

`astrbot_plugin_gm` 是 AstrBot 的 QQ 群管理插件，提供插件管理员维护、禁言/解禁、踢人、撤回消息、群昵称、头衔、精华消息、官方群管理员设置等能力。逻辑集中在 `main.py`，依赖 AstrBot 插件体系与 aiocqhttp / OneBot API。

主要命令：`/设管`、`/取管`、`/禁言`、`/解禁`、`/禁我`、`/踢`、`/头衔`、`/取消头衔`、`/设管理`、`/取消管理`、`/设精`、`/设群昵称`、`/改昵称`、`/撤回`、`/撤回用户`。

## 2. 技术栈与结构

- **语言**：Python 3.10+；**框架**：AstrBot 插件体系；**接口**：aiocqhttp / OneBot action；**配置**：`_conf_schema.json` + 运行时读取；**许可证**：MIT。
- 主目录：`main.py`（命令、权限、配置、OneBot 兼容）、`metadata.yaml`、`_conf_schema.json`、`README.md`、`docs/`。

## 3. 权限与配置模型

1. **插件管理员**：`plugin_admins` 或 `/设管` 动态维护；群主天然具备。
2. **QQ 官方权限**：禁言、踢人、撤回、设精、设/取消管理/头衔仍依赖机器人群内官方权限。
3. **专项权限/按群覆盖**：`title_admins`、`group_admin_admins`、`kick_admins`、`group_admins`、`group_overrides`、`get_group_setting` 及 `has_*_admin_rights` 机制；权限变更应优先检查这些入口。
4. **全局默认 + 群级覆盖**：明确缺省回退、空列表/空对象/`0` 的语义。`0` 可能是合法配置（如关闭阈值），不能误当缺失回退。
5. **跨私聊审批**：若新增私信申请解禁等远程审批功能，必须明确插件管理员是否可对所有群代 bot 解禁、审批者所属与可审计状态。

## 4. 近期反思沉淀

### 4.1 AstrBot async / yield / return 风险（PR #116）

- `async def` 内出现 `yield` 即变为 async generator，不可 `return True/False`，不能被 `await func()` 当作 coroutine。
- 权限 helper（被 `await` 并返回业务值）不得含 `yield`；发送提示统一走 `_send` / `_build_text`。
- 顶层 handler 可 `yield event.plain_result(...)`，但不要与 `return` 混用。
- 验证至少 `python -m py_compile main.py`，并验证插件加载、无权限提示、有权限继续。
- 审查固定项：搜索 `async def`、`yield`、`return <value>`，确认所有被 `await` 的 helper 非 async generator（与 Issue #121 风险叠加）。

### 4.2 `/撤回` count 参数解析（Issue #106）

- 命令签名 `count: int = 0/1` 时 AstrBot 可能按注解提前转换，函数体内 `try/except int(count)` 捕获不到。
- 复杂命令入口用 `str` 或原始参数接收，统一在函数内解析、校验、给友好提示。
- 同类入口要一起排查：`recall_cmd`、`recall_user_cmd` 及所有 `: int =`、`count: int` 参数。
- 测试：`/撤回`、`/撤回 3`、`/撤回 abc`、引用撤回、带 @+ 数量、空串、负数、过大、中文数字。

### 4.3 `/撤回 N` 语义与本地缓存回退（Issue #109、#110、#117、#118、PR #123）

- `/撤回 1` 期望撤回"当前指令上方一条消息"而非指令自身：先取群历史，过滤当前命令 `message_id`，只撤回之前的 N 条可撤回消息；历史接口不可用时不可回退为"撤回指令自身"误报成功；引用撤回优先级保持。
- **本地缓存回退骨架**：① 按群隔离（`{str(group_id): deque[dict]}`）+ 最小化（仅 `message_id` + `user_id`）+ `deque(maxlen)` 显式上限；② 工厂函数 `{}` + `setdefault(key, factory())` 优于 `defaultdict(factory)`，避免 `in`/`copy`/`json.dumps` 意外触发工厂；③ **bot 消息写入必须独立于业务早退链**，置于 `if not enabled: return` / `if not in list: return` 之前（PR #123 阻断性 bug 根因）；④ 容量 `maxlen ≥ 4 × N`；⑤ 必须排除命令自身 `message_id`；⑥ 写入失败 try/except + debug，不阻塞主流程；⑦ README/帮助明示重启不可恢复 + 回退标识"（来自本地缓存）"。
- OneBot `get_group_msg_history` 部分实现不支持；空返回语义模糊（不支持/参数不兼容/权限/历史不足/错误被吞），提示"可能不支持或未返回群消息历史"，不可一概写"不支持"。
- @ 解析不按空格拆昵称（昵称可含空格），优先 segment、`user_id` 或 `_extract_at_qq(raw)`。
- 多编号参数与命令匹配器冲突：`/撤回 1 3 5` 需确认 AstrBot 把整串当 `arg_str` 还是按空格拆。
- PR 描述"提示附带『来自本地缓存』"必须到 diff 中定位确认，避免文档与实现脱钩。

### 4.4 `/撤回 @用户 N` 与 `/撤回用户`（Issue #110、#117）

- `/撤回 @用户 N` 进入普通 `/撤回` 兜底属命令路由/解析 bug，应复用 `recall_user_cmd` 或抽 helper，不复制两套撤回逻辑。
- `/撤回 @用户 N` 和 `/撤回用户 @用户 N` 依赖群历史按 `user_id` 筛选；引用撤回已有 `message_id` 不依赖历史。
- 增量审查风险：只审 diff 会漏掉 `recall_cmd`/`recall_user_cmd` 两端缓存回退的对称性——必须抽样确认三条分支（按数量、按用户、引用）行为一致。

### 4.5 `/取消头衔` 提示成功但实际未清空（Issue #111、#119、#125、PR #123）

- 链路：`/取消头衔` → `unset_group_title_cmd` → `_clear_group_title` → `set_group_special_title`。
- 严格区分 `""`、`" "`、`\t`、`None`、字段缺失；`strip()` 会把单空格误判为空（反模式）；严格判空 `title is None or title == ""`。
- `special_title=""` vs `" "` vs `None` vs 不传、`duration=-1/0/不传` 在 NapCat / Lagrange / go-cqhttp 语义可能不同；API 返回 ok ≠ 实际生效。
- 排查：@ 解析（按空格 split 会误判 `@晚风抱抱我` 类带空格昵称）、`user_id`/`group_id`、目标是否在群、bot 权限、目标为群主/管理员（协议限制）、平台限制或客户端缓存；必要时 `get_group_member_info` 回读，注意缓存、字段差异、刷新延迟。

### 4.6 本地缓存回退模式与严格判空（PR #123）

- 主路径失败 → 本地缓存静默回退 → 双重失败才报错 → 成功时附加来源标识；适用于 OneBot 适配器碎片化场景。
- 对外暴露的通用 helper 优先 `x is None` / `x == ""` 严格判空，避免误丢合法值（0、空集合等）；业务内部 shortcut 可保留 falsy 但 docstring 注明"`0` 视为缺失"等约定。
- 撤回类本地缓存强制 6 项：① 全局 key 数量上限 + LRU；② 写入失败不影响主流程（try/except + debug）；③ 读取排除命令自身；④ README/帮助/schema 同步；⑤ 仅覆盖进程启动后；⑥ 隐私最小化（不缓存内容）。
- `ast.parse` ≠ 真实加载：PR 验证应至少 `python -m py_compile main.py`，最好有最小 AstrBot 启动验证。

### 4.7 禁言踢出阈值按群配置/展示（Issue #107）

- 配置 UI/Schema/展示未体现"全局默认 + 群级覆盖"，归 `bug`。
- 不删除全局 `mute_kick_threshold`；它应说明为"全局默认禁言踢出阈值"，可被群级覆盖。
- 展示建议"有效值 + 来源"，如"禁言踢出阈值：3，来源：当前群覆盖；全局默认：5"。
- 缺失 key 回退全局；显式配置 `0` 表示关闭/覆盖，不能当未配置。

### 4.8 专项权限按群配置（Issue #105）

- 属 `enhancement`，按动作授权、符合最小权限原则。
- 改变 `group_admins` 语义需考虑迁移/废弃期/兼容读取。
- 敏感操作（踢人、设/取消管理、头衔）必须做越权和误授权测试。

### 4.9 私信申请解禁与管理员审批流程（Issue #120、#121）

- 归 `enhancement`/`feature`，优先级 `medium`：涉及自动解禁敏感动作，存在越权/误解禁/并发风险。
- 标签：`enhancement`、`group-management`、`moderation`、`mute`、`private-message`、`approval-flow`、`permission`、`configuration`、`needs-discussion`。
- 最小实现：私聊 `申请解禁 群号 说明` → 生成申请 ID → 转发固定管理员 QQ → 编号同意/驳回 → 复用 `_unmute_member` → 私信通知。
- 完整实现：管理员 QQ + 管理群、引用/编号/专用命令审批、状态持久化、重启恢复、过期、重复申请去重、多管理员并发幂等、权限校验、文档同步。
- 私聊无 `group_id`：必须用户输入群号或复用最近禁言/禁我记录；校验用户是否在目标群、是否真被禁言。
- 审批不能仅靠"同意/驳回"关键词（管理群易误触发）。应优先申请编号、引用申请消息或 `/解禁审批 同意 <id>`，并校验审批者 `sender.user_id`（非 `event.user_id`）。
- 自动解禁是敏感动作：只有配置的插件管理员或授权群管理员可审批；记录审批日志；失败反馈原因。
- 工作量：最小可用版中等偏低；完整版中等到中高。
- PR #116 风险叠加：新增 helper 必须纯 `async def`，避免 `await` 业务 helper 中混 `yield`。

### 4.10 合入外部仓库与批量撤回增强（Issue #122）

- 标题为"。"、正文仅 URL+命令形式时，必须从正文重建需求，不能依赖标题分类。
- 分类 `enhancement`（辅以 `merge-request` / `external-repo`），优先级 `medium`。
- **必查项**：① OneBot 适配器是否暴露 `set_group_todo` ② 消息 ID 跨适配器识别 ③ 引用消息中的 `message_id` 提取路径 ④ 权限模型（群主专属 vs 群管即可）⑤ 错误反馈（消息已过期/不是机器人发送的消息）⑥ 撤回场景下的待办撤销。
- **新增命令七处同步清单**（owner-driven 命令新增）：main.py + README + 帮助命令 + `_conf_schema.json`（若新增配置项）+ CHANGELOG + `_GM_COMMAND_NAMES` 元组注册 + 跨适配器兼容性文档。
- **新增标签建议**：`reply`/`quote-message`（标识依赖 reply_id 解析的命令）+ `bot-capability`（依赖 bot 特定能力的命令）+ `onebot-extension`（依赖 OneBot 非 v11 标准 API 的命令）。

### 4.11 PR #123 多轮增量审查教训（含 incr1–5，3878727 第五轮）

- **增量审查结构性盲区**：只看 diff 易遗漏 `__init__` 初始化兼容、`after_message_sent` 钩子注册与签名兼容、读取端对新数据结构适配；重写型 PR 必须额外审视 API 兼容性、回退路径、新旧接口映射（`recent_messages` → `message_history` 升级时旧结构残留风险）。
- **审查评分校准**：撤回/缓存核心命令回退路径 bug，影响面涉及"绝大多数未启用某配置的群组"时评分上限 ≤5/10，决策 `request_changes`/`comments`。
- **结构化输出校验失败的反模式**：校验错误出现时应**修复字段输出格式并保留实质判断**（分类/可行性/标签/关键问题列表五项），不得整体退化为"无法评估"——这是仓库反复出现的反模式（PR #123 第六轮审查即因此完全失败）。审查模板固化"字段校验失败时的最小输出保底"。
- **"quick" ≠ 零审查**：涉及权限/撤回/缓存的 PR 最低限度必须覆盖安全检查、schema 一致性、命令签名、关键风险点。
- **commit 信息去重**：多个相同 `chore(sakura): add reflection for ...` 应标记为提交历史质量问题，建议合并或跳过；增量审查优先识别"有价值的代码 commit"与"chore 噪音"（3878727 第五轮 6 个新增 commit 中 4 个 chore 完全相同）。
- **PR 描述数字与 diff 不一致**：描述 +520/-103 vs 提交 +494/-52，审查应主动核对要求作者澄清。
- **`ast.parse` ≠ 真实加载**：PR 用 `ast.parse` 验证是常见错误认知，应替换为 `python -m py_compile main.py`，最好附最小 AstrBot 启动验证。
- **撤回类 PR 强制检查清单（9 项）**：时间窗（2 分钟）、限流、部分失败处理、自身消息排除、bot 消息处理、撤回目标过滤、缓存数据结构、OneBot 适配差异、配置 schema 一致性。
- **多编号参数与命令匹配器冲突**：`/撤回 1 3 5` 需确认 AstrBot 把整串当 `arg_str` 还是按空格拆。

### 4.12 `/取消头衔` 头衔清除失败回读仍存在（Issue #125）

- 分类 `bug`，优先级 `medium`：不是加载级问题，但"提示成功但实际未生效"会让用户误以为已清空，头衔涉及身份标识不可轻视。
- 排查链路（按概率）：① bot 权限不足（非群管理/无设头衔权限）→ ② 目标用户是群主（协议限制）→ ③ OneBot 适配器对 `set_group_special_title(special_title="")` 语义差异（NapCat/Lagrange/go-cqhttp）→ ④ API 返回成功但状态回读缓存未刷新。
- 严格区分 `special_title=""`、`" "`、`\t`、`None`、字段缺失（§4.5 已沉淀）；`strip()` 是已知反模式。
- `@昵称` 解析走 segment / `user_id` / `_extract_at_qq`，不要按空格 split（昵称可含空格，如 `@晚风抱抱我`）。
- 必要时 `get_group_member_info` 回读，但注意缓存、字段差异、刷新延迟。
- 标签建议：`bug` + `command` + `group-management` + `title`/`special-title` + `onebot` + `needs-info`。
- `needs-info` 收集清单：bot 群角色、目标用户角色（是否群主）、OneBot 实现与版本、AstrBot 版本、插件 commit、完整命令与日志、近期配置变更。
- 仓库内 `set_group_special_title` 调用点集中于 `main.py` 头衔 handler；审查该类 Issue 时主动检查 `_extract_at_qq`、`special_title` 传参、是否有回读。

### 4.13 Issue #124 三轮反思共性沉淀（撤回默认行为与按用户指定编号）

- 分类 `enhancement`，优先级 `medium`：UX 改进而非核心功能缺失；语义歧义处理不当会导致误撤回，故不能降为 `low`。
- **路由语义歧义分析框架**（高频陷阱，单参数多语义）：① 列出数字 N 所有候选语义（数量/编号/时长/次数）② 检查现有代码实际语义 ③ 检查文档/帮助承诺语义 ④ 显式标记"语义决策点"为 `needs-discussion`。
- **序号基准必须由维护者拍板**：相对序号（用户在该群最近发言的相对位置）还是绝对序号（`/消息列表` 列表中 1-based 编号）必须明确，不能由实现方自行决定。
- **行为变更 vs 实现风险要严格区分**：用户主动请求的语义修改（如 `/撤回 @用户 5` 从 5 条→1 条）是需求不是回归；风险聚焦实现层副作用（缓存一致性、对称性破坏、文档缺失）。
- **"默认 1 条"类改动的隐藏风险**：误输入空参数时不再看到用法提示→误触发撤回；与引用撤回优先级协调；与本地缓存兜底路径对接；长消息前缀解析失败误触发；是否需二次确认。
- **可行性强检查项**：`batch_max_count` 与新单数字路由交互（路由后 N 是否仍受约束、N> 时提示）；`/撤回自身 N` 与新默认行为冲突；`recall_user_cmd` 与 `recall_cmd` 对称性；历史快照编号语义（1-based/0-based、该用户最近一条=编号 1 需单独语义）；`defaultdict` 副作用（§4.6）；bot 消息本地缓存写入路径不得被破坏（§4.3 第 3 点）。
- **工作量估算常偏低**：看似"加个默认 1 条"实际涉及入口分流 + 命令提示 + README + 帮助命令 + `/消息列表` 帮助 + 行为兼容性说明 + 测试；估算需预留 40-80 行而非 20-40。
- **标签建议组合**：`enhancement` + `command` + `parser` + `recall` + `message-history` + `group-management` + `needs-discussion`（关键）；`good first issue`/`help wanted` 谨慎使用。
- **`good first issue` / `help wanted` 使用边界**：已规划清楚、有 owner、有明确改法→`enhancement`+模块标签即可；改法不明欢迎外部贡献→`help wanted`+`needs-design`；简单到任何人可上手、纯重构/文档→`good first issue`（增强类通常不适合）。
- 关联追溯：本次增强直接建立在 PR #123 本地缓存骨架上，必须主动声明依赖、避免重复实现。

### 4.14 PR #123 第五轮增量审查教训（追加）

- **增量覆盖度仍严重不足**：本次仅看 6 个新提交中"有价值的代码变更"（`38787272`、`4f3f94ee`）而非整 PR，但 38787272 前 main.py 状态、`recent_messages` → `message_history` 升级前的回退路径均未审查——增量审查天然缺少完整上下文，需主动索取被改动的接口上下游状态或假设评审。
- **提交模式异常**：6 个新提交中 4 个 commit 信息完全相同（`chore(sakura): add reflection for PR#123`），属于"批量反思 commit"噪音。增量审查应识别"有价值代码 commit"与"chore 噪音"，建议合并或跳过。
- **PR 描述数字与实际 diff 不一致**：描述自述 +520/-103，提交哈希显示 +494/-52——审查应主动核对，要求作者澄清。
- **重写型 PR 的额外审视**：撤回/缓存重做 PR 即使评分无变更也应作为 red flag 标记，额外审查 API 兼容性、回退路径、新旧接口映射（`recent_messages` → `message_history` 时旧结构残留风险）。
- **`ast.parse` ≠ 真实加载**：PR 用 `ast.parse` 验证是常见错误认知，应替换为 `python -m py_compile main.py`，最好附最小 AstrBot 启动验证（装饰器注册、命令注册等不会在 ast 阶段触发）。
- **审查评分校准**：撤回/缓存等核心命令回退路径 bug，当影响面涉及"绝大多数未启用某配置的群组"时，评分上限不超过 5/10，决策 `request_changes`。

### 4.15 Issue #126 功能裁剪/删除类沉淀（owner-driven 减法，多轮合并）

- **场景特征**：维护者本人发起（mjy1113451），要求删除 `/撤回 编号...` 与 `/撤回 @用户 编号...` 两种按编号撤回用法；属接口收窄而非 bug 或新增；属 `breaking-change`（已部署用户脚本/教程会失效）。
- **分类与优先级**：分类锁定 `enhancement`（功能减法，接口收敛）；优先级 `medium`（breaking change 但维护者自提 + 改动局部 + 影响存量少数用户）；不可降 `low`（对外契约删减），也不可升 `high`（无横切关注点）。
- **标签必填组合**：`enhancement` + `breaking-change` + `recall` + `command` + `parser` + `documentation` + `needs-discussion`；与缓存/历史相关的补 `cache`/`message-history`；`help wanted` 置信度 ≤0.1（owner 自提且改法明确，不需要外部抢着接）。
- **必查 9 项检查清单（删除类反向清单）**：① 精确删除路径（行号 ± 验证已读 main.py）② 保留路径边界（剩余 Path 1/3/5 不受影响）③ 调用点清空（测试用例同步删）④ README 帮助文案清理（移除"按编号"措辞）⑤ `/撤回` 自身 `yield event.plain_result(...)` 帮助文本同步 ⑥ 配置 schema 语义缩窄（`batch_max_count` 从"约束两种分支"→"仅约束按数量分支"）⑦ bot 回复模板/错误提示措辞清理 ⑧ CHANGELOG/release notes 标注破坏性 ⑨ 替代方案提示（引导引用撤回、`/撤回用户`）。
- **AstrBot 消歧层选择**：撤回/计数类命令的参数语义修改必须明确是在 `@filter.command` 装饰器签名层（改类型注解/默认值，僵硬但简单）还是 handler 内部（先收 `str` 再分流，灵活但要处理 `try/except int()` 已被 AstrBot 提前按注解转换的情况——参 §4.2）。
- **`/撤回 N` 与 `/撤回自身 N` / `after_message_sent` 钩子的循环风险**：删除按编号语义后必须确认未影响 bot 自身消息记录路径（PR #123 引入的 `after_message_sent`）。
- **OneBot 兼容性回退路径**：若 `/撤回 N` 在某些 OneBot 客户端下不可用，用户原本可用 `/撤回 1 3 5`（纯本地缓存）作精确手段，删除后这条精确路径消失，回退到 `/撤回 N`（数量）也失效，会形成双重退化——删除前需评估。
- **与并行 Issue 的方向冲突**：Issue #124（要求按编号撤回增强）与本 Issue（删除按编号）方向相反——单独实施任何一方都会制造新不兼容，必须显式标 `needs-discussion` 并建议先解决 #124 决议再实施；冲突项应升级或强化 `needs-discussion` 权重。
- **行号定位必须标注证据来源**：精确行号（L2024-2057 等）若无"已读取 main.py 验证"说明，会让读者怀疑是猜测——要么读取验证，要么用"约 L2000-2060"模糊表述。
- **工作量估算需拆分**："代码 X 天 + 文档 Y 天 + 测试 Z 天 + 验证 W 天"区间估，预留 40-80 行而非 20-40（README + 帮助 + CHANGELOG + schema + 测试 + 验证六面同步）。
- **关联追溯（区别于重复）**：与 Issue #124 方向相反但同主题，应标 `related` 而非"重复"；与 PR #123 缓存骨架强耦合，应标 `related`。

### 4.16 语音 STT + 违规词自动撤回/禁言（Issue #127、#128，新增横切关注点）

- **场景**：维护者本人发起，语音消息 STT 转写 → 命中关键词 → 撤回 + 禁言，复用 `_moderation_dispatch` / `_handle_violation`。属非文本消息类型的违规检测扩展。
- **分类与优先级**：`enhancement` + `medium`。**误触发风险**（语音转写误识别 × 关键词模糊匹配 = 双重误判 → 误撤回/误禁言）使优先级不低于 medium；**隐私与生物特征风险**（语音 = 敏感生物特征）需 README 显著告知。
- **核心标签**（新增 `stt`/`voice`/`moderation` 模块标签）：`enhancement` + `stt`/`voice`/`speech-to-text` + `moderation` + `group-management` + `configuration` + `onebot`/`compatibility` + `permission` + `privacy`/`compliance` + `documentation` + `needs-discussion`；条件性 `breaking-change`（若复用 `profanity_keywords` 对老用户构成隐性行为变更）。`needs-info` ≤0.2、`needs-discussion` ≥0.8（决策缺失为主）；`help wanted` ≤0.1。
- **可行性分支变量**：AstrBot 是否暴露 STT provider 接口（消息 segment `type='record'` 是否携带转写文本）决定分支 A/B 工作量差距 ±50%（A: 100-150 行 / 2-3 天；B: 200-300 行 + endpoint 配置 + 流式 + 大文件 + 错误 + 配额 / 5-7 天）。Whisper large-v3 约 3GB 依赖体积是显著部署障碍，建议优先选云端 API 或 small/base 模型。
- **STT 类强制检查项（11 维标准模板）**：① 触发场景（`record`/`ptt` segment）② STT 调用（同步/异步、超时阈值）③ 语音文件获取（OneBot `get_record`/`get_file`、silk/opus/amr/mp3 解码、字段差异 `file`/`url`/`file_size`/`duration`）④ 关键词匹配（大小写/全半角/繁简/谐音/正则/白名单、空列表语义）⑤ 撤回时限（2 分钟硬约束 vs STT 耗时——必先撤回再尝试禁言）⑥ 禁言权限（bot 是否管理员、群主/管理员/触发者豁免）⑦ 降级策略（STT 不可用/超时/失败/未配置 → 跳过本条或禁用功能）⑧ 性能限流（每日上限、每分钟上限、API 成本）⑨ 隐私边界（转写内容缓存、日志、审计、撤回后清理）⑩ 配置 schema（全局 + 群覆盖、空值语义、旧配置迁移；默认禁用而非启用）⑪ 文档同步（README 隐私告知 + 误伤风险 + CHANGELOG）。
- **多模态违规检测标准模式（新增横切关注点）**：消息类型（文本/语音/图片/文件/转发）→ 转文字/转写/标准化层 → 关键词匹配 → 标准处置。`_moderation_dispatch` 中应建立"输入源 → 转写/标准化 → 文本违规检测 → 处置"标准流水线，新增输入源只需补"转写/标准化"环节，避免双份匹配逻辑。
- **异步链路约束**：`on_group_message` 必为纯 `async def + await`，禁 yield（参 §4.1）；STT 异步转写必须 `asyncio.create_task` 包裹 + 异常不外抛；跨事件边界校验（消息 ID 时效性、操作时限）——用户发完语音立即撤回群内其他消息、STT 还没转完，需明确"放弃撤回但仍记录违规次数"的兜底语义。
- **撤回命令族五处同步清单扩展为七处**（涉及多媒体转写层）：`main.py` + README + 帮助命令 + 配置 schema + CHANGELOG + **STT 配置文件/字段**（如 AstrBot 框架 STT 配置项的位置） + **误伤率文档说明**（README 风险提示）。
- **重复检测关键词**（新增）：`voice`、`语音`、`stt`、`speech-to-text`、`transcrib`、`whisper`、`asr`、`voice_violation`、`voice_recall`、`音频`、`语音转文字`、`撤回语音`。
- **关联追溯**：与本仓库现有 `_check_image`（图片 AI 审核）模式同源（多模态内容审核），可作为同类扩展参考。

### 4.17 Issue #130 权限模型重构（移除 plugin_admins，自动继承群原生身份）

- 维护者自提"将插件管理员改为群管/群主自动继承，取消 plugin_admins 设置"。属权限模型重构 + 接口删除（breaking change）。
- 分类 `enhancement` + `breaking-change` + `medium`：删除既有配置项是用户可见 breaking change；横切所有管理类命令但有迁移路径，绝不可降 low。
- 核心标签：`enhancement` + `breaking-change` + `permission`/`permission-model`（建议新建）+ `configuration` + `group-management` + `onebot` + `bot-role`/`sender-role`（建议新建）+ `cleanup`/`deprecation`（建议新建）+ `command`。`needs-discussion` 中高，`needs-info` ≤0.25，`help wanted` ≤0.1。
- sender 角色 vs 配置项本质差异：① 私聊无 `group_id` 降级 ② 匿名消息 `sender.role` 不准 ③ bot 自身角色降级 ④ `get_group_member_info` 缓存策略 ⑤ 跨适配器字段（NapCat/Lagrange/go-cqhttp）。
- 必查 6 项：① 待删除配置项影响范围 ② 跨适配器 API 差异 ③ 缓存与失效 ④ 权限提升风险审计 ⑤ 配置迁移路径 ⑥ API 失败兜底（拒绝 vs 放行）。
- 五处同步清单扩展为六处：`main.py` + `_conf_schema.json` + README + 帮助命令 + CHANGELOG + **迁移指南**。
- 同类配置项扫描：分析时主动检查 `title_admins`/`group_admin_admins`/`kick_admins` 是否同步迁移。
- 建议新标签：`bot-role`/`sender-role`、`permission-model`、`deprecation`（按仓库习惯映射或注明"需维护者确认新增"）。

### 4.18 Issue #129 加群申请拒绝自定义理由（双重需求 + 跨适配器）

- 维护者自提加群申请拒绝时支持自定义理由（**填写**或**配置**两种路径并存）。属 owner-driven UX 增强，与 #57（引用回复同意/拒绝）同工作流扩展。
- 分类 `enhancement` + `medium`：不可降 low（涉及代为拒绝加群申请，存在误拒/理由不当风险）。
- 核心标签：`enhancement` + `group-management` + `join-request`/`join-approval`（建议新建）+ `configuration` + `onebot` + `ux` + `needs-info`（中高：OneBot 适配器版本）+ `needs-discussion`（中：填写策略）+ `related` 指向 #57。`help wanted` ≤0.05。
- OneBot `reason` 字段跨适配器矩阵（建议沉淀）：NapCat ≤10 字符；Lagrange ≤10-20 字符；go-cqhttp 部分 ≤30 字符、部分不限制；空串/None/特殊字符处理各实现可能不同。建议 schema 暴露 `join_reject_reason_max_length` 避免硬编码。
- 双重路径：① 填写（引用回复时输入）→ 解析引用 + 剥离关键词 + 长度截断 + 敏感词过滤；② 配置（全局默认 + 按群覆盖 + 模板列表）→ 五处同步同 `get_group_setting` 模式。复用 `_handle_group_request` 入口避免并行通道。
- 工作量陷阱：看似 60-100 行，实际 80-120 行（解析 + 截断 + L2660/L2709/L2721 三调用点同步 + 五处配置 + 同意侧 `reason` 透传校验）。
- 高频改动点沉淀：`pending_join_requests` + `_handle_group_request` 是高频改动点（#57 + #129 已两次扩展），未来可能再有"同意附言"/"批量审批"/"审批历史"。建议固化调用点地图。
- **未来防御（记忆幻觉警告）**：引用项目记忆 § 编号前必须确认存在（本次反思曾编造"§4.13 UX 增强低估"——§4.13 实际是撤回默认行为/按用户指定编号 Issue #124 而非 UX 估算）。

### 4.19 校验失败整体降级反模式（Issue #130、#131 三轮、#132）

- **场景特征**：原始输出含 `reserved tag syntax in USERNAME` / `expected <SUGGESTED_TITLE>` 等字段校验错误提示，分析流程把校验失败退化为全部"无法评估"/`other`/空标签/无建议。
- **核心反模式**：结构化输出校验失败 ≠ 信息不足。看到字段校验错误时，应**仅修复字段输出格式**，**不得把所有判断都退化为"无法评估"/`other`/空标签/无建议**——这是仓库反复出现的反模式（PR #123 第六轮审查 + Issue #130 + Issue #131 三轮 + Issue #132 五次触发的同一根因）。
- **Pre-check 强制规则**：在任何字段出现"无建议/无法评估/空/未检测到重复"前，必须先确认是否源于校验失败——校验失败应**局部修复**而非**整体降级**。建议固化"校验失败短路器"到反思流程。
- **优先级章节必须显式化决策路径**：owner-driven breaking-change 决策应写出 ① 横切重写 + breaking-change → 最低 medium；② 维护者本人发起 + 已有标准化模式 → 不升 high；③ 工作量可控 + 迁移路径明确 → 不升 high；④ 跨适配器风险已识别但有兜底方案 → 维持 medium。
- **行号引用必须前置声明**：批量行号引用（如 L398-401、L454-468、L607-620、L2266-2296、L2387-2433）前必须声明"基于项目记忆 + 既有 PR 模式推断，建议 PR 提交前以最新 main.py 行号为准"，避免被怀疑是猜测。
- **正确输出保底模板**（校验失败时仍须输出）：分类 + 优先级 + 摘要 + 建议标签列表 + 重复检测（"暂未发现"+ 关键词 + ⚠️措辞核对）+ 标题改写（"可保留"或轻量规范化）。

### 4.20 权限模型重构类 Issue 标签基线 v2（Issue #132 反思）

- **必给标签基线**（与 §4.17 配套的标签清单）：`enhancement` + `breaking-change`（置信度 ≥0.90）+ `deprecation`（≥0.75）+ `permission-model`（≥0.80，新建）+ `configuration`（≥0.85）+ `group-management`（≥0.85）+ `bot-role`/`sender-role`（≥0.70，新建）+ `onebot`/`compatibility`（≥0.65）+ `command` + `needs-discussion`（升 0.75）+ `needs-info`（≤0.45，owner-driven 决策待确认）。
- **应移除标签**：`question`（置信度应 ≤0.05，owner-driven 增强类有明确诉求，非询问）。
- **重复检测必须分类前置过滤**：若两 Issue 的主分类标签不同（一个 bug 一个 enhancement），duplicate 置信度上限 0.3；自动检测算法可能仅基于关键词相似度未结合分类差异（Issue #132 曾把 #125 bug 误判为 #132 enhancement 的重复，置信度 0.95 完全失真）。
- **权限提升风险审计必须给具体命令示例**：至少 2-3 个示例（如 `/踢人`、`/全员禁言`、`/改群名`），并建议"是否需要为这些命令保留 super-admin 概念"作为决策点。
- **同类配置项扫描决策**：主动检查 `title_admins`/`group_admin_admins`/`kick_admins` 是否同步迁移，建议一次性重构而非分散迁移。
- **工作量估算需更保守**：18+ 调用点 + 五处同步 + 迁移路径 + 测试验证 → 实际 200-350 行、3-5 天（不是 150-250 行、1-3 天）。

### 4.21 OneBot 群待办类 Issue 分析模板（Issue #131 三轮反思沉淀）

- **触发场景**：维护者本人发起新增 `/添加群待办` 命令，引用消息回复即可设为群待办。属 owner-driven 命令新增。
- **关键技术事实**：OneBot v11 标准协议**并未定义** `set_group_todo` 或 `send_group_todo`——这是 go-cqhttp/NapCat/Lagrange 等实现的**非标准扩展**。表面与 `/设精` 同构（都是 reply_id + 单 API 调用），实质 API 可用性本身就是个未解问题——是分析中最严重的可行性误判来源。
- **跨适配器差异**（必须列矩阵）：NapCat `_set_group_todo(group_id, message_id)`；Lagrange `set_group_todo(group_id, message_id)`；go-cqhttp 无标准群待办 API（需 HTTP API 插件扩展）。与"群公告"（`set_group_announce`）是不同入口，不可混用。
- **权限风险（被低估）**：QQ 群中**只有群主**能设置群待办（部分客户端允许管理员），与"群管理员可设精"不同——必须建议复用 `_is_group_owner` 而非 `_is_group_admin_or_owner`。
- **"API 返回 ok ≠ UI 生效"专项警示**：QQ 群待办的特殊性：API 返回成功 → QQ 服务端写入 → 客户端 UI 异步刷新（1-2 秒），部分适配器（尤其旧版 go-cqhttp）API 接受但实际不写入。建议指令提示"已设为群待办，请打开群消息顶部查看（约 1-2 秒后生效）"以降低误判。
- **必查项**：① OneBot 适配器是否暴露 `set_group_todo` ② 消息 ID 跨适配器识别 ③ 引用消息中的 `message_id` 提取路径 ④ 权限模型（群主专属 vs 群管即可）⑤ 错误反馈（消息已过期/不是机器人发送的消息）⑥ 撤回场景下的待办撤销。
- **新增命令七处同步清单**（owner-driven 命令新增）：main.py + README + 帮助命令 + `_conf_schema.json`（若新增配置项）+ CHANGELOG + `_GM_COMMAND_NAMES` 元组注册 + 跨适配器兼容性文档。
- **新增标签建议**：`reply`/`quote-message`（标识依赖 reply_id 解析的命令）+ `bot-capability`（依赖 bot 特定能力的命令）+ `onebot-extension`（依赖 OneBot 非 v11 标准 API 的命令）。`todo`/`group-todo`（若仓库未建应建议新建）。
- **引用消息触发型命令分析模板**：OneBot API 标准性 + API 权限要求 + reply_id 解析路径兼容 + 跨适配器 fallback + 消息已撤回兜底。

### 4.22 owner-driven Issue 标签权重校准规则（Issue #131）

- **`needs-info` 调整**：owner-driven issue 缺的是"设计决策"而非"事实信息"，应降 `needs-info` ≤0.2-0.30（不是 0.4）。
- **`needs-discussion` 调整**：owner-driven 缺决策时 `needs-discussion` 应保持高权重（0.75-0.85），不是 0.55-0.65。
- **`good first issue` 慎用**：增强类涉及设计决策/兼容性时置信度 ≤0.15（不是 0.2）。
- **`help wanted` 几乎不适用**：owner-driven self-implementation 置信度 ≤0.05-0.1。
- **config 决策清单**：① 是否需要"某些群禁用"场景？② 是否需要"某些群仅特定人可用"？③ 默认值：全局 enabled，按群 override 关闭为主（与 `group_overrides` 模式一致）。

### 4.23 反思流程纪律性强化（Issue #130 教训）

- **USERNAME 等保留字校验错误的处置**：Issue 中"作者：unknown"、原标题"。"等异常输入不应触发整体降级。应：① 单独标注"作者信息缺失，建议人工补充"；② 对"。"标题进行主动改写（反模式明令）；③ 其他字段继续基于 Issue 正文评估。
- **重复触发同一反模式的根因诊断**：PR #123 第六轮 + Issue #130 + #131 三轮 + #132 五次触发"校验失败整体降级"同一根因，说明反思流程中存在自动化保护机制缺失。应在反思流程中加入"校验失败短路器"——若摘要提及字段校验错误，则：仅修复字段输出格式；实质性判断维持原始判断；标题字段在原标题清晰时直接给"可保留"。
- **失败案例的复用价值**：Issue #130、#131、#132 三类分析失误（整体降级 + 禁用措辞 + 标签遗漏）应固化到反思流程 Pre-check 步骤，作为新反思的强制核对项。

### 4.24 Issue #133 解禁 bug 标准分析模板（两轮反思沉淀）

- **触发场景**：`/解禁 @用户` / `unmute_cmd` 提示成功但目标仍被禁言；或解禁命令对装饰字符 QQ 解析错误。
- **必查 9 项**：① Bot 在该群角色（部分协议要求群主专属解禁）② 目标用户是否仍在群内（已退群如何兜底）③ OneBot 实现版本（NapCat/Lagrange/go-cqhttp 对 `delete_group_ban` 语义差异）④ `duration` 参数语义（`duration=0` vs 不传 vs `duration=-1`，**falsy 判空陷阱**：`0` 是合法值但 `if not duration` 会误判）⑤ `_extract_at_qq` 解析（必须按 segment/user_id，不按空格 split；花体字/数学字母/装饰 Unicode 必须 NFKC 归一化 + `\d{5,12}` 强校验）⑥ `get_group_member_info` 回读校验（API 返回 ok ≠ 实际生效）⑦ 完整执行日志（含早退语句是否吞噬回读/日志）⑧ 最近配置变更 ⑨ 完整堆栈/截图。
- **优先级 `medium`**（核心命令局部不可用 + 误判用户状态风险 + 跨适配器兼容风险已识别但有兜底）。
- **必给标签**：`bug` (0.92) + `command` (0.90) + `group-management` (0.88) + `unmute`/`lift-ban`/`mute-management` (0.85-0.90，建议仓库新建) + `onebot` (0.70-0.78) + `compatibility` (0.65-0.75) + `bot-role` (0.65) + `needs-info` (0.85-0.90，缺适配器版本/截图/目标身份无法定位根因)。
- **修复方向分支判定**：分支 A（API 层兼容问题，`duration=0` 语义差异）~20-40 行 + 1 天；分支 B（需补完整状态回读链路）~50-100 行 + 1.5-2 天；分支 C（需重构解禁入口或权限校验）~100-150 行 + 2-3 天。
- **"提示成功但实际未生效"通用模式**（扩展自 §4.5 头衔类模板）：覆盖解禁 (`set_group_ban` duration)、头衔 (`set_group_special_title`)、设精 (`set_essence_msg`)、改群名 (`set_group_name`)、全员禁言 (`set_group_whole_ban`)；通用必查项：① bot 权限 ② 目标用户身份 ③ OneBot 实现版本 ④ `_extract_at_qq` 解析 ⑤ API 参数语义（duration=0/""/None）⑥ 状态回读 ⑦ 适配器差异 ⑧ 提示语区分接口成功与实际生效。
- **解禁三层语义**（与禁言对称）：A. 解除群管 API 禁言（`set_group_ban` duration=0）；B. 解除"禁我"自怼状态（插件内部记录）；C. 解除审批工作流中"待审批禁言状态"。分析前必须确认指哪一层。

### 4.25 Issue #134 装饰字符 QQ / 视觉欺骗型用户名解析 bug 模板

- **场景**：用户用花体字（𝓒𝓪𝓷𝓬𝓮𝓻）、手写体、组合 Unicode 字母（𝐀、𝕒 等数学字母块）等"看起来是 QQ 号"但实际是字符串的输入，导致 `_extract_at_qq` 解析失败或解析为字面量字符串。
- **触发关键词**：`@用户` + `解禁`/`禁言`/`踢人`/`设管` + 装饰字符/数学字母/花体字。
- **必查项**：① `_extract_at_qq` 是否 NFKC 归一化 ② OneBot 适配器是否对装饰字符 QQ 拒绝/截断 ③ 群号是否也污染 ④ 是否需要在输入层加"QQ 必须是纯数字"硬校验 ⑤ 装饰字符 QQ 跨适配器命令成功率矩阵。
- **修复建议**：在 `_extract_at_qq` 顶部加 `unicodedata.normalize('NFKC', s)` + 正则 `\d{5,12}` 强校验 + 错误提示"目标 QQ 格式不正确"。
- **必给标签**：`bug` (0.95) + `command` (0.90) + `parser` (0.85) + `at-parse`/`at-extract` (0.80，建议新建) + `group-management` (0.75) + `onebot`/`compatibility` (0.65) + `needs-info` (0.55) + `mute`/`unmute` (0.70)。
- **优先级 `medium`**：核心禁言/解禁命令对群管理影响范围大，误解禁可能放大已有禁言执行错误；但用户场景是单群单次误操作，不升 high。
- **重复检测关键词必须包含模块名**：仅按症状相似度（如"获取信息问题"）会反复误报 #125。下次遇到"获取信息问题"+"命令解析错误"模板时，先按模块归类再判断重复。
- **工作量估算反向校准**：小修复（<50 行）也要避免低估为 1 天内——装饰字符 QQ 测试矩阵（NapCat/Lagrange/go-cqhttp × 数学字母/手写体/Emoji 风格）至少需要半天构造测试用例。小修复工作量下限 1 天，包含最小适配器验证。

### 4.26 Issue #135 群管理读类命令模板（owner-driven 新增命令，与 #131 群待办对称）

- **场景**：维护者本人发起新增"读类"群管理命令（如禁言列表查询），复用 `_execute_action`、`_moderation_require_admin_msg`、元组注册等既有模式。属 owner-driven 纯加法增强，**读类 vs 写类**对称于 #131 群待办。
- **分类与优先级**：`enhancement` (0.95) + `medium`。与 #131 同级（owner-driven + 已有标准化模式 → 不升 high；工作量 100-200 行 → 不升 high；跨适配器风险已识别但有兜底 → 维持 medium；不涉及核心命令局部不可用 → 不升 high）。**不应升 high、不应降 low**（读权限涉及侦察工具风险）。
- **必给标签**：`enhancement`(0.95) + `command`(0.95) + `group-management`(0.90) + `mute`/`ban-list`/`mute-status`(0.85-0.90，建议仓库新建高频主题标签，与 `title`/`recall`/`vote` 并列) + `onebot`/`compatibility`/`onebot-extension`(0.65-0.85，`shut_up_timestamp`/`ban_expire_time`/`mute_end_time` 非 v11 标准字段) + `needs-discussion`(0.65-0.75，**owner-driven 但涉及设计决策**，应保持高权重) + `read-permission`/`viewer-role`(0.70，**读权限 vs 写权限独立维度**，建议仓库新建) + `permission`(0.45-0.55) + `configuration`(0.65-0.75，可能新增 `mute_list_max_display`/`mute_list_show_remaining` 等按群配置) + `needs-info`(≤0.20-0.30，owner-driven 缺决策非信息) + `help-wanted`(≤0.05-0.10) + `good-first-issue`(≤0.10-0.15)。
- **`needs-discussion` 必给的 3 个决策点**：① 跨适配器字段差异 fallback（哪些实现直接不支持）② 大群分页/分批（`get_group_member_list` 在 500+ 人群是否分页？是否需要缓存？缓存失效策略？）③ 读权限模型（仅群管可见 vs 全员可见——读权限 vs 写权限区分 + 是否需"按群开关"/"按群特定人可用"）。
- **跨适配器字段差异矩阵**（#135 沉淀为标准矩阵，建议仓库固化）：`shut_up_timestamp`（NapCat / go-cqhttp）/ `ban_expire_time`（Lagrange）/ `mute_end_time`（部分实现）。同属待整理范围：`role`、`level`、`special_title`、`join_time`、`last_sent_time`。建议仓库建立正式的"读取类群成员字段"对照表。
- **读类 vs 写类细分**：写类（#131 群待办）关注写权限、API 返回、UI 生效延迟、回执；读类（#135 禁言列表）关注读权限、字段差异、数据脱敏、缓存策略、空状态/时间格式、侦察工具风险。
- **读类命令必查 7 项**（#135 沉淀为标准清单）：① 跨适配器字段差异矩阵 ② 大群分页/分批 ③ 缓存层（成员列表缓存策略/失效条件/是否每次实时拉取）④ 空状态友好提示（"当前群无被禁言成员" vs "适配器可能未返回禁言字段"）⑤ 时间格式显示边界（>30 天、永久禁言、`shut_up_timestamp` 未清零）⑥ 隐私/侦察工具风险评估（揭示群管执法记录，是否需控制可见范围/脱敏昵称/仅管理员可见）⑦ 读权限模型决策（群主/群管/普通成员/插件管理员分层）。
- **可行性分支判定**（#133/#135 沉淀硬性要求显式分支）：分支 A（最小：单群单次遍历+基础字段适配，~80-150 行 / 1.5-2 天）；分支 B（完整：分页+缓存+脱敏+读权限分级+空状态/时间格式+五处同步，~200-350 行 / 3-5 天）；分支 C（与 #131 群待办同类合并实施，统一群管理动作模板，~300-500 行 / 5-7 天）。
- **五处同步清单**：main.py + README + schema + 帮助命令 + CHANGELOG。
- **重复检测措辞**：**禁止**"未检测到重复"/"无重复"/"可能是#X的重复"；正确输出"暂未发现（建议检索关键词：`get_group_member_list`、`shut_up_timestamp`、`禁言列表`、`ban list`、`mute list`、`被禁言成员`）"。与 #131 群待办显式互引说明一致性（owner-driven + 跨适配器风险共享）。
- **校验失败短路反模式**（#135 三轮反思核心教训）：本次分析触发"字段校验错误 → 整体退化为 `other`/`medium`/`无法评估`/空标签/`无建议`/`未检测到重复`"——是项目记忆第 4-5 次明令禁止的反模式。**实质性判断（分类/可行性/标签/标题）不得因校验失败而连带退化**；仅局部修复字段输出格式即可。已在反思 Pre-check 阶段固化硬约束。
- **建议标题**：`[enhancement][medium] 新增 /禁言列表 命令查询本群被禁言成员`（与 #131/#133 风格一致，加 `/` 前缀）。原标题清晰时标"可保留"避免过度改写。
- **同类沉淀建议**：仓库正式建立 `mute`/`ban`/`ban-list`/`unmute`/`mute-action` 标签子体系（与 `title`/`recall`/`vote` 并列）+ `read-permission`/`viewer-role`（与现有 `permission` 区分）+ `onebot-extension`（标识依赖 OneBot 非 v11 标准字段的命令）。

### 4.27 Issue #136 装饰字符 QQ 解析 bug 第二次触发（同根因 #134 增量沉淀）

- **场景**：维护者上报 `/解禁 @用户` 命令对装饰字符 `@𝓒𝓪𝓷𝓬𝓮𝓻` 误识别为解禁目标，与 #134 踢人场景同根因——`_extract_at_qq` 未做装饰字符校验。
- **必查项**（与 #134 完全对齐）：① `_extract_at_qq` NFKC/NFKD 归一化与白名单强校验；② OneBot 适配器是否对装饰字符 AT 段预处理/丢弃；③ raw["message"] 中装饰字符 AT 段字段差异（`{data:{qq:"xxx"}}` vs `{data:{user_id:"xxx"}}`）；④ 群号是否也污染；⑤ 跨命令传染性测试（`/禁言` `/解禁` `/踢人` `/设管` `/取管` 同源解析）。
- **NFKC vs NFKD 关键技术陷阱**：NFKC 对花体字 𝓒𝓪𝓷𝓬𝓮𝓻 / 数学字母 𝐀𝕒 / U+1D400-U+1D7FF Mathematical Alphanumeric Symbols **无效**——只能处理全角数字 ０-９ 这类兼容性分解字符。正确方案：① 白名单 `\d{5,12}` 强校验 + ② NFKD + 自定义映射表 或 ③ 直接拒绝非纯数字输入并提示。
- **必给标签**（与 #134 同根因一致）：`bug` (0.95) + `command` (0.90) + `parser` (0.85) + `at-parse`/`at-extract` (0.85，建议新建) + `group-management` (0.80) + `unmute`/`mute-action` (0.85，与 #133/#135 配套) + `onebot` (0.35-0.50，与 #134 一致但本 Issue 根因在插件层而非协议层) + `decorative-unicode`/`unicode-normalization` (建议新建) + `needs-info` (≤0.30-0.55) + `mute` (0.70)。
- **优先级 `medium`**（与 #134 一致；同根因 issue 应保持优先级一致，章节显式说明避免读者横向对比质疑）。
- **重复检测置信度上限**：主分类 + API 都相同时 0.75-0.85；症状/触发命令差异明显（#134 踢人 vs #136 解禁）时上限 0.75；**严格禁止**"可能是#X的重复"措辞，改为"高度疑似重复（与 #134 同根因：_extract_at_qq 未做装饰字符校验），建议合并修复"。
- **传染性测试成本估算陷阱**：13+ 调用点扫描 + 适配器差异测试 + 边界场景构造，传染性测试成本 +1 天而非 +0.5 天；总工作量 2-3 天。
- **建议标题**：`[bug][medium] /解禁 @装饰字符用户名 被误识别为解禁目标`（与 #134 风格一致，精简模式）。
- **仓库特异性沉淀**：`_extract_at_qq` 是仓库高频解析入口（除 #134/#136 外需主动扫描 #133 等同类 Issue）；13+ 调用点修复应在统一入口层（分支 B）而非分散到各命令 handler；建议仓库建立"装饰字符 AT 段跨适配器矩阵"避免未来重复分析。
- **同根因 issue 差异化分析原则**：即使根因相同，优先级/传染性测试/风险点三方面也需差异化（避免简单复制粘贴）。

### 4.28 Issue #138 命令别名/重命名决策模板（owner-driven 减法+改名）

- **场景**：维护者自提"移除 `/删群公告` + 新增 `/取消群精华`"（实质是 `/取消设精` 的语义收敛/别名重命名）。
- **核心洞察**（仓库特有高频陷阱）：中文 bot 命令插件常有"功能等价但命名不同"现象（`/取消精华` `/取消群精华` `/取消设精` 共存）。**分析必须主动 grep `_GM_COMMAND_NAMES` 与所有 `@filter.command(...)` 装饰器，建立"命令名 → handler"映射**，避免重复实现。
- **分类与优先级**：`enhancement` + `low`（owner-driven + 主动减法 + 无横切关注点）。若保留 medium 应显式说明"因涉及既有命令删除 breaking-change 而非纯减法"。**决策路径**：① 主动减法+owner-driven → 应降 low ② 但涉及既有命令删除 → 维持 medium ③ 别名决策未确定 → `needs-discussion` 高权重 ④ 跨适配器风险低（v11 标准）→ 维持 medium。
- **必给标签**：`enhancement`(0.95) + `command`(0.95) + `rename`/`alias`(0.75-0.85, **建议仓库新建**) + `breaking-change`(0.65-0.70, 仅当删除) + `deprecation`(0.65) + `group-management`(0.85) + `needs-discussion`(0.75-0.85) + `documentation`(0.55-0.65) + `needs-info`(≤0.30) + `onebot`(0.30-0.40, 辅助)。
- **常见误标**：`parser`（仅解析逻辑改动时给，命令别名不涉及）、`recall`（仅消息撤回场景给，与 `delete_essence_msg` 不同）。
- **可行性分支必显式**：分支 A（仅删除旧命令）/ 分支 B（保留双名共存+迁移指南）/ 分支 C（仅改名不保留别名）。纯删除工作量可低至 0.25-0.5 天（5-8 处修改）。
- **依赖识别**：`_GM_COMMAND_NAMES` 元组位置、外部调用点扫描、helper 是否有其他调用路径、删除后下游钩子（`after_message_sent`、`on_group_message`）是否还有引用。

### 4.29 Issue #139 删除+新增对称命令复合 Issue 模板（首次出现）

- **场景**：维护者自提"移除 `/删群公告` + 新增 `/取消群待办`"（与 #131 `/添加群待办` 形成 add/cancel 对称命令）。属**仓库首次出现的双方向复合 Issue**（区别于纯减法 #130/#132 或纯加法 #131）。
- **分类与优先级**：`enhancement` + `medium`（**不能降 low**：双向改动非纯减法 + 跨适配器扩展 API；**不能升 high**：owner-driven + 工作量可控）。**4 条决策路径必须显式列出**：① 双向改动（删+增）非纯减法 → 不能降 low ② 涉及 OneBot 扩展 API → 跨适配器风险 → 维持 medium ③ owner-driven 主动实施 + 改法明确 → 不升 high ④ 与 #131 强耦合（依赖 `/添加群待办` 提供待办对象）→ 需联动讨论。
- **必给标签**（breaking-change 复合 issue 升级基线）：`enhancement`(0.95) + `breaking-change`(删除部分 0.95，新增部分 ≤0.30，**必须按子诉求分别评估不能统一**) + `deprecation`(≥0.85) + `command`(0.95) + `group-management`(0.85-0.92) + `onebot`(0.55-0.70) + `compatibility`/`onebot-extension`(0.75-0.85, **关键遗漏高发**) + `permission-model`/`bot-role`(0.70-0.80, 涉及权限时) + `reply`/`quote-message`(0.70-0.85, **关键遗漏高发，#131 模板已建议仓库新建**) + `write-permission`(0.80, 撤销动作) + `changelog`(0.85, breaking-change 必须) + `needs-discussion`(0.85, breaking-change 决策点多) + `needs-info`(≤0.30)。
- **七处同步清单**（删除类扩展，比五处多）：main.py + `_GM_COMMAND_NAMES` + README + 帮助命令 + CHANGELOG + metadata.yaml + 迁移指南。**纯减法也不能跳过七处同步**。
- **复合诉求应强制拆分建议**：删除+新增两条诉求工作量/风险/决策点完全不同，混合 PR 会导致审查困难。分析必须显式建议拆分为两个 Issue 跟踪或合并实施并标注。
- **撤销类命令的特殊复杂度**：群待办撤销与 `/取消设精`（设精撤销）有本质差异：扩展 API（非 v11 标准）+ 群主专属权限（QQ 原生行为）+ UI 异步刷新（API ok ≠ UI 生效）。工作量应**保守估 ≈ 1.2-1.5× 同方向既有 Issue**（参考 #131）。撤销必须配 reply_id，可能需要"列出当前群所有群待办"辅助命令。
- **权限不对称风险**：添加群待办可能允许群管，撤销群待办通常需群主。分析必须显式对比"添加侧权限 vs 撤销侧权限"。
- **跨 Issue 联动讨论**：本 Issue 与 #131 应在重复检测章节显式写"非重复但建议合并实施"，并标 `Refs #131` 或 `Depends on #131`。维护者需决定合并到一个 PR 还是拆分。
- **可行性分支硬约束**：A/B/C 三档显式 + **可选分支 D（先 A 后 B 的分阶段方案）**。分支 B 工作量下限 100-180 行/2-3.5 天（高于 #131 的 50-100 行/1.5-2 天）。

### 4.30 校验失败短路反模式第 7 次触发（#138 + #139 联合沉淀）

- **同根因反复触发**：#138 + #139 反思中**均触犯"未检测到重复"/"无重复"/"可能是 #X 的重复"禁用措辞**（#134/#135/#136 之后连续第 7-8 次）。
- **Pre-check 必检项升级**：反思 checklist 必须前置扫描本次分析中是否出现"未检测到重复"/"无重复"/"可能是 #X 的重复"/"#X 的重复"任一措辞——命中则视为红旗信号，必须改写为"暂未发现重复 Issue，建议检索关键词：..."。
- **新禁用清单扩展**：未来重复检测章节禁用措辞清单已扩展至包含"可能是 #X 的重复"（即使 #X 内容确实存在也不允许用此措辞）。凡涉及同源但动作不同的 Issue，应改用"与 #X 同源/相关但非重复"。
- **强制落款提醒**：重复检测章节末尾建议加注"⚠️ 措辞核对：是否使用了禁用的'未检测到重复'/'无重复'/'可能是 #X 的重复'？"

### 4.31 Issue #140 举报/通知路由类 Issue 标准模板（"单命令内权限路由分层"模式，#130 之后第 4 种角色模式）

- **触发场景**：某命令（举报/审批/操作类）需根据**被操作对象角色**决定通知/响应路由，而非"统一通知所有管理员"。典型表现"举报 A→通知 X、举报 B→通知 Y"。仓库第 4 种角色相关模式（与 #130/#132 权限模型重构、#131 群待办 API 权限、#135 读权限分级并列）。
- **关键差异 vs #130**（全局权限模型替换）：后者是 `plugin_admins` → 群原生权限的全局替换，前者是**保持现有权限模型、单命令内通知路由分支**。
- **分类与优先级**：`enhancement`(0.95) + `medium`。**4 条决策路径显式列出**：①横切重写（举报权限）+ 既有命令语义变化 → 最低 medium；②owner-driven + 已标准化模式 → 不升 high；③工作量可控 + 迁移路径明确 → 不升 high；④跨适配器风险已识别但有降级路径 → 维持 medium。**不应升 high**（无数据丢失/无安全风险），**不应降 low**（非纯减法）。
- **必给标签**：`enhancement`(0.95) + `command`(0.95) + `permission-model`(0.80-0.90，#130 沉淀高频) + `notification-routing`/`notify`(0.75-0.85，**仓库新建**) + `report`/`reporting`(0.85，**仓库新建**) + `bot-role`/`sender-role`(0.70-0.80，#130/#132 必给) + `group-management`(0.85-0.92) + `onebot`/`compatibility`(0.55-0.70，`get_group_member_info` 跨适配器差异) + `configuration`(0.65-0.75，若 `report_notify_admins` 扩展) + `breaking-change`(0.65-0.75，修改既有命令语义) + `needs-discussion`(0.75-0.85，决策点多) + `needs-info`(≤0.30，owner-driven) + `help wanted`(≤0.05) + `good first issue`(≤0.10)。
- **`needs-discussion` 必须 ≥0.80**（#140 反复触发）：owner-driven ≠ 无决策点，至少 5 个未明决策点：①举报群主是否允许 ②通知私聊 vs 群内 ③是否 @全体 ④频次限制 ⑤是否需白名单/敏感词过滤。
- **权限矩阵必显式列出 2 维**：举报人 ∈ {群主,群管,普通成员} × 被举报人 ∈ {群员,群管,群主} = 9 种组合，每种预期行为列清。
- **通知三要素必须明确**：①**通道**（私聊 vs 群内）②**对象**（所有管理/仅群主/特定名单）③**形式**（@全体 vs 单发 vs 群待办）。
- **必查项**（举报/通知路由类 7 项）：①举报人/被举报人角色获取 API 跨适配器差异（继承 #135/#136 沉淀的群成员字段差异矩阵）②通知通道实现方式（直接转发/合并/留痕）③扇出性能（大群群管+群主 × 高频举报）④通知风暴风险（一条举报 7 个管理各收一条）⑤"举报群主"边界（是否允许/静默失败 vs 显式提示）⑥bot 自身被举报场景（`/举报 @bot` 应 early return）⑦举报记录入库语义（pending 列表是否仍记录所有举报）。
- **可行性分支**（强制 A/B/C + 可选 D）：A 最小仅加权限分级判断 ~40-80 行/0.5-1 天；B 完整含通知通道可配置+留痕+豁免边界 ~120-200 行/2-3 天；C 完整 + 与既有通知/告警体系打通 ~200-300 行/3-5 天；D 分阶段（先 A 上线→收反馈→迭代 B/C）。
- **工作量反向校准**：分支 B 含六处同步（main.py + schema + README + 帮助 + CHANGELOG + 迁移指南），实际 2.5-3.5 天更稳妥而非 1.5-2.5 天。
- **滥用风险审计**（"全员可 X"必须评估）：①恶意刷举报 ②虚假举报骚扰管理者 ③群管互举报报复循环（建议加举报冷却）④被举报身份公开化风险 ⑤举报内容隐私边界（"色情链接"等是否转发）。
- **配置 schema 扩展建议**：`report_enabled`（全局开关）、`report_notification_targets`（按群 override 通知接收方）、`report_cooldown_seconds`（防滥用）、`report_privacy_redact`（是否脱敏举报内容）、`report_max_per_user_per_day`。
- **重复检测措辞强制模板**："**暂未发现**精确重复 Issue。建议检索关键词：`举报`/`report`/`举报人`/`被举报人`/`群主豁免`/`角色路由`/`通知通道`。与 #130/#132 同属权限模型重构模式但主诉求不同，建议互引 `Refs #130` 但**不**标记为重复。⚠️ 措辞核对"——`needs-info` ≤0.30 + `needs-discussion` ≥0.80 双校准。
- **标题改写**：原标题"。"等无意义信息时**必须改写**（禁止"无建议"），保留"全员开放"等关键权限变更信号。示例：`[enhancement][medium] /举报 命令全员开放并按角色分级通知（举报群员→全员管理，举报群管→仅群主，群主豁免）`。
- **跨 Issue 互引必备**：同类权限模型 Issue（#130/#132/#139）显式对比句（"与 §X 同级，因 Y 原因定 medium"），否则违反 #135/#136 沉淀的"同类 Issue 显式互引"要求。

### 4.32 校验失败短路器升级为系统级硬约束（#140 第 9 次触发，#131/#132/#133/#134/#135/#136/#138/#139 累计）

- **触发链路还原**：字段校验失败（`missing </SUGGESTED_TITLE>` 等）→ 反思摘要提示 `expected <X>` → 分析助手误判"所有判断都不可信" → 整体退化为 `other`/`无法评估`/空标签/`无建议`/`未检测到重复`。**这正是 §4.19 反模式第一条明令禁止的行为**。
- **必须固化到反思 Pre-check 第一优先级自检项**（落笔前必扫）：
  1. ⛔ **校验失败扫描**：所有字段是否含"无建议/无法评估/空/未检测到重复"？是 → **仅局部修复字段格式，不得连带退化实质性判断**。
  2. ⛔ **空标签扫描**：标签列表是否为空？若分类已确定但标签空 → 至少给 3-5 个通用候选。
  3. ⛔ **标题扫描**：原标题是否为"。"/空/无意义？若是 → 必须改写，禁止输出"无建议"；清晰原标题应标"可保留"。
  4. ⛔ **重复检测措辞扫描**：是否写了"未检测到重复/无重复/可能是 #X 重复"？若是 → 改为"暂未发现"+建议检索关键词+⚠️ 措辞核对。
  5. ⛔ **可行性分支扫描**：是否只给范围估算未给 A/B/C 显式分支？若是 → 补全三档（#139 加分支D）。
  6. ⛔ **优先级决策路径扫描**：是否显式列出 4 条判定路径？若否 → 补全。
- **正确输出保底模板**（校验失败时仍须输出）：分类 + 优先级（带 4 条决策路径）+ 摘要 + 建议标签列表（≥3 个通用候选）+ 重复检测（"暂未发现"+关键词+⚠️）+ 标题改写（"可保留"或轻量规范化）+ 关键问题列表 + 可行性 A/B/C 三档。
- **禁用措辞清单**（扩展至包含"可能是 #X 的重复"，即使 #X 内容确实存在也不允许用此措辞）："未检测到重复"/"无重复"/"无建议"/"无法评估"/"可能是 #X 的重复"/"无关键问题"。凡涉及同源但动作不同的 Issue，应改用"与 #X 同源/相关但非重复"。
- **未读取代码也应有条件性判断**：禁止用"无法评估"逃避——基于项目记忆 #131/#135/#133 沉淀可给出条件性评估（必查项 + 可行性分支 A/B/C）。
- **元教训**：同一反模式若在多个反思中反复触发（"校验失败整体降级"在 PR #123 + Issue #130 + #131 三轮 + #132 五次 + #140 九次触发），说明反思流程缺乏自动化保护机制，必须固化 Pre-check 短路器到流程而非仅依赖记忆。**已升级为反思流程前置硬阻断清单**。

### 4.33 Issue #143 动作联动型模式（仓库第 5 种角色相关模式）

- **触发场景**：动作 A 成功后自动触发动作 B（kick → clear-history / ban → notify / mute → log 等），A 与 B 共享配置与权限。
- **5 种模式对照**：
  1. 权限模型重构：#130/#132/#139
  2. 引用消息触发型：#131 群待办
  3. 装饰字符 QQ：#134/#136
  4. 举报/通知路由：#140
  5. **动作联动型（新增 #143）**：A 成功后触发 B
- **必查 10 项**：① 回执一致性（B 部分失败时 A 状态如何回执）② 通知风暴（B 触发的群内"撤回消息"提示是否影响体验）③ 限流策略（B 高成本 API 调用如何分批）④ 配置粒度（是否可关闭/可调上限/按群覆盖）⑤ 权限双校验（A 与 B 各自权限如何对齐 + bot 自身权限边界）⑥ 时间窗约束（OneBot 2 分钟）⑦ 跨适配器差异矩阵 ⑧ 本地缓存兜底 ⑨ 可观测性（成功 N/M、跳过 X 因时间窗、失败 Y 因限流分类回执）⑩ 去重（本地缓存 vs 服务端历史可能产生重复 message_id）。
- **必给标签**：`enhancement`(0.95) + `command`(0.85) + `configuration`(0.80) + `group-management`(0.90, #140 硬约束) + `recall`(0.85) 或对应动作标签 + `permission`(0.80) + `message-history`(0.75) 或对应数据源 + `onebot`(0.85) + `compatibility`(0.75) + `external-reference`(0.85, 仓库新建) + `needs-discussion`(0.80) + `needs-info`(≤0.30, #140 双校准硬约束)。
- **优先级决策路径 5 条**（升级自 #140 4 条）：① 高风险动作 → 最低 medium ② owner-driven + 已标准化模式 → 不升 high ③ 工作量可控 + 迁移路径明确 → 不升 high ④ 跨适配器风险已识别但有降级 → 维持 medium ⑤ **默认值决策（默认 false → 维持；默认 true → silent behavior change 升 high/breaking-change 0.55-0.65）**。
- **breaking-change 置信度 4 档精细化**（#142 沉淀）：① 新增配置项 + 默认 false/缺省 → ≤0.30 ② 新增配置项 + 默认 true → 0.55-0.65（silent behavior change）③ 修改既有配置项默认值 → 0.70-0.80 ④ 删除既有配置项/命令 → 0.90-0.95。
- **外部参考实现 vs 直接合入标签区分**：`external-reference`（借鉴外部仓库实现）vs `merge-request`+`license-check`（直接合并代码），不可混用。
- **工作量下限规则**：涉及"本地缓存 + 协议兜底 + 配置 schema + 指令入口"4 件套的最小分支 A 至少 1 天起步。

### 4.34 Issue #142 撤回增强第 5 模式（/踢 后批量撤回，按群覆盖）

- **触发场景**：`/踢` 后按配置自动/手动批量撤回被踢用户本群全部聊天记录（支持按群覆盖）。owner-driven 撤回增强类。
- **核心风险**：批量删除他人消息（高权限面）+ OneBot `delete_msg` 限速（不同实现 1/s 到 5/s）+ 2 分钟时间窗逐条检测 + PR #123 缓存骨架依赖。
- **必查项**：① `get_group_msg_history` 跨适配器最大返回差异矩阵 ② `delete_msg` 限速差异矩阵 ③ 缓存骨架真实容量与重启语义（`message_history` maxlen/重启清空/跨群隔离）④ 自身消息排除（`after_message_sent` 早退前写入）⑤ 业务早退链吞噬共享逻辑规避 ⑥ 节流方案（全局 sleep vs 令牌桶/滑动窗口）⑦ `group_overrides` 嵌套结构与生效顺序 ⑧ "全量"语义在 README 的诚实告知。
- **必给标签**：`enhancement`(0.95) + `recall`(0.95) + `command`(0.95) + `message-history`(0.90) + `group-management`(0.88) + `configuration`(0.85) + `onebot`(0.80-0.85) + `onebot-extension`(0.55-0.65, #139 漏过的硬约束) + `throttling`/`rate-limit`(新建) + `pagination`(新建) + `silent-behavior`(默认 true 时) + `partial-failure`(新建) + `needs-discussion`(0.85) + `needs-info`(≤0.30) + `breaking-change`(0.30-0.40 默认 false 时)。
- **可行性分支**：A 80-120 行 1.5-2 天 / B 200-350 行 3-5 天 / C 350-500 行 5-7 天 / D 分阶段先 A 后 B/C。
- **跨 Issue 决策路径同构互引规则**：当两个 Issue 决策路径同构（如 #142 与 #140）时，应在优先级章节**显式互引**——"本 Issue 与 #140 决策路径同构（横切权限但有兜底 / owner-driven + 已标准化模式 / 工作量可控 + 迁移路径明确 / 风险已识别但有降级），维持 medium 优先级"。
- **缓存骨架依赖显式标注**：若 PR #123 未 merge，#142 分支 A 工作量翻倍（需先建缓存）。可行性评估必须标注"假设 PR #123 已 merge（待查证）"。

### 4.35 PR #123 message_history 本地缓存统一提示模板

- **基础设施地位**：PR #123 引入的 `message_history` 本地缓存是后续多个 issue 的强依赖（#142 批量撤回、#143 踢人清历史、自动撤回、按用户撤回等）。
- **README/帮助统一告知**："本地缓存仅进程内有效，重启丢失；服务端撤回受 2 分钟时间窗限制"——避免分散到各命令帮助中导致用户认知不一致。
- **缓存设计 6 要素**（#123 沉淀）：① 按群隔离（`{str(group_id): deque[dict]}`）+ 最小化（仅 `message_id` + `user_id`）② 工厂函数 `{}` + `setdefault(key, factory())` 优于 `defaultdict(factory)` ③ bot 消息写入独立于业务早退链（PR #123 阻断性 bug 根因）④ 容量 `maxlen ≥ 4 × N` ⑤ 必须排除命令自身 `message_id` ⑥ 写入失败 try/except + debug。
- **应用层命令共用缓存时必查**：① 跨群隔离 vs 共享语义 ② 缓存容量对"全量"操作的真实上限 ③ 重启后历史不可恢复的兜底（OneBot `get_group_msg_history` 跨适配器兼容性问题）④ 自身消息排除路径是否被业务早退吞噬。

## 5. Issue 分析与标签经验（高层规则，详见 memory.md）

- 标题为"。"、"，"或信息极少时必须基于正文错误文本、复现命令和代码线索检索。
- **结构化输出校验失败 ≠ 信息不足**：应**修复字段输出**保留实质判断（分类/可行性/标签/关键问题列表），不得整体退化为"无法评估"——这是仓库反复出现的反模式（PR #123 第六轮审查即因此完全失败）。pre-check：任何字段出现"无建议/无法评估/空"前，确认是否源于校验失败。
- 不能因校验失败把 `bug`/`enhancement` 降级为 `other`、标签留空、可行性"无法评估"。
- 标签建议覆盖主类型、模块和风险；至少保留主标签与核心模块；高频模块标签：`recall`、`message-history`、`command`、`parser`、`onebot`、`group-management`、`moderation`、`stt`/`voice`、`title`/`special-title`。
- **重复检测措辞模板**：无历史列表时**必须**写"暂未发现（建议检索关键词：...）"，不得写"未检测到重复"/"无重复"/"可能是 #X 的重复"。方向相反但同主题（如 #124 增强 vs #126 删除）严格归为 `related` 而非重复。
- **同根因不同症状 issue 重复检测置信度上限**（§4.27 #136 沉淀）：主分类 + API 都相同时置信度上限 0.75-0.85；症状/触发命令差异明显时上限 0.75；**严格禁止**"可能是#X的重复"措辞。
- **撤回命令族五处同步清单**（高频踩坑）：`main.py` + `README.md` + `/撤回`/`/消息列表` 帮助文本 + `_conf_schema.json` + CHANGELOG；扩展为七处（涉及多媒体转写层）：+ STT 配置文件/字段 + 误伤率文档说明。
- **群管理动作新增可细分为读类 vs 写类 vs 动作联动型子模板**：写类（#131 群待办）关注写权限、API 返回、UI 生效延迟、回执；读类（#135 禁言列表）关注读权限、字段差异、数据脱敏、缓存策略、空状态/时间格式、侦察工具风险；动作联动型（§4.33 #143）关注回执一致性、通知风暴、限流、配置粒度、可观测性、去重。
- **owner-driven + 涉及设计决策的 needs-discussion 校准**（§4.22）：即使维护者本人发起（自实施），只要涉及字段差异/权限/性能等设计决策，`needs-discussion` 应保持 0.65-0.85 高权重，不能因 owner-driven 就降到 0.4。
- **默认值决策必须列入优先级路径**（§4.33 #143 沉淀，升级自 #140 4 条为 5 条）：owner-driven Issue 的"默认开启 vs 默认关闭"是优先级决策的核心变量；默认 false → 维持；默认 true → silent behavior change 升 high/breaking-change 0.55-0.65。
- **breaking-change 置信度 4 档精细化**（§4.33 #142/143 沉淀）：① 新增配置项 + 默认 false/缺省 ≤0.30 ② 新增配置项 + 默认 true 0.55-0.65 ③ 修改既有配置项默认值 0.70-0.80 ④ 删除既有配置项/命令 0.90-0.95。
- **跨 Issue 决策路径同构互引规则**（§4.34 #142 沉淀）：当两个 Issue 决策路径同构（如 #142 与 #140）时，应在优先级章节**显式互引**——"本 Issue 与 #X 决策路径同构，维持 medium 优先级"。
- **外部参考实现 vs 直接合入标签区分**（§4.33 #143 沉淀）：`external-reference`（借鉴外部仓库实现）vs `merge-request`+`license-check`（直接合并代码），不可混用。
- **删除既有命令用法类 Issue 强制结构**（维护者自提减法）：① 精确删除目标（Path 分支/行号）② 保留目标边界 ③ 删除后旧语法处理 ④ 依赖解耦（如 `/消息列表` ↔ `/撤回 编号`）⑤ 用户迁移成本与告知 ⑥ CHANGELOG breaking-change 标注。
- **"功能裁剪"类优先级判定**：维护者自提 + 纯删减 + 改动局部化 → `low`~`medium`；涉及对外契约删减且与并行 Issue 方向冲突 → `medium` 且强化 `needs-discussion`。
- **路由语义歧义类 Issue**：必须显式列出候选语义并请求维护者确认，标 `needs-discussion`。
- 信息不足时可标 `needs-info`；头衔类 bug 还需 bot 群角色、目标用户角色、近期配置变更。
- **"默认行为"类改动**：UX 增强常被低估工作量，预留 40-80 行而非 20-40。
- **校验失败短路器（强制 Pre-check）**：在任何字段出现"无建议/无法评估/空/未检测到重复"前，必须先确认是否源于校验失败——校验失败应**局部修复字段输出格式**（如把"无建议"改成实际建议），**不得把所有判断都退化为"无法评估"/`other`/空标签/无建议**。**实质性判断（分类/可行性/标签/标题）维持原始判断**。
- **重复触发反模式的纪律性**：同一反模式若在多个反思中反复触发（如"校验失败整体降级"在 PR #123 + Issue #130 + #131 三轮 + #132 五次触发），说明反思流程缺乏自动化保护机制，必须固化 Pre-check 短路器到流程而非仅依赖记忆。
- **正确输出保底模板**（校验失败时仍须输出）：分类 + 优先级 + 摘要 + 建议标签列表 + 重复检测（"暂未发现"+ 关键词 + ⚠️措辞核对）+ 标题改写（"可保留"或轻量规范化）。

## 6. 开发约定与注意事项

- 逻辑集中在 `main.py`，修改时全局搜索相似命令模式，避免复制粘贴式遗漏。
- 管理员 QQ 号按字符串处理；群号统一 `str()` 归一化（缓存键、日志键、配置键一致）。
- QQ 操作依赖平台 API 和机器人群权限，必须处理接口失败、权限不足、消息超时和异常返回。
- 权限类改动必须覆盖：全局配置、群级覆盖、命令入口、权限判断函数、文档说明和回归测试。
- AstrBot API/命令解析兼容问题，应区分插件启动、命令注册、命令调用、特定输入触发四个阶段。
- 对群管理"成功提示"保持保守：设/取消头衔、设/取消管理、禁言、踢人、撤回、改群昵称等都应检查 API 返回与状态，失败时给明确提示；"API 返回 ok ≠ 实际生效"是已知反模式，必要时回读。
- 对依赖 OneBot 能力的命令，应在 README/帮助中标注依赖与替代方案。
- 新增跨私聊/群聊工作流时，必须先设计状态机、申请 ID、权限边界、持久化/过期、并发幂等、隐私和发送失败处理。
- 引入外部代码前评估：许可证兼容、依赖与框架版本、代码风格、维护责任、配置 schema 兼容性、是否应抽取通用逻辑而非直接复制。
- 撤回相关改动必须做**对称性检查**：`recall_cmd` 与 `recall_user_cmd` 在入口分流、缓存读写、用法提示上必须对称；`/撤回自身 N` 与新默认行为不得冲突；`message_history` 写入路径不得被业务早退链吞噬。
- **撤回类 PR 强制检查清单（9 项）**：时间窗（2 分钟）、限流、部分失败处理、自身消息排除、bot 消息处理、撤回目标过滤、缓存数据结构、OneBot 适配差异、配置 schema 一致性。**删除类反向清单（9 项，参 §4.15）**：精确删除路径 + 保留路径边界 + 调用点清空 + README 帮助文案清理 + yield 帮助文本同步 + 配置 schema 语义缩窄 + bot 回复模板措辞清理 + CHANGELOG breaking-change 标注 + 替代方案提示。
- 验证清单：`python -m py_compile main.py` ≥ `ast.parse`；本地缓存回退类改动应附最小单元测试；README/帮助/schema/CHANGELOG 必须在同一 PR 同步；提交信息避免批量重复 `chore` commit（属提交历史质量问题——参 §4.11、§4.14）；行号引用必须标注"已读取 main.py 验证"或用"约 Lxxxx"模糊表述。
- **AstrBot 消歧层选择**：撤回/计数类命令的参数语义修改必须明确是在 `@filter.command` 装饰器签名层（改类型注解/默认值）还是 handler 内部（先收 `str` 再分流）做消歧——后者要处理 `try/except int()` 已被 AstrBot 提前按注解转换的情况（参 §4.2、§4.15）。
- **OneBot 群待办类命令七处同步清单**（新增 owner-driven 命令模板，参 §4.21）：main.py + README + 帮助命令 + `_conf_schema.json` + CHANGELOG + `_GM_COMMAND_NAMES` 元组注册 + 跨适配器兼容性文档。
- **"单命令内权限路由分层"模板**（§4.31 #140 沉淀，#130 之后第 4 种角色模式）：举报/通知路由类命令的必给标签、2 维权限矩阵、通知三要素、7 项必查、A/B/C/D 可行性分支、配置 schema 扩展建议（`report_*` 系列）。
- **owner-driven Issue 标签权重校准**（§4.22）：`needs-info` ≤0.2-0.30（不是 0.4），`needs-discussion` 0.75-0.85（不是 0.55-0.65），`good first issue` ≤0.15（不是 0.2），`help wanted` ≤0.05-0.1。
- **权限模型重构类 Issue 必给标签基线**（§4.20）：`enhancement` + `breaking-change` ≥0.90 + `deprecation` ≥0.75 + `permission-model` ≥0.80 + `bot-role`/`sender-role` ≥0.70 + `onebot` ≥0.65 + `needs-discussion` 升 0.75；移除 `question`。

## 7. 协作与维护

README 维护功能表、安装、配置、权限说明和使用示例。Bug 与功能建议通过 GitHub Issue 管理；低到中等复杂度修复类 PR 优先合并；提交信息避免批量重复 `chore` commit 污染历史。项目仅供学习与交流使用，需遵守 QQ / QQ 群相关规范。