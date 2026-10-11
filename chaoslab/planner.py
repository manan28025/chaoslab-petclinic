"""AI planner: chooses the next experiment. The model proposes, the code decides.

1. Code ranks every allowed (service, fault) pair from the dependency snapshot, the impact
   prediction for that pair and the run history: how many users are exposed, what the
   prediction says breaks, how much of the prediction is still unverified, and what was
   already tested.
2. The model (optional) investigates with read-only tools, picks one of the top candidates,
   tunes its strength inside planner bounds and writes a falsifiable hypothesis. It submits
   through one tool; code validates the submission and returns the reason when it rejects it,
   so the model can correct itself (at most three submissions, six turns).
3. Without a model, or when it fails, the top-ranked candidate runs with a hypothesis taken
   from the code prediction. Either way the plan records who planned it and why.

Plans that could do lasting harm (a crash that wipes an in-memory database, an outage of a
start-up dependency, a large blast radius, heavy settings) need a person's approval.
"""

import json
import re
import statistics
import time
from pathlib import Path

from . import blast_radius
from .faults import FAULTS
from .llm import LLM, extract_json
from .settings import ACTION_ROUTES, RUNS_DIR

PLANS_DIR = RUNS_DIR / "plans"
ACTIONS = list(ACTION_ROUTES)
OUTCOMES = ("unavailable", "degraded", "preserved")
TOP_N = 5
MAX_TURNS = 6            # model calls per plan, investigation included
INVESTIGATE_TURNS = 3    # after this many turns the model must submit
MAX_SUBMISSIONS = 3      # first try plus two corrections
DURATION_S = (15, 60)    # fault length the planner may choose
DEFAULT_DURATION_S = 30

# Defaults and the planner's own bounds, tighter than the faults' hard limits: an autonomous
# plan may not cut a service off completely or use the heaviest settings.
PARAMS = {
    "delay": {"delay_ms": (1000, 100, 3000)},
    "loss": {"loss_pct": (30, 5, 60)},
    "cpu": {"cpus": (0.1, 0.05, 1.0)},
    "spike": {"multiplier": (5, 1.5, 6)},
}
# How much a first run of each fault type usually teaches; breaks near-ties.
INFORMATIVENESS = {"kill": 0.4, "pause": 0.35, "delay": 0.3, "loss": 0.3, "cpu": 0.15, "spike": 0.2}
OUTCOME_WEIGHT = {"unavailable": 1.0, "degraded": 0.7, "uncertain": 0.5, "preserved": 0.0}
TYPICAL_RECOVERY_S = {"kill": 30, "pause": 20, "delay": 10, "loss": 10, "cpu": 10, "spike": 10}

SYSTEM = """You are the planning agent of ChaosLab, a chaos-engineering tool for a local Spring PetClinic
microservices sandbox (users -> api-gateway -> customers/visits/vets services, each with an in-memory
database; config-server and discovery-server are start-up dependencies).
Choose the single most informative next experiment from the ranked candidates that code produced.
Rules:
- You may only choose a listed candidate index. Code clamps parameters to the bounds shown.
- Prefer candidates whose predicted impact is unverified or uncertain: confirming or refuting a
  prediction teaches the most. Avoid repeating what history already answered.
- Investigate with the read-only tools first (several calls per turn are fine), then call
  submit_plan exactly once. If a submission is rejected, read the reason and submit a corrected plan.
- The hypothesis must be falsifiable: for each user action you expect to be affected, say
  unavailable, degraded or preserved; give the highest error rate you expect (0..1) and the seconds
  you expect users need to get normal service back after the fault is undone.
- The rationale is one plain sentence for a live demo audience."""


# ------------------------------------------------------------------ history
def load_history(runs_dir: Path = RUNS_DIR) -> list[dict]:
    """Finished experiments, oldest first, without the bulky timelines."""
    out = []
    if not runs_dir.exists():
        return out
    for path in sorted(runs_dir.glob("*/result.json")):
        try:
            r = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if r.get("recovery_pending"):
            continue
        comparison = r.get("prediction_comparison") or {}
        out.append({
            "id": r.get("id", path.parent.name), "kind": (r.get("fault") or {}).get("kind"),
            "target": (r.get("fault") or {}).get("target"), "params": (r.get("fault") or {}).get("params", {}),
            "outcome": r.get("outcome"), "abort_reason": r.get("abort_reason"),
            "recovered": r.get("recovered"), "recovery_s": r.get("recovery_s"),
            "peak_error_rate": r.get("peak_error_rate"), "peak_p95_ms": r.get("peak_p95_ms"),
            "data_loss": bool(r.get("data_loss")),
            "predictions": {name: c.get("match") for name, c in comparison.items()},
        })
    return out


