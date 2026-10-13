"""Offline, grouped synthetic routing pilot. Never imports providers or uploads data."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

LABELS = {"bot", "coding"}
SECRET = re.compile(r"(?i)(?:sk-[a-z0-9]{12,}|(?:api[_-]?key|password|secret)\s*[:=]\s*[^\s]{4,}|[\w.+-]+@[\w.-]+\.[a-z]{2,}|/home/[^/ ]+/)")


def load_dataset(path, manifest_path):
    path = Path(path)
    manifest = json.loads(Path(manifest_path).read_text())
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("sha256"):
        raise ValueError("Dataset digest does not match frozen manifest")
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    groups, texts, ids = {}, set(), set()
    for row in rows:
        required = {"id", "family_id", "split", "label", "raw_text", "visible_text", "source", "authorized", "annotation"}
        if not required <= row.keys(): raise ValueError("Missing dataset provenance or label fields")
        if row["authorized"] is not True or row["source"] != "original_synthetic":
            raise ValueError("This pilot accepts original synthetic data only; real data needs a separate reviewed manifest")
        if row["label"] not in LABELS or row["split"] not in {"train", "dev", "test"}:
            raise ValueError("Invalid label or split")
        if not row["family_id"] or not row["raw_text"].strip(): raise ValueError("Empty family/input")
        if row["visible_text"] != row["raw_text"].strip()[:4000]: raise ValueError("Visible input mismatch")
        if SECRET.search(row["raw_text"]): raise ValueError("Possible sensitive data in input")
        if row["id"] in ids: raise ValueError("Duplicate id")
        ids.add(row["id"])
        normalized = " ".join(row["raw_text"].casefold().split())
        if normalized in texts: raise ValueError("Duplicate input, possible split leakage")
        texts.add(normalized)
        previous = groups.setdefault(row["family_id"], row["split"])
        if previous != row["split"]: raise ValueError("Family crosses split boundary")
    if len(rows) != manifest.get("rows") or len(groups) != manifest.get("families"):
        raise ValueError("Manifest counts mismatch")
    for split in ("train", "dev", "test"):
        if {r["label"] for r in rows if r["split"] == split} != LABELS:
            raise ValueError("Each split must contain both labels")
    return rows, manifest


def rule_predict(text):
    lowered = text.casefold()
    if any(word in lowered for word in ("解释", "介绍", "比喻", "原理", "概念", "不用", "不需要", "不修改", "explain", "concept", "analogy", "poem", "story", "essay", "without inspecting")):
        return "bot"
    action = any(word in lowered for word in ("修复", "修改", "重构", "编辑", "定位", "运行", "补", "审查", "对比", "更新", "复现", "阅读", "fix", "edit", "refactor", "run", "debug", "inspect", "review", "implement", "update", "reproduce"))
    artifact = any(word in lowered for word in ("代码", "函数", "项目", "文件", "本地", "测试", "repo", "code", "file", "function", ".py", "git", "script", "css", "docker", "test"))
    return "coding" if action and artifact else "bot"


def metrics(rows, predictions):
    from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support, confusion_matrix
    labels = [r["label"] for r in rows]
    p, r, f, _ = precision_recall_fscore_support(labels, predictions, labels=["coding"], zero_division=0)
    return {"samples": len(rows), "families": len({r["family_id"] for r in rows}),
            "accuracy": float(accuracy_score(labels, predictions)),
            "macro_f1": float(f1_score(labels, predictions, average="macro", zero_division=0)),
            "coding_precision": float(p[0]), "coding_recall": float(r[0]),
            "confusion_labels": ["bot", "coding"], "confusion_matrix": confusion_matrix(labels, predictions, labels=["bot", "coding"]).tolist(),
            "confidence_interval": None, "inference": "Exploratory only: six held-out families cannot establish 1pp/2pp non-inferiority."}


def run(dataset, manifest_path):
    import sklearn
    from sklearn.pipeline import make_pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    rows, manifest = load_dataset(dataset, manifest_path)
    train = [r for r in rows if r["split"] == "train"]
    # Frozen before holdout; do not fit the vocabulary on dev/test or tune on test.
    linear = make_pipeline(TfidfVectorizer(analyzer="char", ngram_range=(2, 4), min_df=1, max_features=10000),
                           LogisticRegression(C=1.0, random_state=17, max_iter=500))
    started = time.perf_counter()
    linear.fit([r["visible_text"] for r in train], [r["label"] for r in train])
    train_seconds = time.perf_counter()-started
    result = {"schema_version": "routing_pilot_results.v1", "data_sha256": manifest["sha256"],
        "source": manifest["source"], "sklearn_version": sklearn.__version__, "linear_train_seconds": train_seconds,
        "training_kind": "CPU linear classifier only; no language-model fine-tuning", "evaluations": {},
        "llm_prompt_baseline": "not_run: no paid/private input calls authorized",
        "same_base_zero_fewshot": "not_run: Qwen3-0.6B not cached, training Go unmet",
        "lora": "not_run", "deployment": "no_go", "end_to_end_benefit": "not_measured"}
    for split in ("dev", "test"):
        part = [r for r in rows if r["split"] == split]
        for name, predict in (("rule_v1", lambda texts: [rule_predict(t) for t in texts]),
                              ("char_ngram_logistic_v1", linear.predict)):
            start = time.perf_counter()
            predicted = list(predict([r["visible_text"] for r in part]))
            elapsed = time.perf_counter()-start
            result["evaluations"][f"{name}:{split}"] = {**metrics(part, predicted),
                "batch_prediction_seconds": elapsed,
                "predictions": [{"id": r["id"], "expected": r["label"], "predicted": p} for r,p in zip(part,predicted)]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("pilot.jsonl"))
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.dataset, args.manifest)
    if args.output.exists(): raise SystemExit("Output exists; choose a new path to preserve prior results")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps({k:v for k,v in result.items() if k != "evaluations"}, ensure_ascii=False))
