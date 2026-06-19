# 任务进度

## 当前目标

把当前项目的流程机制对齐到参考项目：

- 固定 runner 主链路
- 单总入口工具
- 主体确认独立
- 固定证据采集
- 独立 scoring 阶段
- 报告阶段只消费上游结构化结果

同时保持：

- 启信宝 API 为主数据源
- 企查查 MCP 为补充数据源
- 不使用企查查 OpenAPI
- 不接入飞书、钉钉

## 已完成

- [x] 新增固定主链路 `src/services/enterprise_analysis_runner.py`
- [x] 新增总入口工具 `src/tools/enterprise_analysis_tool.py`
- [x] 新增独立评分工具 `src/tools/scoring_builder_tool.py`
- [x] 将主链路固定为 `collect -> scoring -> report`
- [x] `generate_enterprise_report_single` 改为向后兼容包装层
- [x] 默认 Agent 工具面收口为 `analyze_enterprise_report`
- [x] 修复 `src/main.py` 中 fixed runner 分支缺少 import 的确定性问题
- [x] README、TECHNICAL、TASKS 文档同步到当前真实架构

## 待验证

这些项需要在 Coze 环境里验证：

- [ ] `/run` 是否稳定优先走 fixed runner
- [ ] `/stream_run` 下外层 Agent 是否稳定只走 `analyze_enterprise_report`
- [ ] 多候选主体时，是否会立即停止后续采集并要求用户确认
- [ ] `build_enterprise_scoring_json` 是否稳定输出合法 `scoring_json`
- [ ] `generate_enterprise_report` 是否稳定复用上游 `qcc_data_json`
- [ ] 启信宝不可用时，企查查 MCP 补位链路是否符合预期
- [ ] 深度模式和标准模式的采集结果是否仍符合当前业务要求

## 本地检查

```bash
python -m compileall -q src
python -m json.tool config/agent_llm_config.json
git diff --check
```
