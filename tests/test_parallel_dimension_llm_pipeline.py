import sys
import types


for _mod_name in ("langchain", "langchain.tools"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))


sys.path.insert(0, "src")

from services.parallel_dimension_llm_pipeline import (
    build_dimension_payload,
    get_parallel_runtime_config,
    merge_parallel_payload,
)


def test_build_dimension_payload_keeps_only_relevant_groups_and_fields():
    evidence = {
        "identity": {"enterprise_name": "测试企业"},
        "evidence_summary": {"subject_profile": "主体", "finance_signal_summary": "财务"},
        "collection_diagnostics": {"recommended_next_step": "continue_scoring"},
        "qcc_data_json": {
            "registration": "工商信息",
            "financial": "财务信息",
            "dishonest": "风险信息",
            "unrelated": "不应进入财务维度",
        },
        "search_evidence": {
            "finance": {"items": [{"title": "财务线索", "snippet": "年产值信息"}], "stats": {"result_count": 1}},
            "risk": {"items": [{"title": "风险线索", "snippet": "处罚"}], "stats": {"result_count": 1}},
        },
    }

    payload = build_dimension_payload(evidence, "finance", max_input_chars=10000)

    assert "financial" in payload["qcc_data_json"]
    assert "dishonest" not in payload["qcc_data_json"]
    assert "unrelated" not in payload["qcc_data_json"]
    assert "finance" in payload["search_evidence"]
    assert "risk" not in payload["search_evidence"]


def test_merge_parallel_payload_protects_dimension_scores():
    dimensions = {
        "industry": {"industry": {"industry_advancement": {"score": 4, "reliability": "green", "basis": "行业稳定"}}},
        "operation": {"operation": {"survival_years": {"score": 5, "reliability": "green", "basis": "存续较久"}}},
        "finance": {
            "finance": {"registered_capital": {"score": 5, "reliability": "green", "basis": "注册资本较高"}},
            "missing_financial_fields": ["现金流"],
        },
        "credit": {
            "credit": {"dishonest_status": {"score": 0, "reliability": "green", "basis": "未见失信"}},
            "red_line_data": {"is_dishonest": False},
        },
    }
    summary = {
        "industry": {"industry_advancement": {"score": 0}},
        "red_line_data": {"is_dishonest": True},
        "overall_summary": "综合评价",
        "action_recommendation": {"conclusion": "谨慎合作"},
    }

    merged = merge_parallel_payload(dimensions, summary)

    assert merged["industry"]["industry_advancement"]["score"] == 4
    assert merged["red_line_data"]["is_dishonest"] is False
    assert merged["missing_financial_fields"] == ["现金流"]
    assert merged["overall_summary"] == "综合评价"
    assert merged["action_recommendation"]["conclusion"] == "谨慎合作"


def test_parallel_runtime_config_defaults_and_overrides():
    runtime = get_parallel_runtime_config({"parallel_generation": {"dimension_launch_interval_seconds": 0}})

    assert runtime["max_parallel_workers"] == 4
    assert runtime["dimension_launch_interval_seconds"] == 0
    assert runtime["max_input_chars"] == 7000
