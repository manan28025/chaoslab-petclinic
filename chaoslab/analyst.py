"""Analyst: what one experiment showed, how bad it is, and what to change.

Code computes every finding, number, score and grade from the saved run. The model, when
one is available, only writes the explanation (summary, likely cause, what it would mean in
production) from those facts, and may suggest at most two extra fixes that are labelled
unverified. Fixes are advice; nothing here changes the sandbox.

    analyze(result)          one run -> findings, risk 0-10, fixes, hypothesis check
    scorecard(analyses)      per-service 0-100 and A-F, worst first
    report(...)              ranked weaknesses, fixes, scorecard and coverage for the campaign
"""

import json
import math
import statistics
from pathlib import Path

from .llm import LLM
from .settings import ACTION_ROUTES, REPORTS_DIR, RUNS_DIR

ACTIONS = list(ACTION_ROUTES)
SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
REPORT_MD = REPORTS_DIR / "resilience-report.md"
REPORT_JSON = REPORTS_DIR / "resilience-report.json"
HANG_MS = 5000            # a p95 this high means requests hung instead of failing fast
FAILING = 0.05            # an action is affected when its error rate rose by more than this


# ------------------------------------------------------------------ findings
def _finding(code: str, severity: str, title: str, evidence: str) -> dict:
    return {"code": code, "severity": severity, "title": title, "evidence": evidence}


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def observed_outcomes(result: dict) -> dict[str, str]:
    """What each user action actually experienced, from the measured error rates and the
    owner-details correctness probe."""
    out = {}
    for action, i in (result.get("impact_by_action") or {}).items():
        rise = (i.get("fault_error_rate") or 0) - (i.get("baseline_error_rate") or 0)
        out[action] = "unavailable" if rise > 0.5 else "degraded" if rise > FAILING else "preserved"
    for c_action, c in (result.get("prediction_comparison") or {}).items():
        if c_action in out and c.get("observed") in ("unavailable", "degraded", "preserved"):
            rank = {"unavailable": 2, "degraded": 1, "preserved": 0}
            if rank[c["observed"]] > rank[out[c_action]]:
                out[c_action] = c["observed"]
    if any(o.get("correctness") == "degraded" and o.get("phase") == "fault"
           for o in result.get("semantic_observations") or []) and out.get("owner details") == "preserved":
        out["owner details"] = "degraded"
    if stalled(result):   # requests through the target hung: slow is degraded even without errors
        for action, i in (result.get("impact_by_action") or {}).items():
            if i.get("uses_target") and out.get(action) == "preserved":
                out[action] = "degraded"
    return out


def stall_depth(result: dict) -> float:
    """How far completed requests fell below normal during the fault: 0 none, 1 nothing completed."""
    rps = [s["rps"] for s in result.get("timeline") or [] if s.get("phase") == "fault" and s.get("rps") is not None]
    base = (result.get("baseline") or {}).get("rps")
    return max(0.0, 1 - min(rps) / base) if rps and base else 0.0


def stalled(result: dict) -> bool:
    base_p95 = (result.get("baseline") or {}).get("p95_ms") or 0
    p95 = result.get("peak_p95_ms") or 0
    return stall_depth(result) > 0.7 or (bool(base_p95) and p95 >= max(5 * base_p95, base_p95 + 1000))


