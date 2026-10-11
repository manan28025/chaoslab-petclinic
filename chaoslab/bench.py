"""Resilience Test Bench: the live operator console for the sandbox.

    python3 -m chaoslab bench            # http://127.0.0.1:8090

It shows live traffic and container health, the dependency map with
blast-radius predictions, a test matrix of every attack tried, and a replay of
every experiment in runs/. Like the command line, it is an operator tool: it can
launch one guarded experiment at a time (the same `chaoslab run`, with all its
safety layers) and press the kill switch. It listens on this machine only.
The shareable, read-only page is `chaoslab dashboard`.
"""

import json
import re
import statistics
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from . import analyst, blast_radius, depmap, planner, safety
from .docker import docker, targets
from .faults import FAULTS
from .llm import from_env
from .settings import COMPOSE_PROJECT, LOCUST_URL, ROOT, RUNS_DIR

PAGE = Path(__file__).with_name("bench.html")
HOST = "127.0.0.1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
RUN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z]+-[a-z0-9-]+")
KILL_SWITCH_HEADER = "X-Chaoslab-Kill-Switch"   # a custom header blocks cross-site posts
LAUNCH_LOG = RUNS_DIR / "bench-launch.log"
DURATION_S = (10, 120)          # fault length a bench launch may ask for
LIVE_WINDOW_S = 20              # a run whose logs changed this recently is still going
CLI_FLAGS = {"delay_ms": "--delay-ms", "jitter_ms": "--jitter-ms", "loss_pct": "--loss-pct",
             "cpus": "--cpus", "multiplier": "--multiplier"}

_launch_lock = threading.Lock()
_launched: dict = {}            # the experiment the bench started: proc, request, started


class LaunchError(ValueError):
    """A launch request the bench refuses; the message says why."""


class Busy(LaunchError):
    """Another experiment or fault is already active."""


def _containers() -> list[dict]:
    rows = docker("ps", "--all", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                  "--format", '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}'
                  '\t{{.Label "chaoslab.target"}}', check=False)
    containers = []
    for line in rows.splitlines():
        name, state, status, target = (line.split("\t") + ["", "", ""])[:4]
        health = next((h for h in ("unhealthy", "healthy", "starting") if f"({h}" in status
                       or f"health: {h}" in status), None)
        containers.append({"name": name, "state": state, "status": status,
                           "health": health, "target": target == "true"})
    return sorted(containers, key=lambda c: (not c["target"], c["name"]))


def _traffic() -> dict:
    try:
        with urlopen(f"{LOCUST_URL}/stats/requests", timeout=2) as resp:
            data = json.load(resp)
    except Exception as err:
        return {"available": False, "error": str(err)}
    rps = data.get("current_rps") or 0.0
    fails = data.get("current_fail_per_sec") or 0.0
    actions = {s["name"]: {"rps": round(s.get("current_rps") or 0.0, 2),
                           "fail_per_s": round(s.get("current_fail_per_sec") or 0.0, 2)}
               for s in data.get("stats", []) if s.get("name") != "Aggregated"}
    return {"available": True, "state": data.get("state"), "users": data.get("user_count", 0),
            "rps": round(rps, 2), "error_rate": round(fails / rps, 4) if rps else 0.0,
            "p95_ms": (data.get("current_response_time_percentiles") or {})
            .get("response_time_percentile_0.95"), "actions": actions}


def model_state() -> dict:
    llm = from_env()
    available = llm.available()
    return {"available": available, "model": llm.describe(), "reason": "" if available else llm.last_error}


def status() -> dict:
    return {"containers": _containers(), "faults": safety.active_faults(), "traffic": _traffic(),
            "now": time.time(), "model": model_state()}


def attacks() -> list[dict]:
    return [{"kind": kind, "summary": fault.summary, "mechanism": fault.mechanism, "undo": fault.undo,
             "limits": {name: list(bounds) for name, bounds in fault.limits.items()
                        if name != "expire_after_s"}}
            for kind, fault in FAULTS.items()]


def _run_dirs() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    return sorted((d for d in RUNS_DIR.iterdir()
                   if d.is_dir() and RUN_ID.fullmatch(d.name) and (d / "result.json").exists()),
                  key=lambda d: d.name, reverse=True)


