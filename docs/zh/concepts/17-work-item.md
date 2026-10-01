# Work-item 工作记忆

Work-item 模式只在 working memory 中保留继续当前工作所需的有限状态。已完成或暂时不活跃的工作可以移出该视图，其权威记忆和原始会话归档仍可恢复。此模式需要显式开启，只替换所选会话的逐 section 累积摘要；默认仍为 `legacy`。

## 在新会话中启用

通过 `POST /api/v1/sessions` 创建会话时传入：

```json
{
  "memory_policy": {
    "working_memory": {
      "enabled": true,
      "mode": "work_item"
    }
  }
}
```

该模式自动将 `work_item` 加入提取类型。显式传入空的 `memory_types` 列表时，也只增加这一必需类型，不会开启其他类型。启用时请创建新会话；已有 legacy 会话不会自动迁移。创建和读取接口见[会话 API](../api/05-sessions.md)，客户端开关见 [Pi 集成](../agent-integrations/11-pi.md)。

## 权威状态与热视图

每个 work item 是 `viking://user/{user_id}/memories/work_item/{work_item_id}.md` 下的可变 Markdown，属于认证用户，可被该用户的多个 session 共享，不按 workspace peer 分区。ID 由服务端分配，改名不改变身份。复用现有项必须读取其权威状态；标题相似本身不足以判定是同一项工作。

短字段包括 `title`、`scope`、`goal`、`status`、`current_state`、`next_action`、`constraints`、`waiting_for`、`decisions` 和 `refs`。更新替换当前状态并清除已解决字段，不追加每一轮历史。状态为 `open`、`in_progress`、`waiting`、`blocked`、`done` 或 `cancelled`；重新开启终态工作需要本轮明确的用户请求，且请求时间晚于记录的完成证据。完成证据缺少可用时间时，保守使用终态写入时间；延迟处理的旧请求不能仅靠读到最新版本重新开启工作。共享写入路径在锁内检查最新 `version`，拒绝旧快照覆盖其他 session 的进展。

会话投影选择近期工作，不自动激活该用户的所有未完成项。从 A 切到 B 后再要求继续 A，可以通过既有检索找回 A 并重新绑定。同一次提取最多读取三个语义候选，再明确选择与请求匹配的非终态项进行激活。单独激活不修改权威状态、不增加版本，也不声明消息已覆盖；其他检索命中不会自动激活。context 和 archive 读取按活跃 URI 重读权威状态，用模板生成视图，不调用 LLM。因此，另一个 session 已完成的工作不会继续注入旧的 next action。

V1 使用 OpenViking 的 token 估算器，默认限制为：

| 内容 | 上限 |
| --- | --- |
| 单个 work item 全部短字段之和、权威 Markdown 渲染正文 | 各 10,000 估算 tokens |
| WM 投影，含 continuation 和恢复提示 | 42,000 估算 tokens |
| 单次投影的活跃项 | 最多 3 项 |
| 完整 continuation，含上一轮续接状态和格式开销 | 10,000 估算 tokens |

这三个预算在服务端 `memory` 配置中设置，与会话 policy 分开：

```json
{
  "memory": {
    "work_item_token_budget": 10000,
    "continuation_token_budget": 10000,
    "work_item_projection_token_budget": 42000
  }
}
```

默认总预算可容纳三个达到上限的 work item、一份完整 continuation 和格式开销。这些是容量上限，不是要求模型写满的目标。来源 ID 和逐消息归宿保存在 archive 覆盖账本中，不将整个账本渲染进 working memory。每个 archive 的 `continuation-provenance.json` 保存本轮续接摘要的直接来源；历史出处通过 checkpoint 引用追溯，不递归复制到热 continuation。即使正文未超预算，也会移出旧版携带的来源 ID 清单；压缩模型只接收正文和必要引用。

投影按完整 work-item 块选择，不通过按字符截断约束来凑预算。调用方的 context 预算还必须容纳未覆盖的原文尾部。超限更新会失败，保留此前的权威正文。

