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
- 主体确认后按分层策略采集启信宝白名单接口数据：完整报告入口固定使用 `deep` 全量采集；`standard` 仅用于单独调试或轻量采集工具调用。启信宝成功结果会写入本地 `.cache/qixin` 持久化缓存，减少重复分析时的耗时和额度消耗。
- 按行业、企业经营、财务、信用四个维度评分。
- 输出企业分析结论、数据可信度、财务缺失说明、重点风险和行动建议，并通过 Coze 文档服务生成 PDF 报告链接。

## 单次 LLM 生成

当前默认报告链路已撤销多轮/多维度 LLM，改回 `generate_enterprise_report_single`：

1. 完整采集：内部强制用 `collection_mode=deep` 调用 `collect_enterprise_evidence`，即使外层误传 `standard` 或 `quick`，完整报告仍按全量采集执行。
2. 单次 LLM：将完整采集结果压缩为一次输入，只调用一次 LLM 生成完整 `scoring_json`。
3. 报告生成：调用 `generate_enterprise_report` 计算加权分、兜底补全报告字段并生成 PDF。

并发维度和两阶段相关默认入口已从当前代码中删除；如需追溯，可查看历史提交。

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

默认完整报告入口是 `generate_enterprise_report_single`。它会强制以 `deep`
模式完成固定证据采集，再一次性调用 LLM 生成完整 `scoring_json`，最后调用
`generate_enterprise_report` 输出 PDF。
