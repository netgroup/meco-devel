from itertools import cycle
import logging

logger = logging.getLogger("meco.scheduler")


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
