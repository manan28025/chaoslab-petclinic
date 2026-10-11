# ChaosLab resilience report

**Overall resilience: 77.7/100 (C)** across 12 experiments on 4 of 7 services. Coverage: 9 of 50 service and fault pairs.

Scores, grades and findings are computed by code from the saved runs. Fixes are advice; nothing was changed in the system.

## Ranked weaknesses

| # | Severity | Weakness | Service | Runs | Worst risk | Top fix |
|---|---|---|---|---|---|---|
| 1 | critical | 379 visits written before the fault were lost | `visits-service` | 2 | 8.5 | Move visits-service off its in-memory database |
| 2 | high | Owner pages returned HTTP 200 with their visits missing | `visits-service` | 2 | 8.5 | Make degraded owner pages visible instead of a silent 200 |
| 3 | high | visits-service only came back because ChaosLab restarted it | `visits-service` | 2 | 8.5 | Let visits-service restart itself and run a second instance |
| 4 | high | Users saw errors: up to 100% of requests failed | `customers-service` | 2 | 7.9 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 5 | high | customers-service only came back because ChaosLab restarted it | `customers-service` | 2 | 7.9 | Let customers-service restart itself and run a second instance |
| 6 | high | Users saw errors: up to 60% of requests failed | `vets-service` | 3 | 6.5 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 7 | high | vets-service only came back because ChaosLab restarted it | `vets-service` | 2 | 6.5 | Let vets-service restart itself and run a second instance |
| 8 | high | Actions that never call the target failed too | `vets-service` | 1 | 6.5 | Give each route its own circuit breaker |
| 9 | medium | Requests hung for up to 10 s instead of failing fast | `visits-service` | 2 | 8.5 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 10 | medium | Completed requests fell below 30% of normal | `visits-service` | 2 | 8.5 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 11 | medium | Requests hung for up to 11 s instead of failing fast | `customers-service` | 3 | 7.9 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 12 | medium | Completed requests fell below 30% of normal | `customers-service` | 3 | 7.9 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 13 | medium | Requests hung for up to 10 s instead of failing fast | `vets-service` | 3 | 6.5 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 14 | medium | Completed requests fell below 30% of normal | `vets-service` | 3 | 6.5 | Fail fast: lower the Gateway's 10 s TimeLimiter |
| 15 | low | Users saw errors: up to 19% of requests failed | `visits-service` | 2 | 8.5 | Move visits-service off its in-memory database |
| 16 | low | The dependency model predicted this wrongly | `customers-service` | 1 | 7.9 | Correct the dependency model's rule |

## Fixes applied and verified

Each fix was applied to the sandbox, the experiment that exposed the weakness was run again, and the fix was reverted afterwards.

- **Move customers and visits from in-memory HSQLDB to a persistent MySQL database** (20261011-190331-persistent-database): Verified: the fix removed data_loss. Risk 8.5/10 critical before, 5.2/10 medium after; visits lost 1 before, 0 after; recovery 13.1 s before, 13.2 s after.

## Scorecard (worst first)

| Service | Score | Grade | Runs | Faults tested | Weakest result |
|---|---|---|---|---|---|
| `visits-service` | 68.8 | C | 4 | delay, kill, loss | kill: 379 visits written before the fault were lost |
| `vets-service` | 70.2 | C | 3 | kill, pause | kill: Users saw errors: up to 60% of requests failed |
| `customers-service` | 72.2 | C | 4 | cpu, kill, pause | kill: Users saw errors: up to 100% of requests failed |
| `api-gateway` | 99.7 | A | 1 | spike | spike: Users were unaffected |
| `admin-server` | not tested | – | 0 | – | – |
| `config-server` | not tested | – | 0 | – | – |
| `discovery-server` | not tested | – | 0 | – | – |

## Fixes, most important first

### Move visits-service off its in-memory database

