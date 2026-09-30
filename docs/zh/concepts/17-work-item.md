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

V1 使用 OpenViking 的 token 估算器，当前限制为：

| 内容 | 上限 |
| --- | --- |
| 单个 work item 全部短字段之和 | 1,200 估算 tokens |
| 权威 Markdown 渲染正文 | 4,000 估算 tokens |
| WM 投影，含 residual 和恢复提示 | 3,000 估算 tokens |
| 单次投影的活跃项 | 最多 3 项 |
| 续接摘要与未归属原消息的 residual | 1,000 估算 tokens |

投影按完整 work-item 块选择，不通过按字符截断约束来凑预算。调用方的 context 预算还必须容纳未覆盖的原文尾部。超限更新会失败，保留此前的权威正文。

## Checkpoint 发布与恢复

后台 commit 分别运行普通 memory 和 work-item 两路独立的 LLM 提取，使用各自的 schema、prompt 和召回范围，并行处理后复用现有更新链路。普通 memory 不注入 work-item 规则或工具证据。每路仍可按现有 ExtractLoop 执行读取或格式修复，因此不保证恰好两次底层模型请求。

每条源消息可以归属于成功更新的 work item、保留为有界续接摘要，或明确记录无需续接的原因；没有明确归宿时仍保留原文。同一消息里的任务状态和额外会话约束可以同时保留。摘要通过 work-item 提取输出（Python 的 `sdk.continuation` 或 JSON 的 `continuation_coverage`），不新增总结调用；下轮作为 assistant 背景携带，不能作为重新开启终态任务的新用户证据。原始 transcript 不修改，完整来源保存在覆盖账本中。

多个成功写入的 work item 分担同一原消息的不同片段时，会合并其 ranges 后判断整条消息是否覆盖；失败写入不贡献覆盖。工具 input/output 每个字段最多保留前 2,000 字符，并附截断提示；整块工具证据最多 16,000 估算 tokens，优先近期结果。partial 表示预览不完整，不剥夺消息归属和续接分类资格。状态更新可以使用可见证据，但不能虚构未见结果或将工具结束等同于任务完成；结果不明确时保留待验证事项及引用。模型提供的 source ranges 和分类是归属声明，账本**不是所有续接事实均已保留的形式证明**。缺少归属不能被解释为允许丢弃消息；residual 超限时不发布 checkpoint，并在 archive metadata 保留实际 token 数、预算与逐消息归宿供排查。

Archive 在应用前将已分配 ID 的 work-item 操作保存在现有 metadata，部分写入后重试复用同一计划及来源，不因模型重排或重新分批生成新 ID；已应用的同版本操作不会重复写入，其他 session 的更新仍受版本检查保护。全部计划操作成功后才记录该批完成。生成有界投影后，最后通过临时文件加 rename 发布 `.done`。只有 overview 文件不代表 checkpoint 就绪。未完成项移出热视图前，要逐项确认其向量记录覆盖所需的权威版本；终态项仍异步索引，但不阻塞发布。这不是等待全局 embedding 队列的屏障，仍在热视图内的项不必等待无关索引。保存原文与可通过向量检索找回是两个不同条件。

最后一个就绪 checkpoint 之后的 pending、failed archive 都保留为原文续接。若权威状态刷新后无法生成合法视图，服务端不返回可用 checkpoint，并保留原历史供回退。请求预算不足时返回 `budget_insufficient`，不返回可推进的 checkpoint 边界；调用方必须保留自己的 transcript，或增大预算。

已有本地和 cuvs 向量集合通过现有 schema 迁移增加 `work_item_version` 字段。创建 work-item 会话及提交前，会检查实际集合 schema 是否包含该 int64 字段；缺失、类型不符或无法验证时，在创建归档前明确返回 `FAILED_PRECONDITION`，legacy 模式仍可使用。已有远端集合须在外部完成迁移（新增字段默认 0）并重新索引 work item。Volcengine API-key data-plane 返回的是本地期望 schema，不能证明远端能力；work-item 模式需要能读取实际集合 schema 的连接，例如 AK/SK control-plane 访问。

Pi 请求 compact 时采用两级回退：

1. 使用此前有效的 checkpoint，加上其覆盖边界至本次 cut 之间的完整 Pi 原文；cut 之后的尾部由 Pi 保留。
2. 若就绪状态、分支连续性、刷新或完整上下文预算检查失败，执行 Pi 自带 compact。

Hook 对缓存 checkpoint 只刷新一次，不新增 LLM 调用或等待索引、就绪状态；迟到的 checkpoint 不能覆盖已经完成的新 Pi compact。

V1 暂不提供专门的主动 work-item 写入 API、依赖图、legacy 自动迁移或完美语义匹配保证。通用正文 write 不能绕过 work-item 校验。冷项通过现有 recall 按需恢复，archive 保留原始证据，供 `archive_search` 和原文读取使用。
