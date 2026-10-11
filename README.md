# ChaosLab: safe chaos experiments for Spring PetClinic

ChaosLab is a local chaos engineering prototype for the [Spring PetClinic microservices](https://github.com/spring-petclinic/spring-petclinic-microservices) Docker Compose stack. It maps service dependencies, predicts failure impact, runs guarded experiments, and checks what users actually receive. The dashboard below is a **read-only recording**, so it can be explored without starting Docker.

![ChaosLab dashboard overview showing the recorded Visits failure, automatic abort, and missing visit data](docs/images/dashboard-overview.png)

**Key finding:** In a recorded Visits-service crash, `GET /api/gateway/owners/6` returned HTTP `200` while all six baseline visits were missing from one correctness probe. The watchdog reverted the fault after **9.3 seconds**. The last recovery probe still lacked **two** baseline visits, so the run did **not** claim full recovery. [Open the dashboard](reports/dashboard.html).

**Verified fix:** moving customers and visits from in-memory HSQLDB to PetClinic's MySQL profile, then rerunning the same crash, removed the data loss: the run's canary visit survived and the analyst's risk fell from **8.5/10 critical** to **5.2/10 medium**. The fix was reverted afterwards (`chaoslab verify persistent-database`).

## What this project demonstrates

| Capability | Implemented behavior |
| --- | --- |
| Dependency analysis | Combines source-backed PetClinic semantics with Docker Compose, Eureka, Prometheus, Zipkin, and Locust observations; labels uncertain and stale evidence. |
| Qualitative prediction | Explains which user actions become unavailable or degraded for a selected service failure, including Gateway fallbacks. |
| AI experiment planner | Code ranks every allowed service and fault by users exposed, predicted impact and unverified predictions; an optional language model (DeepSeek by default) investigates with read-only tools, picks from the top five and writes a falsifiable hypothesis; code validates it, and risky plans need approval. |
| Controlled injection | Supports crash, graceful stop, freeze, network partition, network delay, packet loss, CPU starvation, and traffic spike against allowlisted local services. |
| Safety and recovery | Requires a healthy baseline; watches errors, p95 latency, throughput, and owner-data correctness; reverts on thresholds or time limits, with a kill switch and dead-man's switch. |
| Measured outcomes | Saves per-run timelines and audit events, compares predictions with observed responses, and distinguishes HTTP success from complete data. |
| Analysis and scoring | Turns every run into findings, a deterministic 0-10 risk score and PetClinic-specific fixes with verification commands; ranks weaknesses and grades each service A to F. |
| Fix and verify | Applies a recommended fix as a Compose override, reruns the experiment that exposed the weakness, compares before and after, and restores the sandbox. |
| Live operator console | `chaoslab bench`: live traffic on the dependency map, the AI planner, hold-to-launch, a second-by-second recorder and an emergency stop, on this machine only. |

**Stack:** Python 3.11 (standard library only), Spring Boot microservices, Docker Compose, Locust, Prometheus, Grafana, Zipkin, an optional OpenAI-compatible language model, and dependency-free HTML pages.

**Quick check and demo:** `make doctor` says whether the machine is ready and how to fix what is not; `make demo` runs an AI-planned campaign of three experiments and writes `reports/resilience-report.md`.

## Architecture at a glance

[![ChaosLab architecture showing dependency discovery, guarded fault injection, the PetClinic request path, and automatic recovery with read-only reports](docs/images/architecture.svg)](docs/images/architecture.svg)

[Open the full-size architecture diagram](docs/images/architecture.svg).

The AI planner or the operator selects a fault. The controller uses the saved map to predict impact, checks a healthy baseline, and injects one bounded fault into an allowlisted service. Locust signals and an owner-data probe feed the watchdog; the resulting run record and dependency snapshot feed read-only reports. The analyst scores each run and recommends fixes, which `chaoslab verify` can apply and test. The [architecture guide](docs/architecture.md) details the safety and data contracts.

## Explore the dependency map

[Open the published dependency map](https://singhchinmayy.github.io/chaoslab-petclinic/) or [view the HTML in this repository](docs/index.html). This is a **saved snapshot generated on 2026-10-11**, not a live health page. Running `python3.11 -m chaoslab map` refreshes both `docs/index.html` and `reports/dependency-map.html` from the same JSON report. The map used Docker Compose, Docker, Eureka, Prometheus, and Locust; Zipkin had no recent traces, so route gaps are labelled as unknown or backed by source code.

![Service topology showing Gateway, Customers, Visits, Vets, platform services, and their dependency arrows](docs/images/dependency-topology.png)

**See the request path.** The graph highlights Gateway as the entry point, business services behind it, and Config and Discovery as startup dependencies. It helps identify which single instance or dependency to examine before injecting a fault.

![User-action table linking owner details and four other routes to the services they pass through](docs/images/dependency-actions.png)

**Translate infrastructure into user impact.** The owner-details route passes through Gateway, Customers, and Visits. Its route is a documented manual correction because the trace context is lost on that path; the source column makes this visible.

![Blast-radius selector predicting a Visits crash will make add-visit unavailable and owner details degraded](docs/images/dependency-blast-radius.png)

**Compare failure scenarios.** Choose a target, fault type, and runtime or restart phase. For a Visits crash, the saved view predicts an unavailable add-visit route and degraded owner details through the Gateway fallback, with evidence and uncertainty shown beside each claim. The recorded experiment and measured outcome remain in the [resilience dashboard](reports/dashboard.html).

![Evidence section showing runtime source timestamps, missing Zipkin traces, manual corrections, and stale-observation labels](docs/images/dependency-evidence.png)

**Check confidence before acting.** Source timestamps, the empty Zipkin result, and manual corrections show where the topology is measured or inferred. Short-lived runtime observations become labelled stale when this saved page is opened later.

## Two-minute tour

1. Open [the standalone dashboard](reports/dashboard.html). Its header marks the demo date and that it is a saved export.
2. Follow the Gateway → Customers → Visits path and the `200` response with missing visits.
3. Inspect the baseline, fault, and recovery charts, then the prediction-versus-observation table.
4. Read the safety controls and source freshness table. The displayed map is newer than the recorded run; the dashboard labels that difference.

![ChaosLab experiment detail showing the watchdog abort, traffic and latency timeline, and matched impact predictions](docs/images/dashboard-experiment.png)

The [demo guide](docs/demo.md) includes a speaking script and the commands for generating the dashboard from a new local run. The [architecture guide](docs/architecture.md) explains data flow and component boundaries. [Detailed setup and operations](docs/operations.md) covers prerequisites, monitoring, the CLI, and safety controls. To run and watch experiments live instead of reading saved ones, use the [Resilience Test Bench](docs/operations.md#live-test-bench) (`make bench`).

## Reproduce locally

The committed dashboard is viewable immediately. To regenerate it from the recorded example:

```bash
python3.11 -m chaoslab dashboard --demo
```

To run the full sandbox, install Docker Desktop, Docker Compose, JDK 17+, and Python 3.11+, then:

```bash
make build
make up
python3.11 -m chaoslab map
python3.11 -m chaoslab run kill visits-service --duration 30
```

`python3.11 -m chaoslab dashboard` selects the newest finished local run; without one, it uses the committed recording. Map refreshes and finished runs also regenerate the dashboard. The live experiment requires healthy Docker services and Locust traffic; use `make kill-switch` to revert active faults. See [operations](docs/operations.md) before running an experiment.

Current experiments write a canary visit before injection and distinguish a recovered service from lost visit data. The recorded demo predates that improvement, so it preserves the original run's **recovery not confirmed** outcome.

## Evidence, limits, and next steps

The [curated demo JSON](demo/visits-failure.json) comes from a local run on **2026-10-10**. It retains measured outcomes and the timeline while omitting individual visit IDs. The runtime source snapshot and run prediction were captured at different times, and the dashboard says so. The dashboard never injects faults or polls live services.

The AI planner and separate analysis agent described in [AGENTS.md](AGENTS.md) are **planned**, not implemented. The current Python controller supplies the safety boundary and machine-readable evidence they would use. This is a two-day prototype, not a production fault-injection service.

**Resume-ready summary:** Built a guarded chaos engineering prototype for Spring PetClinic microservices using Python, Docker Compose, Locust, Prometheus, and Zipkin; detected HTTP 200 responses with incomplete visit data, automatically reverted a Visits failure, and compared predicted impact with recorded user outcomes.

Run `python3.11 -m unittest discover -s tests -q` for the Python tests. [Change history](history.md) records the project milestones.