def _tested(history: list[dict]) -> dict[tuple[str, str], list[dict]]:
    tested: dict[tuple[str, str], list[dict]] = {}
    for run in history:
        if run["outcome"] not in (None, "refused"):
            tested.setdefault((run["target"], run["kind"]), []).append(run)
    return tested


# ------------------------------------------------------------------ ranking
def _nodes(snapshot: dict) -> dict[str, dict]:
    return {n["id"]: n for n in snapshot["nodes"]}


def feature_outcomes(prediction: dict) -> dict[str, dict]:
    """User action -> the predicted runtime item for it (worst outcome when several rules apply)."""
    rank = {"unavailable": 3, "degraded": 2, "uncertain": 1, "preserved": 0}
    out: dict[str, dict] = {}
    for item in prediction["runtime"]:
        name = item.get("feature")
        if name and (name not in out or rank.get(item["outcome"], 0) > rank.get(out[name]["outcome"], 0)):
            out[name] = item
    return out


def default_params(kind: str) -> dict:
    return {name: spec[0] for name, spec in PARAMS.get(kind, {}).items()}


def candidates(snapshot: dict, history: list[dict], allowed: set[str]) -> list[dict]:
    nodes, entry = _nodes(snapshot), snapshot["entry_point"]
    restart = snapshot.get("restart_effects") or {}
    tested = _tested(history)
    last_target = next((r["target"] for r in reversed(history) if r["outcome"] not in (None, "refused")), None)
    out = []
    for target in sorted(allowed):
        node = nodes.get(target)
        if not node or node.get("kind") != "container" or node.get("active") is False:
            continue
        exposure = 1.0 if target == entry else float(node.get("share_of_requests") or 0)
        for kind in FAULTS:
            if kind == "spike" and target != entry:
                continue
            prediction = blast_radius.analyze(snapshot, target, kind)
            features = feature_outcomes(prediction)
            impact = sum(OUTCOME_WEIGHT.get(item["outcome"], 0) for item in features.values())
            unverified = [i for i in prediction["runtime"] + prediction["startup_restart"]
                          if i["outcome"] == "uncertain" or i.get("basis") not in ("measured", None)]
            learning = min(0.25 * len(unverified), 1.5)
            runs = tested.get((target, kind), [])
            priority = (2.0 * exposure + impact + learning) * (1.4 if not runs else 1.0)
            priority += INFORMATIVENESS[kind] - 1.5 * len(runs) - (0.8 if target == last_target else 0)
            reasons = [f"{round(exposure * 100)}% of requests pass through it" if exposure else "on no user path"]
            hit = {o: [f for f, i in features.items() if i["outcome"] == o] for o in ("unavailable", "degraded")}
            if hit["unavailable"] or hit["degraded"]:
                reasons.append("predicted " + "; ".join(f"{o}: {', '.join(names)}" for o, names in hit.items() if names))
            if unverified:
                reasons.append(f"{len(unverified)} unverified prediction{'s' if len(unverified) != 1 else ''}")
            reasons.append("untested" if not runs else f"tested {len(runs)}x")
            if target == last_target:
                reasons.append("same target as the last run")
            approval = []
            if kind == "kill" and restart.get(target, {}).get("data"):
                approval.append(f"a crash loses data: {restart[target]['data']}")
            if node.get("role") == "platform" and kind in ("kill", "pause"):
                dependents = len(node.get("needed_at_startup_by") or [])
                approval.append(f"start-up dependency of {dependents} services")
            if len(hit["unavailable"]) >= 3:
                approval.append(f"predicted to take down {len(hit['unavailable'])} user actions")
            out.append({"target": target, "kind": kind, "priority": round(priority, 2), "reasons": reasons,
                        "predicted": {f: i["outcome"] for f, i in features.items()},
                        "requires_approval": bool(approval), "approval_reason": "; ".join(approval),
                        "measured_recovery_s": statistics.median([r["recovery_s"] for r in runs if r["recovery_s"] is not None])
                        if any(r["recovery_s"] is not None for r in runs) else None})
    out.sort(key=lambda c: (-c["priority"], c["target"], c["kind"]))
    for rank, c in enumerate(out, 1):
        c["rank"] = rank
    return out