## Checkpoint 发布与恢复

后台 commit 分别运行普通 memory 和 work-item 两路独立的 LLM 提取，使用各自的 schema、prompt 和召回范围，并行处理后复用现有更新链路。普通 memory 不注入 work-item 规则或工具证据。每路仍可按现有 ExtractLoop 执行读取或格式修复，因此不保证恰好两次底层模型请求。

提取成功后，没有被选入 work-item 状态或 continuation 的新原消息记为 `archive_only`。完整内容仍保存在 `messages.jsonl` 及其引用的工具结果存储中，不再复制进热视图。同一消息里的任务状态和额外会话约束可以同时保留。

Continuation 维护带有服务端稳定 ID 的当前事项列表，不再累积历轮摘要。每轮提取结合已有事项和新消息：未变化的事项沿用原文；更新时替换该事项的当前正文，保留原 ID；明确解决需说明原因，随后移出热视图；升级为 work item 时，只有对应任务成功写入后才移除原事项。模型漏报的事项继续保留，不将遗漏解释成解决。历史内容和状态变化留在 archive。投影统一显示一次背景标题，不再给每个事项重复追加旧摘要标题。

提取协议支持 `create`、`keep`、`update`、`resolve` 和 `promote`。旧事项通过 `continuation_id` 定位，新建事项不接受模型编造的 ID。更新与解决需要本轮消息依据，不能仅凭旧 checkpoint 正文。升级时，Python 指定 work-item 对象（`work_item=...`），JSON 指定其 page ID（`work_item_page_id`）。只选中或读取任务不算升级成功：成功写入的任务必须覆盖被转移的 continuation 事项。未变化的事项可以直接 keep，无需重复正文或补造新依据。

常规 continuation 更新来自 work-item 提取输出（Python 的 `sdk.continuation` 或 JSON 的 `continuation_coverage`），不额外调用总结模型。稳定事项 ID 与原始消息 ID 分开，事项携带来源引用，作为 assistant 背景传递，不能成为重新开启终态任务的新用户证据。明确成功的空提取结果可以让新消息仅归档，同时保留旧事项；异常、无返回或根本未执行提取时，不能仅因原文已保存就推进 checkpoint。

多个成功写入的 work item 分担同一原消息的不同片段时，会合并其 ranges 后判断整条消息是否覆盖；失败写入不贡献覆盖。工具 input/output 每个字段最多保留前 2,000 字符，并附截断提示；整块工具证据最多 16,000 估算 tokens，优先近期结果。partial 表示预览不完整，不剥夺消息归属和续接分类资格。状态更新可以使用可见证据，但不能虚构未见结果或将工具结束等同于任务完成；结果不明确时保留待验证事项及引用。模型提供的 source ranges 和分类是归属声明，账本**不是所有续接事实均已保留的形式证明**。archive_only 内容可从存储追回，但模型仍可能漏选应进入热视图的新信息；存储层保留原文不等于热记忆没有遗漏。

Archive 在应用前将已分配 ID 的 work-item 操作保存在现有 metadata，部分写入后重试保留原 ID、消息批次和来源，并单独冻结解释 ranges 时使用的 continuation 背景快照。新批次即使跳过已经提取的原消息，仍能看到合并后的最新 continuation；重放批次则沿用自己的背景快照。普通记忆提取不会收到这份额外背景。每项成功写入有独立回执；即使进程在记录批次进度前中断，或其他 session 随后更新该项，也不会重复应用已成功的操作。升级事项必须由当前提取计划的成功写入回执确认，历史上写过同一任务不能替本次升级作证。权威文件只携带一个待落盘回执，历史回执保存在原 archive 的 `work-item-receipts/` 下，避免权威状态随重试无限增长。

