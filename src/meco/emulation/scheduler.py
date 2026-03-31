from itertools import cycle
import logging
import threading
import time
from typing import Dict, List

from meco.network.manager import NetworkManager
from meco.utils.logger import setup_logging

logger = setup_logging("meco.scheduler")


class Scheduler:
    """
    Distributes unassigned instances across available hypervisors.
    """

    @staticmethod
    def schedule(instances, hypervisors):
        """
        Assigns instances to hypervisors using a Round Robin strategy.

        Args:
            instances (list): List of instance names (str) to be assigned.
            hypervisors (list): List of available hypervisor names/aliases (str).

        Returns:
            dict: Mapping of instance_name -> hypervisor_name.
        """
        if not instances:
            return {}

        if not hypervisors:
            logger.warning(
                "No hypervisors available for scheduling. Defaulting to local if managed elsewhere, or leaving unassigned."
            )
            # Depending on caller usage, we might return all mapped to None (local) or empty.
            # Lifecycle manager interprets None as local.
            return {inst: None for inst in instances}

        assignments = {}
        # Create a cycle iterator for round robin
        # Sort hypervisors to ensure deterministic assignment if the list is consistent
        hv_cycle = cycle(sorted(hypervisors))

        for inst in instances:
            target = next(hv_cycle)
            # If target is "local", we map it to None as per Lifecycle convention,
            # OR we keep it as "local" string if Lifecycle handles that map key.
            # Lifecycle.clients uses "local" key. Lifecycle.node_locations uses None or alias.
            # Let's check lifecycle usage.
            # In lifecycle: remote = self.node_locations.get(name) ... if remote ...
            # clients key is "local".
            # So if we return "local", remote variable becomes "local".
            # clients.get("local", ...) works.
            # But "if remote:" check might treat "local" as true, triggering upload logic?
            # Lifecycle: if remote: upload file...
            # If remote is "local", upload file logic tries to upload to "local"?
            # SshExecutor vs LocalExecutor.
            # Expectation: node_locations value should be consistent with clients keys.
            assignments[inst] = target
            logger.debug(f"Scheduled {inst} -> {target}")

        return assignments

class TopologyScheduler:
    """
    Schedules and applies topology connection updates based on the exact
    timestamps defined in the configuration (e.g. visibility-constellation).
    """

    def __init__(self, topo: dict, network_manager: NetworkManager):
        self.topo = topo
        self.network_manager = network_manager
        self.running = False
        self.thread = None
        
        # Parse all unique timestamps and sort them
        self.timestamps = self._extract_timestamps(topo)
        self.start_time = 0.0
        self.previous_links = set()

    def _extract_timestamps(self, topo: dict) -> List[int]:
        times = set()
        for section in ("visibility-constellation", "visibility-ground"):
            for snap in topo.get(section, []):
                t = snap.get("time")
                if t is not None:
                    times.add(int(t))
        return sorted(list(times))

    def start(self):
        """Starts the background scheduling thread."""
        if not self.timestamps:
            logger.info("No timestamps found in topology. TopologyScheduler not starting.")
            return

        if self.running:
            return

        self.running = True
        self.start_time = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True, name="TopologyScheduler")
        self.thread.start()
        logger.info(f"Started TopologyScheduler with epochs: {self.timestamps}")

    def stop(self):
        """Stops the scheduling thread."""
        self.running = False
        logger.info("Stopped TopologyScheduler.")

    def _run(self):
        for epoch in self.timestamps:
            if not self.running:
                break
                
            # Calculate how long to wait until this epoch
            target_time = self.start_time + epoch
            now = time.time()
            sleep_duration = target_time - now
            
            if sleep_duration > 0:
                logger.info(f"[Scheduler] Sleeping for {sleep_duration:.2f}s until epoch {epoch}")
                # Sleep in small bursts to allow responsive shutdown
                while sleep_duration > 0 and self.running:
                    chunk = min(0.5, sleep_duration)
                    time.sleep(chunk)
                    sleep_duration -= chunk

            if not self.running:
                break

            logger.info(f"[Scheduler] Activating epoch {epoch}")
            try:
                # 1. Fetch current links for this epoch
                current_links = set(self.network_manager._collect_topology_links(self.topo, epoch_time=epoch))
                
                # 2. Calculate delta
                links_to_add = current_links - self.previous_links
                links_to_remove = self.previous_links - current_links
                
                # 3. Apply changes incrementally
                self.network_manager.apply_topology_delta(links_to_add, links_to_remove, self.topo)
                
                # 4. Update state
                self.previous_links = current_links
                        
                logger.info(f"[Scheduler] Successfully applied flows for epoch {epoch} (Added: {len(links_to_add)}, Removed: {len(links_to_remove)})")
            except Exception as e:
                logger.error(f"[Scheduler] Failed to apply epoch {epoch}: {e}", exc_info=True)
                
        if self.running:
            logger.info("[Scheduler] Finished all configured epochs.")