def findings(result: dict, snapshot: dict | None = None) -> list[dict]:
    fault = result.get("fault") or {}
    kind, target = fault.get("kind"), fault.get("target")
    baseline = result.get("baseline") or {}
    out = []
    if result.get("outcome") == "refused":
        return [_finding("refused", "info", "Not started: the system was not in a steady state",
                         result.get("abort_reason") or "steady-state check failed")]

    peak = result.get("peak_error_rate")
    impact = result.get("impact_by_action") or {}
    hit = {a: i["fault_error_rate"] for a, i in impact.items()
           if (i.get("fault_error_rate") or 0) - (i.get("baseline_error_rate") or 0) > FAILING}
    if peak is not None and peak >= FAILING:
        severity = "high" if peak >= 0.5 else "medium" if peak >= 0.2 else "low"
        out.append(_finding("user_errors", severity, f"Users saw errors: up to {_pct(peak)} of requests failed",
                            "failing actions: " + (", ".join(f"{a} {_pct(r)}" for a, r in hit.items()) or "short spikes only")))

    degraded_200 = [o for o in result.get("semantic_observations") or []
                    if o.get("status") == 200 and o.get("correctness") == "degraded" and o.get("phase") == "fault"]
    if degraded_200:
        out.append(_finding("silent_degradation", "high",
                            "Owner pages returned HTTP 200 with their visits missing",
                            f"{len(degraded_200)} correctness probes during the fault got 200 with no visits; "
                            "a status-code monitor would report everything as fine"))

    loss = result.get("data_loss")
    if loss:
        n = len(loss.get("missing_visit_ids") or [])
        out.append(_finding("data_loss", "critical", f"{n} visit{'s' if n != 1 else ''} written before the fault "
                            f"{'were' if n != 1 else 'was'} lost",
                            f"missing visit IDs {loss.get('missing_visit_ids')}; the run's canary visit "
                            f"{loss.get('canary_visit_id')} {'was lost' if loss.get('canary_lost') else 'survived'}. "
                            "The service came back with an empty in-memory database"))

    p95 = result.get("peak_p95_ms")
    if p95 is not None and p95 >= HANG_MS:
        out.append(_finding("hang_not_fail", "medium", f"Requests hung for up to {p95 / 1000:.0f} s instead of failing fast",
                            f"peak p95 {p95:.0f} ms against a baseline of {baseline.get('p95_ms')} ms: callers waited "
                            "for the Gateway's 10 s TimeLimiter before getting an answer"))

    rps = [s["rps"] for s in result.get("timeline") or [] if s.get("phase") == "fault"]
    base_rps = baseline.get("rps")
    if rps and base_rps and min(rps) < 0.3 * base_rps:
        out.append(_finding("throughput_collapse", "medium", "Completed requests fell below 30% of normal",
                            f"lowest {min(rps):.1f} req/s against {base_rps:.1f} normally: users were stuck waiting"))

    if result.get("outcome") == "aborted":
        breach = result.get("time_to_first_breach_s")
        out.append(_finding("watchdog_stop", "info", "The watchdog stopped the fault early",
                            f"{result.get('abort_reason')}; first limit breach {breach} s after injection"
                            if breach is not None else str(result.get("abort_reason"))))

    if result.get("recovered") is False and result.get("outcome") not in ("refused", "interrupted"):
        out.append(_finding("no_recovery", "critical", "Users never got normal service back",
                            "the recovery window ended without 10 s of normal errors, latency and owner data"))
    elif result.get("recovery_s") is not None and result["recovery_s"] > 30:
        out.append(_finding("slow_recovery", "medium", f"Recovery took {result['recovery_s']:.0f} s",
                            "more than 30 s from undoing the fault until users had 10 s of normal service"))

    node = next((n for n in (snapshot or {}).get("nodes", []) if n.get("id") == target), None)
    if kind == "kill" and node is not None and node.get("auto_restart") is False:
        out.append(_finding("no_self_healing", "high", f"{target} only came back because ChaosLab restarted it",
                            "the container has no restart policy and runs a single instance: without the "
                            "experiment's undo step it would have stayed down"))

    collateral = result.get("collateral_actions") or []
    if collateral:
        out.append(_finding("collateral_damage", "high", "Actions that never call the target failed too",
                            f"{', '.join(collateral)} failed although they do not use {target}: the failure spread "
                            "through something they share, such as the Gateway's circuit breaker"))

    contradicted = {a: c for a, c in (result.get("prediction_comparison") or {}).items() if c.get("match") == "contradicted"}
    if contradicted:
        out.append(_finding("prediction_wrong", "low", "The dependency model predicted this wrongly",
                            "; ".join(f"{a}: predicted {c.get('predicted', '?')}, observed {c.get('observed', '?')}"
                                      for a, c in contradicted.items())))
    if not out:
        out.append(_finding("held", "info", "Users were unaffected",
                            f"errors stayed at {_pct(peak)} and p95 at {p95} ms during the fault"))
    out.sort(key=lambda f: -SEVERITY_RANK[f["severity"]])
    return out


