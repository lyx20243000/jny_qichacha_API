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
4. 完整企业分析默认调用 `generate_enterprise_report_single`。
5. `generate_enterprise_report_single` 内部默认先以 `collection_mode=standard` 调用 `collect_enterprise_evidence`；只有命中显式深度请求、核心风险、关键字段缺失较多或诊断建议 `trigger_deep` 时，才自动重跑 `deep`。这里的核心风险优先看失信、被执行、处罚、经营异常、严重违法、限制高消费、税收违法、股权冻结等信号，不把股权出质、动产抵押这类常规融资字段直接作为 deep 触发条件。
6. 采集完成后，代码把完整证据压缩为一次输入，只调用一次 LLM 生成完整 `scoring_json`。
7. `generate_enterprise_report` 基于 `scoring_json`、`qcc_data_json` 和 `collection_diagnostics_json` 计算加权分、兜底补全报告字段并输出 PDF。

### 流式/非流式兼容策略

- 外层 Agent 模型配置默认 `streaming=true`，继续兼容 Coze 前端 `/stream_run` 的 SSE 链路，同时也兼容 `/run` 的一次性返回。
- 内部评分链路 `single_stage_generation.report_llm` 默认 `streaming=false`，即评分 JSON 生成阶段走稳态非流式调用，避免长文本评分与模型 SSE chunk 解析强耦合。
- `src/main.py` 的 `stream_sse` 对流式 chunk 做了归一化处理：会过滤 `reasoning_content` 这类非最终正文 chunk，尽量只向上游透传有效 `content`。
- `StopAsyncIteration` 作为正常流结束信号，已与真实异常分开处理，不再误触发 fallback。
- 如果流式执行过程中仍出现异常，服务会自动降级到一次 `run()` 非流式聚合执行，再通过 SSE 发送最终 `final` 结果；即使 fallback 自身失败，也会返回带错误信息的最终 `final` 事件，避免前端长时间停在“分析中”。

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

- `generate_enterprise_report_single`
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
- `single_stage_generation.report_llm`：默认单次 LLM 配置，当前 `timeout` 为 600 秒，且默认 `streaming=false`。
- `single_stage_generation.max_input_chars`：控制完整证据输入体积；实际入模前还会按维度裁剪，优先保留启信宝/QCC 核心结构化事实，强裁剪搜索和新闻等非核心文本。

当前 standard -> deep 自动升级逻辑还额外做了两层收紧：

- 风险文本识别会先排除 `未查询到`、`无相关`、`暂无`、`没有相关`、`无记录`、`未发现`、`0 条`、`0 个` 等安全描述，避免把“有 0 条记录”误判成风险。
- LLM 入参日志阶段与正式评分阶段共用同一份裁剪后 payload，避免重复构造大体积 evidence payload。
- 内部评分调用前会打印 `model / streaming / thinking / timeout / max_completion_tokens / payload_chars` 运行时诊断日志，用于确认 Coze 部署环境实际生效的模型参数。

`config/agent_llm_config.json` 中的外层 `sp` 已压缩为短路由提示，只负责默认入口、主体确认、数据源边界、采集模式和输出规则。完整评分细则不放在外层 Agent SP，避免每次工具选择消耗大量上下文窗口。

默认单次链路定义在 `src/services/single_stage_llm_pipeline.py`。它复用 `src/services/llm_json_pipeline.py` 中的 `invoke_stage_json` 和 `compact_json` 通用能力，配置只读取 `single_stage_generation`。

工具运行公共逻辑位于 `src/tools/tool_runtime_helpers.py`。`invoke_langchain_tool` 专用于工具内部编排，优先调用 LangChain tool 的 `.func`，避免内部工具再次进入 `.invoke` 链。`src/agents/agent.py` 只在配置缺失单次默认入口时补充兜底前缀，降低运行时 SP 与配置 SP 冲突风险。

### 单轮 LLM 入参维度限制

当前单轮 LLM 入模前会先按维度裁剪，而不是对整包 evidence 做统一粗截断。原则是：

- 核心结构化事实宽保留：`qixin_api`、`qcc_mcp.basic`、`qcc_mcp.risk`、`qcc_mcp.finance`、`qcc_mcp.extended_risk`、`qcc_data_json`
- 重要补充信息中度裁剪：`qcc_mcp.operation`、`qcc_mcp.ip`、`triggered_mcp`
- 非核心文本强裁剪：`search_evidence`、`qcc_mcp.news`

当前代码中的维度限制如下：

- `identity`
  保留 `status`、`enterprise_name`、`unified_social_credit_code`、`match_source`、`match_reason`、`confidence`；每字段最多 `160` 字符。
- `collection_policy`
  保留 `mode`、`available_modes`、`qixin_search_key`、`public_search_key`、`qcc_mcp_search_key`、`subject_confirmation_priority`、`triggered_collection`；普通字段最多 `120-220` 字符，`triggered_collection` 最多 `6` 项。