def _summary(run: Path) -> dict:
    r = json.loads((run / "result.json").read_text())
    comparison = r.get("prediction_comparison") or {}
    matches = [c.get("match") for c in comparison.values()]
    timeline = r.get("timeline", [])
    observations = r.get("semantic_observations") or []
    return {
        "id": run.name, "fault": r.get("fault", {}), "outcome": r.get("outcome"),
        "fix": (r.get("plan") or {}).get("fix"), "planned_by": (r.get("plan") or {}).get("planned_by"),
        "abort_reason": r.get("abort_reason"), "fault_active_s": r.get("fault_active_s"),
        "time_to_first_breach_s": r.get("time_to_first_breach_s"),
        "recovered": r.get("recovered"), "recovery_s": r.get("recovery_s"),
        "pending": bool(r.get("recovery_pending")),
        "peak_error_rate": r.get("peak_error_rate"), "peak_p95_ms": r.get("peak_p95_ms"),
        "min_fault_rps": min((s["rps"] for s in timeline if s.get("phase") == "fault"), default=None),
        "baseline": {k: (r.get("baseline") or {}).get(k) for k in ("rps", "error_rate", "p95_ms")},
        "data_loss": r.get("data_loss"),
        "silent_degradation": any(o.get("status") == 200 and o.get("correctness") == "degraded"
                                  for o in observations),
        "predictions": {"matched": matches.count("matched"), "contradicted": matches.count("contradicted"),
                        "unobserved": matches.count("unobserved")},
    }


def runs() -> dict:
    summaries = []
    for run in _run_dirs():
        try:
            summaries.append(_summary(run))
        except (OSError, ValueError, KeyError):
            continue  # a run still being written, or an unreadable one
    done = [s for s in summaries if not s["pending"] and s["outcome"] != "refused"]   # refused never ran
    recoveries = [s["recovery_s"] for s in done if s["recovery_s"] is not None]
    judged = sum(s["predictions"]["matched"] + s["predictions"]["contradicted"] for s in done)
    return {"runs": summaries, "totals": {
        "experiments": len(done),
        "attack_types": sorted({s["fault"].get("kind") for s in done if s["fault"].get("kind")}),
        "aborted": sum(1 for s in done if s["outcome"] == "aborted"),
        "median_recovery_s": round(statistics.median(recoveries), 1) if recoveries else None,
        "predictions_matched": sum(s["predictions"]["matched"] for s in done),
        "predictions_judged": judged,
        "data_loss_runs": sum(1 for s in done if s["data_loss"]),
    }}


def _events(run: Path) -> list[dict]:
    events = []
    log = run / "events.jsonl"
    if log.exists():
        for line in log.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            events.append({k: event.get(k) for k in ("elapsed", "kind", "message")}
                          | {k: event[k] for k in ("fault", "guardrails", "rps") if k in event})
    return events


def run_detail(run_id: str) -> dict | None:
    run = RUNS_DIR / run_id
    if not RUN_ID.fullmatch(run_id) or run not in _run_dirs():
        return None
    detail = json.loads((run / "result.json").read_text())
    detail["events"] = _events(run)
    detail["analysis"] = _analysis(run, detail)
    return detail


def _analysis(run: Path, result: dict) -> dict | None:
    """The saved analysis, or one computed by code now (no model) for runs that predate the analyst."""
    if result.get("recovery_pending"):
        return None
    saved = run / "analysis.json"
    if saved.exists():
        try:
            return json.loads(saved.read_text())
        except ValueError:
            pass
    result.setdefault("id", run.name)
    try:
        analysis = analyst.analyze(result, _snapshot())
    except (KeyError, TypeError, ValueError) as err:   # an unexpected record must not break the page
        return {"error": f"could not analyse this run: {type(err).__name__}: {err}"}
    analyst.save(analysis, run.parent)   # next to the run it was read from
    return analysis


def _snapshot() -> dict | None:
    try:
        return depmap.load()
    except (FileNotFoundError, ValueError):
        return None


