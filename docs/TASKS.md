# 任务进度

## 当前目标

将项目数据源策略调整为：

- 启信宝 API 做主数据源。
- 企查查 MCP 做补充数据源。
- 企查查 OpenAPI 整体退出。

## 已完成

- [x] 新增启信宝 API 客户端 `src/services/qixin_openapi_client.py`。
- [x] 启信宝 API 客户端增加接口 ID 白名单，仅允许用户批准的接口。
- [x] 主体确认改为优先使用启信宝 API `1.41` 工商照面。
- [x] 固定采集改为主体确认后采集启信宝白名单接口。
- [x] 保留企查查 MCP 工具，作为缺失字段、核心风险核验和 deep 尽调补充数据源。
- [x] 从 Agent 工具列表移除企查查 OpenAPI 工具 `query_enterprise_detail_api` 和 `query_qcc_openapi`。
- [x] 删除旧企查查 OpenAPI 客户端 `src/services/qcc_openapi_client.py`。
- [x] 删除旧企查查 OpenAPI 工具 `src/tools/enterprise_api_tool.py`。
- [x] 更新主体消歧工具，改用启信宝 API 基础信息结果。
- [x] 主体确认增加企查查 MCP 回退链路，作为启信宝 API `1.41` 不可用时的备份。
- [x] 更新固定证据采集工具，返回 `qixin_api` 数据。
- [x] 保留 `qcc_data_json` 参数名以兼容报告工具，内部数据改为“启信宝主源 + 企查查 MCP 补充”。
- [x] `collect_enterprise_evidence` 已增加 `collection_diagnostics`，输出熔断、补位、缺失字段和来源冲突摘要。
- [x] `qcc_data_json` 已补充 `field_sources` 和 `source_conflicts`，便于报告复用、字段来源追踪和人工复核。
- [x] 内置公开搜索已升级为按场景 profile 的结构化返回，`search_evidence` 不再只保存纯文本。
- [x] `evidence_summary` 已重构为分层摘要结构，优先服务评分阶段而不是直接暴露大块原始证据。
- [x] `collection_diagnostics` 已补充搜索统计、模块完整度和建议下一步动作。
- [x] 更新报告摘要口径。
- [x] 更新 Agent 提示词，明确企查查 OpenAPI 已整体退出。
- [x] 更新 README 和技术文档到当前数据源策略。
- [x] 同步文档口径：`standard` 固定公开搜索 `industry/basic/finance/development`，`gsxt/gsxt_risk` 仅在 `deep` 模式固定带出。
- [x] 同步文档口径：`generate_enterprise_report` 在缺少 `qcc_data_json` 时不会自动回查企查查 MCP，只基于现有 `scoring_json` 和已传入数据生成报告。
- [x] `generate_enterprise_report` 已支持可选传入 `collection_diagnostics_json`，用于复用 `recommended_next_step` / `review_reasons` 补全报告动作建议。
- [x] 删除 CNBizAPI 兼容代码和相关文档口径，不再保留该备用链路。
- [x] 已确认当前 Coze 部署链路下，工具参数暴露直接跟随 LangChain `@tool` + 函数签名；未发现需要额外维护的工具 schema 配置文件。
- [x] 历史上曾实现两阶段和并发维度 LLM 链路；按最新要求已撤销默认多轮/多维度 LLM，并删除相关默认入口代码。
- [x] 抽取 `src/tools/tool_runtime_helpers.py`，用于当前单次工具内部复用采集工具和报告工具。
- [x] 已收敛 Agent 运行时默认入口前缀，并同步 `config/agent_llm_config.json`，避免配置 SP 与运行时前缀产生默认入口冲突。
- [x] 已压缩外层 Agent SP，将完整评分细则下沉到单次 LLM prompt；外层只保留工具路由、主体确认、数据源边界和禁止事项。
- [x] 已为 `tool_runtime_helpers.invoke_langchain_tool` 增加注释，明确它是工具内部编排直调 `.func` 的 helper，避免误认为要走 LangChain invoke 中间件链。
- [x] 已按最新要求撤销默认多轮/多维度 LLM，新增 `generate_enterprise_report_single`，默认 deep 采集全部数据后只调用一次 LLM 生成完整 `scoring_json`。
- [x] Agent 默认工具列表已移除 `generate_enterprise_report_parallel` 和 `generate_enterprise_report_two_stage`，避免 Coze 默认选到多轮链路。
- [x] `config/agent_llm_config.json` 已移除 `parallel_generation` / `two_stage_generation` 默认配置，改为 `single_stage_generation`。
- [x] 单次 LLM 超时已设置为 600 秒。

