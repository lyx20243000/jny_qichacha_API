# 技术说明

## 当前架构结论

当前项目已经从“外层 Agent 自由选工具”改成“固定企业分析主链路优先”。

参考的目标机制是：

- 固定 runner 负责主链路
- 外层 Agent 只暴露一个企业分析总入口
- 主体确认独立
- 证据采集固定
- 评分独立成一个阶段
- 报告阶段只消费上游结构化结果

本项目现在的真实实现也是这个方向，只是底层数据源仍然是本项目自己的：

- 启信宝 API 为主
- 企查查 MCP 为补
- web_search 为公开搜索补充

## 当前默认运行链路

### `/run`

`src/main.py` 中，`/run` 已经优先走固定 runner：

1. 解析用户输入
2. 判断是否使用 `should_use_fixed_enterprise_runner`
3. 命中后执行 `run_enterprise_analysis`
4. 内部固定执行：
   - `collect_enterprise_evidence`
   - `build_enterprise_scoring_json`
   - `generate_enterprise_report`

### `/stream_run`

`/stream_run` 在 agent 项目下如果命中企业分析请求，也会直接旁路外层 Agent，改走 fixed runner 的 SSE 包装流：

1. 周期性发送 `progress`
2. 后台执行 `run_enterprise_analysis`
3. 完成后发送 `final`

这样可以避免“工具执行完了，外层 Agent 还要再接一轮 LLM 收口”带来的额外等待。

### `/v1/chat/completions`

`/v1/chat/completions` 现在也按同样原则处理企业分析请求：

1. 先复用 `should_use_fixed_enterprise_runner` 做统一路由判断
2. 命中企业分析时，直接执行 `run_enterprise_analysis`
3. `stream=false` 时返回 OpenAI 兼容的单次 completion
4. `stream=true` 时把 fixed runner 结果包装成 OpenAI chunk SSE
5. 未命中企业分析时，才回退到原有 `OpenAIChatHandler`

这样 OpenAI 兼容入口不再是另一条独立企业分析链路。

### 飞书 / 钉钉

飞书和钉钉渠道的企业分析消息也已经收口：

1. 渠道层提取用户文本
2. 命中企业分析时直接调用 `run_enterprise_analysis_sync`
3. 非企业分析消息才回退到 `build_agent(...).invoke(...)`

这样渠道侧不会再因为额外的 Agent 收口而走出和 HTTP 入口不同的分析路径。

## Agent 工具面

当前默认 Agent 只暴露：

- `analyze_enterprise_report`

这样做的目的：

- 不让外层 LLM 自己决定分析顺序
- 不让外层 LLM 在工具之间搬运超大 `evidence_json`
- 避免采集/评分/报告链路被错误拆开

## 主体确认

主体确认由固定采集阶段统一处理，逻辑是：

1. 优先启信宝 `1.41`
2. 如果启信宝未确认，回退企查查 MCP
3. 如果仍未确认，再走公开搜索候选
4. 多候选则返回 `need_user_confirmation`
5. 主体未确认，不继续采集

## 固定证据采集

`src/tools/enterprise_evidence_tool.py` 仍保留本项目当前的数据采集实现，但已经作为固定主链路的一部分使用，而不是让外层 Agent 自己决定是否调用。

当前返回结构包括：

- `identity`
- `collection_progress`
- `collection_policy`
- `evidence_summary`
- `search_evidence`
- `qixin_api`
- `qcc_mcp`
- `triggered_mcp`
- `qcc_data_json`
- `collection_diagnostics`

说明：

- `qcc_data_json` 是兼容字段名
- 真实含义已经不是“企查查 OpenAPI 数据”
- 而是“启信宝主结构化结果 + 企查查 MCP 补充结果”的统一承载对象

另外，主体确认返回的 `identity` / `candidates` 已在固定采集出口做标准化清洗。当前对外统一字段是：

- `enterprise_name`
- `unified_social_credit_code`
- `region`
- `industry`
- `status`

fixed runner 和候选展示层不再依赖历史乱码 key；旧字段仅作为内部兼容来源保留。

## 独立评分阶段

新增：

- `src/tools/scoring_builder_tool.py`

职责：

1. 接收 `user_input + evidence_json`
2. 校验主体已确认
3. 构建单轮评分输入 payload
4. 调用一次 LLM 输出完整 `scoring_json`

注意：

- 评分阶段不再顺手做报告生成
- 评分阶段也不再负责再次采集

## 报告阶段

`src/tools/report_tool.py` 继续负责：

- 消费 `enterprise_name`
- 消费 `scoring_json`
- 消费 `qcc_data_json`
- 可选消费 `collection_diagnostics_json`
- 最终输出 PDF 报告

当前要求是：

- 优先复用上游结构化结果
- 不在报告阶段重新拉主数据

## 兼容保留

`src/tools/single_stage_report_tool.py` 仍保留 `generate_enterprise_report_single`，但它已经不再是默认架构，只是一个向后兼容入口，内部直接代理到固定 runner。

## 启信宝白名单接口

当前允许使用：

`1.41`
`1.31`
`79.14`
`55.2`
`22.1`
`61.1`
`5.5`
`17.5`
`66.1`
`85.71`
`32.1`
`1.55`
`56.1`
`51.1`
`63.2`
`20.1`
`20.3`
`26.1`
`34.1`
`25.1`

## 静态检查

当前环境下建议只做静态检查：

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```
