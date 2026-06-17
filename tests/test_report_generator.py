import json
import sys
import types


class _FakeDocumentGenerationClient:
    last_title = ""
    last_markdown = ""

    def create_pdf_from_markdown(self, markdown_content, title):
        type(self).last_markdown = markdown_content
        type(self).last_title = title
        return "https://example.com/report.pdf"


sdk_stub = types.ModuleType("coze_coding_dev_sdk")
sdk_stub.DocumentGenerationClient = _FakeDocumentGenerationClient
sys.modules.setdefault("coze_coding_dev_sdk", sdk_stub)

storage_stub = types.ModuleType("storage.s3.s3_storage")
storage_stub.S3SyncStorage = object
sys.modules.setdefault("storage.s3.s3_storage", storage_stub)

for _mod_name in ("langchain", "langchain.tools"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))

_scoring_stub = types.ModuleType("services.scoring_engine")
_scoring_stub.evaluate = lambda *a, **kw: {}
sys.modules.setdefault("services.scoring_engine", _scoring_stub)

_qcc_client_stub = types.ModuleType("services.qcc_mcp_client")
for _fn in (
    "is_qcc_mcp_available", "query_company_registration", "query_shareholder_info",
    "query_dishonest_info", "query_administrative_penalty", "query_business_exception",
    "query_high_consumption_restriction", "query_patent_info", "query_credit_evaluation",
    "query_qualifications", "query_external_investments", "query_financial_data",
):
    if not callable(getattr(_qcc_client_stub, _fn, None)):
        setattr(_qcc_client_stub, _fn, lambda *a, **kw: "")
sys.modules.setdefault("services.qcc_mcp_client", _qcc_client_stub)

import services.report_generator as report_generator

from services.report_generator import _build_pdf_report_title, generate_markdown_report, generate_pdf_report

sys.path.insert(0, "src")
import tools.report_tool as report_tool
from tools.report_tool import _parse_qcc_data_json, _qcc_data_to_summary


def test_build_pdf_report_title_uses_company_name():
    assert _build_pdf_report_title("久芳农业科技发展（商丘）有限公司") == "久芳农业科技发展（商丘）有限公司 分析报告"


def test_build_pdf_report_title_removes_invalid_filename_chars():
    assert _build_pdf_report_title('江苏/南通:聚力"船舶"?工程有限公司') == "江苏 南通 聚力 船舶 工程有限公司 分析报告"


def test_generate_pdf_report_passes_chinese_title_to_coze_client(monkeypatch):
    monkeypatch.setattr(
        report_generator,
        "generate_markdown_report",
        lambda *args, **kwargs: "# markdown",
    )

    url = generate_pdf_report(
        enterprise_name="瀚博半导体（上海）股份有限公司",
        evaluation_result={
            "weighted_total_score": 88,
            "final_grade": "B",
            "dimensions": {},
        },
        scoring_result={},
        summary="综合评价",
    )

    assert url == "https://example.com/report.pdf"
    assert _FakeDocumentGenerationClient.last_title == "瀚博半导体（上海）股份有限公司 分析报告"
    assert "enterprise_report_" not in _FakeDocumentGenerationClient.last_title


def test_generate_pdf_report_prefers_profile_company_name_for_title(monkeypatch):
    monkeypatch.setattr(
        report_generator,
        "generate_markdown_report",
        lambda *args, **kwargs: "# markdown",
    )

    generate_pdf_report(
        enterprise_name="完整分析报告PDF",
        evaluation_result={
            "weighted_total_score": 88,
            "final_grade": "B",
            "dimensions": {},
        },
        scoring_result={
            "enterprise_profile": {
                "企业名称": "瀚博半导体（上海）股份有限公司",
            }
        },
        summary="综合评价",
    )

    assert _FakeDocumentGenerationClient.last_title == "瀚博半导体（上海）股份有限公司 分析报告"


