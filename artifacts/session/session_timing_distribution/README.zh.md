# session_timing_distribution

**在一个编码智能体所消耗的墙钟时间里，有多少是人类在思考、LLM 在生成、以及工具在执行——分别按会话、按请求、按步骤看？**

`session_cost_distribution` 的时间域姊妹篇。计算 `tab:timing_distribution`（`src/04_SessionContext.tex`）背后的数据：对每种粒度和每个类别，给出每单位的 avg /
p50 / p90 / p99 以及该类别在总时间中的占比（与成本表相同的 Avg/P50/P90/P99 + % 布局）。类别集合随粒度而不同，因为**人类思考是一个请求之间的量**，所以它只存在于会话层级：

- **每会话** —— `Total elapsed`（墙钟时间第一个→最后一个 timing event）= `Human thinking` +
  `LLM generation` + `Tool execution` + `Other (overhead)`。
- **每请求** —— `Total (response time)`（轮次端到端）= `LLM generation` + `Tool execution` +
  `Other (overhead)`。没有 human 项：human wait 位于各请求*之间*，永远不会在一个请求内部。
- **每步骤** —— 仅 `LLM generation` vs `Tool execution`（一个轮次没有 human 项，也没有端到端）。

## 定义

- **LLM generation**（每步骤）—— 可观测的生成跨度，从最近的合格输入事件 →
  最后一个模型输出事件；与 `llm_generation/generation_time_cdf` 以及
  `human_in_the_loop/user_turn_decomposition` 中的每轮次生成一致。
- **Tool execution**（每步骤）—— 严格为正的有效工具延迟之和
  （`tool_internal_latency_ms`，否则 `tool_wall_latency_ms`）；与
  `tool_calls/tool_latency_distribution` 一致。
- **Human thinking**（每会话）—— 人类输入等待之和（前一个模型输出 → 下一个触发回复的用户消息）；与 `human_in_the_loop/human_input_wait` 一致。
- **请求端到端** 以及残差 `Other (overhead)` = `e2e − generation − tool`，与
  `user_turn_decomposition` 逐轮次匹配（已验证：merged e2e 2,782.7h，generation 1,141.3h，tool 1,663.4h，残差 −22.0h）。残差可以为**负**：累加的每轮次生成与每工具有效延迟会重叠（并发的工具、工具调用期间的生成流式传输），所以它们之和可以超过测得的端到端。
- **请求** —— 一个用户轮次（与 `user_turn_decomposition`、
  `user_turn_response_time`、`session_internal_counts`、`session_cost_distribution` 相同的轮次状态机）。**步骤** ——
  一个 LLM 轮次。**会话** —— 一个 `session_id`（4,258 个会话有正的墙钟时间跨度；其余是 single-timestamp 的，被丢弃）。

## 运行方式

```bash
uv run python artifacts/session/session_timing_distribution/analyze.py -i trace/syfi_coding_trace.jsonl
uv run python artifacts/session/session_timing_distribution/analyze.py            # default merged trace
```

## 输出

- `session_timing_distribution.tex` —— 合并后的单列计时表（Avg / P50 / P90 / P99
  + % time），用于论文。
- `session_timing_distribution.md` —— 该表格的 GFM Markdown 镜像，渲染在网页详情页上。
- `headline.json` —— 用于 Overview gallery 卡片的几个 headline 数字。
- stdout —— 合并 + 按提供商（Claude / Codex）的各类别分位数与时间占比。

## 关键数字（公开数据）

- **会话大部分时间是空闲的：人类思考占会话墙钟时间的 92.3%**（在一个 8.2h 的会话中平均 7.6h；中位数极小——一个单请求的会话没有请求间间隔）。这条长长的空闲尾部（会话 p99 ≈ 206h）正是把 prompt 前缀推过缓存 TTL 的原因。
- **在一个请求内部，主导的是工具执行，而非生成：在 2,782.7h 的总响应时间中，工具占 59.8% vs 生成占 41.0%**（两者略有重叠，因此占比之和可超过 100%）。
- 平均响应时间：**4.3 min / 请求**（p50 38s，p90 6.4 min）；平均有效工作量为每个步骤 **11.5s
  生成 + 16.8s 工具**。

在与提供商无关的定义下，会话的人类占比在各提供商间是一致的（Claude 89.9%，Codex 94.3%）——早先基于触发器的定义低估了 Codex（81.6%），并把约 ~13% 溢出到一个 "Other" 残差中，这正是那一行残差被移除的原因。

无图。

## SyFI result analysis

### session_timing_distribution.md

一个编码会话大部分时间是空闲的，在等待人类（论文的 `tab:timing_distribution`）。
**人类思考占会话墙钟时间的 92.3%**，远超 LLM 生成（3.3%）和工具执行（4.8%）；大多数会话都很短——中位数是一个没有请求间间隔的单请求——但一条沉重的尾部，即那些被搁置数小时或数天的会话（会话 p99 elapsed ≈ 206h），累积了大部分的空闲时间。把每个间隔截断在一小时（与缓存相关的预算）会使人类占比降到 64.3%，而生成和工具则升至 14.5% 和 21.2%。在单个请求内部，human 项消失，而 **工具执行领先于生成，在 2,783h 的总响应时间中为 59.8% vs 41.0%**；平均一个请求端到端运行 4.3 min（中位数 38s，p90 6.4 min），而每个有效步骤，模型花费约 ~11.5s 在生成、约 ~16.8s 在工具上。
