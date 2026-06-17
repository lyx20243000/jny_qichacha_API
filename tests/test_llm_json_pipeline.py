import sys
import types


for _mod_name in ("langchain", "langchain.tools"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)
if not hasattr(sys.modules["langchain.tools"], "tool"):
    sys.modules["langchain.tools"].tool = lambda fn=None, **kw: (fn if fn is not None else (lambda f: f))


sys.path.insert(0, "src")

from services.llm_json_pipeline import compact_json, extract_json_object


def test_extract_json_object_from_fenced_response():
    parsed = extract_json_object('```json\n{"a":1,"b":{"c":2}}\n```')
    assert parsed == {"a": 1, "b": {"c": 2}}


def test_compact_json_uses_no_extra_spaces():
    assert compact_json({"企业": "测试", "值": [1, 2]}) == '{"企业":"测试","值":[1,2]}'