Why: missing visit IDs [9, 40, 41, 57, 71, 75, 81, 82, 87, 95, 99, 106, 110, 130, 148, 161, 179, 191, 232, 235, 241, 278, 280, 293, 301, 303, 308, 310, 318, 365, 381, 389, 394, 400, 426, 431, 443, 472, 483, 500, 506, 511, 516, 530, 540, 601, 616, 629, 636, 655, 657, 659, 660, 667, 672, 679, 683, 684, 700, 717, 719, 734, 752, 770, 781, 793, 799, 808, 809, 810, 816, 819, 827, 835, 840, 844, 871, 882, 913, 914, 916, 930, 931, 935, 941, 944, 963, 975, 980, 1014, 1027, 1028, 1033, 1049, 1061, 1083, 1085, 1097, 1098, 1102, 1103, 1109, 1124, 1134, 1151, 1159, 1164, 1184, 1225, 1268, 1289, 1291, 1293, 1296, 1299, 1300, 1301, 1309, 1312, 1318, 1340, 1347, 1362, 1396, 1404, 1410, 1417, 1419, 1424, 1429, 1445, 1452, 1453, 1455, 1459, 1461, 1468, 1479, 1481, 1496, 1508, 1546, 1574, 1576, 1577, 1608, 1628, 1630, 1634, 1648, 1654, 1665, 1670, 1675, 1679, 1682, 1699, 1701, 1716, 1740, 1741, 1742, 1751, 1752, 1755, 1764, 1781, 1793, 1795, 1804, 1817, 1819, 1829, 1833, 1841, 1842, 1843, 1845, 1852, 1857, 1863, 1888, 1916, 1948, 1967, 1985, 2008, 2011, 2016, 2027, 2036, 2037, 2038, 2044, 2055, 2083, 2094, 2096, 2116, 2127, 2136, 2141, 2153, 2160, 2168, 2177, 2179, 2181, 2182, 2187, 2196, 2220, 2243, 2257, 2272, 2286, 2291, 2292, 2294, 2306, 2311, 2313, 2332, 2339, 2345, 2347, 2350, 2354, 2363, 2374, 2378, 2382, 2395, 2397, 2434, 2436, 2471, 2475, 2487, 2490, 2492, 2518, 2524, 2531, 2568, 2570, 2578, 2599, 2610, 2619, 2637, 2657, 2658, 2670, 2671, 2672, 2677, 2685, 2700, 2704, 2708, 2712, 2721, 2730, 2740, 2746, 2757, 2776, 2778, 2784, 2808, 2813, 2816, 2825, 2829, 2838, 2840, 2853, 2886, 2897, 2915, 2922, 2937, 2947, 2962, 2971, 2987, 2988, 2995, 3013, 3033, 3050, 3055, 3074, 3079, 3081, 3082, 3087, 3090, 3111, 3112, 3121, 3130, 3132, 3137, 3143, 3154, 3170, 3172, 3200, 3219, 3223, 3255, 3276, 3289, 3293, 3321, 3323, 3328, 3329, 3339, 3345, 3349, 3390, 3393, 3406, 3418, 3422, 3440, 3444, 3460, 3477, 3490, 3519, 3525, 3527, 3529, 3536, 3539, 3550, 3555, 3569, 3572, 3574, 3575, 3591, 3604, 3605, 3643, 3647, 3673, 3681, 3683, 3691, 3692, 3700, 3720, 3725, 3767, 3771, 3783, 3796, 3803, 3831, 3844, 3849, 3852, 3854, 3870, 3873, 3897, 3899, 3900, 3912, 3923, 3924, 3928, 3931, 3979]; the run's canary visit 3979 was lost. The service came back with an empty in-memory database

1. Add a MySQL service with a named volume to the Compose file
2. Start visits-service with the Spring profile `mysql` (the service already ships db/mysql/schema.sql and the MySQL driver) and point SPRING_DATASOURCE_URL at it
3. Keep the HSQLDB profile for unit tests only

Verify: `python3 -m chaoslab run kill visits-service  # expect: no data-loss line, canary visit survives`

### Make degraded owner pages visible instead of a silent 200

Why: 2 correctness probes during the fault got 200 with no visits; a status-code monitor would report everything as fine

1. In the Gateway's owner-details fallback, mark the response as partial (for example a `X-Degraded: visits` header or a `visitsUnavailable` field)
2. Count fallback responses in Micrometer and alert on them, not only on 5xx
3. Show 'visits temporarily unavailable' in the UI rather than an empty list

Verify: `python3 -m chaoslab run kill visits-service  # expect: degraded pages carry the marker; the fallback counter rises`

### Let visits-service restart itself and run a second instance

Why: the container has no restart policy and runs a single instance: without the experiment's undo step it would have stayed down

1. Add `restart: unless-stopped` to the service in Compose (an orchestrator's liveness probe in production)
2. Run two instances behind Eureka so the Gateway's load balancer keeps serving while one restarts

Verify: `python3 -m chaoslab run kill visits-service  # with the undo step removed, expect the service back on its own`

### Fail fast: lower the Gateway's 10 s TimeLimiter

Why: peak p95 11000 ms against a baseline of 21 ms: callers waited for the Gateway's 10 s TimeLimiter before getting an answer

1. In spring-petclinic-api-gateway/src/main/java/org/springframework/samples/petclinic/api/ApiGatewayApplication.java, defaultCustomizer sets TimeLimiterConfig timeoutDuration to 10 s; use about 2 s (well above the 20 ms normal p95)
2. Make it configurable (for example resilience4j.timelimiter properties) so each route can differ
3. Add a connect and response timeout to the Gateway's WebClient for the owner lookup, which has none

Verify: `python3 -m chaoslab run kill customers-service  # expect: peak p95 near 2 s instead of 10 s, throughput holds`

### Let customers-service restart itself and run a second instance

Why: the container has no restart policy and runs a single instance: without the experiment's undo step it would have stayed down

1. Add `restart: unless-stopped` to the service in Compose (an orchestrator's liveness probe in production)
2. Run two instances behind Eureka so the Gateway's load balancer keeps serving while one restarts

Verify: `python3 -m chaoslab run kill customers-service  # with the undo step removed, expect the service back on its own`

### Let vets-service restart itself and run a second instance

Why: the container has no restart policy and runs a single instance: without the experiment's undo step it would have stayed down

1. Add `restart: unless-stopped` to the service in Compose (an orchestrator's liveness probe in production)
2. Run two instances behind Eureka so the Gateway's load balancer keeps serving while one restarts

Verify: `python3 -m chaoslab run kill vets-service  # with the undo step removed, expect the service back on its own`

### Give each route its own circuit breaker

Why: list vets failed although they do not use vets-service: the failure spread through something they share, such as the Gateway's circuit breaker

1. In spring-petclinic-api-gateway/src/main/resources/application.yml, the default filter uses one `defaultCircuitBreaker` for every route
2. Name one circuit breaker per downstream service (customers, visits, vets) so one failing service cannot open the breaker for the others

Verify: `python3 -m chaoslab run kill vets-service  # expect: no collateral actions`

### Correct the dependency model's rule

Why: list owners: predicted unavailable, observed preserved; pet types: predicted unavailable, observed preserved

1. Update the matching rule in dependency-semantics.json with what this run measured, and record the run as its evidence

Verify: `python3 -m chaoslab impact customers-service --failure kill`

