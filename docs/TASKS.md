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
- [x] 更新固定证据采集工具，返回 `qixin_api` 数据。
- [x] 保留 `qcc_data_json` 参数名以兼容报告工具，内部数据改为“启信宝主源 + 企查查 MCP 补充”。
- [x] 更新报告摘要口径。
- [x] 更新 Agent 提示词，明确企查查 OpenAPI 已整体退出。
- [x] 更新 README 和技术文档到当前数据源策略。

## 启信宝白名单

`1.41`、`1.31`、`79.14`、`55.2`、`22.1`、`61.1`、`5.5`、`17.5`、`66.1`、`85.71`、`32.1`、`1.55`、`56.1`、`51.1`、`63.2`、`20.1`、`20.3`、`26.1`、`34.1`、`25.1`

## 待验证

- [ ] 在 Coze 环境配置 `QIXIN_APPKEY` 和 `QIXIN_SECRET_KEY`。
- [ ] 在 Coze 环境验证企业名称输入时优先命中启信宝 API `1.41`。
- [ ] 在 Coze 环境验证统一社会信用代码输入时优先命中启信宝 API `1.41`。
- [ ] 验证 `collect_enterprise_evidence` 返回 `qixin_api`、`qcc_mcp` 和兼容字段 `qcc_data_json`。
- [ ] 验证企查查 MCP 额度不足时不再尝试企查查 OpenAPI。
- [ ] 验证报告工具能复用 `qcc_data_json`，减少报告阶段重复 MCP 查询。
- [ ] 验证启信宝接口 `32.1` 的“地产行政处罚”在报告中不会被误写成通用行政处罚。
- [ ] 为 `qixin_openapi_client.py` 增加单元测试。

## 本地检查

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```

## 风险提示

- `qcc_data_json` 是兼容字段名，短期不建议改名，否则需要同步更新 Agent prompt、报告工具和 Coze 配置。
- 启信宝 `1.31` 模糊搜索目前主要用于固定采集，后续可考虑纳入主体消歧增强。
- 启信宝接口字段结构需要在真实 Coze 环境用生产凭据验证。
