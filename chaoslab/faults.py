"""Fault types: the menu of failures an experiment (or the AI planner) may choose from.

Every fault can be injected and reverted, and reverting is always safe to
repeat: the experiment, the kill switch and the dead-man's switch may all try.
Each fault also declares hard limits for its parameters, so nobody, including
an AI planner, can ask for something more destructive than we allow.
"""

import json
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from .docker import DockerError, docker
from .settings import LOAD_TARGET_URL, LOCUST_URL, NETTOOLS_IMAGE


class Fault:
    kind = ""
    summary = ""                                  # one line for menus and the planner
    mechanism = ""                                # how the fault is applied
    undo = ""                                     # how it is reverted
    limits: dict[str, tuple[float, float]] = {}   # parameter -> (min, max)

    def __init__(self, target: str, fault_id: str | None = None, **params):
        self.target = target
        self.params = params
        self.id = fault_id or f"{self.kind}-{target}-{int(time.time())}"

    def describe(self) -> str:
        raise NotImplementedError

    def problems(self) -> list[str]:
        """Reasons this fault may not run (parameters outside the allowed limits)."""
        found = []
        for name, (low, high) in self.limits.items():
            value = self.params.get(name)
            if not isinstance(value, (int, float)) or not low <= value <= high:
                found.append(f"{name}={value} is outside the allowed range {low}..{high}")
        return found

    def prepare(self) -> None:
        """Record whatever is needed to undo the fault. Runs before it is registered."""

    def inject(self) -> None:
        raise NotImplementedError

    def revert(self) -> None:
        raise NotImplementedError

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "target": self.target, "params": self.params}

    @staticmethod
    def from_dict(data: dict) -> "Fault":
        return FAULTS[data["kind"]](data["target"], fault_id=data["id"], **data["params"])


class ContainerKill(Fault):
    kind = "kill"
    mechanism = 'docker kill sends SIGKILL to the container'
    undo = 'docker start'
    summary = "Crash: SIGKILL the container, like an out-of-memory kill or a lost machine."

    def describe(self) -> str:
        return f"crash {self.target} (SIGKILL)"

    def inject(self) -> None:
        docker("kill", self.target)

    def revert(self) -> None:
        docker("start", self.target)  # no-op if it is already running


class ContainerPause(Fault):
    kind = "pause"
    mechanism = "docker pause freezes the container's processes (cgroup freezer)"
    undo = 'docker unpause'
    summary = ("Hang: freeze every process in the container. Connections stay open but "
               "nothing answers, like a stuck thread pool or a long garbage-collection pause.")

    def describe(self) -> str:
        return f"freeze {self.target} (hung process)"

    def inject(self) -> None:
        docker("pause", self.target)

    def revert(self) -> None:
        if docker("inspect", "--format", "{{.State.Paused}}", self.target, check=False) == "true":
            docker("unpause", self.target)


class GracefulStop(Fault):
    kind = "stop"
    mechanism = "docker stop sends SIGTERM, then SIGKILL after 10 s; Spring shuts down and leaves Eureka"
    undo = "docker start"
    summary = ("Graceful stop: SIGTERM the container, like a rolling deploy or a drained node. "
               "Unlike a crash, the service can deregister before it goes.")

    def describe(self) -> str:
        return f"stop {self.target} gracefully (SIGTERM)"

    def inject(self) -> None:
        docker("stop", "--time", "10", self.target, timeout=40)

    def revert(self) -> None:
        docker("start", self.target)  # no-op if it is already running


