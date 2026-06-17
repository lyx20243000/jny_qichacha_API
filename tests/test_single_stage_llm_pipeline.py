import sys
import types


for _mod_name in ("langchain", "langchain.tools"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))


sys.path.insert(0, "src")

from services.single_stage_llm_pipeline import build_single_stage_payload, get_single_stage_llm_config


def test_build_single_stage_payload_keeps_full_evidence_groups():
    evidence = {
        "identity": {"enterprise_name": "测试企业", "status": "confirmed"},
        "evidence_summary": {"subject_profile": "主体", "finance_signal_summary": "财务"},
        "collection_diagnostics": {"recommended_next_step": "continue_scoring"},
        "qixin_api": {"basic": "启信宝工商"},
        "qcc_mcp": {"risk": "企查查风险"},
        "triggered_mcp": {"finance": "触发补查"},
        "qcc_data_json": {"registration": "工商", "financial": "财务", "dishonest": "失信"},
        "search_evidence": {"industry": {"items": [{"title": "行业线索", "snippet": "增长"}]}},
    }

    payload = build_single_stage_payload(evidence, max_input_chars=10000)

    assert payload["qixin_api"]["basic"] == "启信宝工商"
    assert payload["qcc_mcp"]["risk"] == "企查查风险"
    assert payload["qcc_data_json"]["financial"] == "财务"
    assert payload["search_evidence"]["industry"]["items"][0]["title"] == "行业线索"


def test_single_stage_llm_config_overrides_base_config():
    cfg = {
        "config": {"model": "base", "temperature": 0.4, "thinking": "disabled"},
        "single_stage_generation": {
            "report_llm": {"model": "single", "temperature": 0.2, "timeout": 300}
        },
    }

    stage_cfg = get_single_stage_llm_config(cfg)

    assert stage_cfg["model"] == "single"
    assert stage_cfg["temperature"] == 0.2
    assert stage_cfg["timeout"] == 300
    assert stage_cfg["thinking"] == "disabled"
