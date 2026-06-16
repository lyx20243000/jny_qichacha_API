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
