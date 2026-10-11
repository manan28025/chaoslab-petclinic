"""Command line for the chaos controller.

    python3 -m chaoslab map                             # discover the dependency map
    python3 -m chaoslab dashboard                       # build the read-only dashboard
    python3 -m chaoslab bench                           # live operator console on this machine
    python3 -m chaoslab faults                          # the menu of failure types
    python3 -m chaoslab targets                         # what may be faulted
    python3 -m chaoslab run kill vets-service           # one experiment
    python3 -m chaoslab run delay visits-service --delay-ms 1500
    python3 -m chaoslab status                          # active faults
    python3 -m chaoslab stop                            # KILL SWITCH
    python3 -m chaoslab plan                            # the AI planner's next experiment
    python3 -m chaoslab auto --budget 3                 # campaign: plan, approve, run, analyse
    python3 -m chaoslab analyze                         # findings, risk and fixes for every run
    python3 -m chaoslab report                          # ranked weaknesses and the scorecard
"""

import argparse
import json
import sys
import time

from . import analyst, blast_radius, dashboard, depmap, experiment, planner, safety
from .docker import DockerError, targets
from .faults import FAULTS
from .llm import from_env


def fault_params(kind: str, args: argparse.Namespace, expire_after_s: int) -> dict:
    """Command-line options that apply to each fault type."""
    return {
        "delay": {"delay_ms": args.delay_ms, "jitter_ms": args.jitter_ms, "expire_after_s": expire_after_s},
        "loss": {"loss_pct": args.loss_pct, "expire_after_s": expire_after_s},
        "cpu": {"cpus": args.cpus},
        "spike": {"multiplier": args.multiplier},
    }.get(kind, {})


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def print_summary(r: dict) -> None:
    print(f"\n==== Result: {r['fault']['kind']} {r['fault']['target']} ====")
    print(f"Outcome            {r['outcome']}" + (f" - {r['abort_reason']}" if r["abort_reason"] else ""))
    breach = r["time_to_first_breach_s"]
    print(f"Fault active       {r['fault_active_s']}s"
          + (f" (first threshold breach after {breach}s)" if breach is not None else ""))
    print(f"User impact        errors {_pct(r['error_rate_fault_and_recovery'])} overall, "
          f"peak {_pct(r['peak_error_rate'])}; peak p95 {r['peak_p95_ms']}ms "
          f"(baseline {_pct(r['baseline']['error_rate'])}, {r['baseline']['p95_ms']}ms)")
    print("Recovery           " + (f"{r['recovery_s']}s after the fault was undone"
                                   if r["recovered"] else "NOT recovered in time"))
    semantic = r.get("semantic_recovery")
    if r.get("data_loss"):
        loss = r["data_loss"]
        lost = len(loss["missing_visit_ids"])
        print(f"Data loss          {lost} visit{'' if lost == 1 else 's'} written before the fault "
              f"{'is' if lost == 1 else 'are'} gone "
              f"(canary visit {loss['canary_visit_id']} lost: {'yes' if loss['canary_lost'] else 'no'})")
    elif semantic and semantic["correctness"] != "preserved":
        print(f"Response data      {semantic['correctness']}; missing baseline visit IDs: "
              f"{semantic['missing_visit_ids']}")
    print("\nImpact by user action          baseline  fault+recovery")
    for name, i in r["impact_by_action"].items():
        tag = " (uses target)" if i["uses_target"] else ""  # None: no map yet
        print(f"  {name + tag:<28} {_pct(i['baseline_error_rate']):>8}  {_pct(i['fault_error_rate']):>12}")
    print("Collateral damage  " + (", ".join(r["collateral_actions"]) or "none")
          + "  (actions that do not use the target but still failed)")
    comparison = r.get("prediction_comparison") or {}
    if comparison:
        print("\nPrediction vs observation (from injection until users recovered)")
        for name, c in comparison.items():
            print(f"  {name:<28} predicted {c['predicted']:<11} observed {c['observed']:<11} "
                  f"{c['match']} ({c['fault_requests']} requests, {c.get('fault_failures', 0)} failed)")


def _snapshot() -> dict | None:
    try:
        return depmap.load()
    except (FileNotFoundError, ValueError):
        return None


def analyze_and_save(result: dict, llm) -> dict:
    analysis = analyst.analyze(result, _snapshot(), llm)
    analyst.save(analysis)
    return analysis


