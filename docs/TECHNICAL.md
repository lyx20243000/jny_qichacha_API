# 技术说明

## 运行边界

项目面向 Coze 部署环境，不以普通本地机器为主要运行目标。普通本地环境通常缺少 `COZE_WORKSPACE_PATH`、`COZE_WORKLOAD_IDENTITY_API_KEY`、模型服务、搜索、Fetch、文档生成和对象存储等平台能力。

本地推荐做静态检查：

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```

## 核心数据流

1. 用户通过 `/run`、`/stream_run`、`/async_run` 或 OpenAI 兼容接口提交企业查询请求；其中 `/stream_run` 在长耗时执行期间会每 10 秒输出一次 `progress` SSE 事件，用于告知上游任务仍在进行中。
2. `src/main.py` 创建 Coze 运行上下文和 LangGraph run config。
3. `src/agents/agent.py` 根据 `config/agent_llm_config.json` 构建 Agent 和工具列表。
4. 完整企业分析默认调用 `generate_enterprise_report_parallel`。
5. `generate_enterprise_report_parallel` 内部调用 `collect_enterprise_evidence`，完成主体确认、启信宝白名单 API 固定采集、公开搜索、按模式决定是否追加国家企业信用信息公示系统线索、企查查 MCP 补缺和证据整理。
6. 采集完成后，代码按行业、企业经营、财务、信用/风险四个维度裁剪输入，降低单个 LLM 的上下文体积。
7. 维度 LLM 按 `industry`、`operation`、`finance`、`credit` 顺序错峰启动，默认每 3 秒启动一个；维度正文不向用户流式输出。
8. 等全部维度完成后，再调用 summary LLM 生成综合结论和行动建议；代码合并维度结果与汇总结果，保护四维评分、`red_line_data`、财务缺失字段和诊断字段，最后调用 `generate_enterprise_report` 输出 PDF。

## 数据源策略

当前策略是：

- 启信宝 API 做主数据源。
- 企查查 MCP 做补充数据源。
- 当前结构化采集链路仅包含启信宝 API 和企查查 MCP。

启信宝 API 只允许使用白名单接口：

`1.41`、`1.31`、`79.14`、`55.2`、`22.1`、`61.1`、`5.5`、`17.5`、`66.1`、`85.71`、`32.1`、`1.55`、`56.1`、`51.1`、`63.2`、`20.1`、`20.3`、`26.1`、`34.1`、`25.1`

白名单覆盖：主体确认、工商基础、模糊搜索、科技型企业、股权穿透、企业资质、购地信息、失信、被执行、限制高消费、案件串联、地产行政处罚、经营异常、严重违法、环保处罚、税务异常、欠税、重大税收违法、股权出质、股权冻结、动产抵押等维度。

白名单未覆盖的财务、知识产权、招投标、招聘、新闻舆情等维度，使用公开搜索和企查查 MCP 补缺；启信宝若返回账户未激活、额度不足、签名/鉴权失败等致命错误，会触发短时熔断，后续本轮直接跳过其余启信宝接口并优先使用 MCP/公开搜索。成功结果会同步写入进程内缓存和本地 `.cache/qixin` 缓存。仍缺失时必须标注“未获取/需复核”，不得编造。

## 启信宝 API 客户端

客户端文件：`src/services/qixin_openapi_client.py`

环境变量：

```bash
QIXIN_APPKEY=...
QIXIN_SECRET_KEY=...
QIXIN_AUTH_VERSION=2.0
QIXIN_CACHE_TTL_SECONDS=259200
QIXIN_PERSISTENT_CACHE_TTL_SECONDS=86400
QIXIN_CIRCUIT_BREAKER_SECONDS=600
QIXIN_API_CHECK_TIMEOUT_SECONDS=10
```

认证头：

- `Auth-Version`
- `appkey`
- `timestamp`
- `sign = md5(appkey + timestamp + secret_key)`

代码层有接口 ID 白名单，白名单以外的启信宝接口会被拒绝。

## Agent 工具

当前 Agent 暴露工具包括：

- `generate_enterprise_report_parallel`
- `generate_enterprise_report_two_stage`
- `collect_enterprise_evidence`
- `search_enterprise_candidates`
- `search_industry_info`
- `search_enterprise_basic`
- `search_enterprise_risk`
- `search_enterprise_finance`
- `search_enterprise_development`
- `search_gsxt_info`（Agent 可单独补查；`collect_enterprise_evidence` 仅在 `deep` 模式固定带出 gsxt/gsxt_risk 搜索线索）
- `fetch_enterprise_page`
- `qcc_get_basic_info`
- `qcc_get_finance_info`
- `qcc_get_risk_info`
- `qcc_get_ip_info`
- `qcc_get_operation_info`
- `qcc_get_news_info`
- `qcc_get_extended_risk_info`
- `generate_enterprise_report`

当前仓库未发现独立的 Coze 工具参数声明文件。结合 `.coze` 仅负责 entrypoint/build/run/deploy，和 `src/agents/agent.py` 直接以 `create_agent(..., tools=[...])` 注册工具的方式，可以确认当前部署链路下工具参数暴露直接跟随 LangChain `@tool` 装饰器和函数签名。因此 `generate_enterprise_report` 新增 `collection_diagnostics_json` 后，不需要额外再维护一份 Coze schema 配置。

## LLM 报告链路配置

配置文件：`config/agent_llm_config.json`

- `config`：外层 Agent 配置。当前外层 Agent 只负责工具选择和对话协调，已关闭 `thinking` 并降低 `max_completion_tokens`。
- `parallel_generation.dimension_llm`：默认维度 LLM 共享配置。
- `parallel_generation.summary_llm`：默认汇总 LLM 配置。
- `parallel_generation.dimension_launch_interval_seconds`：维度 LLM 启动间隔，生产默认 3 秒。
- `parallel_generation.max_input_chars` / `summary_max_input_chars`：控制维度和汇总阶段输入体积。
- `two_stage_generation.scoring_llm` / `report_llm`：备用两阶段链路配置，默认入口不使用。

默认并发维度链路定义在 `src/services/parallel_dimension_llm_pipeline.py`。它复用 `src/services/two_stage_llm_pipeline.py` 中的 `invoke_stage_json`、`compact_json` 和 LLM 配置读取能力。

备用两阶段链路提示词定义在 `src/services/two_stage_llm_pipeline.py`：

- `SCORING_CORE_SYSTEM_PROMPT`
- `REPORT_ENRICHMENT_SYSTEM_PROMPT`

默认并发链路的工具运行公共逻辑位于 `src/tools/tool_runtime_helpers.py`，供 `parallel_report_tool.py` 和 `two_stage_report_tool.py` 共同调用，避免并发工具依赖两阶段模块里的私有函数。`src/agents/agent.py` 只保留简短的默认入口兜底前缀，完整默认入口说明以 `config/agent_llm_config.json` 为准，降低运行时 SP 与配置 SP 冲突风险。

## 固定采集返回

`collect_enterprise_evidence` 返回：

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

`evidence_summary` 当前按以下层次组织，供 Agent 优先阅读：

- `subject_profile`
- `official_structured_summary`
- `official_search_summary`
- `operation_signal_summary`
- `finance_signal_summary`
- `risk_signal_summary`
- `search_signal_summary`
- `field_gaps`
- `conflict_flags`
- `scoring_hints`

`search_evidence` 当前是结构化搜索结果，而不是旧的纯文本摘要。每个搜索分组都会带：

- `query`
- `profile_name`
- `search_type`
- `summary`
- `items`
- `stats`

其中 `items` 会保留标题、站点、链接、摘要、发布时间、权威度等字段；`stats` 会汇总 `result_count`、`official_hits`、`high_auth_hits`、`content_hits`。

`qcc_data_json` 名称暂时保留为兼容字段，内部实际承载“启信宝 API 主数据源 + 企查查 MCP 补充数据源”的紧凑 JSON，供 `generate_enterprise_report` 复用。报告阶段默认只复用这里已传入的数据，不再主动发起新的 MCP 查询。当前已增加：

- `field_sources`：标记关键字段最终来自启信宝、企查查 MCP 还是触发补查。
- `source_conflicts`：当多个来源都返回值但内容不一致时，记录字段名和各来源预览，便于排查冲突和后续报告解释。

`collection_diagnostics` 是额外的采集诊断摘要，目前会汇总：

- 启信宝是否发生致命错误、是否提前终止、完成到哪个采集阶段、命中/缺失计数、是否命中持久化缓存。
- standard 模式下是否因启信宝不可用或关键字段缺失而自动提升企查查 MCP seed collection。
- 搜索侧分组、官方命中、高权威命中、正文命中、官网命中、GSXT 命中。
- `module_completeness`，即主体、风险、财务、经营、关联方等模块完整度。
- `qcc_data_json` 中字段来源分布、缺失字段数量、来源冲突数量。
- 是否建议人工复核、建议下一步动作（`continue_scoring` / `trigger_deep` / `human_review`），以及复核原因列表。

报告阶段当前也支持把 `collection_diagnostics` 作为紧凑 JSON 字符串通过 `collection_diagnostics_json` 传给 `generate_enterprise_report`。这样报告工具可在 LLM 未明确写出动作建议时，继续兜底补全：

- `action_recommendation.next_action`
- `action_recommendation.key_risks`
- 与 deep / 人工复核相关的合作建议

## 采集模式

- `quick`：主体确认、启信宝关键接口、少量公开搜索和必要风险核查。
- `standard`：默认模式，适合普通生产评估。
- `deep`：深度尽调，采集更多 KYB、历史风险、税务环保、资产负担、司法详情、知识产权和舆情。

性能相关环境变量：

```bash
ENTERPRISE_COLLECTION_MODE=standard
EVIDENCE_ITEM_TIMEOUT_SECONDS=12
EVIDENCE_GROUP_TIMEOUT_SECONDS=35
QIXIN_API_CHECK_TIMEOUT_SECONDS=10
EVIDENCE_FIELD_MAX_CHARS=2500
```

## 报告输出

`generate_enterprise_report` 必须传入 `enterprise_name` 和合法紧凑 JSON 字符串 `scoring_json`。建议同时传入 `qcc_data_json` 与 `collection_diagnostics_json`：

- `qcc_data_json`：复用固定结构化采集结果；未传入时，报告仍可生成，但不会在报告阶段自动回查 MCP。
- `collection_diagnostics_json`：复用 `recommended_next_step` / `review_reasons`，让报告动作建议继续贴近当前采集缺口和复核需求；未传入时，报告仍可生成，只是缺少这层采集诊断兜底。

PDF 报告由 Markdown 正文通过 Coze `DocumentGenerationClient.create_pdf_from_markdown` 生成。

报告必须包含：

- 企业基础信息
- 主体真实性核验
- 绿电合作适配度
- 履约能力分析
- 关联方风险
- KYB 专项风险
- 四维评分
- 综合评价
- 行动建议
- 重点关注风险
- 需补充资料

## MCP 额度处理

企查查 MCP 支持多 Key 轮换：

```bash
QCC_MCP_API_KEY=...
QCC_MCP_API_KEY02=...
QCC_MCP_API_KEY03=...
QCC_MCP_API_KEY04=...
QCC_MCP_API_KEY05=...
QCC_MCP_API_KEY06=...
```

当 MCP 返回 `code=300008`、积分余额不足或额度不足时，客户端会标记当前 Key 已耗尽并尝试下一个 Key。所有 Key 不可用时，后续 MCP 补查直接跳过，Agent 应转用公开搜索和已采集的启信宝数据。
## Current Default Report Pipeline

The default complete report tool is `generate_enterprise_report_parallel`.
Its execution flow is:

1. Call `collect_enterprise_evidence` for subject verification and fixed evidence collection.
2. Split the collected evidence into four dimension-specific payloads.
3. Start dimension LLM tasks in the order `industry`, `operation`, `finance`, `credit`, with `parallel_generation.dimension_launch_interval_seconds` controlling the launch gap. The production default is 3 seconds.
4. Do not stream dimension text to the user; wait for all dimension tasks to finish.
5. Run the summary LLM after all dimensions are complete.
6. Merge results with score fields protected, then call `generate_enterprise_report`.

`generate_enterprise_report_two_stage` is retained as a fallback/detailed path.
