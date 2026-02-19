import logging
import json
import time
import subprocess
from typing import List, Optional, Dict, Any, Union
from .executors import CommandExecutor, LocalExecutor

logger = logging.getLogger("meco.incus")


class IncusClient:
    """
    A client for interacting with Incus using a pluggable executor.
    Supports local and remote (SSH) execution.
    """

    def __init__(self, executor: CommandExecutor = None):
        self.executor = executor or LocalExecutor()

    def check_installed(self) -> bool:
        """Checks if the 'incus' command is available."""
        try:
            self.executor.run(["incus", "--version"], check=True, capture_output=True)
            return True
        except Exception:
            return False

    def check_remote_connection(self, remote: str) -> bool:
        """
        Check if a remote Incus endpoint is reachable and operational.
        """
        cmd = ["incus", "list", f"{remote}:", "--format=csv"]
        try:
            # Short command to check connectivity
            result = self.executor.run(
                cmd, check=False, capture_output=True, timeout=60
            )
            if result.returncode == 0:
                return True

            stderr = (result.stderr or "").lower()
            if any(
                x in stderr
                for x in ["timeout", "unable to connect", "connection refused"]
            ):
                # Check if it might be a local instance with the same name
                # This often happens when users run hypervisors as nested VMs
                try:
                    # Try to exec into it. If this works, the VM is up but API is down.
                    # We assume the remote name matches the instance name.
                    logger.debug(
                        f"Remote '{remote}' unreachable. Checking if it matches a local instance..."
                    )
                    local_check = self.executor.run(
                        ["incus", "exec", remote, "--", "true"],
                        check=False,
                        capture_output=True,
                        timeout=10,
                    )
                    if local_check.returncode == 0:
                        logger.error(
                            f"Hypervisor '{remote}' is RUNNING locally but its Incus API is unreachable."
                        )
                        logger.error(
                            f"To enable the API, run:\n  incus exec {remote} -- incus config set core.https_address [::]:8443"
                        )
                    else:
                        logger.error(
                            f"Incus remote '{remote}' is unreachable or in stopped state"
                        )
                except Exception:
                    # Fallback to original error if local check fails/blows up
                    logger.error(
                        f"Incus remote '{remote}' is unreachable or in stopped state"
                    )

                return False
            else:
                logger.warning(f"Unknown error connecting to '{remote}': {stderr}")
                return False
        except Exception as e:
            logger.error(f"Error checking Incus remote '{remote}': {e}")
            return False

    def list_instances(self, format_type: str = "json") -> List[Dict[str, Any]]:
        """Gets the list of Incus instances."""
        cmd = ["incus", "list", f"--format={format_type}"]
        try:
            res = self.executor.run(cmd, check=True, capture_output=True, timeout=30)
            if format_type == "json":
                return json.loads(res.stdout)
            return []  # CSV etc not strictly parsed here unless needed
        except Exception as e:
            logger.error(f"Error listing instances: {e}")
            return []

    def wait_for_state(
        self, instance_name: str, state: str = "Running", timeout: int = 300
    ) -> bool:
        """Waits for an Incus instance to reach a specific state."""
        return self.wait_for_instances([instance_name], state, timeout)

    def wait_for_instances(
        self, names: List[str], state: str, timeout: int = 300
    ) -> bool:
        """
        Wait for multiple instances to reach a specific state.
        Optimized to avoid N API calls.

        If waiting for "Running" state, this method will actively attempt to start
        instances found in "STOPPED" or "ERROR" state to recover from deployment flakes.
        """
        start_time = time.time()
        remaining = set(names)

        # Track last start attempt for each instance to avoid spamming start commands
        last_attempts = {}
        RETRY_INTERVAL = 30  # Seconds between start retries

        while time.time() - start_time < timeout:
            if not remaining:
                return True

            try:
                # Get status of all instances in one call
                cmd = ["incus", "list", "--format=json"]
                # Use a timeout to prevent hanging indefinitely on bad connections
                result = self.executor.run(
                    cmd, check=True, capture_output=True, timeout=15
                )
                data = json.loads(result.stdout)

                current_states = {}
                for item in data:
                    # Robustly handle missing or None state data
                    state_data = item.get("state")
                    if (
                        state_data
                        and isinstance(state_data, dict)
                        and "status" in state_data
                    ):
                        current_states[item["name"]] = state_data["status"]
                    else:
                        logger.warning(
                            f"Incomplete state data for instance: {item.get('name')}"
                        )

                # Check which ones are ready
                done = set()
                for name in remaining:
                    s = current_states.get(name)
                    if not s:
                        continue

                    # logger.debug(f"Seeing state '{s}' for {name}")

                    # Case insensitive check
                    if s.lower() == state.lower():
                        done.add(name)
                        logger.info(f"Instance {name} is {state}")
                    elif state.lower() == "running" and s.lower() in [
                        "stopped",
                        "error",
                    ]:
                        # Auto-recovery logic
                        now = time.time()
                        last = last_attempts.get(name, 0)

                        if now - last > RETRY_INTERVAL:
                            logger.warning(
                                f"Instance {name} is in {s} state. Attempting to start/restart..."
                            )

                            # Execute start command
                            # We use background=True if we want async, but here we might want to block briefly
                            # to ensure the command is sent.
                            start_cmd = ["incus", "start", name]
                            try:
                                self.executor.run(
                                    start_cmd,
                                    check=True,
                                    capture_output=True,
                                    timeout=30,
                                )
                                logger.info(f"Sent start command for {name}")
                                last_attempts[name] = now
                            except subprocess.TimeoutExpired:
                                logger.warning(
                                    f"Auto-start for {name} timed out after 30s"
                                )
                                last_attempts[name] = now
                            except subprocess.CalledProcessError as e:
                                err_msg = e.stderr.strip() if e.stderr else str(e)
                                if "busy" in err_msg or "operation" in err_msg:
                                    logger.info(
                                        f"Instance {name} is busy (likely creating). Waiting..."
                                    )
                                elif (
                                    "already running" in err_msg
                                    or "already started" in err_msg
                                ):
                                    # Recovery success! Instance is actually running despite what list said
                                    logger.info(
                                        f"Instance {name} reported as running by start command. Marking as ready."
                                    )
                                    done.add(name)
                                else:
                                    logger.warning(
                                        f"Failed to auto-start {name}: Exited with code {e.returncode}. Error: {err_msg}"
                                    )
                                # Don't update last_attempts so we retry sooner? Or waiting is safer?
                                # Let's update to prevent log spam if it's persistent error
                                last_attempts[name] = now
                            except Exception as e:
                                logger.error(f"Failed to auto-start {name}: {e}")
                                # Don't update last_attempts so we retry sooner? Or waiting is safer?
                                # Let's update to prevent log spam if it's persistent error
                                last_attempts[name] = now
                        else:
                            # Just waiting
                            pass

                remaining -= done

            except Exception as e:
                logger.warning(f"Error checking status: {e}")

            if remaining:
                time.sleep(1)  # Polling interval

        logger.warning(f"Timeout waiting for: {remaining}")
        return False

    def create_profile(self, profile_name: str) -> bool:
        """Creates an Incus profile."""
        cmd = ["incus", "profile", "create", profile_name]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            logger.info(f"Created Incus profile: {profile_name}")
            return True
        except Exception as e:
            if "already exists" in str(e) or (
                hasattr(e, "stderr") and "already exists" in e.stderr
            ):
                logger.debug(f"Profile {profile_name} already exists.")
                return True

            logger.warning(
                f"Failed to create profile {profile_name} (might exist): {e}"
            )
            return False

    def profile_exists(self, profile_name: str) -> bool:
        """Checks if an Incus profile exists (supports remote:name)."""
        remote = ""
        name = profile_name
        if ":" in profile_name:
            remote, name = profile_name.split(":", 1)
            remote = f"{remote}:"

        try:
            # List profiles on the specific remote
            res = self.executor.run(
                ["incus", "profile", "list", remote, "--format=csv"],
                capture_output=True,
            )
            profiles = res.stdout.strip().splitlines()
            # CSV format: name, ...
            existing_profiles = {p.split(",")[0] for p in profiles if p.strip()}
            return name in existing_profiles
        except Exception as e:
            logger.error(f"Error checking profile existence: {e}")
            return False

    def delete_profile(self, profile_name: str) -> bool:
        """Deletes an Incus profile."""
        cmd = ["incus", "profile", "delete", profile_name]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            logger.info(f"Deleted Incus profile: {profile_name}")
            return True
        except Exception as e:
            err = str(e).lower()
            if hasattr(e, "stderr") and e.stderr:
                err += " " + e.stderr.lower()

            if "not found" in err:
                return True

            logger.warning(f"Could not delete profile {profile_name}: {e}")
            return False

    def copy_profile(self, source: str, dest: str) -> bool:
        cmd = ["incus", "profile", "copy", source, dest]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            logger.error(f"Failed to copy profile {source} to {dest}: {e}")
            return False

    def add_profile_device(
        self, profile_name: str, device_name: str, device_type: str, *options: str
    ) -> bool:
        cmd = [
            "incus",
            "profile",
            "device",
            "add",
            profile_name,
            device_name,
            device_type,
        ] + list(options)
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            if "already exists" in str(e) or (
                hasattr(e, "stderr") and "already exists" in e.stderr
            ):
                # logger.info(f"Device {device_name} already in {profile_name}.") # Optional log
                return True
            logger.error(
                f"Failed to add device {device_name} to profile {profile_name}: {e}"
            )
            return False

    def remove_profile_device(self, profile_name: str, device_name: str) -> bool:
        cmd = ["incus", "profile", "device", "remove", profile_name, device_name]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception:
            return False  # Likely didn't exist

    def create_network(
        self, network_name: str, driver: str = "openvswitch", dns_mode: str = "dynamic"
    ) -> bool:
        cmd = [
            "incus",
            "network",
            "create",
            network_name,
            "--type=bridge",
            f"bridge.driver={driver}",
            f"dns.mode={dns_mode}",
        ]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            if "already exists" in str(e) or (
                hasattr(e, "stderr") and "already exists" in e.stderr
            ):
                logger.info(f"Network {network_name} already exists.")
                return True

            logger.error(f"Failed to create network {network_name}: {e}")
            return False

    def delete_network(self, network_name: str) -> bool:
        cmd = ["incus", "network", "delete", network_name]
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            err = str(e).lower()
            if hasattr(e, "stderr") and e.stderr:
                err += " " + e.stderr.lower()

            if "not found" in err:
                return True
            return False

    def launch_instance(
        self,
        image: str,
        name: str,
        profiles: List[str] = None,
        networks: List[str] = None,
        config: Dict[str, str] = None,
        is_vm: bool = False,
        cloud_init_file: str = None,
        devices: Dict[str, Dict[str, str]] = None,
    ) -> bool:
        """
        Launches an Incus instance.
        If devices are provided, uses 'init' -> 'config device add' -> 'start' workflow.
        Otherwise uses 'launch'.
        """
        # Base arguments construction
        cmd = ["incus", "launch", image, name]
        if is_vm:
            cmd.append("--vm")
        if profiles:
            for p in profiles:
                cmd.extend(["-p", p])
        if networks:
            for n in networks:
                cmd.extend(["--network", n])
        if config:
            for k, v in config.items():
                cmd.extend(["-c", f"{k}={v}"])
        if cloud_init_file:
            cmd.extend(["-c", f"user.user-data=@{cloud_init_file}"])

        # Decide workflow
        if devices:
            # 1. Init
            # We need to switch 'launch' to 'init'
            # Create a copy or modify in place? Modifying in place is fine as we don't use 'cmd' for launch in this branch.
            cmd[1] = "init"
            try:
                self.executor.run(cmd, check=True, capture_output=True, timeout=300)
                logger.info(f"Initialized instance {name}")
            except Exception as e:
                logger.error(f"Failed to initialize {name}: {e}")
                return False

            # 2. Add Devices
            for dev_name, props in devices.items():
                # props: {'type': 'nic', 'nictype': 'bridged', ...}
                dev_type = props.pop(
                    "type", "nic"
                )  # Default to nic if not in dict, but should be.

                # Construct config args
                # incus config device add <inst> <dev> <type> key=val ...
                cmd_dev = ["incus", "config", "device", "add", name, dev_name, dev_type]
                for k, v in props.items():
                    cmd_dev.append(f"{k}={v}")

                try:
                    self.executor.run(cmd_dev, check=True, capture_output=True)
                    logger.debug(f"Added device {dev_name} to {name}")
                except Exception as e:
                    logger.error(f"Failed to add device {dev_name} to {name}: {e}")
                    # Try to cleanup
                    self.delete_instance(name, force=True)
                    return False

            # 3. Start
            cmd_start = ["incus", "start", name]
            try:
                self.executor.run(
                    cmd_start, check=True, capture_output=True, timeout=300
                )
                logger.info(f"Started instance {name}")
                return True
            except Exception as e:
                logger.error(f"Failed to start {name}: {e}")
                # Cleanup? Maybe leave it for debug.
                return False

        else:
            # Existing Launch Logic
            # cmd is already set to launch

            # Retry logic for robust remote deployment
            max_retries = 3
            delay = 2
            for attempt in range(max_retries):
                try:
                    # Synchronous execution to ensure we catch launch errors (e.g. image download failure)
                    self.executor.run(
                        cmd,
                        check=True,
                        capture_output=True,
                        background=False,
                        timeout=300,
                    )
                    logger.info(f"Launched instance {name}")
                    return True
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"Launch attempt {attempt + 1}/{max_retries} failed for {name}: Timed out after 120s"
                    )
                except subprocess.CalledProcessError as e:
                    err_msg = e.stderr.strip() if e.stderr else str(e)
                    logger.warning(
                        f"Launch attempt {attempt + 1}/{max_retries} failed for {name}: Exited with code {e.returncode}. Error: {err_msg}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Launch attempt {attempt + 1}/{max_retries} failed for {name}: {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        delay *= 2
                    else:
                        logger.error(
                            f"Failed to launch {name} after {max_retries} attempts: {e}"
                        )
                        return False
            return False

    def delete_instance(self, name: str, force: bool = True) -> bool:
        cmd = ["incus", "delete", name]
        if force:
            cmd.append("--force")
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to delete {name}: {e}")
            return False

    def add_instance_device(
        self, instance_name: str, device_name: str, device_type: str, *options: str
    ) -> bool:
        cmd = [
            "incus",
            "config",
            "device",
            "add",
            instance_name,
            device_name,
            device_type,
        ] + list(options)
        try:
            self.executor.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            logger.error(f"Failed to add device to instance {instance_name}: {e}")
            return False
