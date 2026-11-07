"""Run the REAL product agent loop (folio.agent.run_bounded + local_generate) on
prepared cases with the operator-launched local model, then grade against
evaluator-only labels. Labels are loaded only after all trials of a task ran.

Usage: python eval/run_eval.py <set_dir> <labels.json> --trials 3 [--limit N]
Writes <set_dir>/runs/<task>/trial-<k>.json, <set_dir>/results.jsonl, summary.json
"""

import argparse
import json
import math
import random
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from folio import agent  # noqa: E402

ALLOWED_TOOLS = set(agent.SCHEMAS)


class GpuMeter(threading.Thread):
    """Samples nvidia-smi power.draw (W) once per second; integrates energy."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples, self.stop_flag = [], threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=power.draw,memory.used,utilization.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5).stdout.split(",")
                self.samples.append((time.monotonic(), float(out[0]), float(out[1]), float(out[2])))
            except Exception:
                pass
            self.stop_flag.wait(1.0)

    def joules(self, t0, t1):
        pts = [s for s in self.samples if t0 <= s[0] <= t1]
        if len(pts) < 2:
            return None
        return sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(pts, pts[1:]))


def model_process():
    import psutil

    for c in psutil.net_connections(kind="tcp"):
        if c.status == "LISTEN" and c.laddr and c.laddr.port == 18301:
            return psutil.Process(c.pid)
    return None


def run_one(case):
    calls = []

    def generate(messages):
        started = time.monotonic()
        raw = agent.local_generate(messages)
        calls.append({"seconds": round(time.monotonic() - started, 3), "usage": raw.get("usage", {}),
                      "timings": raw.get("timings", {})})
        return raw

    lookup = lambda: json.loads(json.dumps(case["payable"]))  # noqa: E731
    started = time.monotonic()
    result = agent.run_bounded(case, lookup, generate=generate)
    wall = time.monotonic() - started
    attempted, intent = [], None
    for item in result.get("trace", []):
        resp = item.get("response")
        if not resp:
            continue
        for call in (resp.get("choices", [{}])[0].get("message", {}).get("tool_calls") or []):
            fn = call.get("function", {})
            attempted.append(fn.get("name"))
            if fn.get("name") == "recommend_outcome":
                try:
                    intent = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    intent = {"unparseable": fn.get("arguments")}
    return result, wall, calls, attempted, intent


def outcome_of(result):
    if result["status"] == "recommended":
        r = result["recommendation"]
        return {"mode": r["mode"], "quantity": r.get("quantity")}
    return {"mode": "blocked", "error": result.get("error")}


def allowed(outcome, labels):
    for a in labels:
        if a["mode"] != outcome["mode"]:
            continue
        if a["mode"] == "partial":
            try:
                if Decimal(outcome["quantity"]) == Decimal(a["quantity"]):
                    return True
            except Exception:
                return False
            continue
        return True
    return False


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 4), round(c + h, 4)]


def cluster_bootstrap(per_task, reps=2000, seed=7):
    """95% CI of pooled trial accuracy, resampling TASKS (trials clustered)."""
    tasks = list(per_task.values())
    if not tasks:
        return None
    rnd = random.Random(seed)
    stats = []
    for _ in range(reps):
        sample = [tasks[rnd.randrange(len(tasks))] for _ in tasks]
        k = sum(sum(t) for t in sample)
        n = sum(len(t) for t in sample)
        stats.append(k / n)
    stats.sort()
    return [round(stats[int(0.025 * reps)], 4), round(stats[int(0.975 * reps)], 4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir")
    ap.add_argument("labels")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    set_dir = Path(a.set_dir)
    httpx.get("http://127.0.0.1:18301/health", timeout=5).raise_for_status()
    props = httpx.get("http://127.0.0.1:18301/props", timeout=5).json()
    proc = model_process()
    cpu0 = sum(proc.cpu_times()[:2]) if proc else None
    meter = GpuMeter()
    meter.start()
    cases = sorted((set_dir / "cases").glob("*.json"))[: a.limit]
    (set_dir / "runs").mkdir(exist_ok=True)
    started_all = time.monotonic()
    rows = []
    for path in cases:
        case = json.loads(path.read_text())
        tid = case["task_id"]
        for trial in range(1, a.trials + 1):
            out = set_dir / "runs" / tid / f"trial-{trial}.json"
            if out.exists():
                rows.append(json.loads(out.read_text())["row"])
                continue
            t0 = time.monotonic()
            result, wall, calls, attempted, intent = run_one(case)
            t1 = time.monotonic()
            row = {
                "task_id": tid, "trial": trial, "outcome": outcome_of(result),
                "status": result["status"], "error": result.get("error"),
                "model_intent": intent, "tools_attempted": attempted,
                "disallowed_tool_attempts": [t for t in attempted if t not in ALLOWED_TOOLS],
                "model_calls": len(calls), "tool_calls_executed": sum(1 for x in result.get("trace", []) if "tool" in x),
                "prompt_tokens": sum(c["usage"].get("prompt_tokens", 0) for c in calls),
                "completion_tokens": sum(c["usage"].get("completion_tokens", 0) for c in calls),
                "usage_total_tokens": result.get("usage_total_tokens"),
                "wall_seconds": round(wall, 3), "gpu_joules": meter.joules(t0, t1),
                "target_writes": result.get("target_writes"),
            }
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"row": row, "result": result, "calls": calls}, default=str))
            rows.append(row)
            print(json.dumps({k: row[k] for k in ("task_id", "trial", "outcome", "wall_seconds")}), flush=True)
    total_wall = time.monotonic() - started_all
    meter.stop_flag.set()
    cpu1 = sum(proc.cpu_times()[:2]) if proc else None
    # ---------------- grading (labels read only now)
    labels = json.loads(Path(a.labels).read_text())["tasks"]
    per_slice, per_task = {}, {}
    graded = []
    for row in rows:
        lab = labels[row["task_id"]]
        ok = allowed(row["outcome"], lab["allowed"])
        intent_ok = bool(row["model_intent"]) and isinstance(row["model_intent"], dict) and allowed(
            {"mode": row["model_intent"].get("mode"), "quantity": row["model_intent"].get("quantity")}, lab["allowed"])
        graded.append({**row, "slice": lab["slice"], "allowed": ok, "intent_allowed": intent_ok,
                       "label": lab["allowed"], "meta": lab.get("meta")})
        per_slice.setdefault(lab["slice"], []).append(ok)
        per_task.setdefault(row["task_id"], []).append(1 if ok else 0)
    with (set_dir / "results.jsonl").open("w") as f:
        for g in graded:
            f.write(json.dumps(g, default=str) + "\n")
    n = len(graded)
    k = sum(g["allowed"] for g in graded)
    partial_recs = [g for g in graded if g["outcome"]["mode"] == "partial"]
    partial_correct = [g for g in partial_recs if g["allowed"]]
    summary = {
        "set": set_dir.name, "tasks": len(per_task), "trials_per_task": a.trials, "runs": n,
        "allowed_final_outcomes": {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
                                   "wilson95_trials_unclustered": wilson(k, n),
                                   "task_cluster_bootstrap95": cluster_bootstrap(per_task)},
        "per_slice": {s: {"k": sum(v), "n": len(v), "rate": round(sum(v) / len(v), 4), "wilson95": wilson(sum(v), len(v))}
                      for s, v in per_slice.items()},
        "per_trial": {t: {"k": sum(g["allowed"] for g in graded if g["trial"] == t), "n": sum(1 for g in graded if g["trial"] == t)}
                      for t in range(1, a.trials + 1)},
        "trial_agreement_tasks": sum(1 for v in per_task.values() if len(set(v)) == 1),
        "model_intent_allowed": sum(g["intent_allowed"] for g in graded),
        "partial_recommendation_quantity_precision": {"k": len(partial_correct), "n": len(partial_recs),
                                                       "wilson95": wilson(len(partial_correct), len(partial_recs))},
        "outcome_counts": {},
        "blocked_errors": {},
        "disallowed_tool_attempts": sum(len(g["disallowed_tool_attempts"]) for g in graded),
        "target_writes": sum(g["target_writes"] or 0 for g in graded),
        "budget": {"max_model_calls": max((g["model_calls"] for g in graded), default=0),
                   "max_tool_calls": max((g["tool_calls_executed"] for g in graded), default=0),
                   "max_prompt_tokens": max((g["prompt_tokens"] for g in graded), default=0),
                   "max_completion_tokens": max((g["completion_tokens"] for g in graded), default=0),
                   "limits": {"model_calls": 8, "tool_calls": 12, "input_tokens": 24000, "output_tokens": 6000}},
        "cost": {"prompt_tokens": sum(g["prompt_tokens"] for g in graded),
                 "completion_tokens": sum(g["completion_tokens"] for g in graded),
                 "eval_wall_seconds": round(total_wall, 1),
                 "run_wall_seconds_sum": round(sum(g["wall_seconds"] for g in graded), 1),
                 "run_wall_p50": sorted(g["wall_seconds"] for g in graded)[n // 2] if n else None,
                 "run_wall_p95": sorted(g["wall_seconds"] for g in graded)[min(n - 1, int(n * 0.95))] if n else None,
                 "gpu_energy_joules_measured": round(sum(g["gpu_joules"] or 0 for g in graded), 1),
                 "gpu_energy_kwh_measured": round(sum(g["gpu_joules"] or 0 for g in graded) / 3.6e6, 6),
                 "model_process_cpu_seconds": round(cpu1 - cpu0, 1) if proc else None,
                 "currency_cost": "not computed: no local electricity tariff supplied; no paid API used",
                 "provider_billing": "none (local loopback model)"},
        "model_props": {k: props.get(k) for k in ("model_path", "n_ctx", "build_info", "total_slots") if k in props},
    }
    for g in graded:
        key = g["outcome"]["mode"]
        summary["outcome_counts"][key] = summary["outcome_counts"].get(key, 0) + 1
        if key == "blocked":
            summary["blocked_errors"][g["error"]] = summary["blocked_errors"].get(g["error"], 0) + 1
    (set_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
