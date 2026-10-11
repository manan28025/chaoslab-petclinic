import json
import unittest
from unittest.mock import patch

from chaoslab import faults
from chaoslab.blast_radius import FAILURE_MODES


class FakeDocker:
    """Answers inspect calls from a network table and records every other call."""

    def __init__(self, networks):
        self.networks, self.calls = networks, []

    def __call__(self, *args, check=True, timeout=60):
        if args[:2] == ("inspect", "--format") and "Networks" in args[2]:
            return json.dumps({name: {"Aliases": aliases} for name, aliases in self.networks.items()})
        if args[:2] == ("inspect", "--format") and args[2] == "{{.Id}}":
            return "8b828a10bbafdeadbeef"
        self.calls.append(args)
        if args[:2] == ("network", "disconnect"):
            self.networks.pop(args[2])
        if args[:2] == ("network", "connect"):
            self.networks[args[-2]] = []
        return ""


class FaultTest(unittest.TestCase):
    def test_partition_records_networks_then_restores_them_with_their_aliases(self):
        fake = FakeDocker({"petclinic_default": ["visits-service", "visits-service", "8b828a10bbaf"]})
        with patch.object(faults, "docker", fake):
            fault = faults.FAULTS["partition"]("visits-service")
            fault.prepare()
            self.assertEqual(fault.params["networks"], {"petclinic_default": ["visits-service"]})
            fault.inject()
            self.assertEqual(fake.calls[-1], ("network", "disconnect", "petclinic_default", "visits-service"))
            restored = faults.Fault.from_dict(json.loads(json.dumps(fault.to_dict())))   # as the dead-man's switch does
            restored.revert()
            self.assertEqual(fake.calls[-1], ("network", "connect", "--alias", "visits-service",
                                              "petclinic_default", "visits-service"))
            calls = len(fake.calls)
            restored.revert()
            self.assertEqual(len(fake.calls), calls, "reverting twice does nothing the second time")

    def test_graceful_stop_and_every_fault_has_a_prediction_mode(self):
        fake = FakeDocker({})
        with patch.object(faults, "docker", fake):
            stop = faults.FAULTS["stop"]("vets-service")
            stop.inject()
            stop.revert()
        self.assertEqual(fake.calls, [("stop", "--time", "10", "vets-service"), ("start", "vets-service")])
        self.assertEqual(set(FAILURE_MODES), set(faults.FAULTS))
        self.assertTrue(all(f.mechanism and f.undo and f.summary for f in faults.FAULTS.values()))


if __name__ == "__main__":
    unittest.main()
