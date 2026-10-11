"""Fix and verify: apply a recommended fix to the sandbox, rerun the experiment that exposed the
weakness, compare before and after, then put the sandbox back.

    python3 -m chaoslab fixes                          # fixes that can be applied and verified
    python3 -m chaoslab verify persistent-database     # apply, rerun, compare, revert
    python3 -m chaoslab verify persistent-database --keep
    python3 -m chaoslab revert persistent-database     # undo a kept fix

A fix is a Docker Compose override in fixes/: plain, reviewable configuration that changes only
the services it names. Only fixes listed here can be applied, never while a fault or an
experiment is active, and the sandbox is restored afterwards unless --keep is given.
"""

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import urlopen

from . import analyst, depmap, experiment, safety
from .docker import DockerError, docker
from .faults import FAULTS
from .settings import APP_DIR, COMPOSE_FILE, LOCUST_URL, ROOT, RUNS_DIR

FIXES_DIR = ROOT / "fixes"
VERIFY_DIR = RUNS_DIR / "verifications"
STATE_FILE = RUNS_DIR / "active-fixes.json"
STEADY_S = 15            # healthy traffic this long in a row counts as settled
SETTLE_TIMEOUT_S = 300


@dataclass(frozen=True)
class Fix:
    id: str
    title: str
    why: str
    override: str                       # Compose override file in fixes/
    services: tuple[str, ...]           # services the override recreates, in start order
    added: tuple[str, ...]              # containers that exist only while the fix is applied
    volumes: tuple[str, ...]            # volumes removed on revert, so the next apply starts clean
    addresses: tuple[str, ...]          # analyst finding codes this fix should remove
    experiment: dict                    # the experiment that exposed the weakness
    expect: str
    check_env: dict = field(default_factory=dict)   # container -> environment entry proving it is applied


FIXES = {
    "persistent-database": Fix(
        id="persistent-database",
        title="Move customers and visits from in-memory HSQLDB to a persistent MySQL database",
        why="A crash of visits-service lost every visit written since it started, including the run's canary.",
        override="persistent-database.compose.yml",
        services=("mysql-server", "customers-service", "visits-service"),
        added=("mysql-server",),
        volumes=("chaoslab-petclinic-mysql",),
        addresses=("data_loss",),
        experiment={"kind": "kill", "target": "visits-service"},
        expect="the canary visit and every visit written before the crash are still there after the restart",
        check_env={"visits-service": "SPRING_PROFILES_ACTIVE=docker,mysql",
                   "customers-service": "SPRING_PROFILES_ACTIVE=docker,mysql"},
    ),
}


class FixError(RuntimeError):
    pass


# ------------------------------------------------------------------ state
def active() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _remember(fix_id: str, applied: bool) -> None:
    state = active()
    if applied:
        state[fix_id] = {"applied_at": time.time()}
    else:
        state.pop(fix_id, None)
    RUNS_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------ compose
def _compose(*args: str, override: Path | None = None, timeout: float = 600) -> str:
    files = ["-f", str(COMPOSE_FILE)] + (["-f", str(override)] if override else [])
    proc = subprocess.run(["docker", "compose", *files, *args], cwd=APP_DIR, capture_output=True,
                          text=True, timeout=timeout)
    if proc.returncode != 0:
        raise FixError(f"docker compose {' '.join(args)}: {(proc.stderr or proc.stdout).strip()[-800:]}")
    return proc.stdout


def _project() -> str:
    return APP_DIR.name