def report() -> dict:
    snapshot = _snapshot()
    if snapshot is None:
        raise FileNotFoundError("no dependency map")
    analyses = analyst.load_all(snapshot=snapshot)
    services = [n["id"] for n in snapshot["nodes"] if n.get("kind") == "container" and n.get("active") is not False]
    from . import fixes
    return analyst.report(analyses, services, list(FAULTS), snapshot["entry_point"], fixes.load_verifications())


def make_plan(request: dict) -> dict:
    snapshot = _snapshot()
    if snapshot is None:
        raise LaunchError("no dependency map yet: run `python3 -m chaoslab map`")
    llm = from_env()
    use_ai = bool(request.get("use_ai", True)) and llm.available()
    record = planner.plan(snapshot, set(targets()), llm=llm if use_ai else None)
    if record is None:
        raise LaunchError("nothing on the allowlist can be planned")
    if not use_ai:
        record["notes"].insert(0, "rules only: " + ("you chose not to ask the language model" if llm.available()
                                                    else f"no language model ({llm.last_error})"))
    planner.save(record)
    return record


def _live_run() -> Path | None:
    """The newest run, while it is still being written."""
    if not RUNS_DIR.exists():
        return None
    newest = max((d for d in RUNS_DIR.iterdir() if d.is_dir() and RUN_ID.fullmatch(d.name)),
                 key=lambda d: d.name, default=None)
    if newest is None:
        return None
    result = newest / "result.json"
    if result.exists():
        try:
            if not json.loads(result.read_text()).get("recovery_pending"):
                return None   # finished
        except ValueError:
            pass              # being rewritten right now
    touched = max((p.stat().st_mtime for p in (newest / "events.jsonl", newest / "timeline.jsonl")
                   if p.exists()), default=0)
    return newest if time.time() - touched <= LIVE_WINDOW_S else None


def _launcher() -> dict | None:
    if not _launched:
        return None
    code = _launched["proc"].poll()
    output = []
    if code is not None and LAUNCH_LOG.exists():
        output = LAUNCH_LOG.read_text(errors="replace").splitlines()[-8:]
    return {**_launched["request"], "started": _launched["started"], "running": code is None,
            "exit_code": code, "output": output}


def live() -> dict:
    run = _live_run()
    detail = None
    if run is not None:
        timeline = []
        path = run / "timeline.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    timeline.append(json.loads(line))
                except ValueError:
                    continue   # the line being written
        events = _events(run)
        start = next((e for e in events if e["kind"] == "start"), {})
        detail = {"id": run.name, "fault": start.get("fault"), "events": events, "timeline": timeline}
    return {"run": detail, "launch": _launcher()}


def _busy() -> str | None:
    if _launched and _launched["proc"].poll() is None:
        return "the experiment launched from the bench is still running"
    if _live_run() is not None:
        return "an experiment is already running"
    if safety.active_faults():
        return "a fault is still active; use the kill switch first"
    return None


def launch(request: dict) -> dict:
    """Start one guarded experiment, exactly as `python3 -m chaoslab run` would, or a saved plan
    exactly as `python3 -m chaoslab execute` would."""
    if "plan_id" in request:
        return _launch_plan(str(request["plan_id"]), request.get("approved") is True)
    kind, target = request.get("kind"), request.get("target")
    if kind not in FAULTS:
        raise LaunchError(f"unknown attack {kind!r}; choose one of {', '.join(FAULTS)}")
    allowed = targets()
    if target not in allowed:
        raise LaunchError(f"{target!r} is not on the allowlist; choose one of {', '.join(sorted(allowed))}")
    duration = request.get("duration_s", 30)
    if isinstance(duration, bool) or not isinstance(duration, int) or not DURATION_S[0] <= duration <= DURATION_S[1]:
        raise LaunchError(f"duration_s must be a whole number from {DURATION_S[0]} to {DURATION_S[1]}")
    params = request.get("params") or {}
    fault_type = FAULTS[kind]
    unknown = set(params) - (set(fault_type.limits) - {"expire_after_s"})
    if unknown:
        raise LaunchError(f"{kind} does not take {', '.join(sorted(unknown))}")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in params.values()):
        raise LaunchError("attack settings must be numbers")
    check = dict(params)
    if "expire_after_s" in fault_type.limits:
        check["expire_after_s"] = duration + safety.Guardrails.expiry_grace_s
    problems = fault_type(target, **check).problems()
    if problems:
        raise LaunchError("; ".join(problems))

    command = [sys.executable, "-m", "chaoslab", "run", kind, target, "--duration", str(duration)]
    for name, value in sorted(params.items()):
        command += [CLI_FLAGS[name], str(value)]
    return _start(command, {"kind": kind, "target": target, "duration_s": duration, "params": params})