def print_analysis(a: dict) -> None:
    r = a["risk"]
    print(f"\n==== Analysis (text by {a['written_by']}{', ' + a['model'] if a['model'] else ''}) ====")
    print(f"Verdict            {a['verdict']}" + (f", risk {r['score']}/10 {r['level']}" if r else ""))
    for key, label in (("summary", "Summary"), ("likely_cause", "Likely cause"), ("production_impact", "In production")):
        if a["prose"].get(key):
            print(f"{label:<18} {a['prose'][key]}")
    for f in a["findings"]:
        print(f"  [{f['severity']:<8}] {f['title']}")
    if a["hypothesis"]:
        h = a["hypothesis"]
        print(f"Hypothesis         {'held' if h['held'] else 'did NOT hold'} (planned by {h['planned_by']})"
              + "".join(f"; {m['action']} expected {m['expected']}, observed {m['observed']}" for m in h["misses"]))
    for f in a["fixes"][:3]:
        print(f"  fix: {f['title']}" + (f"\n       verify: {f['verify']}" if f["verify"] else ""))
    print(f"Saved              runs/{a['run_id']}/analysis.md")


def print_plan(p: dict) -> None:
    print(f"Candidates ranked by code (top {min(len(p['candidates']), 8)}):")
    for c in p["candidates"][:8]:
        mark = "->" if (c["target"], c["kind"]) == (p["target"], p["kind"]) else "  "
        print(f" {mark} #{c['rank']:<2} {c['priority']:>5}  {c['kind']:<6} {c['target']:<18} {'; '.join(c['reasons'])}"
              + ("  [needs approval]" if c["requires_approval"] else ""))
    settings = ", ".join(f"{k}={v}" for k, v in p["params"].items()) or "no settings"
    print(f"\nPlan               {p['kind']} on {p['target']} ({settings}) for {p['duration_s']} s")
    print(f"Planned by         {p['planned_by']}" + (f" ({p['model']})" if p["model"] else ""))
    print(f"Why                {p['rationale']}")
    print(f"Hypothesis         {p['hypothesis']['statement']}")
    if p["requires_approval"]:
        print(f"Needs approval     {p['approval_reason']}")
    for note in p["notes"]:
        print(f"  note: {note}")


def approve(p: dict, yes: bool) -> bool:
    if not p["requires_approval"]:
        return True
    if yes:
        print("Approved by --approve-risky")
        return True
    if sys.stdin.isatty():
        return input("Approve this experiment? [y/N] ").strip().lower() in ("y", "yes")
    print("Not approved: no terminal to ask, and --approve-risky was not given")
    return False


def execute(p: dict, baseline_s: int, llm) -> dict | None:
    guard = safety.Guardrails()
    fault = FAULTS[p["kind"]](p["target"], **planner.fault_params(p, guard.expiry_grace_s))
    try:
        result = experiment.run(fault, guard, p["duration_s"], baseline_s=baseline_s, plan=p)
    except safety.SafetyError as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return None
    print_summary(result)
    print_analysis(analyze_and_save(result, llm))
    return result