def test_markdown_report_hides_data_source_summary():
    markdown = generate_markdown_report(
        enterprise_name="瀚博半导体（上海）股份有限公司",
        evaluation_result={
            "weighted_total_score": 88,
            "final_grade": "B",
            "weights": {"industry": 30, "operation": 30, "finance": 25, "credit": 15},
            "triggered_red_lines": [],
            "dimensions": {
                "industry": {"weighted_score": 20, "total_score": 20, "max_score": 32, "items": []},
                "operation": {"weighted_score": 20, "total_score": 20, "max_score": 27, "items": []},
                "finance": {"weighted_score": 20, "total_score": 20, "max_score": 35, "items": []},
                "credit": {"weighted_score": 8, "total_score": 8, "max_score": 10, "items": []},
            },
        },
        scoring_result={
            "enterprise_profile": {"企业名称": "瀚博半导体（上海）股份有限公司"},
            "data_source_summary": {"official_or_structured": "内部数据源说明"},
        },
        summary="综合评价",
    )

    assert "数据源分层说明" not in markdown
    assert "内部数据源说明" not in markdown


def test_score_item_score_is_inline_with_heading():
    markdown = generate_markdown_report(
        enterprise_name="瀚博半导体（上海）股份有限公司",
        evaluation_result={
            "weighted_total_score": 88,
            "final_grade": "B",
            "weights": {"industry": 30, "operation": 30, "finance": 25, "credit": 15},
            "triggered_red_lines": [],
            "dimensions": {
                "industry": {
                    "weighted_score": 20,
                    "total_score": 4,
                    "max_score": 5,
                    "items": [
                        {
                            "name": "行业先进性",
                            "score": 4,
                            "max_score": 5,
                            "reliability": "green",
                            "basis": "行业具备技术先进性。",
                        }
                    ],
                },
                "operation": {"weighted_score": 0, "total_score": 0, "max_score": 27, "items": []},
                "finance": {"weighted_score": 0, "total_score": 0, "max_score": 35, "items": []},
                "credit": {"weighted_score": 0, "total_score": 0, "max_score": 10, "items": []},
            },
        },
        scoring_result={"enterprise_profile": {"企业名称": "瀚博半导体（上海）股份有限公司"}},
        summary="综合评价",
    )

    assert "### 行业先进性（得分 4/5，可信度" in markdown
    assert "**得分：4 / 5**" not in markdown


def test_parse_qcc_data_json_with_field_sources_and_conflicts():
    qcc_data_json = json.dumps({
        "provider": "qixin_primary_qcc_mcp_fallback",
        "field_sources": {"registration": "qixin_api_1_41", "dishonest": "qcc_mcp_risk.dishonest"},
        "source_conflicts": [
            {
                "field": "dishonest",
                "sources": [
                    {"source": "qixin_api_5_5", "preview": "启信宝失信记录"},
                    {"source": "qcc_mcp_risk.dishonest", "preview": "MCP失信记录"},
                ],
            }
        ],
        "registration": "工商登记数据",
        "dishonest": "失信记录数据",
    }, ensure_ascii=False)
    parsed = _parse_qcc_data_json(qcc_data_json)
    assert "field_sources" in parsed
    assert "source_conflicts" in parsed
    assert parsed["registration"] == "工商登记数据"
    assert len(parsed["source_conflicts"]) == 1


def test_qcc_data_to_summary_with_field_sources_and_conflicts():
    qcc_data = {
        "provider": "qixin_primary_qcc_mcp_fallback",
        "field_sources": {"registration": "qixin_api_1_41"},
        "source_conflicts": [],
        "registration": "工商登记数据内容",
        "dishonest": "未查询到相关记录",
    }
    summary = _qcc_data_to_summary(qcc_data)
    assert "启信宝" in summary or "结构化数据" in summary
    assert "工商登记信息" in summary


