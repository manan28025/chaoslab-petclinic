import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from chaoslab import fixes

FIX = fixes.FIXES["persistent-database"]


def run_result(run_id, data_loss=True, plan_fix=None, outcome="aborted"):
    return {"id": run_id, "fault": {"kind": "kill", "target": "visits-service", "params": {}}, "outcome": outcome,
            "baseline": {"rps": 5.0, "p95_ms": 20}, "peak_error_rate": 0.25, "peak_p95_ms": 10000,
            "recovered": True, "recovery_s": 13.1, "collateral_actions": [], "timeline": [],
            "data_loss": {"missing_visit_ids": [76], "canary_visit_id": 76, "canary_lost": True} if data_loss else None,
            "impact_by_action": {"add visit": {"baseline_error_rate": 0, "fault_error_rate": 0.3, "uses_target": True}},
            "semantic_observations": [], "prediction_comparison": {},
            **({"plan": {"fix": plan_fix}} if plan_fix is not None else {})}


class FixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name)
        for name, value in (("RUNS_DIR", self.runs), ("VERIFY_DIR", self.runs / "verifications"),
                            ("STATE_FILE", self.runs / "active-fixes.json")):
            patcher = patch.object(fixes, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, run_id, result):
        (self.runs / run_id).mkdir()
        (self.runs / run_id / "result.json").write_text(json.dumps(result))

    def test_only_safe_moments_and_known_fixes(self):
        with patch.object(fixes.safety, "active_faults", return_value={"kill-x": {}}):
            with self.assertRaises(fixes.FixError) as err:
                fixes.verify("persistent-database")
            self.assertIn("a fault is active", str(err.exception))
        with self.assertRaises(fixes.FixError):
            fixes.verify("rm -rf /")

    def test_apply_and_revert_use_the_override_and_restore_the_base_stack(self):
        calls = []
        with patch.object(fixes, "_compose", side_effect=lambda *a, override=None, **k: calls.append((a, override))), \
                patch.object(fixes, "docker", return_value="") as docker, \
                patch.object(fixes, "_quiet"), patch.object(fixes, "wait_until_settled", return_value=20.0), \
                patch.object(fixes, "is_applied", side_effect=[True, False]):
            fixes.apply(FIX, say=lambda m: None)
            self.assertIn("persistent-database", fixes.active())
            fixes.revert(FIX, say=lambda m: None)
            self.assertEqual(fixes.active(), {})
        (up_args, override), (down_args, base) = calls
        self.assertEqual(override.name, "persistent-database.compose.yml")
        self.assertEqual(up_args[-3:], ("mysql-server", "customers-service", "visits-service"))
        self.assertIsNone(base, "revert recreates from the base Compose file only")
        self.assertIn("--force-recreate", down_args)
        self.assertEqual(down_args[-2:], ("customers-service", "visits-service"))
        removed = [c.args for c in docker.call_args_list]
        self.assertIn(("rm", "-f", "mysql-server"), removed)
        self.assertIn(("volume", "rm", "spring-petclinic-microservices_chaoslab-petclinic-mysql"), removed)

    def test_a_fix_that_does_not_take_effect_is_an_error(self):
        with patch.object(fixes, "_compose"), patch.object(fixes, "_quiet"), \
                patch.object(fixes, "is_applied", return_value=False):
            with self.assertRaises(fixes.FixError):
                fixes.apply(FIX, say=lambda m: None)
        self.assertEqual(fixes.active(), {})

    def test_before_is_the_latest_run_without_a_fix(self):
        self.write("20261010-163855-kill-visits-service", run_result("a"))
        self.write("20261010-164228-kill-visits-service", run_result("b"))
        self.write("20261011-190000-kill-visits-service", run_result("c", data_loss=False, plan_fix="persistent-database"))
        self.write("20261011-191000-kill-visits-service", run_result("d", outcome="refused"))
        self.assertEqual(fixes._latest_run("kill", "visits-service")["id"], "b")
        self.assertEqual(fixes._latest_run("kill", "visits-service", fixed=True)["id"], "c")

    def test_verified_only_when_the_weakness_was_there_before_and_gone_after(self):
        before = run_result("20261010-164228-kill-visits-service")
        self.write(before["id"], before)
        after = run_result("20261011-190000-kill-visits-service", data_loss=False, plan_fix="persistent-database")
        steps = []
        with patch.object(fixes, "_quiet"), patch.object(fixes, "is_applied", return_value=False), \
                patch.object(fixes, "_snapshot", return_value={"nodes": [{"id": "visits-service", "auto_restart": False}]}), \
                patch.object(fixes, "apply", side_effect=lambda f, say: steps.append("apply")), \
                patch.object(fixes, "revert", side_effect=lambda f, say: steps.append("revert")), \
                patch.object(fixes, "_run", return_value=after), patch.object(fixes.analyst, "save"):
            record = fixes.verify("persistent-database", say=lambda m: None)
        self.assertEqual(steps, ["apply", "revert"])
        self.assertTrue(record["verified"])
        self.assertIn("data_loss", record["resolved"])
        self.assertEqual((record["before"]["data_loss_visits"], record["after"]["data_loss_visits"]), (1, 0))
        self.assertTrue(record["summary"].startswith("Verified"))
        self.assertTrue((fixes.VERIFY_DIR / f"{record['id']}.md").exists())

    def test_revert_still_happens_when_the_after_run_fails(self):
        self.write("20261010-164228-kill-visits-service", run_result("b"))
        steps = []
        with patch.object(fixes, "_quiet"), patch.object(fixes, "is_applied", return_value=False), \
                patch.object(fixes, "_snapshot", return_value=None), \
                patch.object(fixes, "apply", side_effect=lambda f, say: steps.append("apply")), \
                patch.object(fixes, "revert", side_effect=lambda f, say: steps.append("revert")), \
                patch.object(fixes, "_run", side_effect=fixes.FixError("refused twice")):
            with self.assertRaises(fixes.FixError):
                fixes.verify("persistent-database", say=lambda m: None)
        self.assertEqual(steps, ["apply", "revert"])


if __name__ == "__main__":
    unittest.main()
