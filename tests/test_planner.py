import json
import unittest

from chaoslab import planner


def snapshot():
    services = {"api-gateway": ("entry point", 1.0), "customers-service": ("service", 0.7),
                "visits-service": ("service", 0.4), "config-server": ("platform", 0.0)}
    return {
        "schema_version": 1, "snapshot_id": "fixture", "entry_point": "api-gateway",
        "nodes": [{"id": name, "active": True, "kind": "container", "role": role, "share_of_requests": share,
                   "auto_restart": False, "instances": 1,
                   "needed_at_startup_by": ["api-gateway", "customers-service", "visits-service"]
                   if name == "config-server" else []}
                  for name, (role, share) in services.items()],
        "restart_effects": {"visits-service": {"features": ["owner details"], "outcome": "degraded",
                                               "behavior": "the in-memory database restarts from seed data",
                                               "data": "visits written at runtime are gone", "basis": "measured",
                                               "verification": "verified",
                                               "evidence": [{"kind": "experiment", "locator": "runs/x"}]}},
        "dependencies": [
            {"id": "owner-customers", "consumer": "api-gateway", "provider": "customers-service",
             "feature": "owner details", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "CustomersServiceClient.java"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "owner lookup fails"}}},
            {"id": "owner-visits", "consumer": "api-gateway", "provider": "visits-service",
             "feature": "owner details", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "ApiGatewayController.java"}],
             "effects": {"unavailable": {"outcome": "degraded", "behavior": "empty visits", "http_status": 200}}},
            {"id": "route-visits", "consumer": "api-gateway", "provider": "visits-service",
             "feature": "add visit", "phases": ["runtime"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "application.yml"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "gateway fallback", "http_status": 503}}},
            {"id": "gateway-config", "consumer": "api-gateway", "provider": "config-server",
             "feature": None, "phases": ["startup", "restart"], "verification": "verified",
             "evidence": [{"kind": "source", "locator": "application.yml"}],
             "effects": {"unavailable": {"outcome": "unavailable", "behavior": "config import fails",
                                          "propagate_service": True}}},
        ],
    }


ALLOWED = {"api-gateway", "customers-service", "visits-service", "config-server"}


def run(kind, target, outcome="aborted", recovery_s=12.0):
    return {"id": f"x-{kind}-{target}", "kind": kind, "target": target, "params": {}, "outcome": outcome,
            "abort_reason": None, "recovered": True, "recovery_s": recovery_s, "peak_error_rate": 0.3,
            "peak_p95_ms": 900, "data_loss": False, "predictions": {}}


def tool_call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    """Replies from a script; records what it was sent."""

    def __init__(self, replies):
        self.replies, self.sent, self.last_error = list(replies), [], ""

    def available(self):
        return True

    def describe(self):
        return "fake-model"

    def chat(self, messages, tools=None, tool_choice=None, json_mode=False, max_tokens=0):
        self.sent.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice})
        if not self.replies:
            self.last_error = "script exhausted"
            return None
        return self.replies.pop(0)