# ------------------------------------------------------------------ risk
def risk(result: dict, found: list[dict]) -> dict | None:
    """Deterministic 0-10 severity. Data loss or no recovery is critical regardless."""
    if result.get("outcome") == "refused":
        return None
    codes = {f["code"] for f in found}
    impact = result.get("impact_by_action") or {}
    affected = sum(1 for o in observed_outcomes(result).values() if o != "preserved")
    base_p95 = (result.get("baseline") or {}).get("p95_ms") or 0
    peak_p95 = result.get("peak_p95_ms") or 0
    latency = min(math.log10(peak_p95 / base_p95) / 2, 1.0) if base_p95 and peak_p95 > base_p95 else 0.0
    recovery_s = result.get("recovery_s")
    factors = {
        "user_errors": round(3.5 * min(result.get("peak_error_rate") or 0, 1.0), 2),
        "breadth": round(2.0 * affected / max(len(impact) or len(ACTIONS), 1), 2),
        "slowness": round(1.0 * latency, 2),
        "recovery": round(1.5 * (1.0 if result.get("recovered") is False else min((recovery_s or 0) / 120, 1.0)), 2),
        "silent_degradation": 1.0 if "silent_degradation" in codes else 0.0,
        "collateral_damage": 1.0 if "collateral_damage" in codes else 0.0,
        "hang_not_fail": 0.5 if "hang_not_fail" in codes else 0.0,
        "stall": round(1.0 * stall_depth(result), 2) if "throughput_collapse" in codes else 0.0,
        "no_self_healing": 0.5 if "no_self_healing" in codes else 0.0,
    }
    score = sum(factors.values())
    floor = 9.0 if "no_recovery" in codes else 8.5 if "data_loss" in codes else 0.0
    score = round(min(max(score, floor), 10.0), 1)
    level = "low" if score < 3 else "medium" if score < 5.5 else "high" if score < 8 else "critical"
    return {"score": score, "level": level, "factors": factors,
            "floor": "data loss is critical" if floor == 8.5 else "no recovery is critical" if floor else None}


# ------------------------------------------------------------------ fixes
GATEWAY_CODE = "spring-petclinic-api-gateway/src/main/java/org/springframework/samples/petclinic/api/ApiGatewayApplication.java"
GATEWAY_ROUTES = "spring-petclinic-api-gateway/src/main/resources/application.yml"


# Which findings each library fix addresses; a weakness lists only the fixes aimed at it.
ADDRESSES = {
    "persistent-database": ["data_loss"], "visible-fallbacks": ["silent_degradation"],
    "fail-fast": ["hang_not_fail", "throughput_collapse"], "separate-circuit-breakers": ["collateral_damage"],
    "self-healing": ["no_self_healing"], "faster-recovery": ["no_recovery", "slow_recovery"],
    "rate-limit": ["user_errors"], "update-model": ["prediction_wrong"],
}


def _fix(fix_id: str, title: str, why: str, steps: list[str], verify: str, effort: str, confidence: float) -> dict:
    return {"id": fix_id, "title": title, "why": why, "steps": steps, "verify": verify, "effort": effort,
            "confidence": confidence, "source": "library", "addresses": ADDRESSES.get(fix_id, [])}


