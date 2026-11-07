import pytest


def test_model_tool_authority_is_closed_and_case_bound():
    from folio.agent import ToolBroker

    case = {"id": "local-case", "sources": [], "target_id": "fixed-target"}
    broker = ToolBroker(case, lookup=lambda: {"revision": 7})
    for name, args in [
        ("terminal", {"command": "whoami"}),
        ("lookup_payable", {"target": "foreign"}),
        ("read_sources", {"url": "https://invalid"}),
        (
            "recommend_outcome",
            {"mode": "partial", "quantity": "999", "reason": "x", "bank": "rewrite"},
        ),
    ]:
        with pytest.raises(ValueError, match="TOOL_DENIED|ARGUMENTS_INVALID"):
            broker.execute(name, args)
    assert broker.execute("lookup_payable", {}) == {"revision": 7}
    assert broker.execute("read_sources", {})["sources"] == []


def test_missing_source_quantity_is_a_typed_tool_rejection_not_a_worker_crash():
    from folio.agent import ToolBroker

    broker = ToolBroker(
        {
            "sources": [],
            "invoice_quantity": "120",
            "ordered_quantity": "120",
            "received_quantity": "",
        },
        lookup=lambda: {},
    )
    broker.execute("read_sources", {})
    broker.execute("lookup_payable", {})
    with pytest.raises(ValueError, match="SOURCE_FIELDS_REQUIRE_REVIEW"):
        broker.execute(
            "recommend_outcome",
            {
                "mode": "partial",
                "quantity": "100",
                "reason": "Attempt unsupported source values",
            },
        )


def test_bounded_loop_never_treats_narrative_as_verified_or_executes_unknown_tool():
    from folio.agent import run_bounded

    case = {"id": "local-case", "sources": [], "target_id": "fixed-target"}
    calls = []

    def generate(messages):
        calls.append(messages)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I verified everything",
                        "tool_calls": [
                            {
                                "id": "x",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": '{"command":"delete"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 5},
        }

    result = run_bounded(case, lambda: {}, generate=generate)
    assert result["status"] == "blocked"
    assert result["target_writes"] == 0
    assert len(calls) == 1
    assert "TOOL_DENIED" in result["error"]


def test_model_usage_limit_blocks_before_any_tool_result_is_produced():
    from folio.agent import run_bounded

    def generate(messages):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "1",
                                "type": "function",
                                "function": {"name": "read_sources", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ],
            "usage": {"total_tokens": 20000},
        }

    result = run_bounded({"sources": []}, lambda: {}, generate=generate)
    assert result["status"] == "blocked" and result["error"] == "RUN_BUDGET_EXHAUSTED"
    assert not any("tool" in entry for entry in result["trace"])


def _read_sources_response(usage):
    response = {
        "id": "chatcmpl-local-1",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "read_sources", "arguments": "{}"},
                        }
                    ],
                }
            }
        ],
    }
    if usage is not None:
        response["usage"] = usage
    return response


def test_every_model_request_logs_usage_and_provider_id():
    from folio.agent import run_bounded

    usage = {"prompt_tokens": 700, "completion_tokens": 40, "total_tokens": 740}
    result = run_bounded(
        {"sources": []}, lambda: {}, generate=lambda m: _read_sources_response(usage)
    )
    assert result["usage_requests"]
    for entry in result["usage_requests"]:
        assert entry == {"provider_id": "chatcmpl-local-1", **usage}
    assert result["usage_total_tokens"] == 740 * len(result["usage_requests"])


def test_missing_usage_is_logged_unknown_and_charged_against_budget():
    from folio.agent import UNKNOWN_USAGE_CHARGE, run_bounded

    result = run_bounded(
        {"sources": []}, lambda: {}, generate=lambda m: _read_sources_response(None)
    )
    assert result["status"] == "blocked" and result["error"] == "RUN_BUDGET_EXHAUSTED"
    assert result["usage_requests"]
    for entry in result["usage_requests"]:
        assert entry["total_tokens"] == "unknown"
        assert entry["prompt_tokens"] == "unknown"
    # Unknown usage is never free: the 12000-token run budget stops it within 3 calls.
    assert len(result["usage_requests"]) <= 3
    assert result["usage_total_tokens"] == UNKNOWN_USAGE_CHARGE * len(
        result["usage_requests"]
    )


def test_analysis_command_is_durable_and_unavailable_provider_is_explicit():
    from fastapi.testclient import TestClient
    from test_domain_completion import session

    from folio.workbench import WORKSPACE, app, create_case, migrate
    from folio.worker import analyze_one

    migrate()
    case = create_case()
    client = TestClient(app)
    h = session(client)
    url = f"/api/v1/workspaces/{WORKSPACE}/cases/{case['id']}/analysis-runs"
    result = client.post(url, headers={**h, "If-Match": '"case-v1"'}, json={})
    assert result.status_code == 202, result.text
    run_id = result.json()["run_id"]
    assert analyze_one(run_id=run_id)["status"] == "blocked"
    after = client.get(url + "/" + run_id)
    assert after.status_code == 200 and after.json()["status"] == "blocked"
    assert after.json()["result"]["target_writes"] == 0
    assert (
        client.post(
            url, headers={**session(client, "viewer"), "If-Match": '"case-v1"'}, json={}
        ).status_code
        == 403
    )


def _recommendation_broker(*, supported="5", recognized="0"):
    from folio.agent import ToolBroker

    case = {
        "sources": [{"id": "source-1", "sha256": "0" * 64, "pages": [{"number": 1, "text": "Invoice quantity 5"}]}],
        "invoice_quantity": supported,
        "ordered_quantity": supported,
        "received_quantity": supported,
        "supplier": "Aster Supply Co.",
        "currency": "USD",
        "unit": "each",
        "missing_fields": [],
        "conflicting_fields": [],
    }
    payable = {"supplier": "Aster Supply Co.", "currency": "USD", "recognized_quantity": recognized}
    broker = ToolBroker(case, lookup=lambda: payable)
    broker.execute("read_sources", {})
    broker.execute("lookup_payable", {})
    return broker


def _recommendation(mode, quantity):
    return {"mode": mode, "quantity": quantity, "reason": "deterministic invariant test"}


def test_no_change_requires_exactly_recognized_supported_quantity_and_has_zero_writes():
    broker = _recommendation_broker(supported="5", recognized="5.0")
    with pytest.raises(ValueError, match="NO_CHANGE_QUANTITY_MISMATCH"):
        broker.execute("recommend_outcome", _recommendation("no_change", "0"))
    result = broker.execute("recommend_outcome", _recommendation("no_change", "5"))
    assert result == {"accepted": True, "authority": "recommendation_only", "target_writes": 0}


def test_no_change_is_rejected_when_payable_does_not_recognize_supported_quantity():
    broker = _recommendation_broker(supported="5", recognized="0")
    with pytest.raises(ValueError, match="PAYABLE_DOES_NOT_RECOGNIZE_SUPPORTED_QUANTITY"):
        broker.execute("recommend_outcome", _recommendation("no_change", "5"))


@pytest.mark.parametrize("mode", ["hold", "escalate"])
def test_hold_and_escalate_require_zero_quantity(mode):
    broker = _recommendation_broker()
    with pytest.raises(ValueError, match="NONZERO_NONACTIONABLE_QUANTITY"):
        broker.execute("recommend_outcome", _recommendation(mode, "1"))
    result = broker.execute("recommend_outcome", _recommendation(mode, "0"))
    assert result["accepted"] is True and result["target_writes"] == 0