def is_applied(fix: Fix) -> bool:
    for container, entry in fix.check_env.items():
        env = docker("inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container, check=False)
        if entry not in env.splitlines():
            return False
    return True


def _quiet() -> None:
    if safety.active_faults():
        raise FixError("a fault is active; wait for it to end or press the kill switch")
    from .bench import _live_run   # the same "experiment still being written" rule the bench uses
    if _live_run() is not None:
        raise FixError("an experiment is running; wait for it to finish")


def wait_until_settled(say=print, timeout_s: float = SETTLE_TIMEOUT_S) -> float:
    """Seconds until user traffic was healthy for STEADY_S in a row."""
    start, healthy_since = time.time(), None
    while time.time() - start < timeout_s:
        try:
            with urlopen(f"{LOCUST_URL}/stats/requests", timeout=3) as resp:
                data = json.load(resp)
            rps = data.get("current_rps") or 0
            fails = data.get("current_fail_per_sec") or 0
            ok = rps > 0.5 and fails / rps <= 0.02
        except (OSError, ValueError):
            ok = False
        now = time.time()
        if not ok:
            healthy_since = None
        elif healthy_since is None:
            healthy_since = now
        elif now - healthy_since >= STEADY_S:
            return round(now - start, 1)
        time.sleep(1)
    raise FixError(f"user traffic did not settle within {timeout_s:.0f} s")


def apply(fix: Fix, say=print) -> None:
    _quiet()
    override = FIXES_DIR / fix.override
    say(f"applying fix {fix.id}: recreating {', '.join(fix.services)} with {override.relative_to(ROOT)}")
    _compose("up", "-d", "--wait", "--wait-timeout", "420", *fix.services, override=override)
    if not is_applied(fix):
        raise FixError(f"{fix.id} did not take effect: {fix.check_env}")
    _remember(fix.id, True)
    say(f"fix applied; waiting for user traffic to settle (healthy for {STEADY_S} s)")
    say(f"settled after {wait_until_settled(say)} s")


def revert(fix: Fix, say=print) -> None:
    _quiet()
    restored = [s for s in fix.services if s not in fix.added]
    say(f"reverting fix {fix.id}: recreating {', '.join(restored)} from the base Compose file")
    _compose("up", "-d", "--wait", "--wait-timeout", "420", "--force-recreate", "--no-deps", *restored)
    for container in fix.added:
        docker("rm", "-f", container, check=False)
    for volume in fix.volumes:
        docker("volume", "rm", f"{_project()}_{volume}", check=False)
    if is_applied(fix):
        raise FixError(f"{fix.id} is still in effect after revert")
    _remember(fix.id, False)
    say(f"fix reverted; waiting for user traffic to settle (healthy for {STEADY_S} s)")
    say(f"settled after {wait_until_settled(say)} s")


# ------------------------------------------------------------------ verify
def _latest_run(kind: str, target: str, fixed: bool = False) -> dict | None:
    """The newest finished run of this experiment that did not run with a fix applied."""
    for path in sorted(RUNS_DIR.glob(f"*-{kind}-{target}/result.json"), reverse=True):
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if result.get("outcome") in (None, "refused") or result.get("recovery_pending"):
            continue
        if bool((result.get("plan") or {}).get("fix")) == fixed:
            result.setdefault("id", path.parent.name)
            return result
    return None


def _snapshot() -> dict | None:
    try:
        return depmap.load()
    except (FileNotFoundError, ValueError):
        return None


def _summary(result: dict, analysis: dict) -> dict:
    loss = result.get("data_loss") or {}
    return {"run_id": result.get("id"), "verdict": analysis["verdict"], "risk": analysis["risk"],
            "findings": [f["code"] for f in analysis["findings"] if f["severity"] != "info"],
            "data_loss_visits": len(loss.get("missing_visit_ids") or []), "canary_lost": bool(loss.get("canary_lost")),
            "peak_error_rate": result.get("peak_error_rate"), "peak_p95_ms": result.get("peak_p95_ms"),
            "recovered": result.get("recovered"), "recovery_s": result.get("recovery_s")}


def _run(fix: Fix, duration_s: int, say=print, with_fix: bool = True) -> dict:
    kind, target = fix.experiment["kind"], fix.experiment["target"]
    guard = safety.Guardrails()
    params = dict(fix.experiment.get("params") or {})
    if "expire_after_s" in FAULTS[kind].limits:
        params["expire_after_s"] = duration_s + guard.expiry_grace_s
    plan = {"id": f"verify-{fix.id}", "planned_by": "fix verification" if with_fix else "fix verification (before)",
            "model": None, "rationale": f"{'after' if with_fix else 'before'} applying fix {fix.id}: {fix.title}",
            "hypothesis": None, "fix": fix.id if with_fix else None, "requires_approval": False,
            "approval_reason": "", "approved": True}
    for attempt in range(2):
        try:
            return experiment.run(FAULTS[kind](target, **params), guard, duration_s, plan=plan)
        except safety.SafetyError as err:
            if attempt:
                raise FixError(f"experiment refused twice: {err}") from err
            say(f"refused ({err}); waiting for traffic to settle and trying once more")
            wait_until_settled(say)
    raise FixError("unreachable")


def verify(fix_id: str, keep: bool = False, fresh_before: bool = False, duration_s: int = 30,
           say=print) -> dict:
    fix = FIXES.get(fix_id)
    if fix is None:
        raise FixError(f"unknown fix {fix_id!r}; available: {', '.join(FIXES)}")
    if not (FIXES_DIR / fix.override).exists():
        raise FixError(f"missing {fix.override} in {FIXES_DIR}")
    _quiet()
    if is_applied(fix):
        raise FixError(f"{fix.id} is already applied; revert it first so the before run is honest")
    snapshot = _snapshot()
    notes = []
    before = None if fresh_before else _latest_run(fix.experiment["kind"], fix.experiment["target"])
    if before is None:
        say("running the experiment without the fix first, for an honest comparison")
        before = _run(fix, duration_s, say, with_fix=False)
    else:
        notes.append(f"before: the most recent run of this experiment without the fix, {before['id']}")
    before_analysis = analyst.analyze(before, snapshot)
    record = {"id": f"{time.strftime('%Y%m%d-%H%M%S')}-{fix.id}", "fix": {
        "id": fix.id, "title": fix.title, "why": fix.why, "override": f"fixes/{fix.override}",
        "services": list(fix.services), "addresses": list(fix.addresses), "expect": fix.expect},
        "experiment": {**fix.experiment, "duration_s": duration_s}, "notes": notes, "kept": keep}
    applied = False
    try:
        apply(fix, say)
        applied = True
        record["applied_at"] = time.time()
        after = _run(fix, duration_s, say)
        after_analysis = analyst.analyze(after, snapshot)
        analyst.save(after_analysis)
    finally:
        if applied and not keep:
            revert(fix, say)
            record["reverted_at"] = time.time()
    b, a = _summary(before, before_analysis), _summary(after, after_analysis)
    gone = [c for c in b["findings"] if c not in a["findings"]]
    new = [c for c in a["findings"] if c not in b["findings"]]
    if "prediction_wrong" in new:   # the map describes the system without the fix, so the fixed run differs
        new.remove("prediction_wrong")
        notes.append("the dependency model still describes the system without the fix, so its prediction "
                     "did not match the run with the fix; that is expected, not a new weakness")
    record.update({
        "before": b, "after": a, "resolved": gone,
        "remaining": [c for c in a["findings"] if c in b["findings"]], "new": new,
        "verified": all(c in b["findings"] and c not in a["findings"] for c in fix.addresses),
    })
    record["summary"] = _sentence(fix, record)
    VERIFY_DIR.mkdir(parents=True, exist_ok=True)
    (VERIFY_DIR / f"{record['id']}.json").write_text(json.dumps(record, indent=2, default=str))
    (VERIFY_DIR / f"{record['id']}.md").write_text(markdown(record))
    return record


def _sentence(fix: Fix, r: dict) -> str:
    b, a = r["before"], r["after"]
    risk = lambda s: f"{s['risk']['score']}/10 {s['risk']['level']}" if s["risk"] else "n/a"   # noqa: E731
    if r["verified"]:
        head = f"Verified: the fix removed {', '.join(fix.addresses)}."
    elif not all(c in b["findings"] for c in fix.addresses):
        head = f"Inconclusive: the run before the fix did not show {', '.join(fix.addresses)}."
    else:
        head = f"Not verified: {', '.join(c for c in fix.addresses if c in a['findings'])} is still there with the fix."
    return (f"{head} Risk {risk(b)} before, {risk(a)} after; visits lost {b['data_loss_visits']} before, "
            f"{a['data_loss_visits']} after; recovery {b['recovery_s']} s before, {a['recovery_s']} s after.")


def markdown(r: dict) -> str:
    b, a = r["before"], r["after"]
    rows = [("Verdict", b["verdict"], a["verdict"]),
            ("Risk", f"{b['risk']['score']}/10 {b['risk']['level']}" if b["risk"] else "–",
             f"{a['risk']['score']}/10 {a['risk']['level']}" if a["risk"] else "–"),
            ("Visits lost", b["data_loss_visits"], a["data_loss_visits"]),
            ("Canary lost", "yes" if b["canary_lost"] else "no", "yes" if a["canary_lost"] else "no"),
            ("Peak errors", f"{(b['peak_error_rate'] or 0):.0%}", f"{(a['peak_error_rate'] or 0):.0%}"),
            ("Peak p95", f"{b['peak_p95_ms']} ms", f"{a['peak_p95_ms']} ms"),
            ("Recovery", f"{b['recovery_s']} s", f"{a['recovery_s']} s"),
            ("Findings", ", ".join(b["findings"]) or "none", ", ".join(a["findings"]) or "none")]
    lines = [f"# Fix verification: {r['fix']['title']}", "", r["summary"], "",
             f"Experiment: `{r['experiment']['kind']}` on `{r['experiment']['target']}` for up to "
             f"{r['experiment']['duration_s']} s. Fix: `{r['fix']['override']}` "
             f"({'kept' if r['kept'] else 'applied for the run, then reverted'}).", "",
             f"| | Before (`{b['run_id']}`) | After (`{a['run_id']}`) |", "|---|---|---|"]
    lines += [f"| {k} | {x} | {y} |" for k, x, y in rows]
    lines += ["", f"Resolved: {', '.join(r['resolved']) or 'none'}. Still present: {', '.join(r['remaining']) or 'none'}. "
                  f"New: {', '.join(r['new']) or 'none'}."]
    lines += [f"- {n}" for n in r["notes"]]
    return "\n".join(lines) + "\n"


def load_verifications() -> list[dict]:
    out = []
    for path in sorted(VERIFY_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out