def fixes(result: dict, found: list[dict]) -> list[dict]:
    fault = result.get("fault") or {}
    kind, target = fault.get("kind"), fault.get("target")
    codes = {f["code"]: f for f in found}
    rerun = f"python3 -m chaoslab run {kind} {target}"
    out = []
    if "data_loss" in codes:
        out.append(_fix(
            "persistent-database", f"Move {target} off its in-memory database",
            codes["data_loss"]["evidence"],
            [f"Add a MySQL service with a named volume to the Compose file",
             f"Start {target} with the Spring profile `mysql` (the service already ships "
             "db/mysql/schema.sql and the MySQL driver) and point SPRING_DATASOURCE_URL at it",
             "Keep the HSQLDB profile for unit tests only"],
            f"{rerun}  # expect: no data-loss line, canary visit survives", "medium", 0.9))
    if "silent_degradation" in codes:
        out.append(_fix(
            "visible-fallbacks", "Make degraded owner pages visible instead of a silent 200",
            codes["silent_degradation"]["evidence"],
            ["In the Gateway's owner-details fallback, mark the response as partial "
             "(for example a `X-Degraded: visits` header or a `visitsUnavailable` field)",
             "Count fallback responses in Micrometer and alert on them, not only on 5xx",
             "Show 'visits temporarily unavailable' in the UI rather than an empty list"],
            f"{rerun}  # expect: degraded pages carry the marker; the fallback counter rises", "low", 0.85))
    if "hang_not_fail" in codes or "throughput_collapse" in codes:
        evidence = (codes.get("hang_not_fail") or codes["throughput_collapse"])["evidence"]
        out.append(_fix(
            "fail-fast", "Fail fast: lower the Gateway's 10 s TimeLimiter",
            evidence,
            [f"In {GATEWAY_CODE}, defaultCustomizer sets TimeLimiterConfig timeoutDuration to 10 s; "
             "use about 2 s (well above the 20 ms normal p95)",
             "Make it configurable (for example resilience4j.timelimiter properties) so each route can differ",
             "Add a connect and response timeout to the Gateway's WebClient for the owner lookup, which has none"],
            f"{rerun}  # expect: peak p95 near 2 s instead of 10 s, throughput holds", "low", 0.8))
    if "collateral_damage" in codes:
        out.append(_fix(
            "separate-circuit-breakers", "Give each route its own circuit breaker",
            codes["collateral_damage"]["evidence"],
            [f"In {GATEWAY_ROUTES}, the default filter uses one `defaultCircuitBreaker` for every route",
             "Name one circuit breaker per downstream service (customers, visits, vets) so one failing "
             "service cannot open the breaker for the others"],
            f"{rerun}  # expect: no collateral actions", "low", 0.75))
    if "no_self_healing" in codes:
        out.append(_fix(
            "self-healing", f"Let {target} restart itself and run a second instance",
            codes["no_self_healing"]["evidence"],
            ["Add `restart: unless-stopped` to the service in Compose (an orchestrator's liveness probe in production)",
             "Run two instances behind Eureka so the Gateway's load balancer keeps serving while one restarts"],
            f"{rerun}  # with the undo step removed, expect the service back on its own", "low", 0.8))
    if "no_recovery" in codes or "slow_recovery" in codes:
        f = codes.get("no_recovery") or codes["slow_recovery"]
        out.append(_fix(
            "faster-recovery", "Shorten recovery after the fault ends",
            f["evidence"],
            ["Check whether the Gateway's circuit breaker stays open after the service returns; tune "
             "waitDurationInOpenState and the half-open probe count",
             "Make readiness depend on the database and Eureka registration so traffic waits for a ready instance"],
            f"{rerun}  # expect: recovery well under 30 s", "medium", 0.6))
    if kind == "spike" and ("user_errors" in codes or "hang_not_fail" in codes):
        out.append(_fix(
            "rate-limit", "Protect the Gateway from traffic spikes",
            codes.get("user_errors", codes.get("hang_not_fail"))["evidence"],
            ["Add Spring Cloud Gateway's RequestRateLimiter filter per route",
             "Scale the busiest service (customers) to two instances"],
            f"{rerun}  # expect: errors bounded during the spike", "medium", 0.6))
    if "prediction_wrong" in codes:
        out.append(_fix(
            "update-model", "Correct the dependency model's rule",
            codes["prediction_wrong"]["evidence"],
            ["Update the matching rule in dependency-semantics.json with what this run measured, "
             "and record the run as its evidence"],
            "python3 -m chaoslab impact " + f"{target} --failure {kind}", "low", 0.9))
    return out


# ------------------------------------------------------------------ hypothesis
def hypothesis_check(result: dict, plan: dict | None) -> dict | None:
    hypothesis = (plan or {}).get("hypothesis")
    if not hypothesis:
        return None
    observed = observed_outcomes(result)
    misses = [{"action": a, "expected": e, "observed": observed.get(a)}
              for a, e in (hypothesis.get("expected") or {}).items() if a in observed and observed[a] != e]
    peak = result.get("peak_error_rate") or 0
    error_ok = peak <= (hypothesis.get("expected_max_error_rate") or 0) + 0.1
    rec, expected_rec = result.get("recovery_s"), hypothesis.get("expected_recovery_s")
    recovery_ok = rec is None or expected_rec is None or rec <= 1.5 * expected_rec + 5
    return {"held": not misses and error_ok and recovery_ok and result.get("recovered") is not False,
            "misses": misses, "error_rate": {"expected_max": hypothesis.get("expected_max_error_rate"),
                                             "observed_peak": peak, "ok": error_ok},
            "recovery_s": {"expected": expected_rec, "observed": rec, "ok": recovery_ok},
            "planned_by": plan.get("planned_by")}