# ------------------------------------------------------------------ validation
def clamp_params(kind: str, params: dict) -> tuple[dict, list[str]]:
    specs, out, notes = PARAMS.get(kind, {}), {}, []
    unknown = sorted(set(params or {}) - set(specs))
    if unknown:
        notes.append(f"ignored settings {kind} does not take: {', '.join(unknown)}")
    for name, (default, low, high) in specs.items():
        raw = (params or {}).get(name, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            notes.append(f"{name}={raw!r} is not a number; used {default}")
            value = default
        if value < low or value > high:
            clamped = min(max(value, low), high)
            notes.append(f"{name} {value:g} moved inside the planner's bounds to {clamped:g}")
            value = clamped
        out[name] = int(value) if float(value).is_integer() else round(value, 2)
    return out, notes


def default_hypothesis(candidate: dict) -> dict:
    """The code prediction as a falsifiable claim. An uncertain prediction is stated as degraded,
    the cautious guess, and named as uncertain so the run settles it."""
    predicted = candidate["predicted"]
    expected = {f: ("degraded" if o == "uncertain" else o) for f, o in predicted.items() if o in OUTCOMES + ("uncertain",)}
    unavailable = sum(1 for o in expected.values() if o == "unavailable")
    degraded = any(o == "degraded" for o in expected.values())
    max_error = (min(1.0, round(unavailable / len(ACTIONS) + 0.1, 2)) if unavailable
                 else 0.25 if degraded else 0.05)
    recovery = candidate.get("measured_recovery_s") or TYPICAL_RECOVERY_S[candidate["kind"]]
    parts = [f"{', '.join(names)} {o}" for o in ("unavailable", "degraded")
             if (names := [f for f, x in predicted.items() if x == o])]
    if unsure := [f for f, x in predicted.items() if x == "uncertain"]:
        parts.append(f"{', '.join(unsure)} uncertain, expected degraded")
    statement = (f"{FAULTS[candidate['kind']].summary.split(':')[0]} on {candidate['target']}: "
                 f"{'; '.join(parts) or 'no user action affected'}; at most {max_error:.0%} of requests fail "
                 f"and users recover within {recovery:.0f} s after the fault is undone")
    return {"statement": statement, "expected": expected, "expected_max_error_rate": max_error,
            "expected_recovery_s": float(recovery)}


def validate(submission: dict, top: list[dict]) -> tuple[dict | None, str]:
    """Code disposes: returns (accepted plan parts, "") or (None, reason for the model)."""
    if not isinstance(submission, dict):
        return None, "submission must be a JSON object"
    try:
        choice = int(submission.get("choice"))
    except (TypeError, ValueError):
        return None, f"choice must be one of the candidate indexes 0..{len(top) - 1}"
    if not 0 <= choice < len(top):
        return None, f"choice {choice} is not a listed candidate; pick 0..{len(top) - 1}"
    candidate = top[choice]
    hypothesis = submission.get("hypothesis")
    if not isinstance(hypothesis, dict) or not str(hypothesis.get("statement") or "").strip():
        return None, "hypothesis.statement is required: a falsifiable prediction"
    notes = []
    params, clamp_notes = clamp_params(candidate["kind"], submission.get("parameters") or {})
    notes += clamp_notes
    try:
        duration = int(float(submission.get("duration_s", DEFAULT_DURATION_S)))
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION_S
    if not DURATION_S[0] <= duration <= DURATION_S[1]:
        clamped = min(max(duration, DURATION_S[0]), DURATION_S[1])
        notes.append(f"duration {duration} s moved inside the planner's bounds to {clamped} s")
        duration = clamped
    expected = {}
    for action, outcome in (hypothesis.get("expected") or {}).items():
        if action in ACTIONS and outcome in OUTCOMES:
            expected[action] = outcome
        else:
            notes.append(f"dropped hypothesis entry {action!r}: {outcome!r} (unknown action or outcome)")
    fallback = default_hypothesis(candidate)
    try:
        max_error = min(max(float(hypothesis.get("expected_max_error_rate", fallback["expected_max_error_rate"])), 0.0), 1.0)
        recovery = min(max(float(hypothesis.get("expected_recovery_s", fallback["expected_recovery_s"])), 1.0), 300.0)
    except (TypeError, ValueError):
        return None, "expected_max_error_rate and expected_recovery_s must be numbers"
    rationale = " ".join(str(submission.get("rationale") or "").split())[:300]
    return {"candidate": candidate, "params": params, "duration_s": duration, "notes": notes,
            "rationale": rationale, "hypothesis": {
                "statement": " ".join(str(hypothesis["statement"]).split())[:400],
                "expected": expected or fallback["expected"],
                "expected_max_error_rate": round(max_error, 3), "expected_recovery_s": round(recovery, 1)}}, ""


# ------------------------------------------------------------------ the model's tools
TOOLS = [
    {"type": "function", "function": {
        "name": "inspect_service", "description": "Role, traffic share, user actions, callers, dependencies, "
        "restart behaviour and past results of one service.",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}}},
    {"type": "function", "function": {
        "name": "predict_impact", "description": "The code's evidence-backed impact prediction for one listed "
        "candidate: per user action, the outcome, behaviour, basis (measured or inferred) and open questions.",
        "parameters": {"type": "object", "properties": {"choice": {"type": "integer"}}, "required": ["choice"]}}},
    {"type": "function", "function": {
        "name": "experiment_history", "description": "Past experiments, optionally for one service: outcome, "
        "recovery, peak errors, data loss, and which predictions matched.",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "fault_info", "description": "What a fault type does, how it is undone and the planner's bounds.",
        "parameters": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(FAULTS)}},
                       "required": ["kind"]}}},
    {"type": "function", "function": {
        "name": "submit_plan", "description": "Commit to one listed candidate. Code validates it and replies "
        "ACCEPTED or REJECTED with the reason.",
        "parameters": {"type": "object", "properties": {
            "choice": {"type": "integer", "description": "candidate index"},
            "parameters": {"type": "object", "description": "fault settings inside the bounds shown"},
            "duration_s": {"type": "integer", "description": f"{DURATION_S[0]}..{DURATION_S[1]} seconds"},
            "rationale": {"type": "string"},
            "hypothesis": {"type": "object", "properties": {
                "statement": {"type": "string"},
                "expected": {"type": "object", "description": "user action -> unavailable | degraded | preserved"},
                "expected_max_error_rate": {"type": "number"},
                "expected_recovery_s": {"type": "number"}}, "required": ["statement"]}},
            "required": ["choice", "hypothesis"]}}},
]


