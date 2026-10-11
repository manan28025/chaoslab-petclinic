import json
import unittest

from chaoslab import analyst

ACTIONS = ["add visit", "list owners", "list vets", "owner details", "pet types"]


def result(**changes):
    base = {
        "id": "20261010-164228-kill-visits-service", "fault": {"kind": "kill", "target": "visits-service", "params": {}},
        "baseline": {"rps": 5.0, "error_rate": 0.0, "p95_ms": 20},
        "guardrails": {"max_error_rate": 0.25, "max_p95_ms": 2000, "min_rps_ratio": 0.3},
        "outcome": "aborted", "abort_reason": "watchdog: owner details correctness degraded for 3s in a row",
        "time_to_first_breach_s": 8.1, "peak_error_rate": 0.25, "peak_p95_ms": 10000,
        "recovered": True, "recovery_s": 13.1, "collateral_actions": [],
        "data_loss": {"missing_visit_ids": [76], "canary_visit_id": 76, "canary_lost": True},
        "impact_by_action": {a: {"baseline_error_rate": 0.0, "fault_error_rate": 0.3 if a == "add visit" else 0.0,
                                 "uses_target": a in ("add visit", "owner details")} for a in ACTIONS},
        "prediction_comparison": {"owner details": {"predicted": "degraded", "observed": "degraded", "match": "matched"}},
        "semantic_observations": [{"status": 200, "correctness": "degraded", "phase": "fault"}],
        "timeline": [{"elapsed": 1, "phase": "baseline", "rps": 5.0}, {"elapsed": 2, "phase": "fault", "rps": 1.1}],
    }
    base.update(changes)
    return base


SNAPSHOT = {"nodes": [{"id": "visits-service", "auto_restart": False}]}


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def available(self):
        return True

    def describe(self):
        return "fake-model"

    def chat_json(self, system, user, max_tokens=0):
        self.facts = json.loads(user)
        return self.reply


class AnalystTest(unittest.TestCase):
    def test_visits_crash_is_critical_with_targeted_fixes(self):
        a = analyst.analyze(result(), SNAPSHOT)
        codes = [f["code"] for f in a["findings"]]
        self.assertEqual(codes[0], "data_loss")
        for code in ("silent_degradation", "hang_not_fail", "throughput_collapse", "no_self_healing", "user_errors"):
            self.assertIn(code, codes)
        self.assertEqual(a["verdict"], "data lost")
        self.assertEqual(a["risk"]["level"], "critical")
        self.assertEqual(a["risk"]["floor"], "data loss is critical")
        fixes = {f["id"]: f for f in a["fixes"]}
        self.assertIn("persistent-database", fixes)
        self.assertIn("python3 -m chaoslab run kill visits-service", fixes["persistent-database"]["verify"])
        self.assertIn("ApiGatewayApplication.java", " ".join(fixes["fail-fast"]["steps"]))
        self.assertEqual(a["observed"]["owner details"], "degraded")
        self.assertEqual(a["written_by"], "rules")

    def test_a_clean_run_holds_and_a_refused_run_has_no_score(self):
        clean = result(outcome="completed", abort_reason=None, data_loss=None, peak_error_rate=0.0, peak_p95_ms=40,
                       recovery_s=2.0, semantic_observations=[], timeline=[{"phase": "fault", "rps": 5.0}],
                       impact_by_action={a: {"baseline_error_rate": 0, "fault_error_rate": 0} for a in ACTIONS},
                       fault={"kind": "delay", "target": "visits-service"})
        a = analyst.analyze(clean, SNAPSHOT)
        self.assertEqual([f["code"] for f in a["findings"]], ["held"])
        self.assertEqual(a["verdict"], "held")
        self.assertLess(a["risk"]["score"], 1)
        self.assertEqual(a["fixes"], [])
        refused = analyst.analyze({"id": "r", "fault": {"kind": "kill", "target": "x"}, "outcome": "refused",
                                   "abort_reason": "error rate 96% is above 5%"})
        self.assertIsNone(refused["risk"])
        self.assertEqual(refused["verdict"], "refused")

    def test_hypothesis_is_checked_against_what_users_saw(self):
        plan = {"planned_by": "ai", "hypothesis": {"expected": {"owner details": "preserved", "add visit": "unavailable"},
                                                    "expected_max_error_rate": 0.5, "expected_recovery_s": 30}}
        a = analyst.analyze(result(plan=plan), SNAPSHOT)
        h = a["hypothesis"]
        self.assertFalse(h["held"])
        self.assertEqual(h["misses"], [{"action": "owner details", "expected": "preserved", "observed": "degraded"},
                                       {"action": "add visit", "expected": "unavailable", "observed": "degraded"}])
        self.assertTrue(h["error_rate"]["ok"])

    def test_model_writes_prose_and_at_most_two_unverified_fixes_but_no_numbers(self):
        llm = FakeLLM({"summary": "Visits crashed.", "likely_cause": "In-memory DB.", "production_impact": "Lost visits.",
                       "risk": 0.1, "extra_fixes": [{"title": f"idea {i}", "steps": ["do it"], "why": "w"} for i in range(3)]})
        a = analyst.analyze(result(), SNAPSHOT, llm)
        self.assertEqual(a["written_by"], "ai+rules")
        self.assertEqual(a["prose"]["summary"], "Visits crashed.")
        self.assertEqual(a["risk"]["level"], "critical", "the model cannot move the score")
        extras = [f for f in a["fixes"] if f["source"] != "library"]
        self.assertEqual([f["source"] for f in extras], ["ai, unverified"] * 2)
        self.assertIn("findings", llm.facts)

    def test_scorecard_grades_worst_first_and_weaknesses_carry_their_fix(self):
        crash = analyst.analyze(result(), SNAPSHOT)
        calm = analyst.analyze(result(id="b", fault={"kind": "delay", "target": "vets-service"}, outcome="completed",
                                      data_loss=None, peak_error_rate=0.0, peak_p95_ms=40, recovery_s=2.0,
                                      semantic_observations=[], timeline=[], abort_reason=None,
                                      impact_by_action={a: {"baseline_error_rate": 0, "fault_error_rate": 0}
                                                        for a in ACTIONS}))
        card = analyst.scorecard([crash, calm], ["visits-service", "vets-service", "config-server"])
        rows = {r["service"]: r for r in card["services"]}
        self.assertEqual([r["service"] for r in card["services"]], ["visits-service", "vets-service", "config-server"])
        self.assertEqual(rows["vets-service"]["grade"], "A")
        self.assertIn(rows["visits-service"]["grade"], ("D", "F"))
        self.assertIsNone(rows["config-server"]["grade"])
        weak = analyst.weaknesses([crash])
        self.assertEqual(weak[0]["code"], "data_loss")
        self.assertEqual([f["id"] for f in weak[0]["fixes"]], ["persistent-database"])
        self.assertNotIn("watchdog_stop", [w["code"] for w in weak], "a safety stop is not an app weakness")
        r = analyst.report([crash, calm], ["visits-service", "vets-service", "config-server"],
                           ["kill", "pause", "delay", "loss", "cpu", "spike"], "api-gateway")
        self.assertEqual(r["coverage"], {"tested_pairs": 2, "possible_pairs": 16})
        self.assertIn("Overall resilience", analyst.report_markdown(r))


if __name__ == "__main__":
    unittest.main()
