import unittest
from unittest.mock import MagicMock, patch
import threading
import time
from meco.service.monitor import HypervisorMonitor


class TestHypervisorMonitor(unittest.TestCase):
    def setUp(self):
        # Mock configs
        self.config_patcher = patch(
            "service.monitor.CONFIG", {"hypervisors": ["remote1"]}
        )
        self.config_mock = self.config_patcher.start()

        # Mock IncusClient
        self.incus_patcher = patch("service.monitor.IncusClient")
        self.incus_cls_mock = self.incus_patcher.start()
        self.incus_mock = MagicMock()
        self.incus_cls_mock.return_value = self.incus_mock

    def tearDown(self):
        self.config_patcher.stop()
        self.incus_patcher.stop()

    def test_monitor_lifecycle(self):
        monitor = HypervisorMonitor(interval=0.1)  # Fast interval

        # Mock successful connection
        self.incus_mock.check_remote_connection.return_value = True

        monitor.start()
        time.sleep(0.3)  # Let it run a bit

        status = monitor.get_status()
        self.assertTrue(status.get("remote1"))
        self.incus_mock.check_remote_connection.assert_called_with("remote1")

        monitor.stop()
        self.assertFalse(monitor.thread.is_alive())

    def test_monitor_failure(self):
        monitor = HypervisorMonitor(interval=0.1)

        # Mock failure
        self.incus_mock.check_remote_connection.return_value = False

        monitor.start()
        time.sleep(0.3)

        status = monitor.get_status()
        self.assertFalse(status.get("remote1"))

        monitor.stop()


if __name__ == "__main__":
    unittest.main()