def _launch_plan(plan_id: str, approved: bool) -> dict:
    try:
        record = planner.load(plan_id)
    except ValueError as err:
        raise LaunchError(str(err)) from err
    if record["target"] not in targets():
        raise LaunchError(f"{record['target']!r} is no longer on the allowlist")
    if record["requires_approval"] and not approved:
        raise LaunchError(f"this plan needs your approval first: {record['approval_reason']}")
    command = [sys.executable, "-m", "chaoslab", "execute", plan_id] + (["--approve-risky"] if approved else [])
    return _start(command, {"kind": record["kind"], "target": record["target"], "duration_s": record["duration_s"],
                            "params": record["params"], "plan_id": plan_id})


def _start(command: list[str], request: dict) -> dict:
    with _launch_lock:
        reason = _busy()
        if reason:
            raise Busy(reason)
        RUNS_DIR.mkdir(exist_ok=True)
        with LAUNCH_LOG.open("w") as log:
            # Its own session: stopping the bench must not interrupt a fault mid-way;
            # the experiment, watchdog, kill switch and dead-man's switch still apply.
            proc = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        _launched.clear()
        _launched.update(proc=proc, started=time.time(), request=request)
    shown = ["python3", *command[1:]]
    return {"started": True, "pid": proc.pid, "command": " ".join(shown)}


class Handler(BaseHTTPRequestHandler):
    server_version = "chaoslab-bench"

    def log_message(self, *args) -> None:   # keep the terminal quiet
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = HTTPStatus.OK) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/status":
                self._json(status())
            elif url.path == "/api/attacks":
                self._json(attacks())
            elif url.path == "/api/map":
                self._json(depmap.load())
            elif url.path == "/api/impact":
                self._json(blast_radius.analyze(depmap.load(), query.get("target", [""])[0],
                                                query.get("failure", ["kill"])[0]))
            elif url.path == "/api/runs":
                self._json(runs())
            elif url.path == "/api/live":
                self._json(live())
            elif url.path == "/api/report":
                self._json(report())
            elif url.path.startswith("/api/runs/"):
                detail = run_detail(url.path.removeprefix("/api/runs/"))
                if detail is None:
                    self._json({"error": "unknown run"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(detail)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except FileNotFoundError:
            self._json({"error": "no dependency map yet: run `python3 -m chaoslab map`"}, HTTPStatus.NOT_FOUND)
        except ValueError as err:
            self._json({"error": str(err)}, HTTPStatus.BAD_REQUEST)
        except Exception as err:   # report, never crash the server
            self._json({"error": f"{type(err).__name__}: {err}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _from_this_machine(self) -> bool:
        """Only this page may press buttons: custom header (no cross-site forms) and a
        local Host header (no DNS rebinding)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return self.headers.get(KILL_SWITCH_HEADER) == "1" and host in LOCAL_HOSTS

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/stop", "/api/launch", "/api/plan") or not self._from_this_machine():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if path == "/api/stop":
            messages: list[str] = []
            clean = safety.kill_switch(say=messages.append)
            self._json({"clean": clean, "messages": messages})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= 4096:
                raise LaunchError("send the attack as a small JSON body")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise LaunchError("send the attack as a JSON object")
            self._json(launch(request) if path == "/api/launch" else make_plan(request))
        except Busy as err:
            self._json({"error": str(err)}, HTTPStatus.CONFLICT)
        except (LaunchError, ValueError) as err:
            self._json({"error": str(err)}, HTTPStatus.BAD_REQUEST)
        except Exception as err:
            self._json({"error": f"{type(err).__name__}: {err}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def serve(port: int = 8090) -> None:
    server = ThreadingHTTPServer((HOST, port), Handler)
    print(f"Resilience Test Bench on http://{HOST}:{port}  (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