class Tools:
    """Read-only views for the model; nothing here can change the sandbox."""

    def __init__(self, snapshot: dict, history: list[dict], top: list[dict]):
        self.snapshot, self.history, self.top = snapshot, history, top

    def inspect_service(self, service: str) -> dict:
        node = _nodes(self.snapshot).get(service)
        if not node:
            return {"error": f"unknown service {service}"}
        restart = (self.snapshot.get("restart_effects") or {}).get(service)
        return {"service": service, "role": node.get("role"), "share_of_requests": node.get("share_of_requests"),
                "user_actions": node.get("user_actions"), "called_by": node.get("callers"), "calls": node.get("calls"),
                "needed_at_startup_by": node.get("needed_at_startup_by"), "instances": node.get("instances"),
                "restarts_by_itself": node.get("auto_restart"), "risk_hints": node.get("risk_hints"),
                "on_restart": restart, "past_runs": [r for r in self.history if r["target"] == service][-5:]}

    def predict_impact(self, choice: int) -> dict:
        if not 0 <= int(choice) < len(self.top):
            return {"error": f"choice must be 0..{len(self.top) - 1}"}
        c = self.top[int(choice)]
        p = blast_radius.analyze(self.snapshot, c["target"], c["kind"])
        keep = ("component", "feature", "outcome", "behavior", "basis", "http_status", "latency", "uncertainties")
        return {"candidate": f"{c['kind']} on {c['target']}",
                "while_faulted": [{k: i[k] for k in keep if i.get(k)} for i in p["runtime"]],
                "after_restart": [{k: i[k] for k in keep if i.get(k)} for i in p["startup_restart"]][:6]}

    def experiment_history(self, service: str = "") -> list[dict]:
        runs = [r for r in self.history if not service or r["target"] == service]
        return runs[-10:]

    def fault_info(self, kind: str) -> dict:
        fault = FAULTS.get(kind)
        if not fault:
            return {"error": f"unknown fault {kind}"}
        return {"kind": kind, "what": fault.summary, "how": fault.mechanism, "undo": fault.undo,
                "planner_bounds": {n: {"default": d, "min": lo, "max": hi} for n, (d, lo, hi) in PARAMS.get(kind, {}).items()},
                "duration_s": {"default": DEFAULT_DURATION_S, "min": DURATION_S[0], "max": DURATION_S[1]}}

    def call(self, name: str, args: dict):
        if name in ("inspect_service", "predict_impact", "experiment_history", "fault_info"):
            return getattr(self, name)(**args)
        return {"error": f"unknown tool {name}"}


