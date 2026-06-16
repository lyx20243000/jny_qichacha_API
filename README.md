# 企业查询与评级 Agent

这是一个面向 Coze 部署环境的企业查询、企业评级和报告生成项目。项目基于 FastAPI、LangGraph/LangChain Agent、Coze 工具 SDK、启信宝 API、企查查 MCP、公开搜索和本地评分规则，对企业进行主体确认、信息收集、风险核验、四维评分和 PDF 报告输出。

重要说明：本项目以 Coze 部署环境为准，本地环境可能缺少 Coze 运行时、工作负载身份、数据库、对象存储、搜索和文档生成 SDK 等依赖，因此本地主要用于代码阅读、文档维护、静态检查和轻量语法检查。

## 当前数据策略

- 启信宝 API 是主结构化数据源。
- 企查查 MCP 保留为补充数据源，仅用于启信宝白名单未覆盖字段、缺失字段补查、核心风险核验或 deep 尽调。
- 企查查 OpenAPI 已整体退出，相关客户端和 Agent 工具已移除，不再作为兜底渠道。
- CNBizAPI 客户端保留兼容，但默认固定采集链路不依赖 CNBizAPI，也不参与主体确认。

启信宝 API 仅允许调用以下接口 ID：

`1.41`、`1.31`、`79.14`、`55.2`、`22.1`、`61.1`、`5.5`、`17.5`、`66.1`、`85.71`、`32.1`、`1.55`、`56.1`、`51.1`、`63.2`、`20.1`、`20.3`、`26.1`、`34.1`、`25.1`

## 项目目标

- 根据用户输入的企业名称或统一社会信用代码，先通过 `collect_enterprise_evidence` 完成主体确认和固定证据采集。
- 主体确认优先使用启信宝 API `1.41` 工商照面；未命中时回退到企查查 MCP 工商登记，再使用 Coze/公开搜索候选确认。
- 主体确认后按分层策略采集启信宝白名单接口数据：standard 默认先查主体、核心风险和关键资质，deep 再扩展到资产负担、土地和案件串联；同时调用公开搜索、国家企业信用信息公示系统搜索，并在启信宝不可用或关键字段缺失时自动提升企查查 MCP 补位。启信宝成功结果会写入本地 `.cache/qixin` 持久化缓存，减少重复分析时的耗时和额度消耗。
- 按行业、企业经营、财务、信用四个维度评分。
- 输出企业分析结论、数据可信度、财务缺失说明、重点风险和行动建议，并通过 Coze 文档服务生成 PDF 报告链接。

`collect_enterprise_evidence` 当前除原有 `qixin_api`、`qcc_mcp`、`triggered_mcp`、`qcc_data_json` 外，还会返回：

- `collection_diagnostics`：采集诊断摘要，汇总启信宝是否熔断/提前终止、MCP 是否自动补位、缺失字段数量、来源冲突数量和是否建议人工复核。
- `qcc_data_json.field_sources`：关键字段最终取值来自哪个渠道。
- `qcc_data_json.source_conflicts`：多渠道都返回了值但内容不一致时的冲突摘要，便于报告解释和人工复核。

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

## 环境变量

启信宝 API：

```bash
QIXIN_APPKEY=...
QIXIN_SECRET_KEY=...
QIXIN_AUTH_VERSION=2.0
QIXIN_CACHE_TTL_SECONDS=259200
QIXIN_API_CHECK_TIMEOUT_SECONDS=10
```

企查查 MCP：

```bash
QCC_MCP_API_KEY=...
QCC_MCP_API_KEY02=...
QCC_MCP_API_KEY03=...
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