class NetworkPartition(Fault):
    kind = "partition"
    mechanism = "docker network disconnect takes the container off its networks; its processes keep running"
    undo = "docker network connect with the original aliases"
    summary = ("Partition: cut the container off the network while it keeps running, like a failed "
               "switch or a wrong firewall rule. Callers cannot reach it at all.")

    def __init__(self, target: str, fault_id: str | None = None, networks: dict | None = None):
        super().__init__(target, fault_id, networks=networks)

    def describe(self) -> str:
        return f"cut {self.target} off the network (partition)"

    def _attached(self) -> dict[str, list[str]]:
        raw = docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", self.target)
        container_id = docker("inspect", "--format", "{{.Id}}", self.target)[:12]
        return {name: sorted({a for a in (net.get("Aliases") or []) if a and a != container_id})
                for name, net in json.loads(raw).items()}

    def prepare(self) -> None:
        self.params["networks"] = self._attached()   # recorded first, so any undo can restore them

    def inject(self) -> None:
        for network in self.params["networks"]:
            docker("network", "disconnect", network, self.target)

    def revert(self) -> None:
        attached = self._attached()
        for network, aliases in (self.params.get("networks") or {}).items():
            if network not in attached:
                args = [arg for alias in aliases for arg in ("--alias", alias)]
                docker("network", "connect", *args, network, self.target)


class NetworkFault(Fault):
    """Base for faults applied as a netem rule inside the target's network.

    A helper container joins the target's network namespace, adds the rule and
    removes it when stopped or when its own timer runs out, so the rule expires
    even if this controller dies.
    """

    def netem(self) -> str:
        raise NotImplementedError

    @property
    def helper(self) -> str:
        return f"chaoslab-fault-{self.id}"

    def inject(self) -> None:
        script = (
            f"tc qdisc add dev eth0 root netem {self.netem()} || exit 1; "
            "trap 'tc qdisc del dev eth0 root; exit 0' TERM INT; "
            f"sleep {self.params['expire_after_s']} & wait $!; "
            "tc qdisc del dev eth0 root"
        )
        docker("run", "--detach", "--rm", "--name", self.helper,
               "--network", f"container:{self.target}", "--cap-add", "NET_ADMIN",
               "--label", "chaoslab.fault=true",
               "--entrypoint", "sh", NETTOOLS_IMAGE, "-c", script)
        # Only report success once the rule is really in place.
        for _ in range(10):
            if "netem" in self._qdisc():
                return
            time.sleep(0.3)
        self.revert()
        raise DockerError(f"netem rule did not appear on {self.target}")

    def revert(self) -> None:
        docker("stop", "--time", "5", self.helper, check=False)
        # Belt and braces: remove any rule left behind (e.g. if the helper was SIGKILLed).
        docker("run", "--rm", "--network", f"container:{self.target}", "--cap-add", "NET_ADMIN",
               NETTOOLS_IMAGE, "qdisc", "del", "dev", "eth0", "root", check=False)

    def _qdisc(self) -> str:
        return docker("run", "--rm", "--network", f"container:{self.target}", NETTOOLS_IMAGE,
                      "qdisc", "show", "dev", "eth0", check=False)


class NetworkDelay(NetworkFault):
    kind = "delay"
    mechanism = "tc netem delay inside the container's network namespace, from a helper container"
    undo = 'remove the netem rule; the helper also removes it when its timer ends'
    summary = "Slow network: add latency to everything the container sends."
    limits = {"delay_ms": (1, 5000), "jitter_ms": (0, 2000), "expire_after_s": (1, 600)}

    def __init__(self, target: str, fault_id: str | None = None,
                 delay_ms: int = 1000, jitter_ms: int = 0, expire_after_s: int = 300):
        super().__init__(target, fault_id, delay_ms=delay_ms, jitter_ms=jitter_ms,
                         expire_after_s=expire_after_s)

    def netem(self) -> str:
        return f"delay {self.params['delay_ms']}ms {self.params['jitter_ms']}ms"

    def describe(self) -> str:
        jitter = f" +/-{self.params['jitter_ms']}ms" if self.params["jitter_ms"] else ""
        return f"delay network of {self.target} by {self.params['delay_ms']}ms{jitter}"