def briefing(snapshot: dict, history: list[dict], top: list[dict]) -> str:
    services = [f"- {n['id']}: {n.get('role')}, {round((n.get('share_of_requests') or 0) * 100)}% of requests, "
                f"user actions {n.get('user_actions') or []}" for n in snapshot["nodes"]
                if n.get("kind") == "container" and n.get("active") is not False]
    cands = []
    for i, c in enumerate(top):
        bounds = ", ".join(f"{n} {lo}..{hi} (default {d})" for n, (d, lo, hi) in PARAMS.get(c["kind"], {}).items())
        cands.append(f"[{i}] {c['kind']} on {c['target']}  priority {c['priority']}  ({'; '.join(c['reasons'])})"
                     + (f"  settings: {bounds}" if bounds else "")
                     + (f"  NEEDS APPROVAL: {c['approval_reason']}" if c["requires_approval"] else ""))
    past = [f"- {r['kind']} on {r['target']}: {r['outcome']}, recovered {r['recovery_s']} s, "
            f"peak errors {r['peak_error_rate']}, data loss {r['data_loss']}" for r in history[-6:]] or ["- none yet"]
    return ("Services:\n" + "\n".join(services) + f"\n\nUser actions: {', '.join(ACTIONS)}"
            + "\n\nCandidates ranked by code (choose one index):\n" + "\n".join(cands)
            + "\n\nRecent experiments:\n" + "\n".join(past)
            + "\n\nInvestigate, then call submit_plan.")


# ------------------------------------------------------------------ planning
def _plan_id(candidate: dict) -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{candidate['kind']}-{candidate['target']}"


def _finish(accepted: dict, top: list[dict], all_candidates: list[dict], snapshot: dict, planned_by: str,
            model: str | None, notes: list[str], trace: list[dict]) -> dict:
    c = accepted["candidate"]
    heavy = []
    p = accepted["params"]
    if p.get("delay_ms", 0) >= 2500 or p.get("loss_pct", 0) >= 50 or p.get("multiplier", 0) > 5:
        heavy.append("heavy settings")
    reasons = [r for r in (c["approval_reason"], *heavy) if r]
    rationale = accepted["rationale"] or (
        f"{c['kind']} on {c['target']} ranks #{c['rank']}: {'; '.join(c['reasons'])}")
    return {
        "id": _plan_id(c), "created_at": time.time(), "snapshot_id": snapshot.get("snapshot_id"),
        "planned_by": planned_by, "model": model, "kind": c["kind"], "target": c["target"],
        "params": accepted["params"], "duration_s": accepted["duration_s"], "rationale": rationale,
        "hypothesis": accepted["hypothesis"], "candidate": c, "candidates": all_candidates[:max(TOP_N, 8)],
        "requires_approval": bool(reasons), "approval_reason": "; ".join(reasons), "approved": False,
        "notes": notes + accepted["notes"], "trace": trace,
    }


def _deterministic(top: list[dict]) -> dict:
    c = top[0]
    return {"candidate": c, "params": default_params(c["kind"]), "duration_s": DEFAULT_DURATION_S,
            "rationale": "", "hypothesis": default_hypothesis(c), "notes": []}


