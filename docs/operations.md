# Setup and operations

This repository contains a local Spring PetClinic microservices environment for controlled chaos-engineering experiments. The application, Prometheus, Grafana, Zipkin, a Locust load generator, and optionally cAdvisor run through Docker Compose. The optional GenAI service is disabled by default, so an OpenAI or Azure OpenAI API key is not required.

Run commands from the repository root unless a command changes directory. The chaos controller (`chaoslab`) maps dependencies and injects controlled failures behind several safety layers. The AI planner and analysis agent are planned; see [AGENTS.md](../AGENTS.md).

## Requirements

- Docker Desktop with at least 8 GB of memory
- Docker Compose
- JDK 17 or newer, to build the PetClinic images from source
- Python 3.11 or newer, for the chaos controller
- `curl` and `jq` for the verification commands
- Internet access at startup: the config server reads its settings from GitHub

### Docker Desktop setting for cAdvisor

Only needed for per-container metrics. In **Docker Desktop > Settings > General**, turn off **Use containerd for pulling and storing images**, then restart Docker Desktop.

cAdvisor needs Docker Desktop's classic `overlay2` image storage to discover individual PetClinic containers. It may log a warning that it cannot access the containerd socket; this is harmless in this setup because cAdvisor successfully uses the Docker socket instead. With the containerd store, start the stack without cAdvisor; everything else works the same.

Changing the image-store setting can make containers and images from the other store temporarily disappear. Rebuild the images and recreate the stack, but do not delete Docker volumes.

## Start the application

```bash
make build                  # build the PetClinic images from this repository's source (~3 min)
make up PROFILES=cadvisor   # start everything, including cAdvisor, and wait until healthy
```

Use `make up` instead to start without cAdvisor. `make status` shows container health and `make down` stops everything. The application images are always built from this repository's source; Compose never pulls the published PetClinic images. The Gateway needs about a minute after startup before it routes traffic.

The default stack does not start `genai-service`. To start it explicitly, build it, provide the required credentials, and enable its profile:

```bash
cd spring-petclinic-microservices
docker compose --profile genai up -d
```

## Useful endpoints

| Component | URL |
| --- | --- |
| API Gateway | <http://localhost:8080> |
| Eureka | <http://localhost:8761> |
| Zipkin | <http://localhost:9411> |
| Prometheus | <http://localhost:9091> |
| Grafana | <http://localhost:3030> |
| Spring Boot Admin | <http://localhost:9090> |
| Locust (load generator) | <http://localhost:8089> |
| cAdvisor (optional) | <http://localhost:8085> |

## Verify monitoring

```bash
cd spring-petclinic-microservices
docker compose config --quiet
docker compose ps
docker exec prometheus-server promtool check config /etc/prometheus/prometheus.yml
curl -fsS 'http://localhost:9091/api/v1/query?query=up%7Bjob%3D%22cadvisor%22%7D' | jq
curl -fsS 'http://localhost:9091/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv'
```

When started with the cAdvisor profile, `up{job="cadvisor"}` should return `1`, and the PetClinic scrape targets should report `up`.

Verify that the Gateway returns complete visit data rather than only a successful status code:

```bash
curl -fsS http://localhost:8080/api/gateway/owners/6 \
  | jq '{id, name: (.firstName + " " + .lastName), visits: [.pets[].visits[]?] | length}'
```

The sample data gives owner `6` four visits. The load generator never writes to owner `6`; each current chaos experiment writes one canary visit before injecting a fault, so expect at least four.

## Container metrics

Example CPU usage in cores, averaged over one minute:

```promql
sum by (container_label_com_docker_compose_service) (
  rate(container_cpu_usage_seconds_total{
    container_label_com_docker_compose_project="spring-petclinic-microservices",
    container_label_com_docker_compose_service!="cadvisor"
  }[1m])
)
```

A value of `1` means one full CPU core, while `0.25` means one quarter of a core.

See the [container resource monitoring guide](../spring-petclinic-microservices/docs/container-resource-monitoring.md) for memory and network queries. See the [PetClinic README](../spring-petclinic-microservices/README.md) for complete application documentation.

## Dependency map

```bash
python3.11 -m chaoslab map     # writes reports/dependency-map.{json,md,html}, docs/index.html, and dashboard.html
```

