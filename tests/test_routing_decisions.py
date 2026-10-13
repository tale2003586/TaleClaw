from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hashlib
import json
from pathlib import Path
import pytest

from models.provider import LLMResponse, LLMUsage
from runtime.routing.hybrid_classifier import HybridModeClassifier
from runtime.routing.agent_router import AgentRouter
from runtime.sessions import Session
from runtime.execution.budget import ExecutionCancelled
from research.hybrid_routing.baselines import load_dataset


class Provider:
    def __init__(self, content): self.content, self.calls = content, []
    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=self.content, usage=LLMUsage(2, 1, 3))


@pytest.mark.parametrize("content,valid,fallback", [('{"mode":"bot"}',True,""),('not JSON',False,"invalid_output"),('{"mode":"maybe"}',False,"invalid_output"),('[]',False,"invalid_output"),('{"mode":[]}',False,"invalid_output")])
def test_valid_bot_is_distinct_from_invalid_fallback(content,valid,fallback):
    decision = HybridModeClassifier(provider=Provider(content), model="fake").classify("解释Python")
    assert decision.label == "bot"
    assert decision.valid is valid
    assert decision.fallback_reason == fallback
    assert decision.total_tokens == 3


def test_truncation_and_actual_visible_input_are_auditable():
    provider = Provider('{"mode":"coding"}')
    result = HybridModeClassifier(provider=provider, model="fake").classify("修改代码" + "长"*5000)
    assert result.to_dict()["input_truncated"]
    assert result.visible_chars == len(provider.calls[0]["messages"][1]["content"]) == 4000
    assert provider.calls[0]["tools"] == []
    assert "raw_text" not in result.to_dict()


@pytest.mark.parametrize("mode,role,text,expected_calls,execution", [
    ("hybrid","admin","修改 Python 文件",1,"coding_application"),
    ("hybrid","user","修改 Python 文件",0,"runtime"),
    ("bot","admin","修改 Python 文件",0,"runtime"),
    ("coding","admin","hello",0,"coding_application"),
    ("hybrid","admin","下载 storage 报告文件",0,"runtime"),
    ("hybrid","admin","hello",0,"runtime"),
    ("hybrid","user","/coding",0,"direct_reply"),
])
def test_full_entry_gate_and_permissions_unchanged(mode,role,text,expected_calls,execution):
    provider = Provider('{"mode":"coding"}')
    classifier = HybridModeClassifier(provider=provider, model="fake")
    router = AgentRouter(hybrid_classifier=classifier)
    session = Session("route:test", active_agent=mode, metadata={"user_role":role})
    seen = []
    route = router.route(session,text,on_decision=seen.append)
    assert route.execution == execution
    assert len(provider.calls) == expected_calls
    assert len(seen) == expected_calls
    assert (route.classifier_decision is not None) == bool(expected_calls)


def test_concurrent_decisions_do_not_share_last_result():
    class ConcurrentProvider:
        def chat(self, **kwargs):
            mode = "coding" if kwargs["messages"][1]["content"] == "modify" else "bot"
            return LLMResponse(content=json.dumps({"mode":mode}))
    classifier = HybridModeClassifier(provider=ConcurrentProvider(), model="fake")
    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(classifier.classify, ["modify","explain"]*10))
    assert [v.label for v in values] == ["coding","bot"]*10
    assert not hasattr(classifier,"last_result")


def test_cancel_is_not_silently_downgraded_to_bot():
    class Cancel:
        def chat(self,**kwargs): raise ExecutionCancelled("stop")
    with pytest.raises(ExecutionCancelled):
        HybridModeClassifier(provider=Cancel(), model="fake").classify("修复代码")


def test_frozen_dataset_is_grouped_and_authorized():
    root = Path(__file__).resolve().parents[1]/"research"/"hybrid_routing"
    rows,manifest=load_dataset(root/"pilot.jsonl",root/"manifest.json")
    assert len(rows)==64 and len({r["family_id"] for r in rows})==32
    assert {r["source"] for r in rows}=={"original_synthetic"}


@pytest.mark.parametrize("attack",["leak","family","unauthorized","visible"])
def test_dataset_validator_rejects_corruption(tmp_path,attack):
    root = Path(__file__).resolve().parents[1]/"research"/"hybrid_routing"
    rows=[json.loads(line) for line in (root/"pilot.jsonl").read_text().splitlines()]
    if attack=="leak": rows[0]["raw_text"]=rows[0]["visible_text"]="api_key=sk-fakeprivatekey12345"
    if attack=="family": rows[0]["split"]="test" if rows[0]["split"]=="train" else "train"
    if attack=="unauthorized": rows[0]["authorized"]=False
    if attack=="visible": rows[0]["visible_text"]="different visible input"
    path=tmp_path/"data.jsonl";path.write_text("\n".join(json.dumps(r) for r in rows))
    manifest=json.loads((root/"manifest.json").read_text());manifest["sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
    mp=tmp_path/"manifest.json";mp.write_text(json.dumps(manifest))
    with pytest.raises(ValueError): load_dataset(path,mp)