def _allowed() -> set[str]:
    try:
        return set(targets())
    except DockerError as err:
        raise SystemExit(f"Cannot list targets: {err}. Is Docker running?")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chaoslab",
                                     description="Safe, controlled failure injection for the local sandbox.")
    sub = parser.add_subparsers(dest="cmd", required=True,
                                metavar="{map,dashboard,bench,impact,faults,targets,status,stop,run,plan,execute,auto,analyze,report}")
    sub.add_parser("map", help="discover the dependency map and write it to reports/")
    dashboard_command = sub.add_parser("dashboard", help="build a standalone read-only dashboard")
    dashboard_command.add_argument("--demo", action="store_true",
                                   help="show the committed recorded example even if local runs exist")
    bench_command = sub.add_parser("bench", help="serve the live Resilience Test Bench on this machine")
    bench_command.add_argument("--port", type=int, default=8090)
    impact = sub.add_parser("impact", help="predict a failure's blast radius without injecting it")
    impact.add_argument("target")
    impact.add_argument("--failure", choices=sorted(FAULTS), default="kill")
    impact.add_argument("--phase", choices=("runtime", "startup_restart"))
    impact.add_argument("--feature", help="show one source-backed user feature")
    impact.add_argument("--json", action="store_true", help="print the shared JSON contract")
    sub.add_parser("faults", help="list the failure types and their limits")
    sub.add_parser("targets", help="list the containers that may be faulted")
    sub.add_parser("status", help="show active faults")
    sub.add_parser("stop", help="KILL SWITCH: revert every active fault and restore the sandbox")
    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("kind", choices=sorted(FAULTS))
    run.add_argument("target")
    run.add_argument("--duration", type=int, default=60, help="seconds to keep the fault (default 60)")
    run.add_argument("--baseline", type=int, default=20, help="seconds of steady-state measurement first")
    run.add_argument("--recovery-timeout", type=int, default=240,
                     help="maximum seconds to wait for recovery after revert")
    run.add_argument("--delay-ms", type=int, default=1000, help="delay: added latency")
    run.add_argument("--jitter-ms", type=int, default=0, help="delay: random variation")
    run.add_argument("--loss-pct", type=float, default=20, help="loss: share of packets dropped (100 = cut off)")
    run.add_argument("--cpus", type=float, default=0.1, help="cpu: CPU allowed, in cores (0.1 = 10%% of one)")
    run.add_argument("--multiplier", type=float, default=5, help="spike: how many times the normal users")
    run.add_argument("--max-error-rate", type=float, default=safety.Guardrails.max_error_rate)
    run.add_argument("--max-p95-ms", type=float, default=safety.Guardrails.max_p95_ms)
    run.add_argument("--no-ai", action="store_true", help="analyse without a language model")
    plan_command = sub.add_parser("plan", help="the AI planner's next experiment (runs nothing unless --run)")
    plan_command.add_argument("--no-ai", action="store_true", help="rank and pick by code only")
    plan_command.add_argument("--json", action="store_true", help="print the plan as JSON")
    plan_command.add_argument("--run", action="store_true", help="run the plan after showing it")
    plan_command.add_argument("--approve-risky", action="store_true", help="approve a plan that needs approval")
    plan_command.add_argument("--baseline", type=int, default=20)
    execute_command = sub.add_parser("execute", help="run a saved plan exactly as it was planned")
    execute_command.add_argument("plan_id")
    execute_command.add_argument("--approve-risky", action="store_true", help="the plan was approved by a person")
    execute_command.add_argument("--no-ai", action="store_true", help="analyse without a language model")
    execute_command.add_argument("--baseline", type=int, default=20)
    auto = sub.add_parser("auto", help="campaign: plan, approve, run and analyse until the budget is spent")
    auto.add_argument("--budget", type=int, default=3, help="experiments to run (1-10, default 3)")
    auto.add_argument("--no-ai", action="store_true", help="plan and analyse by code only")
    auto.add_argument("--approve-risky", action="store_true", help="approve plans that need approval")
    auto.add_argument("--pause", type=int, default=20, help="seconds to let the system settle between runs")
    auto.add_argument("--baseline", type=int, default=20)
    analyze = sub.add_parser("analyze", help="findings, risk and fixes for saved runs")
    analyze.add_argument("run_ids", nargs="*", help="runs to analyse (default: every run without an analysis)")
    analyze.add_argument("--no-ai", action="store_true", help="templated text, no language model")
    analyze.add_argument("--refresh", action="store_true", help="recompute existing analyses")
    report_command = sub.add_parser("report", help="ranked weaknesses, fixes and the resilience scorecard")
    report_command.add_argument("--refresh", action="store_true", help="recompute every analysis first")
    report_command.add_argument("--no-ai", action="store_true", help="templated text for new analyses")
    expire = sub.add_parser("_expire")  # internal: the dead-man's switch process
    expire.add_argument("fault_id")
    expire.add_argument("at", type=float)
    args = parser.parse_args(argv)

    if args.cmd == "map":
        m = depmap.build()
        for path in depmap.save(m):
            print(f"wrote {path}")
        print(f"\n{len(m['nodes'])} nodes, {len(m['edges'])} edges, {len(m['user_actions'])} user actions")
        for node in sorted(m["nodes"], key=lambda n: -(n.get("share_of_requests") or 0)):
            share = (f"{node['share_of_requests']:.0%}" if node.get("share_of_requests") is not None
                     else "unknown")
            print(f"  {node['id']:<18} {share:>7} of requests  "
                  f"{'; '.join(node['risk_hints'])}")
        for warning in m["warnings"]:
            print(f"  note: {warning}")
    elif args.cmd == "dashboard":
        try:
            path = dashboard.build(use_demo=args.demo)
        except (OSError, ValueError) as err:
            print(f"Cannot build dashboard: {err}. Run `python3.11 -m chaoslab map` first.",
                  file=sys.stderr)
            return 2
        print(f"wrote {path.relative_to(dashboard.ROOT)}")
    elif args.cmd == "bench":
        from . import bench
        bench.serve(args.port)
    elif args.cmd == "impact":
        try:
            prediction = blast_radius.analyze(depmap.load(), args.target, args.failure,
                                              phase=args.phase, feature=args.feature)
        except (FileNotFoundError, ValueError) as err:
            print(f"Cannot analyze impact: {err}. Run `python3 -m chaoslab map` first.", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(prediction, indent=2))
        else:
            print(f"Potential impact of {args.failure} on {args.target} (snapshot {prediction['snapshot_id']})")
            for phase in ([args.phase] if args.phase else ("runtime", "startup_restart")):
                print(f"\n{phase.replace('_', ' ').title()}:")
                for item in prediction[phase]:
                    print(f"  {item['component']}: {item['outcome']} — {item['behavior']}")
                    details = [f"{label}: {item[key]}" for key, label in
                               (("http_status", "HTTP"), ("data", "data"), ("latency", "latency"),
                                ("basis", "basis")) if key in item]
                    if details:
                        print(f"    {'; '.join(details)}")
                    print(f"    path: {' -> '.join(item['path'])}; evidence: "
                          f"{', '.join(e['locator'] for e in item['evidence'])}")
                    for uncertainty in item["uncertainties"]:
                        print(f"    uncertainty: {uncertainty}")
                if not prediction[phase]:
                    print("  no supported impact predicted")
    elif args.cmd == "faults":
        for kind, fault in FAULTS.items():
            limits = ", ".join(f"{p} {lo}..{hi}" for p, (lo, hi) in fault.limits.items()
                               if p != "expire_after_s")
            print(f"{kind:<6} {fault.summary}" + (f"\n       limits: {limits}" if limits else ""))
    elif args.cmd == "targets":
        for name, state in sorted(targets().items()):
            print(f"{name:<20} {state}")
    elif args.cmd == "status":
        faults = safety.active_faults()
        if not faults:
            print("no active faults")
        for fault_id, entry in faults.items():
            left = entry["expires_at"] - time.time()
            print(f"{fault_id}  (pid {entry['pid']}, dead-man's switch in {left:.0f}s)")
    elif args.cmd == "stop":
        return 0 if safety.kill_switch() else 1
    elif args.cmd == "_expire":
        safety.expire(args.fault_id, args.at)
    elif args.cmd == "run":
        guard = safety.Guardrails(max_error_rate=args.max_error_rate, max_p95_ms=args.max_p95_ms)
        duration = min(args.duration, guard.max_duration_s)
        params = fault_params(args.kind, args, duration + guard.expiry_grace_s)
        fault = FAULTS[args.kind](args.target, **params)
        try:
            result = experiment.run(fault, guard, duration, baseline_s=args.baseline,
                                    recovery_timeout_s=args.recovery_timeout)
        except safety.SafetyError as err:
            print(f"REFUSED: {err}", file=sys.stderr)
            return 2
        print_summary(result)
        print_analysis(analyze_and_save(result, from_env(enabled=not args.no_ai)))
    elif args.cmd == "plan":
        snapshot = _snapshot()
        if snapshot is None:
            print("No dependency map yet: run `python3 -m chaoslab map` first.", file=sys.stderr)
            return 2
        p = planner.plan(snapshot, _allowed(), llm=from_env(enabled=not args.no_ai))
        if p is None:
            print("Nothing on the allowlist can be planned.", file=sys.stderr)
            return 2
        path = planner.save(p)
        if args.json:
            print(json.dumps(p, indent=2, default=str))
        else:
            print_plan(p)
            print(f"Saved              {path.relative_to(planner.RUNS_DIR.parent)}")
        if args.run:
            if not approve(p, args.approve_risky):
                return 3
            p["approved"] = p["requires_approval"]
            planner.save(p)
            return 0 if execute(p, args.baseline, from_env(enabled=not args.no_ai)) else 2
    elif args.cmd == "execute":
        try:
            p = planner.load(args.plan_id)
        except ValueError as err:
            print(str(err), file=sys.stderr)
            return 2
        print_plan(p)
        if not approve(p, args.approve_risky):
            return 3
        p["approved"] = p["requires_approval"]
        planner.save(p)
        return 0 if execute(p, args.baseline, from_env(enabled=not args.no_ai)) else 2
    elif args.cmd == "auto":
        return campaign(args)
    elif args.cmd == "analyze":
        llm = from_env(enabled=not args.no_ai)
        if args.run_ids:
            for run_id in args.run_ids:
                path = planner.RUNS_DIR / run_id / "result.json"
                if not path.exists():
                    print(f"No run {run_id}", file=sys.stderr)
                    return 2
                result = json.loads(path.read_text())
                result.setdefault("id", run_id)
                print_analysis(analyze_and_save(result, llm))
        else:
            for a in analyst.load_all(snapshot=_snapshot(), llm=llm, refresh=args.refresh):
                r = a["risk"]
                print(f"{a['run_id']:<40} {a['verdict']:<13} "
                      f"{(str(r['score']) + '/10 ' + r['level']) if r else '-':<16} {a['findings'][0]['title']}")
    elif args.cmd == "report":
        return write_report(args.refresh, not args.no_ai)
    return 0


