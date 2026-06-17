import json
import sys
import types


for _mod_name in ("langchain", "langchain.tools"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))


sys.path.insert(0, "src")

from services.two_stage_llm_pipeline import extract_json_object, merge_scoring_payload


def test_extract_json_object_from_fenced_response():
    parsed = extract_json_object('```json\n{"a":1,"b":{"c":2}}\n```')
    assert parsed == {"a": 1, "b": {"c": 2}}


def test_merge_scoring_payload_preserves_core_scores_and_conclusion():
    core = {
        "assessment_goal": "企业综合评估",
        "industry": {"industry_advancement": {"score": 4, "reliability": "green", "basis": "行业稳定"}},
        "red_line_data": {"is_dishonest": False},
        "overall_summary_short": "短结论",
        "action_recommendation_core": {
            "conclusion": "谨慎合作",
            "next_action": "进入人工复核",
            "key_risks": ["财务缺失"],
        },
    }
    enrichment = {
        "industry": {"industry_advancement": {"score": 0, "reliability": "red", "basis": "试图改分"}},
        "red_line_data": {"is_dishonest": True},
        "overall_summary": "扩展结论",
        "action_recommendation": {
            "conclusion": "建议合作",
            "next_action": "直接进入合作流程",
            "cooperation_advice": "小额试单",
            "key_risks": ["补充审计报告"],
        },
    }

    merged = merge_scoring_payload(core, enrichment)

    assert merged["industry"]["industry_advancement"]["score"] == 4
    assert merged["red_line_data"]["is_dishonest"] is False
    assert merged["overall_summary"] == "扩展结论"
    assert merged["action_recommendation"]["conclusion"] == "谨慎合作"
    assert merged["action_recommendation"]["next_action"] == "进入人工复核"
    assert merged["action_recommendation"]["cooperation_advice"] == "小额试单"


def test_merge_scoring_payload_uses_short_summary_without_enrichment():
    merged = merge_scoring_payload(
        {
            "overall_summary_short": "短摘要",
            "action_recommendation_core": {"conclusion": "建议合作"},
        },
        {},
    )
    assert merged["overall_summary"] == "短摘要"
    assert merged["action_recommendation"]["conclusion"] == "建议合作"
    assert json.dumps(merged, ensure_ascii=False)