# ------------------------------------------------------------------ prose
PROSE_SYSTEM = (
    "You are an SRE explaining one chaos experiment on the Spring PetClinic microservices sandbox to a "
    "hackathon jury. Use only the facts given; never change or invent a number. Reply with one JSON object: "
    '{"summary": "two sentences", "likely_cause": "two sentences", "production_impact": "two sentences", '
    '"extra_fixes": [{"title": str, "steps": [str], "why": str}]}. At most two extra fixes, only ones the '
    "listed fixes do not already cover; return an empty list when there is nothing to add.")


def template_prose(result: dict, found: list[dict], score: dict | None) -> dict:
    fault = result.get("fault") or {}
    top = found[0] if found else None
    summary = (f"{fault.get('kind')} on {fault.get('target')}: {top['title'].lower() if top else 'no findings'}"
               + (f"; risk {score['score']}/10 ({score['level']})." if score else "."))
    cause = top["evidence"] if top else ""
    return {"summary": summary, "likely_cause": cause, "production_impact": "", "extra_fixes": []}


def ai_prose(llm: LLM, result: dict, found: list[dict], score: dict | None, fix_list: list[dict]) -> dict | None:
    facts = {"fault": result.get("fault"), "outcome": result.get("outcome"), "abort_reason": result.get("abort_reason"),
             "peak_error_rate": result.get("peak_error_rate"), "peak_p95_ms": result.get("peak_p95_ms"),
             "recovery_s": result.get("recovery_s"), "observed_by_action": observed_outcomes(result),
             "findings": found, "risk": score, "fixes": [f["title"] for f in fix_list]}
    data = llm.chat_json(PROSE_SYSTEM, json.dumps(facts, default=str), max_tokens=900)
    if not data:
        return None
    clean = lambda v: " ".join(str(v or "").split())[:600]   # noqa: E731
    extras = []
    for x in (data.get("extra_fixes") or [])[:2]:
        if isinstance(x, dict) and x.get("title"):
            extras.append({"id": "ai-suggestion", "title": clean(x["title"])[:120], "why": clean(x.get("why")),
                           "steps": [clean(s) for s in (x.get("steps") or [])[:4]], "verify": "",
                           "effort": "unknown", "confidence": 0.5, "source": "ai, unverified"})
    return {"summary": clean(data.get("summary")), "likely_cause": clean(data.get("likely_cause")),
            "production_impact": clean(data.get("production_impact")), "extra_fixes": extras}


# ------------------------------------------------------------------ one run
def verdict(result: dict) -> str:
    if result.get("recovery_pending"):
        return "recovering"
    if result.get("outcome") == "refused":
        return "refused"
    if result.get("outcome") in ("stopped", "interrupted"):
        return "stopped early"
    if result.get("data_loss"):
        return "data lost"
    if result.get("recovered") is False:
        return "no recovery"
    return "tripped" if result.get("outcome") == "aborted" else "held"


def analyze(result: dict, snapshot: dict | None = None, llm: LLM | None = None) -> dict:
    found = findings(result, snapshot)
    score = risk(result, found)
    fix_list = fixes(result, found)
    prose, by = template_prose(result, found, score), "rules"
    if llm is not None and result.get("outcome") != "refused" and llm.available():
        written = ai_prose(llm, result, found, score, fix_list)
        if written:
            fix_list += written.pop("extra_fixes")
            prose, by = written, "ai+rules"
    return {"run_id": result.get("id"), "fault": result.get("fault"), "verdict": verdict(result),
            "risk": score, "findings": found, "fixes": fix_list,
            "observed": observed_outcomes(result) if result.get("outcome") != "refused" else {},
            "hypothesis": hypothesis_check(result, result.get("plan")), "prose": prose,
            "written_by": by, "model": llm.describe() if by == "ai+rules" and llm else None,
            "peak_error_rate": result.get("peak_error_rate"), "peak_p95_ms": result.get("peak_p95_ms"),
            "recovered": result.get("recovered"), "recovery_s": result.get("recovery_s")}


