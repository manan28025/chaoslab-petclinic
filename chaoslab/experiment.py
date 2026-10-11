"""One chaos experiment: check steady state, inject, watch, revert, measure recovery."""

import json
import signal
import statistics
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue

from . import blast_radius, depmap, semantic_probe
from .faults import Fault
from .metrics import LocustProbe, Sample, Snapshot, error_rates
from .safety import (Guardrails, SafetyError, active_faults, arm_dead_mans_switch,
                     preflight, register, unregister)
from .settings import ROOT, RUNS_DIR

TICK_S = 1.0
RECOVERY_HOLD_S = 10          # healthy this many seconds in a row counts as recovered
SOAK_S = 15                   # ...and must then stay healthy this much longer: a relapse resets the wait
MAX_BASELINE_ERROR = 0.05     # refuse to start unless the system is this healthy


class EventLog:
    """Prints progress and appends actions to the run's audit log."""

    def __init__(self, path: Path):
        self.path = path
        self.start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def show(self, message: str) -> None:
        print(f"[{self.elapsed():6.1f}s] {message}", flush=True)

    def __call__(self, kind: str, message: str, **data) -> None:
        self.show(f"{kind.upper():<8} {message}")
        record = {"time": time.time(), "elapsed": round(self.elapsed(), 1),
                  "kind": kind, "message": message, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")


def _fmt(s: Sample) -> str:
    err = "  n/a" if s.error_rate is None else f"{s.error_rate:5.0%}"
    p95 = "    n/a" if s.p95_ms is None else f"{s.p95_ms:5.0f}ms"
    failing = sorted(n for n, e in s.action_errors.items() if e)
    return (f"{s.phase:<8} rps {s.rps:4.1f}  errors {err}  p95 {p95}"
            + (f"  failing: {', '.join(failing)}" if failing else ""))


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def _action_observations(before, after) -> dict:
    """Per-action requests completed between two snapshots."""
    observed = {}
    for name, (requests, failures) in after.counts.items():
        prior_requests, prior_failures = before.counts.get(name, (0, 0))
        count = max(0, requests - prior_requests)
        failed = max(0, failures - prior_failures)
        observed[name] = {"request_count": count, "failure_count": failed,
                          "error_rate": failed / count if count else None}
    return observed


def run(fault: Fault, guard: Guardrails, duration_s: int,
        baseline_s: int = 20, recovery_timeout_s: int = 240, plan: dict | None = None) -> dict:
    preflight(fault)
    signal.signal(signal.SIGTERM, _raise_interrupt)

    run_dir = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{fault.kind}-{fault.target}"
    run_dir.mkdir(parents=True)
    log = EventLog(run_dir / "events.jsonl")
    probe = LocustProbe()
    timeline: list[Sample] = []
    snapshots: list[tuple[float, Snapshot]] = []   # (elapsed, counters) for every tick
    semantic_observations: list[dict] = []
    expected_visit_ids: set[int] | None = None
    semantic_results: SimpleQueue[dict] = SimpleQueue()
    semantic_thread: threading.Thread | None = None
    semantic_phase: str | None = None
    latest_correctness: str | None = None
    prediction = None
    try:
        prediction = blast_radius.analyze(depmap.load(), fault.target, fault.kind)
        prediction["captured_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except (FileNotFoundError, ValueError) as err:
        log("prediction", f"unavailable: {err}")

    def tick(phase: str) -> Sample:
        nonlocal semantic_thread, semantic_phase, latest_correctness
        time.sleep(TICK_S)
        sample = probe.sample(log.elapsed(), phase)
        snapshots.append((sample.elapsed, probe.history[-1]))
        if expected_visit_ids is not None:
            if semantic_phase != phase:
                latest_correctness = None
                semantic_phase = phase
            try:
                while True:
                    observation = semantic_results.get_nowait()
                    semantic_observations.append(observation)
                    if observation["phase"] == phase:
                        latest_correctness = observation["correctness"]
            except Empty:
                pass
            if semantic_thread is None or not semantic_thread.is_alive():
                def observe(observed_phase: str) -> None:
                    result = semantic_probe.check(expected_visit_ids)
                    result.update(phase=observed_phase, elapsed=round(log.elapsed(), 1))
                    semantic_results.put(result)

                semantic_thread = threading.Thread(target=observe, args=(phase,), daemon=True)
                semantic_thread.start()
            sample.semantic_correctness = latest_correctness
        timeline.append(sample)
        with (run_dir / "timeline.jsonl").open("a") as f:   # lets the bench draw the run live
            f.write(json.dumps(asdict(sample)) + "\n")
        return sample

    def save(result: dict) -> dict:
        result["timeline"] = [asdict(s) for s in timeline]
        result["prediction"] = prediction
        if plan is not None:   # the planner's choice, rationale and hypothesis, for the analyst
            result["plan"] = {k: plan.get(k) for k in ("id", "planned_by", "model", "rationale", "hypothesis",
                                                       "requires_approval", "approval_reason", "approved", "fix")}
        result["semantic_observations"] = semantic_observations
        result["prediction_comparison"] = semantic_probe.compare(
            prediction, semantic_observations,
            result.get("disturbance_action_observations") or result.get("fault_action_observations"))
        (run_dir / "result.json").write_text(json.dumps(result, indent=2))
        if prediction is not None:
            depmap.update_run_report(result)
        if not result.get("recovery_pending") and result.get("outcome") != "refused":
            try:
                from . import dashboard
                dashboard.build(run=result)
            except (OSError, ValueError) as err:
                log("dashboard", f"could not refresh saved dashboard: {err}")
        log("saved", f"{(run_dir / 'result.json').relative_to(ROOT)}")
        return result

    log("start", f"{fault.describe()} for up to {duration_s}s",
        fault=fault.to_dict(), guardrails=asdict(guard))
    if plan is not None:
        log("plan", f"chosen by {plan.get('planned_by')}: {plan.get('rationale')}",
            hypothesis=plan.get("hypothesis"), plan_id=plan.get("id"))

    # 1. Steady state: only break a system that is healthy to begin with.
    log("baseline", f"measuring steady state for {baseline_s}s")
    first = probe.snapshot()
    for _ in range(baseline_s):
        log.show(_fmt(tick("baseline")))
    last = probe.history[-1]
    base_err, base_actions = error_rates(first, last)
    p95s = [s.p95_ms for s in timeline if s.p95_ms is not None]
    baseline = {
        "rps": round((last.totals()[0] - first.totals()[0]) / (last.at - first.at), 2),
        "error_rate": base_err,
        "p95_ms": statistics.median(p95s) if p95s else None,
        "action_error_rates": base_actions,
    }
    log("baseline", f"rps {baseline['rps']}  errors {(base_err or 0):.1%}  "
                    f"p95 {baseline['p95_ms']}ms", **baseline)
    problem = None
    if baseline["rps"] < 0.5:
        problem = "no user traffic; is the load generator running?"
    elif base_err is None or base_err > MAX_BASELINE_ERROR:
        problem = f"error rate {(base_err or 0):.1%} is above {MAX_BASELINE_ERROR:.0%}"
    elif baseline["p95_ms"] is None or baseline["p95_ms"] > guard.max_p95_ms / 2:
        problem = f"p95 {baseline['p95_ms']}ms is too close to the {guard.max_p95_ms:.0f}ms limit"
    if problem:
        log("refused", f"system is not in a steady state: {problem}")
        save({"id": run_dir.name, "fault": fault.to_dict(), "outcome": "refused",
              "abort_reason": problem, "baseline": baseline})
        raise SafetyError(f"system is not in a steady state: {problem}")

    try:
        canary_id = semantic_probe.add_canary(run_dir.name)
        semantic_baseline = semantic_probe.baseline()
        expected_visit_ids = set(semantic_baseline["visit_ids"])
        if canary_id not in expected_visit_ids:
            raise ValueError(f"canary visit {canary_id} is not visible on owner {semantic_probe.OWNER_ID}")
        semantic_baseline["canary_visit_id"] = canary_id
        baseline["owner_details"] = semantic_baseline
        log("baseline", f"owner {semantic_probe.OWNER_ID} has {len(expected_visit_ids)} visits, "
                        f"including canary visit {canary_id} written for this run",
            owner_details=semantic_baseline)
    except ValueError as err:
        reason = f"semantic baseline unavailable: {err}"
        log("refused", reason)
        save({"id": run_dir.name, "fault": fault.to_dict(), "outcome": "refused",
              "abort_reason": reason, "baseline": baseline})
        raise SafetyError(reason) from err

    # 2. Inject. Register first, so the kill switch knows about the fault even
    #    if injection only half succeeds.
    fault.prepare()
    expires_at = time.time() + duration_s + guard.expiry_grace_s
    register(fault, expires_at)
    arm_dead_mans_switch(fault, expires_at)
    log("safety", f"dead-man's switch armed: auto-revert {duration_s + guard.expiry_grace_s}s "
                  "from now even if this process dies")
    try:
        fault.inject()
    except Exception:
        fault.revert()
        unregister(fault.id)
        raise
    t_inject = log.elapsed()
    snap_inject = probe.snapshot()
    log("inject", fault.describe())

    # 3. Observe under fault while the watchdog checks every sample.
    outcome, reason, first_breach, streak = "completed", None, None, 0
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            sample = tick("fault")
            if fault.id not in active_faults():
                outcome, reason = "stopped", "reverted externally (kill switch or dead-man's switch)"
                log("abort", reason)
                break
            why = guard.breach(sample, baseline["rps"])
            streak = streak + 1 if why else 0
            if why and first_breach is None:
                first_breach = sample.elapsed
            log.show(_fmt(sample) + (f"  <- breach {streak}/{guard.breach_seconds}: {why}" if why else ""))
            if streak >= guard.breach_seconds:
                outcome, reason = "aborted", f"watchdog: {why} for {streak}s in a row"
                log("abort", reason)
                break
    except KeyboardInterrupt:
        outcome, reason = "interrupted", "operator interrupt (Ctrl-C)"
        log("abort", reason)
    finally:
        snap_revert = probe.snapshot()
        fault_action_observations = _action_observations(snap_inject, snap_revert)
        if fault.id in active_faults():
            fault.revert()
            unregister(fault.id)
        t_revert = log.elapsed()
        log("revert", f"undone: {fault.describe()}")
        # A durable checkpoint survives interruption during the recovery window.
        save({"id": run_dir.name, "fault": fault.to_dict(), "outcome": outcome,
              "abort_reason": reason, "baseline": baseline, "recovered": False,
              "recovery_s": None, "recovery_pending": True,
              "fault_action_observations": fault_action_observations})

    # 4. Recovery: how long until users see normal service again?
    recovered_at = None
    relapses: list[float] = []
    if outcome != "interrupted":
        limit_err = max((base_err or 0) + 0.02, 0.02)
        base_p95 = baseline["p95_ms"]
        limit_p95 = max(2 * base_p95, base_p95 + 100)
        log("recovery", f"waiting for {RECOVERY_HOLD_S}s of normal service, then {SOAK_S}s without a relapse "
                        f"(errors <= {limit_err:.0%}, p95 <= {limit_p95:.0f}ms), max {recovery_timeout_s}s")
        healthy_since = None
        give_up = time.monotonic() + recovery_timeout_s
        while time.monotonic() < give_up:
            sample = tick("recovery")
            # Lost data never comes back, so service that is answering normally
            # counts as recovered; the loss itself is reported below.
            ok = (sample.semantic_correctness in ("preserved", "data_lost")
                  and sample.error_rate is not None and sample.error_rate <= limit_err
                  and (sample.p95_ms is None or sample.p95_ms <= limit_p95))
            if not ok:
                if recovered_at is not None:   # looked recovered, then got worse again
                    relapses.append(sample.elapsed)
                    log("relapse", f"users were hurt again {sample.elapsed - recovered_at:.1f}s after service "
                                   f"looked normal; waiting again")
                    recovered_at = None
                healthy_since = None
            elif healthy_since is None:
                healthy_since = sample.elapsed
            log.show(_fmt(sample) + ("  ok" if ok else ""))
            if recovered_at is None and healthy_since is not None and sample.elapsed - healthy_since >= RECOVERY_HOLD_S:
                recovered_at = healthy_since
                log("soak", f"normal for {RECOVERY_HOLD_S}s; watching {SOAK_S}s more for a relapse")
            if recovered_at is not None and sample.elapsed - recovered_at >= RECOVERY_HOLD_S + SOAK_S:
                break
        if recovered_at is not None and timeline[-1].elapsed - recovered_at < RECOVERY_HOLD_S + SOAK_S:
            recovered_at = None   # the recovery window ended during the soak: not confirmed
        log("recovery", f"recovered {recovered_at - t_revert:.1f}s after revert"
                        if recovered_at else f"NOT recovered within {recovery_timeout_s}s")

    # Data written before the fault that is missing afterwards is lost, not late.
    final_probe = next((observation for observation in reversed(semantic_observations)
                        if observation["phase"] == "recovery"), None)
    data_loss = None
    if final_probe and final_probe["correctness"] == "data_lost":
        canary_id = baseline["owner_details"]["canary_visit_id"]
        data_loss = {"missing_visit_ids": final_probe["missing_visit_ids"],
                     "canary_visit_id": canary_id,
                     "canary_lost": canary_id in final_probe["missing_visit_ids"]}
        log("data-loss", f"{len(data_loss['missing_visit_ids'])} of owner {semantic_probe.OWNER_ID}'s "
                         f"{len(expected_visit_ids)} visits did not survive the fault "
                         f"(canary lost: {'yes' if data_loss['canary_lost'] else 'no'})", **data_loss)

    # 5. Summarise. Impact covers fault + recovery: requests that hung during
    #    the fault often only fail after it has been undone.
    snap_end = probe.history[-1] if outcome != "interrupted" else snap_revert
    # Predictions are checked over the disturbance only: from injection until
    # users got normal service back, so later healthy traffic does not dilute it.
    disturbance_end = next((snap for elapsed, snap in snapshots
                            if recovered_at is not None and elapsed >= recovered_at), snap_end)
    disturbance_action_observations = _action_observations(snap_inject, disturbance_end)
    fault_err, fault_actions = error_rates(snap_inject, snap_end)
    paths = depmap.action_services()  # None until `chaoslab map` has been run
    impact = {
        name: {"baseline_error_rate": base_actions.get(name),
               "fault_error_rate": fault_actions.get(name),
               "uses_target": fault.target in paths.get(name, set()) if paths else None}
        for name in sorted(set(base_actions) | set(fault_actions))
    }
    collateral = [name for name, i in impact.items() if i["uses_target"] is False
                  and (i["fault_error_rate"] or 0) - (i["baseline_error_rate"] or 0) > 0.10]
    fault_samples = [s for s in timeline if s.phase in ("fault", "recovery")]
    return save({
        "id": run_dir.name,
        "fault": fault.to_dict(),
        "guardrails": asdict(guard),
        "baseline": baseline,
        "outcome": outcome,
        "abort_reason": reason,
        "fault_active_s": round(t_revert - t_inject, 1),
        "time_to_first_breach_s": round(first_breach - t_inject, 1) if first_breach else None,
        "error_rate_fault_and_recovery": fault_err,
        "peak_error_rate": max((s.error_rate for s in fault_samples if s.error_rate is not None), default=None),
        "peak_p95_ms": max((s.p95_ms for s in fault_samples if s.p95_ms is not None), default=None),
        "recovered": recovered_at is not None,
        "relapses": relapses,
        "recovery_s": round(recovered_at - t_revert, 1) if recovered_at else None,
        "semantic_recovery": final_probe,
        "data_loss": data_loss,
        "fault_action_observations": fault_action_observations,
        "disturbance_action_observations": disturbance_action_observations,
        "impact_by_action": impact,
        "collateral_actions": collateral,
    })
