"""Round 6: output identity of llama-server with and without the host-RAM prompt cache.

  python scripts/round6_cache_identity.py prepare <src_set_dir> <dst_dir>   # copy inputs only
  python scripts/round6_cache_identity.py ram                               # model process memory
  python scripts/round6_cache_identity.py compare <run_a> <run_b> <out.json>

`compare` checks, for every task and trial present in both runs, that the model produced
byte-identical assistant messages (content + tool calls with arguments) at every step,
identical token usage, identical final outcome and identical tool trace. Timing, ids
and timestamps are ignored. Uses only the dev set (never evaluation-sealed/).
"""

import json
import shutil
import sys
from pathlib import Path


def prepare(src, dst):
    src, dst = Path(src), Path(dst)
    assert "evaluation-sealed" not in str(src.resolve())
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "cases", dst / "cases", dirs_exist_ok=True)
    if (src / "manifest.json").exists():
        shutil.copy2(src / "manifest.json", dst / "manifest.json")
    print(json.dumps({"cases": len(list((dst / "cases").glob("*.json"))), "dst": str(dst)}))


def ram():
    import psutil

    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == "llama-server.exe":
            m = p.memory_info()
            print(json.dumps({"pid": p.pid, "private_gib": round(m.private / 2**30, 3),
                              "working_set_gib": round(m.wset / 2**30, 3),
                              "host_available_gib": round(psutil.virtual_memory().available / 2**30, 2),
                              "cmdline_cache_ram": next((p.cmdline()[i + 1] for i, a in enumerate(p.cmdline())
                                                         if a in ("--cache-ram", "-cram")), "default(8192)")}))
            return
    print(json.dumps({"llama_server": "not running"}))


def canonical(run):
    steps = []
    for item in run["result"].get("trace", []):
        if "response" in item:
            resp = item["response"] if isinstance(item["response"], dict) else {}
            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            steps.append({"assistant": msg.get("content"), "reasoning": msg.get("reasoning_content"),
                          "tool_calls": [{"name": t.get("function", {}).get("name"),
                                          "arguments": t.get("function", {}).get("arguments")}
                                         for t in (msg.get("tool_calls") or [])],
                          "finish": choice.get("finish_reason")})
        elif "tool" in item:
            steps.append({"tool": item["tool"], "arguments": item.get("arguments"), "result": item.get("result")})
        else:
            steps.append({"other": item})
    usage = [{k: (c.get("usage") or {}).get(k) for k in ("prompt_tokens", "completion_tokens")} for c in run["calls"]]
    row = run["row"]
    return {"steps": steps, "usage": usage, "outcome": row["outcome"], "status": row["status"],
            "error": row["error"], "tools": row["tools_attempted"], "intent": row["model_intent"],
            "target_writes": row["target_writes"]}


def cached_tokens(run):
    return sum(((c.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) for c in run["calls"])


def compare(a, b, out):
    a, b = Path(a), Path(b)
    pairs, diffs, latency = 0, [], {"a": [], "b": []}
    cached = {"a": 0, "b": 0}
    prompt = {"a": 0, "b": 0}
    for fa in sorted((a / "runs").glob("*/trial-*.json")):
        fb = b / "runs" / fa.parent.name / fa.name
        if not fb.exists():
            continue
        ra, rb = json.loads(fa.read_text()), json.loads(fb.read_text())
        pairs += 1
        latency["a"].append(ra["row"]["wall_seconds"])
        latency["b"].append(rb["row"]["wall_seconds"])
        for key, r in (("a", ra), ("b", rb)):
            cached[key] += cached_tokens(r)
            prompt[key] += sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in r["calls"])
        ca, cb = canonical(ra), canonical(rb)
        if ca != cb:
            diffs.append({"task": fa.parent.name, "trial": fa.name,
                          "first_diff": next((k for k in ca if ca[k] != cb[k]), None)})
    within = {}
    for name, d in (("a", a), ("b", b)):  # determinism across trials inside one mode
        same = total = 0
        for task in sorted((d / "runs").iterdir()):
            trials = [canonical(json.loads(f.read_text())) for f in sorted(task.glob("trial-*.json"))]
            if len(trials) > 1:
                total += 1
                same += all(t == trials[0] for t in trials)
        within[name] = {"tasks_all_trials_identical": same, "tasks": total}

    def stats(xs):
        xs = sorted(xs)
        return {"n": len(xs), "p50_s": xs[len(xs) // 2] if xs else None,
                "p95_s": xs[min(len(xs) - 1, round(0.95 * (len(xs) - 1)))] if xs else None,
                "mean_s": round(sum(xs) / len(xs), 3) if xs else None}

    result = {"run_a": str(a), "run_b": str(b), "pairs_compared": pairs, "identical_pairs": pairs - len(diffs),
              "differences": diffs, "within_mode_determinism": within,
              "latency": {"a": stats(latency["a"]), "b": stats(latency["b"])},
              "prompt_tokens": prompt, "cached_prompt_tokens": cached,
              "verdict": "IDENTICAL" if pairs and not diffs else "DIFFERENT"}
    Path(out).write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("pairs_compared", "identical_pairs", "verdict", "latency")}))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"prepare": prepare, "ram": ram, "compare": compare}[cmd](*args)
