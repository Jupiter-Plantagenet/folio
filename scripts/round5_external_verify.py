"""Round-5 EXTERNAL evidence-bundle verifier. Independent of Folio code: it imports nothing
from `folio` (run it with `python -I`), re-implements every digest from the documented
canonical-JSON rule, and verifies the Ed25519 approval signature with the target's public key.

  python -I round5_external_verify.py verify <pack.json> [report.json]
  python -I round5_external_verify.py tamper <pack.json> <outdir>   # mutated packs + verdicts

A pack holds: the downloaded bundle bytes, the export_jobs row, the case/proposal/job rows,
original upload bytes, the target operation receipt / authority grant / current payable rows
(read through the read-only verifier principal) and the approval public key. Collection is
done by scripts/round5_proof.py; verification is offline and deterministic.
"""

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FIELDS = {"recognized_quantity", "net_amount", "tax_amount", "gross_amount", "status"}


def canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def h(v):
    return hashlib.sha256(canon(v)).hexdigest()


def verify(pack):
    checks, lineage = {}, {}
    raw = base64.b64decode(pack["bundle_b64"])
    job = pack["export_job"]
    bundle = json.loads(raw)
    case, prop = pack["case"], pack["proposal"]
    checks["F1_bundle_bytes_match_stored_artifact_digest"] = (
        hashlib.sha256(raw).hexdigest() == job["artifact_sha256"] and len(raw) == job["artifact_bytes"])
    checks["F2_bundle_equals_frozen_snapshot_and_digest"] = (
        raw == canon(job["snapshot"]) and hashlib.sha256(raw).hexdigest() == job["snapshot_sha256"])
    checks["C1_case_snapshot_digest"] = (
        bundle["case"]["id"] == case["id"] and bundle["case"]["version"] == case["version"]
        and bundle["case"]["snapshot_sha256"] == h(case))
    originals = {o["id"]: o for o in pack["originals"]}
    src_ok, src_hashes = [], set()
    for s in bundle["sources"]:
        cs = next((x for x in case["sources"] if x["id"] == s["id"]), None)
        o = originals.get(s["id"])
        ok = (cs is not None and h(cs) == s["snapshot_sha256"] and cs.get("sha256") == s["sha256"]
              and o is not None and o["sha256"] == s["sha256"]
              and hashlib.sha256(base64.b64decode(o["raw_b64"])).hexdigest() == s["sha256"])
        src_ok.append(ok)
        src_hashes.add(s["sha256"])
        lineage.setdefault("sources", []).append({"id": s["id"], "name": cs and cs.get("name"), "sha256": s["sha256"],
                                                  "fixture_file": pack.get("fixture_files", {}).get(s["sha256"])})
    checks["S1_sources_bytes_and_snapshots"] = bool(src_ok) and all(src_ok)
    prov_ok, spans = [], []
    for field, entries in bundle["field_provenance"].items():
        stored = case["field_provenance"][field]
        for i, e in enumerate(entries):
            ok = e["entry_sha256"] == h(stored[i]) and (e["kind"] != "extracted" or e["source_sha256"] in src_hashes)
            prov_ok.append(ok)
            src = stored[i].get("source") or {}
            if src:
                cs = next((x for x in case["sources"] if x.get("sha256") == src.get("source_sha256")), None)
                page = cs and next((pg for pg in cs["pages"] if pg["number"] == src.get("page")), None)
                exact = bool(page) and page["text"][src["start"]:src["end"]] == src["quote"]
                hashed = bool(page) and hashlib.sha256(page["text"].encode()).hexdigest() == page.get("text_sha256")
                spans.append(exact and hashed)
                lineage.setdefault("field_spans", []).append(
                    {"field": field, "kind": e["kind"], "source_sha256": src.get("source_sha256"), "quote": src.get("quote"),
                     "page": src.get("page"), "span": [src.get("start"), src.get("end")],
                     "roundtrip_exact": exact, "page_text_hash_ok": hashed})
    checks["P1_field_provenance_entries_and_source_links"] = bool(prov_ok) and all(prov_ok)
    checks["P2_spans_roundtrip_exact_quote_from_hashed_text"] = bool(spans) and all(spans)
    checks["R1_proposal_snapshot_digest"] = bundle["proposal"]["id"] == prop["id"] and bundle["proposal"]["snapshot_sha256"] == h(prop)
    intent = h(dict(schema="northstar-intent-v1", workspace=prop["workspace"], target=prop["target"],
                    revision=prop["target_revision"], patch=prop["patch"]))
    checks["I1_intent_digest_recomputed"] = intent == prop["target_digest"] and set(prop["patch"]) == FIELDS
    att = prop["approval_attestation"]
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pack["approval_public_key"])).verify(
            base64.b64decode(att["signature"], validate=True), canon(att["body"]))
        sig = True
    except Exception:
        sig = False
    b = att["body"]
    checks["A1_approval_signature_ed25519"] = sig
    checks["A2_signed_body_binds_intent_and_separation"] = (
        b["proposal"] == prop["id"] and b["operation_key"] == prop["operation_key"] and b["intent_digest"] == prop["target_digest"]
        and b["target_id"] == prop["target"] and b["target_revision"] == prop["target_revision"]
        and b["prepared_by"] == prop["prepared_by"] and b["approved_by"] == prop["approved_by"]
        and b["prepared_by"] != b["approved_by"])
    grant = pack["target_authority"]["grant_body"]
    checks["G1_target_grant_carries_same_signed_approval"] = (
        grant["intent_digest"] == prop["target_digest"] and grant["approval"] == att and grant["patch"] == prop["patch"]
        and pack["target_authority"]["digest"] == prop["target_digest"])
    r = pack["target_operation"]
    before, after = r["before_snapshot"], r["after_snapshot"]
    protected_same = ({k: v for k, v in before.items() if k not in FIELDS | {"revision"}}
                      == {k: v for k, v in after.items() if k not in FIELDS | {"revision"}})
    checks["T1_receipt_digests_recomputed"] = h(before) == r["before_digest"] and h(after) == r["after_digest"]
    checks["T2_single_revision_increment_bound_to_proposal"] = (
        r["before_revision"] == prop["target_revision"] == before["revision"] and r["after_revision"] == before["revision"] + 1
        and after["revision"] == r["after_revision"])
    checks["T3_exact_patch_only_protected_unchanged"] = (
        r["exact_patch"] == prop["patch"] and all(after[k] == v for k, v in prop["patch"].items()) and protected_same)
    checks["T4_receipt_intent_and_grant"] = (
        r["intent_digest"] == prop["target_digest"] and r["operation_key"] == prop["operation_key"]
        and r["grant"]["jti"] == grant["jti"])
    checks["T5_before_snapshot_bound_at_proposal"] = h(before) == prop.get("before_snapshot_digest")
    checks["T6_exactly_one_operation_for_key"] = pack["operations_for_key"] == 1 and pack["operations_on_record"] == 1
    checks["T7_current_record_matches_after"] = pack["target_payable"] == after
    jobs = pack["jobs"]
    checks["J1_one_job_completed_matched"] = (
        len(jobs) == 1 and jobs[0]["state"] == "completed" and (jobs[0]["result"] or {}).get("outcome") == "matched")
    checks["V1_verification_snapshot_digest"] = (
        bundle["verification"]["snapshot_sha256"] == h(case["verification"]) and case["verification"]["outcome"] == "matched")
    lineage.update({
        "parse": [{"source": s["id"], "parser": (next(x for x in case["sources"] if x["id"] == s["id"]).get("parser")
                                                 or next(x for x in case["sources"] if x["id"] == s["id"]).get("containment"))}
                  for s in bundle["sources"]],
        "proposal": {"id": prop["id"], "prepared_by": prop["prepared_by"], "patch": prop["patch"], "intent_digest": prop["target_digest"]},
        "approval": {"approved_by": prop["approved_by"], "approved_at": prop.get("approved_at"), "signature_valid": sig},
        "dispatch": {"job": jobs[0]["id"] if jobs else None, "fence": jobs[0]["fence"] if jobs else None},
        "target_revision": {"record": r["target_id"], "before": r["before_revision"], "after": r["after_revision"],
                            "operation_key": r["operation_key"], "committed_at": r["committed_at"]},
    })
    return {"verified": all(checks.values()), "checks": checks, "lineage": lineage,
            "verifier": "round5-external-v1 (no folio imports; canonical JSON sha256; Ed25519)"}


