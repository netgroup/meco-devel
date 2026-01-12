import threading
import time
import logging
from typing import List, Dict
from meco.infra.incus import IncusClient
from meco.config.loader import CONFIG

logger = logging.getLogger("meco.monitor")


class HypervisorMonitor:
    """
    Monitors the connectivity status of configured hypervisors in a background thread.
    """

    def __init__(self, interval: int = 5):
        self.interval = interval
        self.running = False
        self.thread = None
        self.client = IncusClient()
        self.status: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def start(self):
        """Starts the monitoring thread."""
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        logger.info("Hypervisor monitoring started.")

    def stop(self):
        """Stops the monitoring thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        logger.info("Hypervisor monitoring stopped.")

    def _monitor_loop(self):
        """Main loop for monitoring."""
        hypervisors = CONFIG.get("hypervisors", [])
        if not hypervisors:
            logger.info("No hypervisors configured to monitor.")
            return

        while self.running:
            for remote in hypervisors:
                if not self.running:
                    break

                reachable = self.client.check_remote_connection(remote)

                with self._lock:
                    previous_status = self.status.get(remote)
                    self.status[remote] = reachable

                if not reachable:
                    logger.critical(f"Hypervisor '{remote}' is unreachable or stopped!")
                elif previous_status is False and reachable:
                    logger.info(f"Hypervisor '{remote}' is back online.")

            # Sleep in increments to allow faster shutdown
            sleep_step = 0.5
            if self.interval < sleep_step:
                time.sleep(self.interval)
            else:
                steps = int(self.interval / sleep_step)
                for _ in range(steps):
                    if not self.running:
                        break
                    time.sleep(sleep_step)

    def get_status(self) -> Dict[str, bool]:
        """Returns a copy of the current status."""
        with self._lock:
            return self.status.copy()
