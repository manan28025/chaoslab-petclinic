import json
import tempfile
import threading
import unittest
import unittest.mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from chaoslab import bench


def write_run(root: Path, run_id: str, **result) -> None:
    run = root / run_id
    run.mkdir()
    base = {"fault": {"kind": "kill", "target": "visits-service"}, "outcome": "aborted",
            "recovered": True, "recovery_s": 13.1, "timeline": [{"elapsed": 1, "phase": "fault", "rps": 0.4}],
            "semantic_observations": [{"phase": "fault", "status": 200, "correctness": "degraded"}],
            "prediction_comparison": {"owner details": {"match": "matched"}, "x": {"match": "contradicted"}}}
    (run / "result.json").write_text(json.dumps({**base, **result}))
    (run / "events.jsonl").write_text(json.dumps({"elapsed": 15.5, "kind": "inject", "message": "crash"}) + "\n")


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runs = Path(self.tmp.name)
        patcher = patch.object(bench, "RUNS_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_runs_are_summarised_with_flags_and_totals(self):
        write_run(self.runs, "20261010-164228-kill-visits-service",
                  data_loss={"missing_visit_ids": [76], "canary_lost": True})
        write_run(self.runs, "20261010-135013-cpu-customers-service",
                  fault={"kind": "cpu", "target": "customers-service"}, outcome="completed", recovery_s=1.0)
        data = bench.runs()
        self.assertEqual([r["id"] for r in data["runs"]],
                         ["20261010-164228-kill-visits-service", "20261010-135013-cpu-customers-service"])
        newest = data["runs"][0]
        self.assertTrue(newest["silent_degradation"])
        self.assertEqual(newest["predictions"], {"matched": 1, "contradicted": 1, "unobserved": 0})
        self.assertEqual(data["totals"]["attack_types"], ["cpu", "kill"])
        self.assertEqual(data["totals"]["data_loss_runs"], 1)
        self.assertEqual(data["totals"]["median_recovery_s"], 7.0)

    def test_run_detail_only_serves_runs_inside_the_runs_folder(self):
        write_run(self.runs, "20261010-164228-kill-visits-service")
        detail = bench.run_detail("20261010-164228-kill-visits-service")
        self.assertEqual(detail["events"][0]["kind"], "inject")
        self.assertEqual(detail["analysis"]["run_id"], "20261010-164228-kill-visits-service")
        self.assertTrue((self.runs / "20261010-164228-kill-visits-service" / "analysis.json").exists(),
                        "the analysis is saved next to the run it was read from")
        for bad in ("../secrets", "20261010-164228-kill-visits-service/../../x", "active-faults.json", ""):
            self.assertIsNone(bench.run_detail(bad))

    def test_attack_menu_explains_every_fault(self):
        attacks = bench.attacks()
        self.assertEqual({a["kind"] for a in attacks}, {"kill", "pause", "delay", "loss", "cpu", "spike"})
        self.assertTrue(all(a["mechanism"] and a["undo"] for a in attacks))

    def test_kill_switch_needs_its_header_and_server_stays_local(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bench.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch.object(bench.safety, "kill_switch") as kill_switch:
            with self.assertRaises(HTTPError) as refused:
                urlopen(Request(f"{base}/api/stop", data=b"", method="POST"), timeout=5)
            self.assertEqual(refused.exception.code, 404)
            kill_switch.assert_not_called()
            kill_switch.return_value = True
            with urlopen(Request(f"{base}/api/stop", data=b"", method="POST",
                                 headers={bench.KILL_SWITCH_HEADER: "1"}), timeout=5) as response:
                self.assertTrue(json.load(response)["clean"])
            kill_switch.assert_called_once()
        with self.assertRaises(HTTPError) as missing:
            urlopen(f"{base}/api/runs/..%2F..%2Fetc", timeout=5)
        self.assertEqual(missing.exception.code, 404)
        self.assertEqual(bench.HOST, "127.0.0.1")

    def test_launch_refuses_anything_outside_the_limits(self):
        allowed = {"visits-service": "running", "api-gateway": "running"}
        bad = [
            {"kind": "nuke", "target": "visits-service"},
            {"kind": "kill", "target": "load-generator"},
            {"kind": "kill", "target": "visits-service", "duration_s": 600},
            {"kind": "kill", "target": "visits-service", "duration_s": True},
            {"kind": "kill", "target": "visits-service", "params": {"delay_ms": 500}},
            {"kind": "delay", "target": "visits-service", "params": {"delay_ms": 9000}},
            {"kind": "delay", "target": "visits-service", "params": {"delay_ms": "500; rm -rf /"}},
            {"kind": "spike", "target": "visits-service", "params": {"multiplier": 5}},
        ]
        with patch.object(bench, "targets", return_value=allowed), \
                patch.object(bench.subprocess, "Popen") as popen:
            for request in bad:
                with self.subTest(request=request), self.assertRaises(bench.LaunchError):
                    bench.launch(request)
            popen.assert_not_called()

    def test_launch_runs_the_cli_once_and_refuses_a_second_run(self):
        self.addCleanup(bench._launched.clear)
        proc = unittest.mock.Mock(pid=4242)
        proc.poll.return_value = None   # still running
        with patch.object(bench, "targets", return_value={"visits-service": "running"}), \
                patch.object(bench, "LAUNCH_LOG", self.runs / "launch.log"), \
                patch.object(bench.safety, "active_faults", return_value={}), \
                patch.object(bench.subprocess, "Popen", return_value=proc) as popen:
            started = bench.launch({"kind": "delay", "target": "visits-service", "duration_s": 30,
                                        "params": {"delay_ms": 600}})
            command = popen.call_args.args[0]
            self.assertEqual(command[1:], ["-m", "chaoslab", "run", "delay", "visits-service",
                                           "--duration", "30", "--delay-ms", "600"])
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(started["pid"], 4242)
            with self.assertRaises(bench.Busy):
                bench.launch({"kind": "kill", "target": "visits-service"})
            self.assertEqual(popen.call_count, 1)

    def test_live_run_is_the_newest_run_still_being_written(self):
        run = self.runs / "20261011-101500-kill-visits-service"
        run.mkdir()
        (run / "events.jsonl").write_text(json.dumps({"elapsed": 0.0, "kind": "start", "message": "crash for up to 30s",
                                                     "fault": {"kind": "kill", "target": "visits-service"}}) + "\n")
        (run / "timeline.jsonl").write_text(json.dumps({"elapsed": 1.0, "phase": "baseline", "rps": 5.0}) + "\n{partial")
        live = bench.live()["run"]
        self.assertEqual(live["id"], run.name)
        self.assertEqual(live["fault"]["target"], "visits-service")
        self.assertEqual([s["rps"] for s in live["timeline"]], [5.0])
        (run / "result.json").write_text(json.dumps({"outcome": "completed", "recovery_pending": True}))
        self.assertIsNotNone(bench.live()["run"])   # checkpoint: still recovering
        (run / "result.json").write_text(json.dumps({"outcome": "completed"}))
        self.assertIsNone(bench.live()["run"])

    def test_buttons_refuse_requests_that_did_not_come_from_this_machine(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bench.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with patch.object(bench, "launch") as launch:
            for headers in ({}, {bench.KILL_SWITCH_HEADER: "1", "Host": "attacker.example:8090"}):
                with self.subTest(headers=headers), self.assertRaises(HTTPError) as refused:
                    urlopen(Request(f"{base}/api/launch", data=b'{"kind": "kill"}', method="POST",
                                    headers=headers), timeout=5)
                self.assertEqual(refused.exception.code, 404)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