def _ask_model(llm: LLM, snapshot: dict, history: list[dict], top: list[dict], trace: list[dict],
               notes: list[str]) -> dict | None:
    tools = Tools(snapshot, history, top)
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": briefing(snapshot, history, top)}]
    submissions = 0
    for turn in range(1, MAX_TURNS + 1):
        force = turn > INVESTIGATE_TURNS
        reply = llm.chat(messages, tools=TOOLS, max_tokens=1400,
                         tool_choice={"type": "function", "function": {"name": "submit_plan"}} if force else "auto")
        if reply is None and force:   # some providers reject a forced tool choice
            reply = llm.chat(messages, tools=TOOLS, max_tokens=1400)
        if reply is None:
            notes.append(f"model call failed on turn {turn}: {llm.last_error}")
            return None
        calls = reply["tool_calls"]
        messages.append({"role": "assistant", "content": reply["content"] or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            submission = extract_json(reply["content"])
            trace.append({"turn": turn, "text": (reply["content"] or "")[:300]})
            if submission is None:
                messages.append({"role": "user", "content": "Call submit_plan now with your chosen candidate."})
                continue
            accepted, why = validate(submission, top)
            submissions += 1
            if accepted:
                return accepted
            messages.append({"role": "user", "content": f"REJECTED: {why}. Submit a corrected plan."})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except ValueError:
                args = None
            if not isinstance(args, dict):
                result = "Your tool arguments were not valid JSON; call the tool again."
            elif name == "submit_plan":
                submissions += 1
                accepted, why = validate(args, top)
                trace.append({"turn": turn, "tool": name, "args": args, "result": "accepted" if accepted else why})
                if accepted:
                    return accepted
                result = f"REJECTED: {why}"
            else:
                try:
                    result = json.dumps(tools.call(name, args), default=str)[:6000]
                except (TypeError, ValueError) as err:
                    result = json.dumps({"error": str(err)})
                trace.append({"turn": turn, "tool": name, "args": args})
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        if submissions >= MAX_SUBMISSIONS:
            notes.append(f"model's plan rejected {submissions} times; used the top-ranked candidate")
            return None
    notes.append(f"model did not submit a valid plan within {MAX_TURNS} turns; used the top-ranked candidate")
    return None


def plan(snapshot: dict, allowed: set[str], history: list[dict] | None = None, llm: LLM | None = None,
         exclude: set[tuple[str, str]] = frozenset()) -> dict | None:
    """The next experiment, or None when nothing allowed is left. `exclude` holds (target, kind)
    pairs a campaign already ran or a person declined."""
    history = load_history() if history is None else history
    ranked = [c for c in candidates(snapshot, history, allowed) if (c["target"], c["kind"]) not in exclude]
    if not ranked:
        return None
    top, notes, trace = ranked[:TOP_N], [], []
    accepted = None
    if llm is not None and llm.available():
        accepted = _ask_model(llm, snapshot, history, top, trace, notes)
        if accepted:
            return _finish(accepted, top, ranked, snapshot, "ai", llm.describe(), notes, trace)
    elif llm is not None:
        notes.append(f"no model: {llm.last_error}")
    return _finish(_deterministic(top), top, ranked, snapshot, "rules", None, notes, trace)


def save(plan_record: dict) -> Path:
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANS_DIR / f"{plan_record['id']}.json"
    path.write_text(json.dumps(plan_record, indent=2, default=str))
    return path


PLAN_ID = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z]+-[a-z0-9-]+")


def load(plan_id: str) -> dict:
    path = PLANS_DIR / f"{plan_id}.json"
    if not PLAN_ID.fullmatch(plan_id) or not path.exists():
        raise ValueError(f"no plan {plan_id!r} in {PLANS_DIR}")
    return json.loads(path.read_text())


def fault_params(plan_record: dict, grace_s: int) -> dict:
    """The fault's constructor arguments for a plan (network faults also need their own expiry)."""
    params = dict(plan_record["params"])
    if "expire_after_s" in FAULTS[plan_record["kind"]].limits:
        params["expire_after_s"] = plan_record["duration_s"] + grace_s
    return params