class PlannerTest(unittest.TestCase):
    def test_ranking_respects_spike_entry_point_history_and_approval_rules(self):
        ranked = planner.candidates(snapshot(), [run("kill", "customers-service")], ALLOWED)
        pairs = {(c["target"], c["kind"]): c for c in ranked}
        self.assertEqual({t for t, k in pairs if k == "spike"}, {"api-gateway"})
        self.assertLess(pairs[("customers-service", "pause")]["rank"], pairs[("customers-service", "kill")]["rank"],
                        "an untested pair outranks one already tested on the same service")
        self.assertIn("tested 1x", pairs[("customers-service", "kill")]["reasons"])
        crash_visits = pairs[("visits-service", "kill")]
        self.assertTrue(crash_visits["requires_approval"])
        self.assertIn("visits written at runtime are gone", crash_visits["approval_reason"])
        self.assertTrue(pairs[("config-server", "kill")]["requires_approval"])
        self.assertIn("start-up dependency of 3 services", pairs[("config-server", "kill")]["approval_reason"])
        self.assertFalse(pairs[("customers-service", "delay")]["requires_approval"])
        self.assertEqual(crash_visits["predicted"], {"owner details": "degraded", "add visit": "unavailable"})

    def test_validation_rejects_unlisted_choices_and_clamps_settings(self):
        top = planner.candidates(snapshot(), [], ALLOWED)[:5]
        rejected, why = planner.validate({"choice": 9, "hypothesis": {"statement": "x"}}, top)
        self.assertIsNone(rejected)
        self.assertIn("not a listed candidate", why)
        rejected, why = planner.validate({"choice": 0}, top)
        self.assertIn("hypothesis.statement is required", why)
        index = next(i for i, c in enumerate(top) if c["kind"] == "delay")
        accepted, _ = planner.validate({"choice": index, "parameters": {"delay_ms": 9000, "evil": 1},
                                        "duration_s": 600, "hypothesis": {
                                            "statement": "slow", "expected": {"list vets": "on fire", "add visit": "degraded"},
                                            "expected_max_error_rate": 7}}, top)
        self.assertEqual(accepted["params"], {"delay_ms": 3000})
        self.assertEqual(accepted["duration_s"], 60)
        self.assertEqual(accepted["hypothesis"]["expected"], {"add visit": "degraded"})
        self.assertEqual(accepted["hypothesis"]["expected_max_error_rate"], 1.0)
        self.assertEqual(len(accepted["notes"]), 4)

    def test_model_investigates_corrects_a_rejected_plan_and_wins(self):
        top = planner.candidates(snapshot(), [], ALLOWED)[:5]
        delay = next(i for i, c in enumerate(top) if c["kind"] == "delay")
        llm = FakeLLM([
            {"content": "", "tool_calls": [tool_call("predict_impact", {"choice": 0}, "a"),
                                           tool_call("experiment_history", {}, "b")]},
            {"content": "", "tool_calls": [tool_call("submit_plan", {"choice": 42, "hypothesis": {"statement": "x"}}, "c")]},
            {"content": "", "tool_calls": [tool_call("submit_plan", {
                "choice": delay, "parameters": {"delay_ms": 800}, "duration_s": 20,
                "rationale": "Customers is on most requests and its slow path is unverified.",
                "hypothesis": {"statement": "owner details slows but succeeds", "expected": {"owner details": "degraded"},
                               "expected_max_error_rate": 0.1, "expected_recovery_s": 8}}, "d")]},
        ])
        p = planner.plan(snapshot(), ALLOWED, history=[], llm=llm)
        self.assertEqual(p["planned_by"], "ai")
        self.assertEqual((p["kind"], p["target"]), (top[delay]["kind"], top[delay]["target"]))
        self.assertEqual(p["params"], {"delay_ms": 800})
        self.assertEqual(p["hypothesis"]["expected"], {"owner details": "degraded"})
        tool_results = [m for m in llm.sent[1]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(tool_results), 2)
        self.assertIn("while_faulted", tool_results[0]["content"])
        rejection = [m for m in llm.sent[2]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertTrue(rejection.startswith("REJECTED: choice 42"))
        self.assertEqual([t.get("result") for t in p["trace"] if t.get("tool") == "submit_plan"][-1], "accepted")

    def test_model_is_forced_to_submit_after_investigating(self):
        investigate = {"content": "", "tool_calls": [tool_call("fault_info", {"kind": "kill"})]}
        llm = FakeLLM([investigate, investigate, investigate, None, None])
        p = planner.plan(snapshot(), ALLOWED, history=[], llm=llm)
        self.assertEqual(llm.sent[2]["tool_choice"], "auto")
        self.assertEqual(llm.sent[3]["tool_choice"], {"type": "function", "function": {"name": "submit_plan"}})
        self.assertEqual(p["planned_by"], "rules", "a model that never submits falls back to the code's pick")
        self.assertTrue(any("model call failed" in n for n in p["notes"]))

    def test_text_reply_with_json_counts_as_a_submission(self):
        llm = FakeLLM([{"content": '<think>hmm</think>```json\n{"choice": 0, "hypothesis": {"statement": "s"}}\n```',
                        "tool_calls": []}])
        p = planner.plan(snapshot(), ALLOWED, history=[], llm=llm)
        self.assertEqual(p["planned_by"], "ai")

    def test_without_a_model_the_top_candidate_runs_with_the_code_hypothesis(self):
        llm = FakeLLM([])
        llm.available = lambda: False
        llm.last_error = "no API key"
        p = planner.plan(snapshot(), ALLOWED, history=[], llm=llm)
        ranked = planner.candidates(snapshot(), [], ALLOWED)
        self.assertEqual((p["target"], p["kind"]), (ranked[0]["target"], ranked[0]["kind"]))
        self.assertEqual(p["planned_by"], "rules")
        self.assertIn("no model: no API key", p["notes"])
        self.assertTrue(p["hypothesis"]["statement"])

    def test_heavy_settings_need_approval_and_exclusions_are_honoured(self):
        top = planner.candidates(snapshot(), [], ALLOWED)
        spike = next(c for c in top if c["kind"] == "spike")
        accepted = {"candidate": spike, "params": {"multiplier": 6}, "duration_s": 30, "rationale": "",
                    "hypothesis": planner.default_hypothesis(spike), "notes": []}
        p = planner._finish(accepted, top[:5], top, snapshot(), "ai", "fake-model", [], [])
        self.assertTrue(p["requires_approval"])
        self.assertIn("heavy settings", p["approval_reason"])
        excluded = {(c["target"], c["kind"]) for c in top[:3]}
        p = planner.plan(snapshot(), ALLOWED, history=[], llm=None, exclude=excluded)
        self.assertNotIn((p["target"], p["kind"]), excluded)
        record = {"kind": "delay", "params": {"delay_ms": 500}, "duration_s": 30}
        self.assertEqual(planner.fault_params(record, 30), {"delay_ms": 500, "expire_after_s": 60})
        self.assertEqual(planner.fault_params({"kind": "kill", "params": {}, "duration_s": 30}, 30), {})


if __name__ == "__main__":
    unittest.main()