def tamper(pack, outdir):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    cases = {}

    def mutate(name, fn):
        p = copy.deepcopy(pack)
        fn(p)
        (out / f"{name}.json").write_text(json.dumps(p))
        r = verify(p)
        cases[name] = {"detected": not r["verified"], "failed_checks": [k for k, v in r["checks"].items() if not v]}

    def flip_bundle(p):
        raw = bytearray(base64.b64decode(p["bundle_b64"]))
        i = raw.index(b'"version":')
        raw[i + 10] = ord("9") if raw[i + 10] != ord("9") else ord("8")
        p["bundle_b64"] = base64.b64encode(bytes(raw)).decode()

    mutate("bundle_one_byte_flipped", flip_bundle)
    mutate("proposal_patch_gross_changed", lambda p: p["proposal"]["patch"].update(gross_amount="9999.00"))
    mutate("approval_signature_corrupted", lambda p: p["proposal"]["approval_attestation"].update(
        signature=base64.b64encode(b"\x00" * 64).decode()))
    mutate("approval_body_self_approved", lambda p: p["proposal"]["approval_attestation"]["body"].update(
        approved_by=p["proposal"]["prepared_by"]))
    mutate("receipt_protected_field_changed", lambda p: p["target_operation"]["after_snapshot"].update(
        bank_fingerprint="attacker-bank"))
    mutate("original_upload_bytes_changed", lambda p: p["originals"][0].update(
        raw_b64=base64.b64encode(base64.b64decode(p["originals"][0]["raw_b64"]) + b"\n%tamper").decode()))
    mutate("extracted_quote_changed", lambda p: next(iter(p["case"]["field_provenance"].values()))[0]["source"].update(quote="999"))
    mutate("parsed_page_text_changed", lambda p: p["case"]["sources"][0]["pages"][0].update(
        text=p["case"]["sources"][0]["pages"][0]["text"].replace("1", "7", 1)))
    mutate("duplicate_target_operation", lambda p: p.update(operations_for_key=2))
    mutate("verification_outcome_rewritten", lambda p: p["case"]["verification"].update(outcome="matched", checks={}))
    return cases


if __name__ == "__main__":
    cmd, src, *rest = sys.argv[1:]
    pack = json.loads(Path(src).read_text())
    result = verify(pack) if cmd == "verify" else tamper(pack, rest[0])
    text = json.dumps(result, indent=2)
    if rest and cmd == "verify":
        Path(rest[0]).write_text(text)
    elif cmd == "tamper":
        Path(rest[0], "tamper-verdicts.json").write_text(text)
    print(text)