class PacketLoss(NetworkFault):
    kind = "loss"
    mechanism = "tc netem loss inside the container's network namespace, from a helper container"
    undo = 'remove the netem rule; the helper also removes it when its timer ends'
    summary = ("Lossy network: drop a share of the packets the container sends. "
               "100% cuts it off completely (a failed or unreachable dependency).")
    limits = {"loss_pct": (1, 100), "expire_after_s": (1, 600)}

    def __init__(self, target: str, fault_id: str | None = None,
                 loss_pct: float = 20, expire_after_s: int = 300):
        super().__init__(target, fault_id, loss_pct=loss_pct, expire_after_s=expire_after_s)

    def netem(self) -> str:
        return f"loss {self.params['loss_pct']}%"

    def describe(self) -> str:
        if self.params["loss_pct"] >= 100:
            return f"cut {self.target} off the network (100% packet loss)"
        return f"drop {self.params['loss_pct']}% of packets sent by {self.target}"


class CpuThrottle(Fault):
    kind = "cpu"
    mechanism = "docker update --cpus caps the container's CPU quota"
    undo = 'restore the original CPU limit'
    summary = ("Starved: cap the container's CPU, like an overloaded host or a noisy "
               "neighbour. Also the closest stand-in for an overloaded embedded database.")
    limits = {"cpus": (0.05, 4)}

    def __init__(self, target: str, fault_id: str | None = None,
                 cpus: float = 0.1, original_cpus: float | None = None):
        super().__init__(target, fault_id, cpus=cpus, original_cpus=original_cpus)

    def describe(self) -> str:
        return f"limit {self.target} to {self.params['cpus']:.0%} of one CPU"

    def prepare(self) -> None:
        nano = int(docker("inspect", "--format", "{{.HostConfig.NanoCpus}}", self.target))
        self.params["original_cpus"] = nano / 1e9 if nano else None

    def inject(self) -> None:
        docker("update", "--cpus", str(self.params["cpus"]), self.target)

    def revert(self) -> None:
        # `docker update --cpus 0` does not clear a limit, so "no limit" is
        # restored as "every CPU of the Docker VM", which is equivalent.
        original = self.params.get("original_cpus") or docker("info", "--format", "{{.NCPU}}")
        docker("update", "--cpus", str(original), self.target)


class TrafficSpike(Fault):
    kind = "spike"
    mechanism = 'the Locust web API raises the number of simulated users'
    undo = 'restore the original user count'
    summary = "Sudden traffic spike: multiply the number of simulated users hitting the entry point."
    limits = {"multiplier": (1.5, 10)}

    def __init__(self, target: str, fault_id: str | None = None,
                 multiplier: float = 5, original_users: int | None = None):
        super().__init__(target, fault_id, multiplier=multiplier, original_users=original_users)

    def describe(self) -> str:
        users = self.params["original_users"]
        peak = f" ({users} -> {self._peak()} users)" if users else ""
        return f"traffic spike on {self.target}: {self.params['multiplier']}x users{peak}"

    def problems(self) -> list[str]:
        found = super().problems()
        entry = LOAD_TARGET_URL.split("//")[1].split(":")[0]
        if self.target != entry:
            found.append(f"traffic enters through {entry}, so a spike can only target {entry}")
        return found

    def prepare(self) -> None:
        with urlopen(f"{LOCUST_URL}/stats/requests", timeout=5) as resp:
            self.params["original_users"] = json.load(resp)["user_count"]

    def inject(self) -> None:
        self._swarm(self._peak())

    def revert(self) -> None:
        self._swarm(self.params.get("original_users") or 10)

    def _peak(self) -> int:
        return round(self.params["original_users"] * self.params["multiplier"])

    @staticmethod
    def _swarm(users: int) -> None:
        form = urlencode({"user_count": users, "spawn_rate": max(users, 10),
                          "host": LOAD_TARGET_URL}).encode()
        with urlopen(f"{LOCUST_URL}/swarm", data=form, timeout=10) as resp:
            if not json.load(resp).get("success"):
                raise RuntimeError(f"load generator refused to change to {users} users")


FAULTS = {fault.kind: fault for fault in
          (ContainerKill, ContainerPause, NetworkDelay, PacketLoss, CpuThrottle, TrafficSpike,
           GracefulStop, NetworkPartition)}
