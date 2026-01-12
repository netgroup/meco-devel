import unittest
from unittest.mock import MagicMock, call
import time
import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from meco.infra.incus import IncusClient


class TestIncusAutoStart(unittest.TestCase):
    def test_wait_for_instances_autostart(self):
        """
        Test that wait_for_instances sends a start command if an instance is STOPPED.
        """
        mock_executor = MagicMock()
        client = IncusClient(executor=mock_executor)

        # Simulation:
        # 1. First call: Instance 'node1' is STOPPED
        # 2. Second call: Instance 'node1' is RUNNING (simulating successful start)

        # We need to mock the `run` method to return different JSONs for `incus list`

        stopped_json = '[{"name": "node1", "state": {"status": "STOPPED"}}]'
        running_json = '[{"name": "node1", "state": {"status": "Running"}}]'

        # Sequence of return values for executor.run(...).stdout
        # First call is from wait loop
        # Second call is from `incus start` (returncode 0, empty stdout)
        # Third call is next wait loop

        mock_result_stopped = MagicMock()
        mock_result_stopped.stdout = stopped_json
        mock_result_stopped.returncode = 0

        mock_result_start = MagicMock()
        mock_result_start.stdout = ""
        mock_result_start.returncode = 0

        mock_result_running = MagicMock()
        mock_result_running.stdout = running_json
        mock_result_running.returncode = 0

        # We need to handle deciding which result to return based on the command args
        def side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "incus list" in cmd_str:
                # Toggle between stopped and running based on call count or state
                if side_effect.counter == 0:
                    side_effect.counter += 1
                    return mock_result_stopped
                else:
                    return mock_result_running
            elif "incus start" in cmd_str:
                return mock_result_start
            return MagicMock()

        side_effect.counter = 0
        mock_executor.run.side_effect = side_effect

        # Run the method
        # We expect it to finish successfully
        result = client.wait_for_instances(["node1"], "Running", timeout=5)

        self.assertTrue(result, "wait_for_instances should return True")

        # Verify `incus start node1` was called
        # We need to inspect the calls to mock_executor.run
        # We look for a call that contains 'start' and 'node1'

        start_called = False
        for call_args in mock_executor.run.call_args_list:
            args, _ = call_args
            cmd = args[0]
            if "start" in cmd and "node1" in cmd:
                start_called = True
                break

        self.assertTrue(
            start_called,
            "IncusClient should have attempted to start the STOPPED instance.",
        )


if __name__ == "__main__":
    unittest.main()