def write_report(refresh: bool = False, use_ai: bool = True) -> int:
    snapshot = _snapshot()
    if snapshot is None:
        print("No dependency map yet: run `python3 -m chaoslab map` first.", file=sys.stderr)
        return 2
    analyses = analyst.load_all(snapshot=snapshot, llm=from_env(enabled=use_ai), refresh=refresh)
    services = [n["id"] for n in snapshot["nodes"] if n.get("kind") == "container" and n.get("active") is not False]
    r = analyst.report(analyses, services, list(FAULTS), snapshot["entry_point"])
    paths = analyst.write_report(r)
    card = r["scorecard"]
    print(f"Overall resilience {card['overall']}/100 ({card['overall_grade']}) across {r['experiments']} experiments; "
          f"coverage {r['coverage']['tested_pairs']}/{r['coverage']['possible_pairs']} service and fault pairs")
    for i, w in enumerate(r["weaknesses"][:5], 1):
        print(f"  {i}. [{w['severity']}] {w['service']}: {w['title']}")
    for path in paths:
        print(f"wrote {path.relative_to(analyst.REPORTS_DIR.parent)}")
    return 0


def campaign(args) -> int:
    """Plan, ask for approval when needed, run, analyse; repeat until the budget is spent."""
    budget = max(1, min(args.budget, 10))
    llm = from_env(enabled=not args.no_ai)
    done, tried, exclude = 0, 0, set()
    while done < budget and tried < budget * 3:
        tried += 1
        snapshot = _snapshot()
        if snapshot is None:
            print("No dependency map yet: run `python3 -m chaoslab map` first.", file=sys.stderr)
            return 2
        p = planner.plan(snapshot, _allowed(), llm=llm, exclude=exclude)
        if p is None:
            print("Nothing left to test on the allowlist.")
            break
        print(f"\n######## Campaign experiment {done + 1} of {budget} ########")
        print_plan(p)
        exclude.add((p["target"], p["kind"]))
        if not approve(p, args.approve_risky):
            print("Skipped; planning the next candidate")
            planner.save(p)
            continue
        p["approved"] = p["requires_approval"]
        planner.save(p)
        result = execute(p, args.baseline, llm)
        if result is None:
            print(f"Waiting {args.pause} s for the system to settle before planning again")
            time.sleep(args.pause)
            continue
        done += 1
        if result.get("outcome") in ("stopped", "interrupted"):
            print("The kill switch or an operator stopped the experiment; ending the campaign")
            break
        if done < budget:
            print(f"Pausing {args.pause} s to let the system settle")
            time.sleep(args.pause)
    print(f"\nCampaign finished: {done} experiment{'s' if done != 1 else ''} run")
    return write_report(use_ai=not args.no_ai)


if __name__ == "__main__":
    sys.exit(main())