def test_generate_enterprise_report_enriches_supporting_sections(monkeypatch):
    captured = {}

    def _fake_evaluate(scoring_result):
        return {
            "final_grade": "B",
            "weighted_total_score": 82,
            "triggered_red_lines": [],
            "dimensions": {
                "industry": {"total_score": 0, "max_score": 32, "items": []},
                "operation": {"total_score": 0, "max_score": 27, "items": []},
                "finance": {"total_score": 0, "max_score": 35, "items": []},
                "credit": {"total_score": 0, "max_score": 10, "items": []},
            },
        }

    def _fake_generate_pdf_report(enterprise_name, evaluation_result, scoring_result, summary, qcc_data_summary=""):
        captured["enterprise_name"] = enterprise_name
        captured["scoring_result"] = scoring_result
        captured["summary"] = summary
        captured["qcc_data_summary"] = qcc_data_summary
        return "https://example.com/report.pdf"

    monkeypatch.setattr(report_tool, "evaluate", _fake_evaluate)
    monkeypatch.setattr(report_tool, "generate_pdf_report", _fake_generate_pdf_report)

    scoring_json = json.dumps({"overall_summary": "综合评价"}, ensure_ascii=False)
    qcc_data_json = json.dumps(
        {
            "provider": "qixin_primary_qcc_mcp_fallback",
            "field_sources": {
                "registration": "qixin_api_1_41",
                "shareholder": "qcc_mcp_basic.shareholder",
            },
            "source_conflicts": [
                {
                    "field": "dishonest",
                    "sources": [
                        {"source": "qixin_api_5_5", "preview": "启信宝失信记录"},
                        {"source": "qcc_mcp_risk.dishonest", "preview": "MCP失信记录"},
                    ],
                }
            ],
            "registration": {
                "企业名称": "测试企业",
                "统一社会信用代码": "913100000000000000",
                "登记状态": "存续",
            },
            "financial": "未查询到相关记录。",
            "dishonest": "未查询到相关记录。",
            "business_exception": "未查询到相关记录。",
            "serious_violation": "未查询到相关记录。",
            "high_consumption": "未查询到相关记录。",
        },
        ensure_ascii=False,
    )

    result = report_tool.generate_enterprise_report(
        enterprise_name="测试企业",
        scoring_json=scoring_json,
        qcc_data_json=qcc_data_json,
    )

    enriched = captured["scoring_result"]
    assert enriched["subject_verification"]["核验结论"] == "主体一致"
    assert enriched["subject_verification"]["登记状态"] == "存续"
    assert "年营收" in enriched["missing_financial_fields"]
    assert any("财务透明度不足" in item for item in enriched["financial_assessment_notes"])
    assert "启信宝API" in enriched["data_source_summary"]["official_or_structured"]
    assert any("dishonest" in item for item in enriched["action_recommendation"]["key_risks"])
    assert enriched["action_recommendation"]["next_action"] == "进入人工复核"
    assert captured["enterprise_name"] == "测试企业"
    assert result.endswith("(https://example.com/report.pdf)")


def test_generate_enterprise_report_uses_collection_diagnostics_next_step(monkeypatch):
    captured = {}

    def _fake_evaluate(scoring_result):
        return {
            "final_grade": "B",
            "weighted_total_score": 80,
            "triggered_red_lines": [],
            "dimensions": {
                "industry": {"total_score": 0, "max_score": 32, "items": []},
                "operation": {"total_score": 0, "max_score": 27, "items": []},
                "finance": {"total_score": 0, "max_score": 35, "items": []},
                "credit": {"total_score": 0, "max_score": 10, "items": []},
            },
        }

    def _fake_generate_pdf_report(enterprise_name, evaluation_result, scoring_result, summary, qcc_data_summary=""):
        captured["scoring_result"] = scoring_result
        return "https://example.com/report.pdf"

    monkeypatch.setattr(report_tool, "evaluate", _fake_evaluate)
    monkeypatch.setattr(report_tool, "generate_pdf_report", _fake_generate_pdf_report)

    result = report_tool.generate_enterprise_report(
        enterprise_name="测试企业",
        scoring_json=json.dumps({"overall_summary": "综合评价"}, ensure_ascii=False),
        collection_diagnostics_json=json.dumps(
            {
                "recommended_next_step": "trigger_deep",
                "review_reasons": ["缺失字段>=6", "高权威搜索命中不足"],
            },
            ensure_ascii=False,
        ),
    )

    enriched = captured["scoring_result"]
    assert enriched["action_recommendation"]["next_action"] == "补充深度采集后再评估"
    assert "deep 模式采集" in enriched["action_recommendation"]["cooperation_advice"]
    assert any("采集诊断提示需重点关注" in item for item in enriched["action_recommendation"]["key_risks"])
    assert result.endswith("(https://example.com/report.pdf)")