def markdown(a: dict) -> str:
    fault = a["fault"] or {}
    lines = [f"# {fault.get('kind')} on {fault.get('target')} ({a['run_id']})", "",
             f"**Verdict:** {a['verdict']}" + (f"  **Risk:** {a['risk']['score']}/10 {a['risk']['level']}" if a["risk"] else ""),
             "", a["prose"]["summary"], ""]
    if a["prose"].get("likely_cause"):
        lines += [f"**Likely cause.** {a['prose']['likely_cause']}", ""]
    if a["prose"].get("production_impact"):
        lines += [f"**In production.** {a['prose']['production_impact']}", ""]
    lines += ["## Findings", ""] + [f"- **{f['severity']}** `{f['code']}` {f['title']}. {f['evidence']}" for f in a["findings"]]
    if a["hypothesis"]:
        h = a["hypothesis"]
        lines += ["", f"## Hypothesis ({h['planned_by']}): {'held' if h['held'] else 'did not hold'}", ""]
        lines += [f"- {m['action']}: expected {m['expected']}, observed {m['observed']}" for m in h["misses"]]
    if a["fixes"]:
        lines += ["", "## Recommended fixes (advice only, nothing was changed)", ""]
        for f in a["fixes"]:
            lines += [f"### {f['title']} ({f['source']}, confidence {f['confidence']:.0%})", "", f"Why: {f['why']}", ""]
            lines += [f"{i}. {s}" for i, s in enumerate(f["steps"], 1)]
            if f["verify"]:
                lines += ["", f"Verify: `{f['verify']}`"]
            lines.append("")
    lines += ["", f"_Numbers, findings and scores computed by code; text written by {a['written_by']}"
              + (f" ({a['model']})" if a["model"] else "") + "._"]
    return "\n".join(lines) + "\n"


def save(a: dict, runs_dir: Path = RUNS_DIR) -> Path:
    run = runs_dir / a["run_id"]
    (run / "analysis.json").write_text(json.dumps(a, indent=2, default=str))
    (run / "analysis.md").write_text(markdown(a))
    return run / "analysis.json"


# ------------------------------------------------------------------ campaign scorecard and report
def _penalty(a: dict) -> float:
    codes = {f["code"] for f in a["findings"]}
    return (40 * min(a.get("peak_error_rate") or 0, 1.0)
            + 20 * (1.0 if a.get("recovered") is False else min((a.get("recovery_s") or 0) / 120, 1.0))
            + 25 * ("data_loss" in codes) + 15 * ("silent_degradation" in codes) + 10 * ("collateral_damage" in codes)
            + 5 * ("hang_not_fail" in codes) + 5 * ("no_self_healing" in codes) + 20 * ("no_recovery" in codes))


def grade(score: float) -> str:
    return "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 65 else "D" if score >= 50 else "F"


def scorecard(analyses: list[dict], services: list[str]) -> dict:
    by_service: dict[str, list[dict]] = {s: [] for s in services}
    for a in analyses:
        if a["verdict"] not in ("refused", "recovering"):
            by_service.setdefault(a["fault"]["target"], []).append(a)
    rows = []
    for service, runs in by_service.items():
        if not runs:
            rows.append({"service": service, "score": None, "grade": None, "runs": 0, "tested": [], "weakest": None})
            continue
        score = round(max(0.0, 100 - statistics.mean(_penalty(a) for a in runs)), 1)
        worst = max(runs, key=lambda a: (a["risk"] or {}).get("score", 0))
        rows.append({"service": service, "score": score, "grade": grade(score), "runs": len(runs),
                     "tested": sorted({a["fault"]["kind"] for a in runs}),
                     "weakest": f"{worst['fault']['kind']}: {worst['findings'][0]['title']}"})
    rows.sort(key=lambda r: (r["score"] is None, r["score"] if r["score"] is not None else 0))
    graded = [r["score"] for r in rows if r["score"] is not None]
    overall = round(statistics.mean(graded), 1) if graded else None
    return {"overall": overall, "overall_grade": grade(overall) if overall is not None else None,
            "services": rows, "tested_services": len(graded), "total_services": len(rows)}


def weaknesses(analyses: list[dict]) -> list[dict]:
    """Every non-informational finding, merged per (finding, service), worst first."""
    merged: dict[tuple[str, str], dict] = {}
    for a in analyses:
        target = (a["fault"] or {}).get("target")
        for f in a["findings"]:
            if f["severity"] == "info":
                continue
            key = (f["code"], target)
            entry = merged.setdefault(key, {**f, "service": target, "runs": [], "risk": 0.0, "fixes": []})
            entry["runs"].append(a["run_id"])
            entry["risk"] = max(entry["risk"], (a["risk"] or {}).get("score", 0))
            aimed = [fix for fix in a["fixes"] if f["code"] in fix.get("addresses", [])]
            for fix in aimed or a["fixes"][:1]:   # a symptom such as user_errors takes the run's top fix
                if fix["title"] not in [x["title"] for x in entry["fixes"]] and fix["source"] == "library":
                    entry["fixes"].append(fix)
    return sorted(merged.values(), key=lambda w: (-SEVERITY_RANK[w["severity"]], -w["risk"], -len(w["runs"])))


