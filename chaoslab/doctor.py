"""`chaoslab doctor`: is this machine ready to run experiments, and if not, what to do.

Every check is read-only. Blocking problems make the command exit with 1; warnings do not.
"""

import json
import shutil
import sys
import time
from urllib.request import urlopen

from . import safety
from .docker import DockerError, docker, targets
from .llm import from_env
from .settings import COMPOSE_PROJECT, LOCUST_URL, MAP_FILE, PROMETHEUS_URL, ROOT, RUNS_DIR, ZIPKIN_URL

OK, WARN, FAIL = "ok", "warn", "fail"


def _get(url: str, timeout: float = 3):
    with urlopen(url, timeout=timeout) as resp:
        return json.load(resp) if "json" in (resp.headers.get("Content-Type") or "") else resp.read()


def checks() -> list[tuple[str, str, str]]:
    """(status, what, detail or how to fix)."""
    out = []
    out.append((OK, "Python", sys.version.split()[0]) if sys.version_info >= (3, 11)
               else (FAIL, "Python", f"{sys.version.split()[0]}: ChaosLab needs 3.11 or newer"))
    try:
        docker("info", "--format", "{{.ServerVersion}}", timeout=10)
        out.append((OK, "Docker", "the engine answers"))
    except (DockerError, OSError) as err:
        out.append((FAIL, "Docker", f"not reachable ({err}); start Docker Desktop"))
        return out
    try:
        allowed = targets()
        down = sorted(name for name, state in allowed.items() if state != "running")
        if not allowed:
            out.append((FAIL, "Sandbox", f"no `{COMPOSE_PROJECT}` containers; run `make up`"))
        elif down:
            out.append((FAIL, "Sandbox", f"not running: {', '.join(down)}; run `make up` or `chaoslab stop`"))
        else:
            unhealthy = [n for n in allowed if "unhealthy" in docker("inspect", "--format", "{{json .State.Health}}", n, check=False)]
            out.append((WARN, "Sandbox", f"unhealthy: {', '.join(unhealthy)}") if unhealthy
                       else (OK, "Sandbox", f"{len(allowed)} app services running"))
    except DockerError as err:
        out.append((FAIL, "Sandbox", str(err)))
    try:
        stats = _get(f"{LOCUST_URL}/stats/requests")
        rps, fails = stats.get("current_rps") or 0, stats.get("current_fail_per_sec") or 0
        if rps < 0.5:
            out.append((FAIL, "User traffic", "the load generator sends no traffic; start it at http://localhost:8089"))
        elif fails / rps > 0.05:
            out.append((WARN, "User traffic", f"{fails / rps:.0%} of requests failing; experiments will be refused "
                                              "until the system is steady"))
        else:
            out.append((OK, "User traffic", f"{rps:.1f} req/s, {fails / rps:.0%} failing, {stats.get('user_count')} users"))
    except (OSError, ValueError) as err:
        out.append((FAIL, "User traffic", f"Locust unreachable ({type(err).__name__}); run `make up`"))
    for name, url in (("Zipkin", f"{ZIPKIN_URL}/health"), ("Prometheus", f"{PROMETHEUS_URL}/-/ready")):
        try:
            _get(url)
            out.append((OK, name, "answers"))
        except (OSError, ValueError):
            out.append((WARN, name, f"not reachable at {url}; the map and metrics will have gaps"))
    if MAP_FILE.exists():
        age_h = (time.time() - MAP_FILE.stat().st_mtime) / 3600
        out.append((OK, "Dependency map", f"{age_h:.0f} h old") if age_h < 48
                   else (WARN, "Dependency map", f"{age_h:.0f} h old; refresh with `chaoslab map`"))
    else:
        out.append((FAIL, "Dependency map", "missing; run `chaoslab map`"))
    faults = safety.active_faults()
    out.append((WARN, "Active faults", f"{', '.join(faults)}; run `chaoslab stop`") if faults
               else (OK, "Active faults", "none"))
    from .fixes import active as active_fixes
    applied = active_fixes()
    out.append((WARN, "Applied fixes", f"{', '.join(applied)} still applied; `chaoslab revert <fix>` restores the "
                                       "tested setup") if applied else (OK, "Applied fixes", "none"))
    llm = from_env()
    out.append((OK, "AI model", llm.describe()) if llm.available()
               else (WARN, "AI model", f"{llm.last_error}; planning and analysis run on rules only"))
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    out.append((OK, "Disk", f"{free_gb:.1f} GB free") if free_gb >= 5
               else (WARN, "Disk", f"only {free_gb:.1f} GB free; Docker images and runs need room")
               if free_gb >= 2 else (FAIL, "Disk", f"only {free_gb:.1f} GB free"))
    runs = len(list(RUNS_DIR.glob("2*/result.json"))) if RUNS_DIR.exists() else 0
    out.append((OK, "Runs recorded", str(runs)))
    return out


def main() -> int:
    mark = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    results = checks()
    for status, what, detail in results:
        print(f"[{mark[status]}] {what:<16} {detail}")
    failed = [what for status, what, _ in results if status == FAIL]
    print("\nReady for experiments." if not failed else f"\nNot ready: fix {', '.join(failed)} first.")
    return 1 if failed else 0
