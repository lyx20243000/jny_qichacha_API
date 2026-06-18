# 企业查询与评级 Agent

这是一个面向 Coze 部署环境的企业查询、企业评级和报告生成项目。项目基于 FastAPI、LangGraph/LangChain Agent、Coze 工具 SDK、启信宝 API、企查查 MCP、公开搜索和本地评分规则，对企业进行主体确认、信息收集、风险核验、四维评分和 PDF 报告输出。

重要说明：本项目以 Coze 部署环境为准，本地环境可能缺少 Coze 运行时、工作负载身份、数据库、对象存储、搜索和文档生成 SDK 等依赖，因此本地主要用于代码阅读、文档维护、静态检查和轻量语法检查。

## 当前数据策略

- 启信宝 API 是主结构化数据源。
- 企查查 MCP 保留为补充数据源，仅用于启信宝白名单未覆盖字段、缺失字段补查、核心风险核验或 deep 尽调。
- 当前结构化采集链路仅使用启信宝 API 和企查查 MCP。

启信宝 API 仅允许调用以下接口 ID：

`1.41`、`1.31`、`79.14`、`55.2`、`22.1`、`61.1`、`5.5`、`17.5`、`66.1`、`85.71`、`32.1`、`1.55`、`56.1`、`51.1`、`63.2`、`20.1`、`20.3`、`26.1`、`34.1`、`25.1`

## 项目目标

- 根据用户输入的企业名称或统一社会信用代码，默认通过 `generate_enterprise_report_single` 端到端生成报告；该工具内部先通过 `collect_enterprise_evidence` 完成主体确认和完整证据采集。
- 主体确认优先使用启信宝 API `1.41` 工商照面；未命中时回退到企查查 MCP 工商登记，再使用 Coze/公开搜索候选确认。
- 主体确认后按分层策略采集启信宝白名单接口数据：完整报告入口默认先跑 `standard` 核心采集，只在用户明确要求深度尽调、命中核心风险、关键字段缺失较多或诊断建议 `trigger_deep` 时自动升级到 `deep`。这里的“核心风险”优先指失信、被执行、严重违法、行政处罚、经营异常、限制高消费、税收违法、股权冻结等信号，不把股权出质、动产抵押这类常规融资字段单独视为 deep 触发条件。启信宝成功结果会写入本地 `.cache/qixin` 持久化缓存，减少重复分析时的耗时和额度消耗。
- 按行业、企业经营、财务、信用四个维度评分。
- 输出企业分析结论、数据可信度、财务缺失说明、重点风险和行动建议，并通过 Coze 文档服务生成 PDF 报告链接。

## 单次 LLM 生成

当前默认报告链路已撤销多轮/多维度 LLM，改回 `generate_enterprise_report_single`：

1. 完整采集：内部默认先用 `collection_mode=standard` 调用 `collect_enterprise_evidence`；只有命中显式深度请求、核心风险、字段缺口或 `trigger_deep` 诊断时，才自动重跑 `deep`。风险文本判断会排除“未查询到 / 无相关 / 暂无 / 0 条 / 0 个”这类非风险描述。
2. 单次 LLM：将完整采集结果按维度裁剪后压缩为一次输入，只调用一次 LLM 生成完整 `scoring_json`。
3. 报告生成：调用 `generate_enterprise_report` 计算加权分、兜底补全报告字段并生成 PDF。

当前流式兼容策略是：

- 外层 Agent / 对话入口默认保留流式能力，兼容 `/stream_run` 和 `/run`。
- 内部单轮评分 LLM 默认使用非流式 `invoke`，降低长文本评分阶段与 SSE 解析耦合导致的卡住风险。
- `/stream_run` 过程中如果遇到异常流式 chunk，会过滤 `reasoning_content` 一类非正文内容；若流式执行仍异常，会自动降级为一次非流式聚合执行，再通过 SSE 回传最终结果。
- 正常流式结束的 `StopAsyncIteration` 不再被误判为异常；只有真实流式异常才会触发 fallback。
- fallback 非流式执行即使再次失败，也会通过 SSE 返回最终 `final` 事件，避免前端一直停在“分析中”。
- 内部评分 LLM 调用前会打印实际生效的 `model / streaming / thinking / timeout / max_completion_tokens / payload_chars` 诊断日志，便于 Coze 部署环境排查。

并发维度和两阶段相关默认入口已从当前代码中删除；如需追溯，可查看历史提交。

当前维度裁剪口径是：

- 核心结构化事实宽保留：`qixin_api`、`qcc_mcp.basic/risk/finance/extended_risk`、`qcc_data_json`
- 重要补充中度裁剪：`qcc_mcp.operation`、`qcc_mcp.ip`、`triggered_mcp`
- 非核心文本强裁剪：`search_evidence`、`qcc_mcp.news`

当前上限摘要：