The versioned JSON map is shared by the CLI, future agents, and the HTML report. It combines source-backed feature rules in [dependency-semantics.json](../dependency-semantics.json) with Docker Compose, Docker, Eureka, Prometheus, Zipkin, and Locust observations. Dependencies record their purpose, runtime or startup phase, evidence, verification status, and known fallback behavior. Missing or stale telemetry stays visible; it does not erase source-backed dependencies. [map-overrides.json](../map-overrides.json) still documents trace gaps. Request share is exposure context, not a dependency weight or failure-severity score.

```bash
python3.11 -m chaoslab impact visits-service --failure kill         # qualitative prediction
python3.11 -m chaoslab impact config-server --failure kill --json   # machine-readable contract
python3.11 -m chaoslab impact visits-service --phase runtime --feature 'owner details'
```

The impact command is read-only. It reports running-service effects separately from startup/restart effects, with dependency paths, fallback assumptions, and uncertainty. Open `reports/dependency-map.html` to inspect the same semantics and switch target, failure, and phase. The identical `docs/index.html` is the [published map](https://singhchinmayy.github.io/chaoslab-petclinic/) after committing and pushing a refresh. After a guarded run, the report also shows its prediction-versus-observation comparison when the run used the current snapshot. The [implementation plan](dependency-analysis-plan.md) documents the V1 model, evidence rules, and limits.

## Read-only dashboard

```bash
python3.11 -m chaoslab dashboard          # newest finished local run, or committed demo
python3.11 -m chaoslab dashboard --demo   # deterministic recorded example
```

Open `reports/dashboard.html` in a browser. It is a standalone export with no external assets or fault controls. It displays the run's captured prediction even when the current dependency map is newer, and labels the mismatch. The freshness column is evaluated when the page opens. `chaoslab map` and completed experiments refresh the export automatically. The committed [demo JSON](../demo/visits-failure.json) is a curated recording, not a synthetic success case.

## Live test bench

```bash
python3.11 -m chaoslab bench      # operator console on http://127.0.0.1:8090 (or: make bench)
```

The bench is the live counterpart of the read-only dashboard. It is an operator tool like the CLI, served on this machine only:

- **Live test bench**: the dependency map with one moving dot per request, coloured by user action. Each fault looks different: a crashed service bursts the calls that reach it, a frozen one queues them until they time out, delay slows them, loss drops them, and errors travel back to the users in red.
- **Arm and launch**: pick a fault (keys 1 to 6, or drag it onto a service), a target, its strength and length, then hold the launch button for one second. It starts the same guarded `chaoslab run`, one experiment at a time, inside the same allowlist and parameter limits.
- **What users would feel**: the impact prediction for the armed fault, per user action, before anything is injected, plus what a restart would lose.
- **Test matrix**: every service against every fault with the latest verdict (held, tripped, data lost, no recovery, refused) and totals such as pairs tested, weaknesses found and prediction accuracy.
- **Recorder**: every run in `runs/`, drawn second by second while it happens and afterwards, with watchdog limits, the prediction-versus-observation protocol and the event log. **Replay on the bench** re-enacts a past run on the live map.
- **Safety**: the six interlocks with live countdowns, and an emergency stop that undoes every fault.

Its buttons accept requests only from the page itself on this machine: they require a custom header and a local `Host`, so other websites cannot trigger them. Refused launches show the reason, for example a system that is not in a steady state.

## Chaos experiments

```bash
python3.11 -m chaoslab faults                                    # the failure menu and its limits
python3.11 -m chaoslab targets                                   # containers that may be faulted
python3.11 -m chaoslab run kill customers-service                # crash
python3.11 -m chaoslab run pause vets-service                    # freeze (hung process)
python3.11 -m chaoslab run delay visits-service --delay-ms 600   # slow network
python3.11 -m chaoslab run loss visits-service --loss-pct 30     # lossy network (100 = cut off)
python3.11 -m chaoslab run cpu customers-service --cpus 0.1      # CPU starvation
python3.11 -m chaoslab run spike api-gateway --multiplier 5      # traffic spike
python3.11 -m chaoslab status                                    # active faults
python3.11 -m chaoslab stop                                      # kill switch (or: make kill-switch)
```

Each run measures a baseline, refuses to start unless the system is in a steady state, injects one fault, watches users' error rate, p95 latency, and throughput every second, reverts the fault, and measures recovery. Results go to `runs/<run>/result.json`, and every action is logged in `runs/<run>/events.jsonl`.

Current runs check owner `6` against visit IDs seen in the healthy baseline, including a canary written before the fault. This catches HTTP `200` with missing data: no visits is a degraded Gateway fallback, while visits missing after recovery are reported as `data_lost`. Service recovery and data loss are distinct outcomes. Predictions and `matched`/`contradicted`/`unobserved` comparisons are stored in `result.json` and printed in the summary. Comparisons count requests through the disturbance window, including those that hung during the fault and failed after revert; fewer than three finished requests leave an action unobserved. A checkpoint is written at revert, and `--recovery-timeout` bounds recovery observation.

The committed historical Visits crash test predates the canary and data-loss classification. It returned HTTP `200` with missing visits; two baseline visit IDs were still absent at the final probe, so that recorded run reported recovery not confirmed. New runs report service recovery with data loss separately when appropriate.

### Safety controls

Any one of these stops a fault:

1. **Allowlist**: only services labelled `chaoslab.target=true` can be faulted; monitoring and the load generator never can. Every failure type has hard parameter limits.
2. **Watchdog**: aborts after 3 s above 25% errors, 2 s p95 latency, or below 30% of normal throughput.
3. **Time limit**: every fault is reverted at its planned end (at most 300 s).
4. **Kill switch**: `python3.11 -m chaoslab stop` from any terminal reverts everything and restores the sandbox.
5. **Dead-man's switch**: a detached process reverts the fault 30 s after its planned end, even if the controller crashed.

## AI planner, analyst and campaigns

```bash
python3.11 -m chaoslab plan                          # rank every allowed fault, pick one, write a hypothesis
python3.11 -m chaoslab plan --run                    # ... and run it (asks before a risky plan)
python3.11 -m chaoslab auto --budget 3               # campaign: plan, approve, run, analyse, repeat
python3.11 -m chaoslab analyze                       # findings, risk 0-10 and fixes for every saved run
python3.11 -m chaoslab report                        # ranked weaknesses, fixes and the A-F scorecard
```

**The model proposes, the code decides.** The planner ranks every allowed (service, fault) pair in code: users exposed, what the impact prediction says breaks, how much of that prediction is still unverified, and what history already answered. A language model, when configured, investigates through read-only tools (service details, the prediction for a candidate, history, fault details), picks one of the top five, tunes its strength inside planner bounds that are tighter than the fault limits, and writes a falsifiable hypothesis per user action. It submits through one tool; code validates the plan, clamps the settings and returns the reason when it rejects one, so the model can correct itself (at most three submissions and six turns). Without a model, or when it fails, the top-ranked candidate runs with the code prediction as its hypothesis, and the plan records `planned_by: rules`.

Plans that could do lasting harm need a person's approval: a crash that loses data from an in-memory database, an outage of a start-up dependency such as `config-server`, a prediction that takes down three or more user actions, or heavy settings. `--approve-risky` approves them in advance; otherwise the CLI asks, and a campaign skips a declined plan and plans the next one.

After every run, the analyst computes findings (user errors, HTTP 200 with missing visits, data loss, hangs instead of fast failures, throughput collapse, no self-healing, collateral damage, slow or missing recovery, a wrong prediction), a deterministic risk score from 0 to 10 (data loss or no recovery is always critical), the planner's hypothesis against what users saw, and PetClinic-specific fixes with the command that verifies each one. A model only writes the summary, likely cause and production impact, and may add at most two fixes labelled unverified. The result is `runs/<run>/analysis.json` and `analysis.md`. The report merges every run into ranked weaknesses, fixes and a per-service score from 0 to 100 graded A to F, with untested services listed rather than given a flattering grade (`reports/resilience-report.md` and `.json`).

The language model is optional and configured by environment variables; the key is never stored in the repository:

```bash
export DEEPSEEK_API_KEY=...                        # or CHAOSLAB_LLM_API_KEY
export CHAOSLAB_LLM_MODEL=deepseek-flash           # default
export CHAOSLAB_LLM_BASE_URL=https://api.deepseek.com
export CHAOSLAB_LLM=off                            # rules only, same as --no-ai
```

Any OpenAI-compatible endpoint works, including a local model such as `http://localhost:11434/v1` (Ollama), which needs no key.

