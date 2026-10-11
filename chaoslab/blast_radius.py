"""Qualitative, read-only failure propagation over a dependency snapshot."""

from collections import defaultdict, deque

EVALUATOR_VERSION = 2   # 2: per-mode effects, latency and basis, restart data loss
FAILURE_MODES = {
    "kill": "unavailable",
    "pause": "unresponsive",
    "delay": "slow",
    "loss": "intermittent",
    "cpu": "slow",
    "spike": "slow",
    "stop": "unavailable",
    "partition": "unavailable",
}


def analyze(snapshot: dict, target: str, failure: str, phase: str | None = None,
            feature: str | None = None) -> dict:
    """Explain potential runtime and startup/restart impact without injecting a fault."""
    if failure not in FAILURE_MODES:
        raise ValueError(f"unknown failure type: {failure}")
    if phase not in (None, "runtime", "startup_restart"):
        raise ValueError(f"unknown phase: {phase}")
    if feature is not None and feature not in snapshot.get("features", {}):
        raise ValueError(f"unknown feature: {feature}")
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    if target not in nodes:
        raise ValueError(f"unknown target: {target}")
    if not nodes[target].get("active", True):
        raise ValueError(f"inactive target: {target}")

    by_provider = defaultdict(list)
    for dep in snapshot["dependencies"]:
        by_provider[dep["provider"]].append(dep)
    result = {"schema_version": 1, "evaluator_version": EVALUATOR_VERSION,
              "snapshot_id": snapshot.get("snapshot_id"),
              "scenario": {"target": target, "failure": failure, "mode": FAILURE_MODES[failure],
                           "phase": phase, "feature": feature},
              "runtime": [], "startup_restart": []}

    for result_key, phases in (("runtime", {"runtime"}),
                               ("startup_restart", {"startup", "restart"})):
        if phase is not None and result_key != phase:
            continue
        if result_key == "runtime" and feature is None:
            direct_outcome = "unavailable" if FAILURE_MODES[failure] == "unavailable" else "uncertain"
            result[result_key].append({
                "component": target, "service": target, "feature": None, "outcome": direct_outcome,
                "path": [target], "behavior": f"Target experiences {FAILURE_MODES[failure]}",
                "rule": "direct_target", "evidence": nodes[target].get("evidence", []),
                "uncertainties": ([] if direct_outcome == "unavailable"
                                  else ["Request impact depends on fault intensity and timing"]),
            })
        queue = deque([(target, [target], [], [])])
        expanded_paths = set()
        seen_impacts = set()
        while queue:
            provider, path, inherited_evidence, inherited_uncertainty = queue.popleft()
            path_key = tuple(path)
            if path_key in expanded_paths:
                continue
            expanded_paths.add(path_key)
            for dep in by_provider[provider]:
                if not phases.intersection(dep["phases"]):
                    continue
                consumer = dep["consumer"]
                if consumer in path or not nodes.get(consumer, {}).get("active", True):
                    continue
                effect = dep.get("effects", {}).get(FAILURE_MODES[failure])
                uncertainty = list(inherited_uncertainty)
                if effect is None:
                    effect = {"outcome": "uncertain", "behavior": "Failure behavior is not verified"}
                    uncertainty.append(f"{dep['id']}: no rule for {FAILURE_MODES[failure]}")
                if dep.get("verification") != "verified":
                    uncertainty.append(f"{dep['id']}: relationship is {dep.get('verification', 'unknown')}")
                evidence = inherited_evidence + dep.get("evidence", [])
                impact_path = [consumer] + path
                component = dep.get("feature") or consumer
                key = (dep["id"], effect["outcome"], tuple(impact_path))
                if key not in seen_impacts and (feature is None or dep.get("feature") == feature):
                    seen_impacts.add(key)
                    impact = {"component": component, "service": consumer,
                              "feature": dep.get("feature"), "outcome": effect["outcome"],
                              "path": impact_path, "behavior": effect.get("behavior", "unknown"),
                              "rule": dep["id"], "evidence": evidence,
                              "assumptions": effect.get("assumptions", []),
                              "uncertainties": uncertainty + effect.get("uncertainties", [])}
                    for field in ("http_status", "data", "latency", "basis"):
                        if field in effect:
                            impact[field] = effect[field]
                    result[result_key].append(impact)
                if effect.get("propagate_service") and effect["outcome"] == "unavailable":
                    queue.append((consumer, impact_path, evidence, uncertainty))
        # A crash is undone by restarting the target, which loses what it kept in memory.
        restart = snapshot.get("restart_effects", {}).get(target)
        if result_key == "startup_restart" and failure in ("kill", "stop") and restart:   # both restart it
            for name in restart["features"]:
                if feature is None or name == feature:
                    impact = {"component": name, "service": target, "feature": name,
                              "outcome": restart["outcome"], "path": [target],
                              "behavior": restart["behavior"], "rule": f"restart-data:{target}",
                              "evidence": restart["evidence"], "assumptions": [],
                              "uncertainties": ([] if restart.get("verification") == "verified"
                                                else [f"restart-data:{target}: {restart.get('basis', 'inferred')}"])}
                    for field in ("data", "basis", "probe"):
                        if field in restart:
                            impact[field] = restart[field]
                    result[result_key].append(impact)
        result[result_key].sort(key=lambda i: (i["component"], i["path"]))
    return result