- `collection_diagnostics`
  子对象默认最多 `220` 字符；`field_source_summary` / `module_completeness` 每项 `120` 字符；`missing_or_unknown_fields` 最多 `12` 条，每条 `120` 字符；`review_reasons` 最多 `6` 条，每条 `120` 字符。
- `evidence_summary`
  `subject_profile` / `official_structured_summary` / `operation_signal_summary` / `finance_signal_summary` / `risk_signal_summary` 各 `600` 字符；`official_search_summary` / `search_signal_summary` 各 `500` 字符；`field_gaps` / `conflict_flags` / `scoring_hints` 最多 `8` 条，每条 `140` 字符。
- `qixin_api`
  `_meta` 最多 `240` 字符，`_fatal_error` 最多 `220` 字符；普通接口结果常规最多 `1200` 字符、收紧时 `900` 字符；列表常规最多 `20` 项、收紧时 `14` 项。
- `qcc_mcp.basic`
  常规每项 `420` 字符，收紧每项 `320` 字符；列表常规最多 `12` 项，收紧最多 `8` 项。
- `qcc_mcp.finance`
  常规每项 `420` 字符，收紧每项 `320` 字符；列表常规最多 `12` 项，收紧最多 `8` 项。
- `qcc_mcp.risk`
  常规每项 `360` 字符，收紧每项 `280` 字符；列表常规最多 `12` 项，收紧最多 `8` 项。
- `qcc_mcp.extended_risk`
  常规每项 `320` 字符，收紧每项 `240` 字符；列表常规最多 `12` 项，收紧最多 `8` 项。
- `qcc_mcp.ip`
  常规每项 `220` 字符，收紧每项 `160` 字符；常规最多 `10` 项，收紧最多 `8` 项。
- `qcc_mcp.operation`
  常规每项 `260` 字符，收紧每项 `180` 字符；常规最多 `10` 项，收紧最多 `8` 项。
- `qcc_mcp.news`
  常规每项 `180` 字符，收紧每项 `120` 字符；常规最多 `8` 项，收紧最多 `5` 项。
- `triggered_mcp`
  常规最多保留 `3` 个 section，收紧最多 `2` 个；常规每项 `320` 字符、收紧每项 `220` 字符；列表常规最多 `10` 项，收紧最多 `6` 项。
- `search_evidence`
  每组保留 `query`、`profile_name`、`search_type`、`summary`、`items`、`stats`；常规每组最多 `6` 条结果、收紧最多 `4` 条；`title` 最多 `120` 字符，`site_name` `60`，`publish_time` `40`，`snippet` 常规 `180` / 收紧 `120`，`summary` 常规 `240` / 收紧 `180`，`stats` `80`。
- `qcc_data_json`
  优先保留 `registration`、`company_profile`、`shareholder`、`actual_controller`、`listing_info`、`key_personnel`、`financial`、`investment`、`dishonest`、`admin_penalty`、`business_exception`、`serious_violation`、`high_consumption`、`risk_scan`、`case_filing`、`credit_eval`、`executed_person`、`judicial_documents`、`court_announcement`、`final_case`、`environmental_penalty`、`tax_abnormal`、`tax_arrears`、`tax_violation`、`equity_pledge`、`equity_freeze`、`chattel_mortgage`、`land_mortgage`、`history_risk`、`patent`、`trademark`、`software_copyright`、`bidding`、`qualifications`、`honor`、`recruitment`、`administrative_license`、`taxpayer_qualification`、`product_check`、`state_owned_land_transfer`、`news_sentiment`、`field_sources`、`source_conflicts`；普通字段常规 `420` / 收紧 `280`，`history_risk` 常规 `260` / 收紧 `180`，`field_sources` `80`，`source_conflicts` 常规 `140` / 收紧 `100`；常规最多 `12` 项，收紧最多 `8` 项。

总量收紧顺序如下：

1. 先收 `search_evidence`
2. 再收 `triggered_mcp`
3. 再收 `qcc_mcp`
4. 再收 `qcc_data_json`
5. 再收 `qixin_api`
6. 最后才轻收 `evidence_summary` 和 `collection_diagnostics`

也就是说，启信宝 / QCC 的核心结构化事实是最后才动的，优先牺牲搜索和新闻等非核心文本。

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
- `standard`：默认模式，适合普通生产评估；完整报告入口先跑这一层核心采集。
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

The default complete report tool is `generate_enterprise_report_single`.
Its execution flow is:

1. Call `collect_enterprise_evidence` with `collection_mode=standard` for subject verification and core fixed evidence collection.
2. Re-run `collect_enterprise_evidence` with `collection_mode=deep` only when explicit deep triggers are detected.
3. Compress the fixed evidence payload into one bounded LLM input with dimension-aware trimming.
4. Call one LLM once to produce the complete `scoring_json`.
5. Call `generate_enterprise_report` to calculate scores, apply report fallbacks, and generate the PDF.

Parallel dimension and two-stage code is kept for traceability, but those tools are no longer registered in the default Agent tool list.
