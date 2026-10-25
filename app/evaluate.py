"""Handout 5 test: compare Step-by-step (classify + extract) vs Maker and Grader on the same documents.

Put documents in eval_data/docs/*.txt (first line may be 'TITLE: ...') and the manual answer key in
eval_data/answer_key.csv with columns: doc,event_type,field,value
(one row per expected field; for a noise document write one row with event_type=noise and empty field).
"""
import csv
import time
from collections import defaultdict
from pathlib import Path

from app import ai, config, db

ROOT = config.ROOT


def load_docs(folder: Path) -> dict[str, tuple[str, str]]:
    docs = {}
    for p in sorted(folder.glob("*.txt")):
        raw = p.read_text(encoding="utf-8")
        title = ""
        if raw.startswith("TITLE:"):
            first, _, raw = raw.partition("\n")
            title = first[6:].strip()
        docs[p.name] = (title, raw)
    return docs


def load_key(path: Path):
    events, fields = {}, defaultdict(dict)
    for row in csv.DictReader(open(path, encoding="utf-8")):
        events[row["doc"]] = row["event_type"].strip()
        if row.get("field"):
            fields[row["doc"]][row["field"].strip()] = row["value"].strip()
    return events, fields


def same(expected: str, got) -> bool:
    try:
        e, g = float(expected), float(str(got).replace(",", "").replace("%", ""))
        return abs(e - g) <= max(0.01 * abs(e), 1e-9)
    except ValueError:
        pass
    e, g = str(expected).lower().strip(), str(got).lower().strip()
    return e == g or (len(e) > 3 and (e in g or g in e))


def cost(rows) -> float | None:
    total = 0.0
    for r in rows:
        price = config.MODEL_PRICES.get(r["model"])
        if price is None:
            return None
        total += r["input_tokens"] / 1e6 * price[0] + r["output_tokens"] / 1e6 * price[1]
    return total


def run_mode(mode: str, docs, events, fields) -> dict:
    c = db.get()
    start_id = c.execute("SELECT COALESCE(MAX(id),0) m FROM ai_usage").fetchone()["m"]
    t0 = time.perf_counter()
    stats = {"docs": len(docs), "class_ok": 0, "correct": 0, "wrong": 0, "missed": 0, "expected": 0}
    for name, (title, text) in docs.items():
        exp_event = events.get(name, "noise")
        got_event = ai.classify(text, title)
        stats["class_ok"] += got_event == exp_event
        exp_fields = {k: v for k, v in fields.get(name, {}).items() if k != "headline"}
        stats["expected"] += len(exp_fields)
        if got_event == "noise":
            stats["missed"] += len(exp_fields)
            continue
        if mode == "simple":
            items = ai.extract(text, title, got_event)
        else:
            items, _ = ai.extract_verified(text, title, got_event)
        got = {i["field"]: i["value"] for i in items if i["field"] != "headline"}
        for k, v in got.items():
            if k in exp_fields and same(exp_fields[k], v):
                stats["correct"] += 1
            else:
                stats["wrong"] += 1  # wrong value, or a field the key says is not in the document
        stats["missed"] += sum(1 for k in exp_fields if k not in got)
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    usage = c.execute("SELECT * FROM ai_usage WHERE id > ?", (start_id,)).fetchall()
    stats["input_tokens"] = sum(r["input_tokens"] for r in usage)
    stats["output_tokens"] = sum(r["output_tokens"] for r in usage)
    stats["cost_usd"] = cost(usage)
    return stats


def main(docs_dir="eval_data/docs", key_path="eval_data/answer_key.csv", modes=("simple", "maker_grader")):
    db.use(db.connect(ROOT / "data" / "eval.db"))
    db.init(db.get())
    docs = load_docs(ROOT / docs_dir)
    events, fields = load_key(ROOT / key_path)
    print(f"{len(docs)} documents, {sum(len(v) for v in fields.values())} expected fields\n")
    for mode in modes:
        s = run_mode(mode, docs, events, fields)
        acc = s["correct"] / s["expected"] if s["expected"] else 0
        cost_txt = f"${s['cost_usd']:.4f}" if s["cost_usd"] is not None else "set MODEL_PRICES to compute"
        print(f"== {mode} ==")
        print(f"  classification correct: {s['class_ok']}/{s['docs']}")
        print(f"  fields correct: {s['correct']}/{s['expected']} ({acc:.0%})  wrong or invented: {s['wrong']}  missed: {s['missed']}")
        print(f"  time: {s['seconds']}s  tokens: {s['input_tokens']} in / {s['output_tokens']} out  cost: {cost_txt}\n")