def load_all(runs_dir: Path = RUNS_DIR, snapshot: dict | None = None, llm: LLM | None = None,
             refresh: bool = False) -> list[dict]:
    """Analyses of every finished run, computing and saving the missing ones."""
    out = []
    for path in sorted(runs_dir.glob("*/result.json")):
        try:
            result = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if result.get("recovery_pending"):
            continue
        saved = path.parent / "analysis.json"
        if saved.exists() and not refresh:
            try:
                out.append(json.loads(saved.read_text()))
                continue
            except ValueError:
                pass
        result.setdefault("id", path.parent.name)
        analysis = analyze(result, snapshot, llm)
        save(analysis, runs_dir)
        out.append(analysis)
    return out


def report(analyses: list[dict], services: list[str], fault_kinds: list[str], entry_point: str) -> dict:
    card = scorecard(analyses, services)
    real = [a for a in analyses if a["verdict"] not in ("refused", "recovering")]
    pairs = {(a["fault"]["target"], a["fault"]["kind"]) for a in real}
    possible = len(services) * (len(fault_kinds) - 1) + (1 if "spike" in fault_kinds else 0)
    held = [a["hypothesis"]["held"] for a in real if a.get("hypothesis")]
    return {"scorecard": card, "weaknesses": weaknesses(real), "experiments": len(real),
            "refused": len(analyses) - len(real), "coverage": {"tested_pairs": len(pairs), "possible_pairs": possible},
            "hypotheses": {"held": sum(held), "checked": len(held)},
            "risk_levels": {lvl: sum(1 for a in real if (a["risk"] or {}).get("level") == lvl)
                            for lvl in ("critical", "high", "medium", "low")}}


def report_markdown(r: dict) -> str:
    card = r["scorecard"]
    lines = ["# ChaosLab resilience report", "",
             f"**Overall resilience: {card['overall']}/100 ({card['overall_grade']})** across {r['experiments']} "
             f"experiments on {card['tested_services']} of {card['total_services']} services. "
             f"Coverage: {r['coverage']['tested_pairs']} of {r['coverage']['possible_pairs']} service and fault pairs.",
             "", "Scores, grades and findings are computed by code from the saved runs. Fixes are advice; "
             "nothing was changed in the system.", "", "## Ranked weaknesses", "",
             "| # | Severity | Weakness | Service | Runs | Worst risk | Top fix |", "|---|---|---|---|---|---|---|"]
    for i, w in enumerate(r["weaknesses"], 1):
        top = w["fixes"][0]["title"] if w["fixes"] else "–"
        lines.append(f"| {i} | {w['severity']} | {w['title']} | `{w['service']}` | {len(w['runs'])} | {w['risk']} | {top} |")
    lines += ["", "## Scorecard (worst first)", "", "| Service | Score | Grade | Runs | Faults tested | Weakest result |",
              "|---|---|---|---|---|---|"]
    for row in card["services"]:
        lines.append(f"| `{row['service']}` | {row['score'] if row['score'] is not None else 'not tested'} | "
                     f"{row['grade'] or '–'} | {row['runs']} | {', '.join(row['tested']) or '–'} | {row['weakest'] or '–'} |")
    fixes_seen, fix_lines = set(), []
    for w in r["weaknesses"]:
        for f in w["fixes"]:
            if f["title"] not in fixes_seen:
                fixes_seen.add(f["title"])
                fix_lines += [f"### {f['title']}", "", f"Why: {f['why']}", ""] + \
                             [f"{i}. {s}" for i, s in enumerate(f["steps"], 1)] + ["", f"Verify: `{f['verify']}`", ""]
    lines += ["", "## Fixes, most important first", ""] + fix_lines
    return "\n".join(lines) + "\n"


def write_report(r: dict) -> list[Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(r, indent=2, default=str))
    REPORT_MD.write_text(report_markdown(r))
    return [REPORT_MD, REPORT_JSON]