遇到版本冲突时，下一次后台重试只读取冲突项的最新权威状态，并结合原始证据重新提取这些项的更新；已成功项、原有续接分类和任务身份保持不变。修订后的计划必须先持久化，才能再次尝试版本检查和写入；后续 archive 继承最高修订版本。每次处理最多执行一轮冲突重提取，再次冲突则保留进度等待下次重试。全部计划操作成功后才记录该批完成。模型、存储持续不可用或冲突持续发生时仍会失败，不会强行覆盖最新状态或发布不完整 checkpoint。

提取完成与 checkpoint 就绪分开记录。完整 continuation 超预算，或旧 checkpoint 仍携带旧格式原文 residual 时，单独的后台 repair 阶段负责压缩续接内容。这个异常恢复路径会调用 LLM；正常投影、context 读取、archive 读取和 Pi compact hook 不调用 LLM。Repair 重试复用已完成的权威状态写入，不重新执行这些写入。成功的 repair 或降级结果会持久化，后续发布失败时可以复用。

模型调用失败、返回无效结果或仍然超预算时，先将完整续接快照写入 `continuation-overflow.json`，再按最新相关消息优先保留能装下的完整条目，较旧条目优先移出热视图，不按字符截断。单条本身过大时可以只保留恢复提示。标题、来源引用和恢复提示都计入 continuation 与整体 WM 预算。归档或来源账本写入失败、预算小到容不下提示时，仍拒绝发布。

降级 checkpoint 标记 `continuation_degraded: true`，并携带独立的 `pending_continuation_uri`。后续摘要省略它或将普通 continuation 标为已解决，都不会清除该指针；context、archive 和 Pi compact 的视图持续提示先读取相关归档。再次溢出时，新快照引用此前待恢复快照，热视图仅携带一个入口。当前不会因一次读取自动清除待恢复状态；该提示保证可追溯，不是强制 agent 读取的执行门禁，也不代表所有约束仍完整位于热记忆中。

生成有界投影后，最后通过临时文件加 rename 发布 `.done`。只有 overview 文件不代表 checkpoint 就绪。未完成项移出热视图前，要逐项确认其向量记录覆盖所需的权威版本；终态项仍异步索引，但不阻塞发布。这不是等待全局 embedding 队列的屏障，仍在热视图内的项不必等待无关索引。保存原文与可通过向量检索找回是两个不同条件。

最后一个就绪 checkpoint 之后的 pending、failed archive 都保留为原文续接。若权威状态刷新后无法生成合法视图，服务端不返回可用 checkpoint，并保留原历史供回退。请求预算不足时返回 `budget_insufficient`，不返回可推进的 checkpoint 边界；调用方必须保留自己的 transcript，或增大预算。

已有本地和 cuvs 向量集合通过现有 schema 迁移增加 `work_item_version` 字段。创建 work-item 会话及提交前，会检查实际集合 schema 是否包含该 int64 字段；缺失、类型不符或无法验证时，在创建归档前明确返回 `FAILED_PRECONDITION`，legacy 模式仍可使用。已有远端集合须在外部完成迁移（新增字段默认 0）并重新索引 work item。Volcengine API-key data-plane 返回的是本地期望 schema，不能证明远端能力；work-item 模式需要能读取实际集合 schema 的连接，例如 AK/SK control-plane 访问。

Pi 请求 compact 时采用两级回退：

1. 使用此前有效的 checkpoint，加上其覆盖边界至本次 cut 之间的完整 Pi 原文；cut 之后的尾部由 Pi 保留。
2. 若就绪状态、分支连续性、刷新或完整上下文预算检查失败，执行 Pi 自带 compact。

Hook 对缓存 checkpoint 只刷新一次，不新增 LLM 调用或等待索引、就绪状态；迟到的 checkpoint 不能覆盖已经完成的新 Pi compact。

V1 暂不提供专门的主动 work-item 写入 API、依赖图、legacy 自动迁移或完美语义匹配保证。通用正文 write 不能绕过 work-item 校验。冷项通过现有 recall 按需恢复；原始证据可通过列举 session history，找到对应 archive，再分段读取 `messages.jsonl` 及其引用的工具结果恢复。
