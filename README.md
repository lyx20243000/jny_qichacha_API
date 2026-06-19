# 企业查询与评分 Agent

这是一个面向 Coze 部署环境的企业分析项目。当前版本已经按固定编排主链路收口，核心目标是：

- 先确认主体
- 再固定采集证据
- 再单次构建 `scoring_json`
- 最后生成 PDF 报告

项目的数据源策略是：

- 启信宝 API：主结构化数据源
- 企查查 MCP：补充数据源
- 内置 web_search：公开搜索补充
- 不再使用企查查 OpenAPI
- 不接入飞书、钉钉

## 当前默认流程

当前默认流程已经对齐参考项目的机制，`/run` 和命中企业分析的 `/stream_run` 都优先走固定主链路：

1. `analyze_enterprise_report`
2. `collect_enterprise_evidence`
3. `build_enterprise_scoring_json`
4. `generate_enterprise_report`

其中：

- `src/services/enterprise_analysis_runner.py` 负责固定编排
- `src/tools/enterprise_analysis_tool.py` 是外层唯一默认企业分析入口
- `src/tools/scoring_builder_tool.py` 是独立评分阶段
- `src/tools/report_tool.py` 只负责消费上游结构化结果生成报告

## 主体确认策略

主体确认统一走固定逻辑：

1. 优先启信宝 `1.41`
2. 不足时回退企查查 MCP 主体确认
3. 仍不够时走公开搜索候选
4. 如果有多个候选，直接返回 `need_user_confirmation`
5. 未确认主体前，不继续采集

## 证据采集策略

`collect_enterprise_evidence` 负责固定采集，当前仍使用本项目既有数据源实现，但调度逻辑已经按固定流水线组织。

采集结果会输出：

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

`qcc_data_json` 名称暂时保留，只是为了兼容现有报告工具；内部承载的已经是“启信宝主源 + 企查查 MCP 补充”的统一结构化结果。

## Agent 暴露面

当前默认 Agent 工具面已收口，不再把采集、搜索、MCP、报告工具全部暴露给外层 LLM。

默认只暴露：

- `analyze_enterprise_report`

这样做的目的，是避免外层 Agent 自己搬运大 `evidence_json`，也避免它自由决定分析步骤。

## 兼容入口

`generate_enterprise_report_single` 还保留着，但现在只是一个向后兼容包装层，内部直接转到固定 runner，不再是独立主架构。

## 关键文件

- `src/main.py`
- `src/agents/agent.py`
- `src/services/enterprise_analysis_runner.py`
- `src/tools/enterprise_analysis_tool.py`
- `src/tools/enterprise_evidence_tool.py`
- `src/tools/scoring_builder_tool.py`
- `src/tools/report_tool.py`
- `src/services/qixin_openapi_client.py`
- `src/services/qcc_mcp_client.py`

## 环境变量

启信宝：

```bash
QIXIN_APPKEY=...
QIXIN_SECRET_KEY=...
QIXIN_AUTH_VERSION=2.0
QIXIN_CACHE_TTL_SECONDS=259200
QIXIN_PERSISTENT_CACHE_TTL_SECONDS=86400
QIXIN_CIRCUIT_BREAKER_SECONDS=600
QIXIN_API_CHECK_TIMEOUT_SECONDS=10
```

企查查 MCP：

```bash
QCC_MCP_API_KEY=...
QCC_MCP_API_KEY02=...
QCC_MCP_API_KEY03=...
QCC_MCP_API_KEY04=...
QCC_MCP_API_KEY05=...
QCC_MCP_API_KEY06=...
QCC_MCP_TIMEOUT_SECONDS=20
QCC_MCP_CACHE_TTL_SECONDS=3600
```

## 本地建议

这个项目以 Coze 环境为准，本地主要做静态检查，不建议把本地运行结果当成生产链路结论。

可做的本地检查：

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```