- `qixin_api`：普通接口常规最多 `1200` 字符，收紧 `900`；列表常规最多 `20` 项，收紧 `14` 项
- `qcc_mcp.basic/finance`：常规每项 `420` 字符，收紧 `320`；列表常规 `12` 项，收紧 `8` 项
- `qcc_mcp.risk`：常规每项 `360`，收紧 `280`；列表常规 `12` 项，收紧 `8` 项
- `qcc_mcp.extended_risk`：常规每项 `320`，收紧 `240`；列表常规 `12` 项，收紧 `8` 项
- `qcc_mcp.operation`：常规每项 `260`，收紧 `180`
- `qcc_mcp.ip`：常规每项 `220`，收紧 `160`
- `qcc_mcp.news`：常规每项 `180`，收紧 `120`
- `triggered_mcp`：常规最多 `3` 个 section，收紧 `2` 个；每项常规 `320`，收紧 `220`
- `search_evidence`：每组常规 `6` 条，收紧 `4` 条；`snippet` 常规 `180` / 收紧 `120`
- `qcc_data_json`：普通字段常规 `420` / 收紧 `280`，`history_risk` 常规 `260` / 收紧 `180`

超限时的收紧顺序是：先收 `search_evidence`，再收 `triggered_mcp`，再收 `qcc_mcp`，再收 `qcc_data_json`，再收 `qixin_api`，最后才轻收 `evidence_summary` 和 `collection_diagnostics`。

`collect_enterprise_evidence` 当前除原有 `qixin_api`、`qcc_mcp`、`triggered_mcp`、`qcc_data_json` 外，还会返回：

- `evidence_summary`：面向评分阶段的紧凑摘要，当前按 `subject_profile`、`official_structured_summary`、`official_search_summary`、`operation_signal_summary`、`finance_signal_summary`、`risk_signal_summary`、`search_signal_summary`、`field_gaps`、`conflict_flags`、`scoring_hints` 分层组织。
- `search_evidence`：公开搜索结构化结果，不再只是纯文本；每个分组都会带 `query`、`profile_name`、`search_type`、`summary`、`items`、`stats`，便于后续按权威度、官网命中、GSXT 命中和内容命中继续判断。
- `collection_diagnostics`：采集诊断摘要，汇总启信宝是否熔断/提前终止、MCP 是否自动补位、缺失字段数量、来源冲突数量和是否建议人工复核。
- `collection_diagnostics.search`：搜索侧诊断，包含分组、官方命中、高权威命中、正文命中、官网命中和 GSXT 命中。
- `collection_diagnostics.module_completeness` / `recommended_next_step`：用于提示当前是继续评分、触发 deep 还是建议人工复核。
- `qcc_data_json.field_sources`：关键字段最终取值来自哪个渠道。
- `qcc_data_json.source_conflicts`：多渠道都返回了值但内容不一致时的冲突摘要，便于报告解释和人工复核。

当前 `generate_enterprise_report` 除了建议继续传入 `qcc_data_json` 外，也建议一并传入 `collection_diagnostics_json`。这样报告阶段可以继续复用：

- `recommended_next_step`：把“继续评分 / 补充 deep / 人工复核”映射到 `action_recommendation.next_action`
- `review_reasons`：把采集缺口或冲突原因补进 `action_recommendation.key_risks`

## 关键入口

- Coze 项目配置：`.coze`
- HTTP/Agent 入口：`src/main.py`
- Agent 构建：`src/agents/agent.py`
- Agent 提示词配置：`config/agent_llm_config.json`
- 启信宝 API 客户端：`src/services/qixin_openapi_client.py`
- 企查查 MCP 客户端：`src/services/qcc_mcp_client.py`
- 固定证据采集工具：`src/tools/enterprise_evidence_tool.py`
- 主体消歧工具：`src/tools/enterprise_disambiguate_tool.py`
- 报告工具：`src/tools/report_tool.py`
- 默认单次报告工具：`src/tools/single_stage_report_tool.py`
- 单次 LLM 服务：`src/services/single_stage_llm_pipeline.py`
- 工具运行公共 helper：`src/tools/tool_runtime_helpers.py`

当前项目未发现独立的 Coze 工具 schema/manifest 配置文件；工具参数暴露以 LangChain `@tool` 装饰器和 Python 函数签名为准，再由 `src/agents/agent.py` 中 `create_agent(..., tools=[...])` 注册到 Agent。因此 `generate_enterprise_report` 新增 `collection_diagnostics_json` 后，不需要再额外同步一份 Coze 工具参数文件。

## 环境变量

启信宝 API：

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
QCC_MCP_TOOL_ALIASES={}
```

采集性能：

```bash
ENTERPRISE_COLLECTION_MODE=standard
EVIDENCE_ITEM_TIMEOUT_SECONDS=12
EVIDENCE_GROUP_TIMEOUT_SECONDS=35
EVIDENCE_FIELD_MAX_CHARS=2500
```

LLM 生成：

```bash
# 配置位于 config/agent_llm_config.json
# config 控制外层 Agent；single_stage_generation 控制默认单次 LLM。
```

外层 Agent 的 `sp` 仅保留工具路由、数据源边界、主体确认和禁止事项等短指令。具体评分规则由 `src/services/single_stage_llm_pipeline.py` 的单次 LLM prompt 和报告工具执行。

## 本地可做的检查

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```

不建议直接本地启动完整服务，普通本地环境通常缺少 Coze 平台能力。

## 文档

- 技术说明：`docs/TECHNICAL.md`
- 任务进度：`docs/TASKS.md`
## 当前默认报告流程

默认完整报告入口是 `generate_enterprise_report_single`。它会先以 `standard`
模式完成固定证据采集，必要时再自动升级到 `deep`，然后一次性调用 LLM 生成完整 `scoring_json`，最后调用
`generate_enterprise_report` 输出 PDF。
