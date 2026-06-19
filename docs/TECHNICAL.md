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

`/stream_run` 仍走 Coze/Agent 的流式通道，但由于默认 Agent 工具面已收口为单工具，实际业务分析也会被引导到同一条固定主链路。

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