## 启信宝白名单

`1.41`、`1.31`、`79.14`、`55.2`、`22.1`、`61.1`、`5.5`、`17.5`、`66.1`、`85.71`、`32.1`、`1.55`、`56.1`、`51.1`、`63.2`、`20.1`、`20.3`、`26.1`、`34.1`、`25.1`

## 待验证

- [ ] 在 Coze 环境配置 `QIXIN_APPKEY` 和 `QIXIN_SECRET_KEY`。
- [ ] 在 Coze 环境验证企业名称输入时优先命中启信宝 API `1.41`。
- [ ] 在 Coze 环境验证统一社会信用代码输入时优先命中启信宝 API `1.41`。
- [x] `collect_enterprise_evidence` 已增加启信宝分层采集、不可用熔断和 standard 模式 MCP 自动补位逻辑。
- [ ] 在 Coze 环境验证企查查 MCP 额度不足时会直接跳过同类 MCP 补查，并转用已采集启信宝数据与公开搜索线索。
- [x] `qcc_data_json` 已补充 `field_sources` / `source_conflicts` 字段，便于报告复用、字段来源追踪和冲突提示。
- [x] 已补充 `tests/test_evidence_diagnostics.py`，覆盖采集诊断、字段来源和来源冲突逻辑。
- [ ] 在 Coze 环境验证 `standard` 模式默认不固定追加 `gsxt/gsxt_risk`，仅 `deep` 模式固定带出。
- [ ] 在 Coze 环境验证未传 `qcc_data_json` 时，报告阶段不会自动回查企查查 MCP。
- [ ] 验证启信宝接口 `32.1` 的“地产行政处罚”在报告中不会被误写成通用行政处罚。
- [ ] 为 `qixin_openapi_client.py` 增加单元测试。
- [ ] 在 Coze 环境验证 `generate_enterprise_report_single` 是否优先被 Agent 调用。
- [ ] 在 Coze 环境验证默认 deep 采集后仅发生一次 LLM scoring 调用，再进入 PDF 生成。
- [ ] 根据真实耗时调整 `single_stage_generation.max_input_chars`、`max_completion_tokens` 和 `timeout`。
- [x] 已将 `config/agent_llm_config.json` 外层 SP 从约 2 万字符压缩到约 1.7k 字符。

## 本地检查

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```

## 风险提示

- `qcc_data_json` 是兼容字段名，短期不建议改名，否则需要同步更新 Agent prompt、报告工具和 Coze 配置。
- `collection_diagnostics` 是诊断摘要，不是评分证据本身；Agent 应优先把它当作采集健康度和是否需要人工复核的提示。
- 报告阶段现在除了复用 `qcc_data_json`，也建议复用 `collection_diagnostics_json`；否则报告仍能生成，但会少一层基于采集健康度的动作建议兜底。
- `evidence_summary` 已从旧的平铺摘要改成分层摘要；后续如果继续改字段名，需要同步更新 Agent prompt、文档和可能依赖这些键的评分逻辑。
- 启信宝 `1.31` 模糊搜索目前主要用于固定采集，后续可考虑纳入主体消歧增强。
- `standard` 模式当前固定公开搜索 `industry/basic/finance/development`，`gsxt` 相关线索属于 `deep` 固定链路或 Agent 按需补查，不应在文档中写成默认必查。
- 报告阶段当前是“复用已采集数据”模式；如果 `collect_enterprise_evidence` 没有传出 `qcc_data_json`，报告会继续生成，但不会再自动补查 MCP。
- 启信宝接口字段结构需要在真实 Coze 环境用生产凭据验证。
- 单次 LLM 链路重新引入一次性长文本生成风险；上线后需要观察 `single_llm_scoring` 耗时、JSON 合法率和 `max_input_chars` 截断是否影响报告质量。

## Recent LLM Pipeline Update

- [x] Reverted the default report flow from multi-round/dimension LLM to one full-evidence LLM call.
- [x] Added `generate_enterprise_report_single` as the default complete report entry.
- [x] Default single-stage collection uses `collection_mode=deep` before the one LLM scoring call.
- [x] Removed parallel/two-stage report tools from the default Agent tool list.
- [ ] Validate in Coze that `generate_enterprise_report_single` is selected by default.
- [ ] Compare `evidence_collection`, `single_llm_scoring`, `pdf_report`, and total runtime.
