#!/usr/bin/env python

import os
import sys
import time
import signal
import argparse
import argcomplete
import logging
import grpc
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import yaml
import subprocess
from yaml import YAMLError
import psutil  # For process checking
from jsonschema import validate, ValidationError
import json
import functools
from typing import List, Dict
import re
import meco_pb2
import meco_pb2_grpc


# === Colored Log Setup with [Server-LEVEL] Format ===
class LogColors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


# --- Centralized Subprocess Execution ---


def run_command(
    cmd,
    check=True,
    capture_output=True,
    text=True,
    shell=False,
    log_level=logging.DEBUG,
):
    """
    Executes a shell command using subprocess.run with standardized options.

    Args:
        cmd (list or str): The command and its arguments as a list, or a string if shell=True.
        check (bool): If True, raises CalledProcessError on non-zero exit code. Defaults to True.
        capture_output (bool): If True, captures stdout and stderr. Defaults to True.
        text (bool): If True, returns output as strings. Defaults to True.
        shell (bool): If True, command is executed through the shell. Use cautiously. Defaults to False.
        log_level (int): The logging level for debug messages (e.g., logging.DEBUG). Defaults to DEBUG.

    Returns:
        subprocess.CompletedProcess: The result of the command execution.

    Raises:
        subprocess.CalledProcessError: If check=True and the command fails.
        Exception: For other unexpected errors during execution.
    """
    try:
        # Execute the command
        result = subprocess.run(
            cmd, check=check, capture_output=capture_output, text=text, shell=shell
        )

        # Log output if captured and at appropriate level
        if capture_output and log_level <= logging.INFO:
            if result.stderr:
                # Log stderr, potentially at WARNING or ERROR level depending on check/result
                logger.log(
                    logging.WARNING if result.returncode == 0 else logging.ERROR,
                    f"Command stderr: {result.stderr.strip()}",
                )

        return result

    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}: {e.cmd}")
        if e.stderr:
            logger.error(f"Command stderr: {e.stderr.strip()}")
        raise  # Re-raise the exception
    except Exception as e:
        logger.error(f"Unexpected error running command {cmd}: {e}")
        raise  # Re-raise the exception


# --- Incus Specific Operations ---


def incus_check_installed() -> bool:
    """Checks if the 'incus' command is available."""
    try:
        run_command(["incus", "--version"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def incus_list_instances(format_type="json"):
    """Gets the list of Incus instances."""
    cmd = ["incus", "list", f"--format={format_type}"]
    result = run_command(cmd, capture_output=True)
    if format_type == "json":
        return json.loads(result.stdout)
    elif format_type == "csv":
        return result.stdout.strip().splitlines()  # Return list of lines
    else:
        return result.stdout  # Return raw string for other formats


def incus_wait_for_state(
    instance_name: str, target_state: str = "Running", timeout: int = 60
) -> bool:
    """Waits for an Incus instance to reach a specific state."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = run_command(
                ["incus", "query", f"/1.0/instances/{instance_name}/state"]
            )
            state_info = json.loads(result.stdout)
            if state_info and state_info.get("status") == target_state:
                return True
        except Exception as e:
            logger.debug(f"Error checking status for {instance_name}: {e}")
        time.sleep(1)  # Poll every second
    logger.error(
        f"Instance {instance_name} did not reach state '{target_state}' within {timeout} seconds."
    )
    return False


def incus_create_profile(profile_name: str) -> bool:
    """Creates an Incus profile."""
    cmd = ["incus", "profile", "create", profile_name]
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Created Incus profile: {profile_name}")
        return True
    except subprocess.CalledProcessError as e:
        if "already exists" in e.stderr.lower():
            logger.info(f"Incus profile {profile_name} already exists.")
            return True
        else:
            logger.error(f"Failed to create profile {profile_name}: {e.stderr.strip()}")
            return False
    except Exception as e:
        logger.error(f"Unexpected error creating profile {profile_name}: {e}")
        return False


def incus_profile_exists(profile_name: str) -> bool:
    """Checks if an Incus profile exists."""
    try:
        result = run_command(
            ["incus", "profile", "list", "--format=csv"], capture_output=True
        )
        profiles = result.stdout.strip().splitlines()
        # Assuming CSV format: name,description,used_by
        existing_profiles = {p.split(",")[0] for p in profiles if p.strip()}
        return profile_name in existing_profiles
    except Exception as e:
        logger.error(f"Error checking if profile {profile_name} exists: {e}")
        return False  # Assume it doesn't exist if check fails


def incus_delete_profile(profile_name: str) -> bool:
    """Deletes an Incus profile."""
    cmd = ["incus", "profile", "delete", profile_name]
    try:
        # Use check=False to handle profile not existing
        result = run_command(
            cmd, check=False, capture_output=True, log_level=logging.INFO
        )
        if result.returncode == 0:
            logger.info(f"Deleted Incus profile: {profile_name}")
            return True
        elif (
            "not found" in result.stderr.lower()
            or "does not exist" in result.stderr.lower()
        ):
            logger.info(f"Profile {profile_name} not found, nothing to delete.")
            return True
        else:
            logger.warning(
                f"Failed to delete profile {profile_name}: {result.stderr.strip()}"
            )
            return False  # Indicate failure if it was something other than not found
    except Exception as e:
        logger.error(f"Unexpected error deleting profile {profile_name}: {e}")
        return False


def incus_copy_profile(source_profile: str, dest_profile: str) -> bool:
    """Copies an Incus profile."""
    cmd = ["incus", "profile", "copy", source_profile, dest_profile]
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Copied Incus profile {source_profile} to {dest_profile}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Failed to copy profile {source_profile} to {dest_profile}: {e.stderr.strip()}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error copying profile {source_profile} to {dest_profile}: {e}"
        )
        return False


def incus_add_profile_device(
    profile_name: str, device_name: str, device_type: str, *options: str
) -> bool:
    """Adds a device to an Incus profile."""
    cmd = [
        "incus",
        "profile",
        "device",
        "add",
        profile_name,
        device_name,
        device_type,
    ] + list(options)
    # Use check=False if adding might fail (e.g., device already exists) and you want to handle it
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(
            f"Added device {device_name} ({device_type}) to profile {profile_name}"
        )
        return True
    except subprocess.CalledProcessError as e:
        # Handle specific error like "already exists" if needed
        logger.warning(
            f"Failed to add device {device_name} to profile {profile_name}: {e.stderr.strip()}"
        )
        return False  # Or raise depending on desired behavior for failures
    except Exception as e:
        logger.error(
            f"Unexpected error adding device {device_name} to profile {profile_name}: {e}"
        )
        return False


def incus_remove_profile_device(profile_name: str, device_name: str) -> bool:
    """Removes a device from an Incus profile."""
    cmd = ["incus", "profile", "device", "remove", profile_name, device_name]
    # Use check=False if removing might fail (e.g., device doesn't exist) and you want to handle it
    try:
        result = run_command(
            cmd, check=False, capture_output=True, log_level=logging.INFO
        )
        if result.returncode == 0:
            logger.info(f"Removed device {device_name} from profile {profile_name}")
            return True
        elif (
            "not found" in result.stderr.lower()
            or "does not exist" in result.stderr.lower()
        ):
            logger.info(
                f"Device {device_name} not found on profile {profile_name}, nothing to remove."
            )
            return True
        else:
            logger.warning(
                f"Failed to remove device {device_name} from profile {profile_name}: {result.stderr.strip()}"
            )
            return False
    except Exception as e:
        logger.error(
            f"Unexpected error removing device {device_name} from profile {profile_name}: {e}"
        )
        return False


def incus_create_network(network_name: str) -> bool:
    """
    Creates a managed Incus network using the Open vSwitch driver.
    Uses configuration for driver and DNS mode.
    """
    network_type = "bridge"  # Standard for this use case
    driver = CONFIG_DEFAULTS["bridge_driver"]
    dns_mode = CONFIG_DEFAULTS["dns_mode"]

    cmd = [
        "sudo",
        "incus",
        "network",
        "create",
        network_name,
        f"--type={network_type}",
        f"bridge.driver={driver}",
        f"dns.mode={dns_mode}",
    ]
    try:
        result = run_command(
            cmd, check=False, capture_output=True, log_level=logging.INFO
        )
        if result.returncode == 0:
            logger.info(
                f"Created Incus network '{network_name}' using {driver} driver."
            )
            return True
        elif "already exists" in result.stderr.lower():
            logger.info(f"Incus network '{network_name}' already exists.")
            return True
        else:
            logger.error(
                f"Failed to create Incus network '{network_name}': {result.stderr.strip()}"
            )
            return False
    except Exception as e:
        logger.error(f"Unexpected error creating Incus network '{network_name}': {e}")
        return False


def incus_delete_network(network_name: str) -> bool:
    """Deletes an Incus network using sudo."""
    cmd = ["sudo", "incus", "network", "delete", network_name]
    try:
        # Use check=False to handle network not existing
        result = run_command(
            cmd, check=False, capture_output=True, log_level=logging.INFO
        )
        if result.returncode == 0:
            logger.info(f"Deleted Incus network: {network_name}")
            return True
        elif (
            "not found" in result.stderr.lower()
            or "does not exist" in result.stderr.lower()
        ):
            logger.info(f"Network {network_name} not found, nothing to delete.")
            return True
        else:
            logger.warning(
                f"Failed to delete network {network_name}: {result.stderr.strip()}"
            )
            return False  # Indicate failure if it was something other than not found
    except Exception as e:
        logger.error(f"Unexpected error deleting network {network_name}: {e}")
        return False


def incus_launch_instance(
    image: str,
    name: str,
    profiles: list[str] = None,
    networks: list[str] = None,
    config: dict = None,
    is_vm: bool = False,
    cloud_init_file: str = None,
) -> bool:
    """Launches an Incus instance (container or VM)."""
    cmd = ["incus", "launch", image, name]
    if is_vm:
        cmd.append("--vm")
    if profiles:
        for profile in profiles:
            cmd.extend(["-p", profile])
    if networks:
        for network in networks:
            cmd.extend(["--network", network])
    if config:
        for key, value in config.items():
            cmd.extend(["-c", f"{key}={value}"])
    if cloud_init_file:
        cmd.extend(["-c", f"user.user-data=@{cloud_init_file}"])

    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Launched Incus instance: {name}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to launch instance {name}: {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error launching instance {name}: {e}")
        return False


def incus_add_instance_device(
    instance_name: str, device_name: str, device_type: str, *options: str
) -> bool:
    """Adds a device to a running Incus instance."""
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
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(
            f"Added device {device_name} ({device_type}) to instance {instance_name}"
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(
            f"Failed to add device {device_name} to instance {instance_name}: {e.stderr.strip()}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error adding device {device_name} to instance {instance_name}: {e}"
        )
        return False


def incus_delete_instance(name: str, force: bool = True, wait: bool = False) -> bool:
    """
    Deletes an Incus instance.

    Args:
        name (str): The name of the instance.
        force (bool): If True, forces deletion. Defaults to True.
        wait (bool): If True, waits for the operation to complete (Incus usually handles this). Defaults to False.

    Returns:
        bool: True if the command succeeded or the instance didn't exist, False otherwise.
    """
    cmd = ["incus", "delete", name]
    if force:
        cmd.append("--force")
    # Use check=False to handle cases where instance might not exist gracefully
    # unless specific error handling for that case is needed.
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        # logger.info(f"Deleted instance: {name}")
        return True
    except subprocess.CalledProcessError as e:
        # Check if error is because instance doesn't exist (exit code might vary)
        if "not found" in e.stderr.lower() or "does not exist" in e.stderr.lower():
            logger.info(f"Instance {name} not found, nothing to delete.")
            return True  # Consider it a success if it's already gone
        else:
            logger.error(f"Failed to delete instance {name}: {e.stderr.strip()}")
            return False
    except Exception as e:
        logger.error(f"Unexpected error deleting instance {name}: {e}")
        return False


def setup_meco_base_profiles() -> bool:
    """
    Sets up the base Incus profiles required for MECO using config names.
    """
    success = True
    try:
        profile_container = CONFIG_DEFAULTS["profile_base_container"]
        profile_vm = CONFIG_DEFAULTS["profile_base_vm"]
        ovs_bridge_internal = CONFIG_DEFAULTS["ovs_bridge_internal"]
        ovs_bridge_tunnel = CONFIG_DEFAULTS["ovs_bridge_tunnel"]
        storage_pool = CONFIG_DEFAULTS["storage_pool"]

        # --- 1. Ensure 'meco-base' Incus profile exists ---
        if not incus_profile_exists(profile_container):
            if not incus_create_profile(profile_container):
                success = False

        # --- 2. Add root disk device to 'meco-base' ---
        root_opts = ["path=/", f"pool={storage_pool}", "type=disk"]
        if not incus_add_profile_device(profile_container, "root", "disk", *root_opts):
            logger.warning(
                f"Failed to add root disk to '{profile_container}'. It might already exist."
            )

        # --- 3. Configure eth0 NIC device in 'meco-base' -> br-int ---
        # Note: Using the configured internal bridge name
        if not incus_add_profile_device(
            profile_container,
            "eth0",
            "nic",
            "nictype=bridged",
            f"parent={ovs_bridge_internal}",
        ):
            success = False

        # --- 4. Create 'meco-vm' profile based on 'meco-base' ---
        if not incus_profile_exists(profile_vm):
            if not incus_copy_profile(profile_container, profile_vm):
                success = False
            # --- 5. Add eth1 NIC device to 'meco-vm' -> br-tun ---
            # Note: Using the configured internal bridge name for VMs as well
            # if not incus_add_profile_device(profile_vm, "eth1", "nic", "nictype=bridged", f"parent={ovs_bridge_tunnel}"):
            #      success = False

        if success:
            logger.info(
                f"MECO base profiles ('{profile_container}', '{profile_vm}') configured successfully."
            )
        else:
            logger.error("MECO base profile setup completed with errors.")

    except Exception as e:
        logger.error(f"Failed to set up MECO base profiles: {e}")
        success = False

    return success


def _get_meco_instances():
    """
    Retrieves a list of running MECO Incus instances.

    Returns:
        list: A list of dictionaries representing running MECO instances
            as returned by `incus list --format=json`.
            Returns an empty list if no instances are found or on error.
    """
    try:
        result = run_command(
            ["incus", "list", "--format=json"],
            check=True,
            capture_output=True,
            text=True,
        )
        all_instances = json.loads(result.stdout)
        meco_instances = [
            inst
            for inst in all_instances
            if inst.get("config", {}).get("user.meco") == "true"
            and inst.get("status") == "Running"
        ]
        if not meco_instances:
            logger.warning("No running MECO instances found.")
        return meco_instances
    except subprocess.CalledProcessError as e:
        logger.error(f"Error listing Incus instances: {e.stderr.strip()}")
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing Incus list JSON output: {e}")
    except Exception as e:
        logger.error(f"Unexpected error getting MECO instances: {e}")
    return []


def delete_meco_instances(force: bool = True):
    """
    Deletes all Incus instances with user.meco=true (any status).
    Uses ThreadPoolExecutor for parallel deletion.
    Returns a dict with instance name as key and True/False for success.
    """
    meco_instances = _get_meco_instances()
    names = [i["name"] for i in meco_instances]
    results = {}
    if not names:
        logger.info("No MECO instances to delete.")
        return results
    workers = min(len(names), 16)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(incus_delete_instance, nm, force): nm for nm in names}
        for fut in as_completed(futs):
            nm = futs[fut]
            try:
                res = fut.result()
                results[nm] = res
                if res:
                    logger.info(f"Deleted instance: {nm}")
                else:
                    logger.error(f"Failed to delete instance: {nm}")
            except Exception as e:
                logger.error(f"Error deleting {nm}: {e}")
                results[nm] = False
    return results


# --- OVS Specific Operations ---


def ovs_del_flows(bridge: str) -> bool:
    """Deletes all flows from an OVS bridge."""
    cmd = ["sudo", "ovs-ofctl", "del-flows", bridge]
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Cleared flows from OVS bridge: {bridge}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to delete flows from bridge {bridge}: {e.stderr.strip()}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error deleting flows from bridge {bridge}: {e}")
        return False


def ovs_add_flow(bridge: str, flow_rule: str) -> bool:
    """Adds a flow rule to an OVS bridge."""
    cmd = ["sudo", "ovs-ofctl", "add-flow", bridge, flow_rule]
    # --- Add this debug line ---
    logger.debug(f"Executing OVS command: {' '.join(cmd)}")
    # --- End of addition ---
    try:
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Added flow rule to {bridge}: {flow_rule}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Failed to add flow rule to {bridge} ({flow_rule}): {e.stderr.strip()}"
        )
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error adding flow rule to {bridge} ({flow_rule}): {e}"
        )
        return False


def ovs_list_ports(bridge: str) -> list[str]:
    """Lists ports on an OVS bridge."""
    cmd = ["sudo", "ovs-vsctl", "list-ports", bridge]
    try:
        result = run_command(cmd, capture_output=True)
        ports = result.stdout.strip().splitlines()
        logger.debug(f"Ports on OVS bridge {bridge}: {ports}")
        return ports
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to list ports on bridge {bridge}: {e.stderr.strip()}")
    except Exception as e:
        logger.error(f"Unexpected error listing ports on bridge {bridge}: {e}")
    return []


def ovs_del_port(bridge: str, port: str) -> bool:
    """Deletes a port from an OVS bridge."""
    cmd = ["sudo", "ovs-vsctl", "--if-exists", "del-port", bridge, port]
    try:
        # --if-exists makes it succeed even if port doesn't exist, so check=True is usually fine
        run_command(cmd, check=True, capture_output=True, log_level=logging.INFO)
        logger.info(f"Deleted port {port} from OVS bridge {bridge}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Failed to delete port {port} from bridge {bridge}: {e.stderr.strip()}"
        )
        return False
    except Exception as e:
        logger.error(f"Unexpected error deleting port {port} from bridge {bridge}: {e}")
        return False


def ovs_port_to_br(port: str) -> str | None:
    """Gets the bridge a port is attached to."""
    cmd = ["sudo", "ovs-vsctl", "port-to-br", port]
    try:
        result = run_command(
            cmd, check=False, capture_output=True
        )  # check=False in case port isn't attached
        bridge = result.stdout.strip()
        if bridge:
            logger.debug(f"Port {port} is attached to bridge {bridge}")
            return bridge
        else:
            logger.debug(f"Port {port} is not attached to any OVS bridge.")
            return None
    except subprocess.CalledProcessError as e:
        # Likely means port doesn't exist or isn't managed by OVS
        logger.debug(
            f"Port {port} not found in OVS or error checking: {e.stderr.strip()}"
        )
        return None
    except Exception as e:
        logger.error(f"Unexpected error checking bridge for port {port}: {e}")
        return None


def ovs_get_interface_ofport(interface: str) -> int | None:
    """Gets the OpenFlow port number for an OVS interface."""
    cmd = ["sudo", "ovs-vsctl", "get", "Interface", interface, "ofport"]
    try:
        result = run_command(cmd, capture_output=True)
        port_str = result.stdout.strip()
        if port_str.isdigit():
            port_num = int(port_str)
            logger.debug(f"OVS interface {interface} has ofport {port_num}")
            return port_num
        else:
            logger.warning(
                f"Invalid port number for OVS interface {interface}: {port_str}"
            )
            return None
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Failed to get ofport for interface {interface}: {e.stderr.strip()}"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting ofport for interface {interface}: {e}")
    return None


# --- Refactored _setup_bridges using the new functions ---


def _setup_bridges():
    """
    Sets up the core MECO infrastructure: OVS bridges/networks and base profiles.
    Uses configuration for names.
    """
    bridges_created = True
    profiles_setup = True

    try:
        # Use configured bridge names
        bridge_internal = CONFIG_DEFAULTS["ovs_bridge_internal"]
        bridge_tunnel = CONFIG_DEFAULTS["ovs_bridge_tunnel"]

        # 1) Create two managed Incus networks with OVS driver
        logger.info("Creating MECO Incus networks (OVS bridges)...")
        for net in (bridge_internal, bridge_tunnel):
            if not incus_create_network(net):
                bridges_created = False

        if not bridges_created:
            logger.error("Failed to create one or more Incus networks (OVS bridges).")
            raise RuntimeError(
                "Failed to create required Incus networks (OVS bridges)."
            )

        # 2) Ensure 'meco-base' and 'meco-vm' Incus profiles exist and are configured
        logger.info("Setting up MECO base profiles...")
        profiles_setup = setup_meco_base_profiles()

        if profiles_setup:
            logger.info("Bridges and base profiles initialized successfully.")
        else:
            logger.error("Bridge creation succeeded, but profile setup had errors.")

    except Exception as e:
        logger.error(f"Critical error during _setup_bridges: {e}")
        raise


# --- Update _teardown_bridges to use config names ---


def _teardown_bridges():
    """Tears down the MECO infrastructure using config names."""
    # Use configured bridge names
    bridge_internal = CONFIG_DEFAULTS["ovs_bridge_internal"]
    bridge_tunnel = CONFIG_DEFAULTS["ovs_bridge_tunnel"]
    profile_container = CONFIG_DEFAULTS["profile_base_container"]
    profile_vm = CONFIG_DEFAULTS["profile_base_vm"]

    # 1) Remove all OpenFlow rules and ports from br-int & br-tun
    for br in (bridge_internal, bridge_tunnel):
        ovs_del_flows(br)
        try:
            ports = ovs_list_ports(br)
            for p in ports:
                if p == br:
                    continue
                ovs_del_port(br, p)
                run_command(
                    ["sudo", "ip", "link", "del", p],
                    check=False,
                    log_level=logging.INFO,
                )
        except Exception as e:  # Catch broader exceptions from helpers
            logger.warning(f"Error listing/processing ports for {br}: {e}")

        # 2) Delete the Incus networks
        incus_delete_network(br)

    # 3) Delete the Incus profiles
    # Note: server_off also handles this. This is just for symmetry if called independently.
    # incus_delete_profile(profile_container)
    # incus_delete_profile(profile_vm)
    # logger.info(f"Deleted Incus profiles: {profile_container}, {profile_vm}")


# --- Refactored _apply_openflow_rules to use helper functions ---
def _apply_openflow_rules(bridge: str, flows: list[str]):
    """Applies a list of OpenFlow rules to a specified bridge using helper functions."""
    logger.info(f"Applying OpenFlow rules to bridge {bridge}...")
    try:
        ovs_del_flows(bridge)
        logger.info(f"Cleared existing flows from bridge {bridge}.")
        for rule in flows:
            ovs_add_flow(bridge, rule)
            # logger.info(f"OF rule added: {rule}") # Already logged in ovs_add_flow
        logger.info("All flows installed successfully.")
    except Exception as e:  # Catch exceptions from helpers
        logger.error(f"Failed to apply OpenFlow rules to {bridge}: {e}")
        raise


def _get_instance_network_state(instance_name: str):
    """
    Fetches the network state information for a specific Incus instance.

    Args:
        instance_name (str): The name of the Incus instance.

    Returns:
        dict: The 'network' section of the instance's state, or an empty dict on error.
    """
    try:
        result = run_command(
            ["incus", "query", f"/1.0/instances/{instance_name}/state"],
            check=True,
            capture_output=True,
            text=True,
        )
        state_info = json.loads(result.stdout)
        return state_info.get("network", {})
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Error querying state for instance {instance_name}: {e.stderr.strip()}"
        )
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing state JSON for instance {instance_name}: {e}")
    except Exception as e:
        logger.error(
            f"Unexpected error getting network state for instance {instance_name}: {e}"
        )
    return {}


def _wait_for_ipv4_addresses(
    topo: dict, timeout: int = 120, poll_interval: int = 3
) -> dict[str, tuple[str, int]]:
    """
    Waits for all declared nodes to obtain an IPv4 address and builds the port map.

    Args:
        topo: The topology dictionary.
        timeout: Maximum time to wait in seconds.
        poll_interval: Time to wait between checks in seconds.

    Returns:
        A dictionary mapping instance_id:interface_index to (ovs_bridge, ovs_port_number).
        Returns an empty dict if timeout is reached before all IPs are found.
    """
    logger.info("[Flows] Waiting for IPv4 addresses to build adjacency flows...")

    start_time = time.time()
    expected = len(topo.get("nodes", []))
    port_map: dict[str, tuple[str, int]] = {}

    while time.time() - start_time < timeout:
        port_map = _build_port_map()
        got = len(port_map)
        logger.info(
            f"[Flows] Port map progress {got}/{expected} (elapsed {time.time() - start_time:.1f}s)"
        )
        if got >= expected:
            logger.info("[Flows] All expected IPv4 addresses acquired.")
            break
        time.sleep(poll_interval)
    else:
        logger.error(
            f"[Flows] Timeout after {timeout}s waiting for IPv4 mappings; proceeding with {len(port_map)} of {expected} (some links may be skipped)."
        )

    if not port_map:
        logger.error("[Flows] Empty port map; aborting flow installation.")
    return port_map


def _collect_topology_links(topo: dict) -> list[tuple[int, int]]:
    """
    Collects undirected links from the topology's visibility sections.

    Args:
        topo: The topology dictionary.

    Returns:
        A list of tuples representing undirected links (source_id, destination_id).
    """
    links: list[tuple[int, int]] = []
    for section in ("visibility-constellation", "visibility-ground"):
        for snap in topo.get(section, []):
            for conn in snap.get("connection", []):
                src = conn.get("source")
                dst = conn.get("destination")
                # Basic validation and type checking could be added here if needed
                if src is not None and dst is not None:
                    # Ensure consistent ordering for undirected link representation if needed,
                    # though for adjacency list, (a,b) and (b,a) both add connections.
                    links.append((src, dst))
    return links


def _build_adjacency_map(
    links: list[tuple[int, int]],
    port_map: dict[str, tuple[str, int]],
    managed_bridges: set[str],
) -> dict[str, dict[int, set[int]]]:
    """
    Builds an adjacency list representing connections per OVS bridge based on topology links and port mappings.

    Args:
        links: List of (source_id, destination_id) tuples.
        port_map: Dictionary mapping instance_id:interface_index to (ovs_bridge, ovs_port_number).
        managed_bridges: Set of bridge names that are managed by this system.

    Returns:
        A dictionary: {bridge_name: {ofport: {neighbour_ofport, ...}}}.
        Returns an empty dict if no valid connections are found.
    """
    from collections import defaultdict

    # adjacency[bridge][ofport] -> set(ofport, ...)
    adjacency: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))

    def port_key(node_id: int) -> str:
        # Assuming eth0 (index 0) is the primary interface for all nodes
        return f"{node_id}:0"

    for a, b in links:
        a_key = port_key(a)
        b_key = port_key(b)
        a_info = port_map.get(a_key)
        b_info = port_map.get(b_key)
        if not a_info or not b_info:
            logger.warning(
                f"[Flows] Skipping link {a_key}<->{b_key}: missing port mapping."
            )
            continue

        a_br, a_port = a_info
        b_br, b_port = b_info

        if a_br != b_br:
            logger.warning(
                f"[Flows] Link {a_key}<->{b_key} spans different bridges ({a_br}!={b_br}); skipping."
            )
            continue
        if a_br not in managed_bridges:
            logger.warning(
                f"[Flows] Bridge {a_br} for link {a_key}<->{b_key} not managed; skipping."
            )
            continue
        # Add both directions (undirected graph)
        # Prevent self-loop connections on the same port
        if a_port != b_port:
            adjacency[a_br][a_port].add(b_port)
            adjacency[a_br][b_port].add(a_port)

    if not adjacency:
        logger.error("[Flows] Built empty adjacency map; no valid connections found.")
    return dict(adjacency)  # Convert from defaultdict for cleaner return


def _generate_arp_rules(all_ports: list[int]) -> list[str]:
    """
    Generates ARP broadcast rules to help neighbours learn MAC addresses.

    Args:
        all_ports: A list of unique OpenFlow port numbers involved in the topology.

    Returns:
        A list of OpenFlow rule strings for ARP handling. Empty if not needed.
    """
    arp_flows = []
    # Optional ARP broadcast across all participating ports
    # Only add if there are multiple ports to connect
    if len(all_ports) > 1:
        arp_actions = ",".join(f"output:{p}" for p in all_ports)
        # Higher priority than forwarding rules
        arp_flows.append(f"priority=150,arp,actions={arp_actions}")
        logger.debug(f"[Flows] Generated ARP broadcast rule for ports {all_ports}")
    return arp_flows


def _generate_forwarding_rules(neigh_map: dict[int, set[int]]) -> list[str]:
    """
    Generates the main multi-output forwarding rules based on the adjacency map.

    Args:
        neigh_map: A dictionary mapping an input port to its set of neighbour output ports
                for a specific bridge ({in_port: {out_port1, out_port2, ...}}).

    Returns:
        A list of OpenFlow rule strings for forwarding.
    """
    forwarding_flows = []
    # Per-port multi-output rules
    for in_p, outs in neigh_map.items():
        if (
            not outs
        ):  # Should not happen if adjacency is built correctly, but check anyway
            logger.debug(f"[Flows] Skipping in_port {in_p} with no neighbours.")
            continue
        # Deterministic ordering for reproducibility
        out_list = sorted(outs)
        actions = ",".join(f"output:{p}" for p in out_list)
        forwarding_flows.append(f"priority=100,in_port={in_p},actions={actions}")
        logger.debug(
            f"[Flows] Generated forwarding rule: in_port={in_p} -> outputs={out_list}"
        )
    return forwarding_flows


# --- Refactored _process_interface to use helper functions ---
def _process_interface(
    instance_name: str, instance_id: str, iface_name: str, iface_data: dict
):
    """
    Processes a single interface's data from Incus state.
    Returns (ovs_bridge, ovs_port) tuple if successful and IPv4 is found, otherwise None.
    """
    # Validate basic structure
    if not isinstance(iface_data, dict) or not iface_name or not instance_id:
        logger.warning(
            f"Invalid data for _process_interface: name={instance_name}, id={instance_id}, iface={iface_name}"
        )
        return None

    # 1. Filter for ethX interfaces (as in original logic)
    if not iface_name.startswith("eth"):
        logger.debug(f"Skipping non-eth interface {instance_name}.{iface_name}")
        return None

    # 2. Extract interface index from name (e.g., eth0 -> 0)
    idx_match = re.search(r"\d+", iface_name)
    if idx_match:
        idx = int(idx_match.group())
    else:
        logger.warning(
            f"Could not extract numerical index from interface name {instance_name}.{iface_name}"
        )
        return None

    # 3. Extract host interface name (tap device)
    host_dev = iface_data.get("host_name", "")
    if not host_dev:
        logger.warning(f"No host device found for {instance_name}.{iface_name}")
        return None
    logger.debug(f"Found host device {host_dev} for {instance_name}.{iface_name}")

    # 4. --- CRITICAL CHANGE: Check for IPv4 address ---
    ipv4_address = None
    for addr_info in iface_data.get("addresses", []):
        if addr_info.get("family") == "inet":  # Look for IPv4 ('inet')
            ipv4_address = addr_info.get("address")
            break

    if not ipv4_address:
        logger.debug(
            f"Skipping interface {iface_name} for instance {instance_name} (ID: {instance_id}) - No IPv4 address found yet."
        )
        # Return None to indicate this interface isn't ready for mapping
        return None

    logger.debug(f"Found IPv4 address {ipv4_address} for {instance_name}.{iface_name}")
    # --- End of IPv4 check ---

    # 5. Find the corresponding OVS bridge using the helper function
    try:
        ovs_bridge = ovs_port_to_br(host_dev)
        if not ovs_bridge:
            logger.warning(
                f"{host_dev} (for {instance_name}.{iface_name}) is not attached to any OVS bridge. Ignoring."
            )
            return None
    except Exception as e:  # Catch exceptions from ovs_port_to_br
        logger.error(
            f"Error finding OVS bridge for {host_dev} (for {instance_name}.{iface_name}): {e}"
        )
        return None

    # 6. Find the OpenFlow port number using the helper function
    try:
        port_num = ovs_get_interface_ofport(host_dev)
        # Validate port number (handle potential negative values like -1 for internal ports)
        # 0 is a valid port number for the local interface
        if port_num is not None and port_num >= 0:
            # Valid port number found
            pass
        else:
            logger.warning(
                f"Invalid port number ({port_num}) for {host_dev} (for {instance_name}.{iface_name})"
            )
            return None
    except Exception as e:  # Catch exceptions from ovs_get_interface_ofport
        logger.error(
            f"Error getting ofport for {host_dev} (for {instance_name}.{iface_name}): {e}"
        )
        return None

    # 7. Return successful mapping (key, value) tuple as expected by _build_port_map
    port_key = f"{instance_id}:{idx}"
    port_value = (ovs_bridge, port_num)
    logger.info(f"Mapped {port_key} -> {ovs_bridge}:{port_num} (IPv4: {ipv4_address})")
    return (port_key, port_value)

    except Exception as e:
        logger.error(f"Error building port map: {e}")

    logger.info(f"Port mapping complete. Mapped {len(port_map)} interfaces.")
    return port_map


class ServerColorFormatter(logging.Formatter):
    def format(self, record):
        level_color = {
            "DEBUG": LogColors.GRAY,
            "INFO": LogColors.CYAN,
            "WARNING": LogColors.YELLOW,
            "ERROR": LogColors.RED,
            "CRITICAL": LogColors.RED,
        }.get(record.levelname, LogColors.RESET)

        record.levelname = f"[Server-{record.levelname}]"
        record.msg = f"{level_color}{record.msg}{LogColors.RESET}"
        return super().format(record)


handler = logging.StreamHandler()
handler.setFormatter(ServerColorFormatter("%(levelname)s %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
)
logger = logging.getLogger("meco")

PID_FILE = "/tmp/meco_server.pid"  # Tracks server process
UPLOADS_DIR = "/tmp/meco_uploads"  # Stores received files
PID_LIST_FILE = "/tmp/meco_pids.txt"  # File to track active Meco PIDs
ACTIVITY_FLAG = "/tmp/meco_activity.flag"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.yaml")


def load_schema():
    with open(SCHEMA_PATH, "r") as f:
        return yaml.safe_load(f)


class MecoServiceServicer(meco_pb2_grpc.MecoServiceServicer):
    """Handles gRPC service requests."""

    def MecoCall(self, request, context):
        logger.info(f"Received MecoCall: {request.message}")
        response_msg = f"Hello from M-E-C-O! You said: {request.message}"
        logger.info(f"Sending response: {response_msg}")
        return meco_pb2.MecoResponse(message=response_msg)

    def _check_incus(self):
        try:
            subprocess.run(
                ["incus", "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def _wait_running(self, name: str, timeout: int = 60) -> bool:
        """Waits for an Incus instance to reach the 'Running' state."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                result = subprocess.run(
                    ["incus", "list", name, "--format=json"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                instances_info = json.loads(result.stdout)
                if instances_info and instances_info[0].get("status") == "Running":
                    return True
            except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
                logger.debug(f"Error checking status for {name}: {e}")
            time.sleep(1) # Poll every second
        logger.error(f"Instance {name} did not reach 'Running' state within {timeout} seconds.")
        return False
    
    def Start(self, request, context):
        """Handles Start requests with server_file_path or client_file_content."""
        try:
            file_content = None
            # 1. Load file content
            
            if request.HasField("server_file_path"):
                file_path = request.server_file_path
                logger.info(f"Start() received a file path: {file_path}")

                if not os.path.exists(file_path):
                    logger.error(f"Server file not found: {file_path}")
                    return meco_pb2.StartResponse(
                        success=False, message=f"Server file not found: {file_path}"
                    )

                with open(file_path, "r") as f:
                    file_content = f.read()
                logger.info(
                    f"Successfully read file from {file_path} with size of {len(file_content)} bytes"
                )

            elif request.HasField("client_file_content"):
                file_content = request.client_file_content
                logger.info(
                    f"Received inline file content (first 50 chars): {file_content[:50]}..."
                )

            else:
                logger.error("No valid input provided to Start()")
                return meco_pb2.StartResponse(
                    success=False, message="No valid input provided"
                )

            # 2. Validate YAML
            try:
                parsed_yaml = yaml.safe_load(file_content)
                validation_result = self._validate_yaml(parsed_yaml)
                if not validation_result["success"]:
                    logger.error(f"Validation failed: {validation_result['message']}")
                    return meco_pb2.StartResponse(
                        success=False, message=validation_result["message"]
                    )
                else:
                    if request.dry_run:
                        logger.info("YAML validation successful (dry run).")
                    else:
                        logger.info(
                            "YAML validation successful, proceeding to deployment."
                        )
            except YAMLError as e:
                logger.error(f"YAML parsing failed: {str(e)}")
                return meco_pb2.StartResponse(
                    success=False, message=f"YAML parsing failed: {str(e)}"
                )

            # 3. Handle save_as if requested
            if request.save_as:
                save_path = self._get_save_path(request.save_as)
                if os.path.exists(save_path):
                    logger.warning(f"File already exists: {save_path}")
                    return meco_pb2.StartResponse(
                        success=False, message=f"File already exists: {save_path}"
                    )
                self._save_yaml(file_content, save_path)

            # 4. Check for running emulation to prevent concurrent runs
            if os.path.exists(ACTIVITY_FLAG) and not request.dry_run:
                with open(ACTIVITY_FLAG, "r") as f:
                    running_file = f.read().strip()
                logger.warning(f"Emulation based on {running_file} is already running.")
                return meco_pb2.StartResponse(
                    success=False,
                    message=f'Emulation based on "{running_file}" is running. Please shut it down before starting a new one.',
                )

            # 5. Check for dry_run
            if not request.dry_run:
                # 6. Check if 'incus' is installed
                if not self._check_incus():
                    logger.error("'incus' not found. Please install Incus first.")
                    return meco_pb2.StartResponse(
                        success=False,
                        message="Incus not found. Please install Incus before deploying.",
                    )
                # Proceed with deployment
                try:
                    self._emulate_deployment(parsed_yaml)
                except Exception as e:
                    logger.error(f"Emulation failed: {str(e)}")
                    return meco_pb2.StartResponse(
                        success=False, message=f"Emulation failed: {str(e)}"
                    )

                # Mark activity
                with open(ACTIVITY_FLAG, "w") as f:
                    activity_file_name = request.save_as if request.save_as else "last_emulation.yaml"
                    f.write(activity_file_name)

                # Install initial flows
                try:
                    self._install_initial_flows(parsed_yaml)
                except Exception as e:
                    logger.warning(f"Failed to install initial flows: {e}")

            logger.info("Start() request processed successfully")
            return meco_pb2.StartResponse(
                success=True,
                message="Processing successful"
                + (" (dry run)" if request.dry_run else ""),
            )

        except Exception as e:
            logger.error(f"Processing failed: {str(e)}")
            return meco_pb2.StartResponse(
                success=False, message=f"Server error: {str(e)}"
            )

    def Shutdown(self, request, context):
        # Tear down all MECO instances in parallel, then clear flag
        if not os.path.exists(ACTIVITY_FLAG):
            logger.warning("No active emulation to shut down.")
            return meco_pb2.ShutdownResponse(
                success=False, message="No active emulation."
            )

        try:
            out = subprocess.run(
                ["incus", "list", "--format=json"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            insts = json.loads(out)
            # select only those we marked with user.meco=true
            names = [
                i["name"]
                for i in insts
                if i.get("config", {}).get("user.meco") == "true"
            ]

            if names:
                workers = min(len(names), 32)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures_ = {
                        pool.submit(
                            subprocess.run,
                            ["incus", "delete", nm, "--force"],
                            capture_output=True,
                            text=True,
                        ): nm
                        for nm in names
                    }
                    for fut in as_completed(futures_):
                        nm = futures_[fut]
                        try:
                            res = fut.result()
                            if res.returncode == 0:
                                logger.info(f"Deleted instance: {nm}")
                            else:
                                logger.error(
                                    f"Failed delete {nm}: {res.stderr.strip()}"
                                )
                        except Exception as e:
                            logger.error(f"Error deleting {nm}: {e}")
        except Exception as e:
            logger.error(f"Shutdown cleanup error: {e}")

        # finally clear the activity flag
        try:
            os.remove(ACTIVITY_FLAG)
            logger.info("Activity flag cleared.")
        except OSError:
            pass

        logger.info("Emulation shut down successfully.")
        return meco_pb2.ShutdownResponse(success=True, message="Emulation shut down.")

    def _get_save_path(self, filename):
        # Ensure filename ends with .yaml or .yml
        if not filename.lower().endswith((".yaml", ".yml")):
            filename += ".yaml"
        os.makedirs(UPLOADS_DIR, exist_ok=True)
        return os.path.join(UPLOADS_DIR, filename)

    def _save_yaml(self, content, save_path):
        """Saves YAML content to a file with proper error handling"""
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "w") as f:
                f.write(content)
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to save file: {str(e)}")
            return {"success": False, "message": f"Save failed: {str(e)}"}

    def _validate_yaml(self, parsed_yaml):
        try:
            schema = load_schema()
            validate(instance=parsed_yaml, schema=schema)
            return {"success": True}
        except ValidationError as e:
            # Extract simplified message
            path = " → ".join(str(p) for p in e.path) if e.path else "root"
            message = f"{path}: {e.message}"
            return {"success": False, "message": f"Validation failed: {message}"}

    def _emulate_deployment(self, data):
        type_map = data["node-types"]
        tasks = []

        for node in data["nodes"]:
            # 1) get the instance name
            name = f"{node['id']}-{node['type']}"

            # 2) write the cloud-init for this node
            cloud_yaml = self._generate_cloudinit(node)
            cloud_file = f"/tmp/{name}-cloud.yaml"
            with open(cloud_file, "w") as f:
                f.write(cloud_yaml)

            # 3) get interfaces
            iface_defs = node.get("interfaces", type_map.get(node["type"], {}).get("interfaces", []))

            # 5) schedule launch
            launcher = self._create_vm if node["type"].lower(
            ) == "groundstation" else self._create_container
            tasks.append(functools.partial(launcher, name, cloud_file, iface_defs, node_obj=node))

        max_workers = min(len(tasks), 32) or 1
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(t) for t in tasks]
            for fut in as_completed(futures):
                fut.result()  # exceptions will bubble up

    def _generate_cloudinit(self, node):
        """
        Build a #cloud-config snippet matching your sample:
        - package_update
        - packages: vim, curl, wget, net-tools
        - runcmd writes to /var/log/meco-init.log
        - ubuntu user with hashed passwd and empty SSH keys
        """
        node_name = f"{node['id']} ({node['type']})"
        # IMPORTANT: Replace with a real SHA512 hash or remove if not needed.
        # Example: mkpasswd -m sha-512 "your_password"
        # For testing, you can use a known hash for 'password' like:
        # "$6$rounds=40000$yoursaltstring$yourhashedpasswordstring"
        # Or remove the 'passwd' line if you only rely on SSH keys.
        passwd_hash = node.get(
            "passwd_hash",
            "$6$rounds=4096$mecosalt$mecoP4ssw0rdH4sh" # Replace with a real hash or remove
        )
        return f"""#cloud-config
package_update: true
packages:
  - vim
  - curl
  - wget
  - net-tools
runcmd:
  - echo "Node {node_name} started" > /var/log/meco-init.log
users:
  - name: ubuntu
    passwd: {passwd_hash}
    lock_passwd: false
    shell: /bin/bash
    ssh_authorized_keys: []
    sudo: ALL=(ALL) NOPASSWD:ALL
"""
    
    def _instance_exists(self, name):
        result = subprocess.run(
            ["incus", "list", "--format=json"], capture_output=True, text=True
        )
        return name in result.stdout

    def _create_container(self, name: str, cloud_file: str, iface_defs: List, node_obj: Dict):
        """Launch a container with one profile, N networks, and cloud-init."""
        if self._instance_exists(name):
            logger.info(f"Container {name} exists, skipping.")
            return
        
        # Launch the container with the base profile and cloud-init only
        cmd = [
            "incus", "launch", "images:ubuntu/22.04", name,
            "-p", "meco-base",
            "--network", "incus-br-int",
            "-c", "user.meco=true",
            "-c", f"user.user-data=@{cloud_file}",
            "-c", f"user.meco.type={node_obj['type']}", 
            "-c", f"user.meco.altitude={node_obj['altitude']}", 
        ]
        logger.info("Running: " + " ".join(cmd))
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if not self._wait_running(name, timeout=30):
            raise RuntimeError(f"{name} did not reach RUNNING")

        logger.info(f"{name} RUNNING")

        # Add network devices after launch
        for i in range(1, len(iface_defs)):
            device_name = f"{name}-eth{i}"
            device_args = [
                "incus", "config", "device", "add", name, device_name,
                "nic", "network=incus-br-int"
            ]
            logger.info(f"Adding network device: {' '.join(device_args)}")
            subprocess.run(device_args, check=True, stdout=subprocess.DEVNULL)

    def _create_vm(self, name: str, cloud_file: str, iface_defs: List, node_obj: Dict):
        """Launch a VM with one profile, N networks, and cloud-init."""
        if self._instance_exists(name):
            logger.info(f"VM {name} exists, skipping.")
            return
        
        # Launch the VM with the base profile and cloud-init only
        cmd = [
            "incus", "launch", "images:ubuntu/noble", name,
            "--vm",
            "-p", "meco-vm",
            "-c", "user.meco=true",
            "-c", f"user.user-data=@{cloud_file}",
            "-c", f"user.meco.type={node_obj['type']}", 
            "-c", f"user.meco.altitude={node_obj['altitude']}", 
        ]
        # Add network devices after launch
        for iface_def in iface_defs:
            cmd.extend(["--network", iface_def.get("network", "incus-br-int")])

        logger.info("Running: " + " ".join(cmd))

        # Execute the single, complete launch command
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)

        if not self._wait_running(name, timeout=5):
            raise RuntimeError(f"{name} VM did not reach RUNNING")

        logger.info(f"{name} RUNNING")

    def _install_initial_flows(self, topo):
        """
        Installs flows for every link in the topology.
        """
        port_map = _build_port_map()
        if not port_map:
            logger.error("No ports mapped; cannot install flows.")
            return

        # 2) Collect all (src, dst, delay, loss, bandwidth)
        links = []
        for section in ("visibility-constellation", "visibility-ground"):
            for snap in topo.get(section, []):
                for conn in snap.get("connection", []):
                    links.append((
                        conn["source"], conn["destination"],
                        # conn.get("delay", 0),
                        # conn.get("loss", 0),
                        # conn.get("bandwidth", 0)
                    ))

        # Group flows by bridge
        bridge_flows: Dict[str, List[str]] = {
            "incus-br-int": [], "incus-br-tun": []}

        # 3) Build flow rules and TC commands
        of_rules = []
        for src_id, dst_id in links:
            src_key = f"{src_id}:0"
            dst_key = f"{dst_id}:0"

            src_info = port_map.get(src_key)
            dst_info = port_map.get(dst_key)

            if src_info is None or dst_info is None:
                logger.error(f"Missing port_map entries for {
                             src_key} or {dst_key}. Skipping flow.")
                continue

            src_bridge, p_src = src_info
            dst_bridge, p_dst = dst_info

            if src_bridge != dst_bridge:
                logger.error(f"Link between {src_key} and {dst_key} spans different bridges ({
                             src_bridge} vs {dst_bridge}). This is not supported. Skipping flow.")
                continue

            of_rules += [
                f"priority=100,in_port={p_src},actions=output:{p_dst}",
                f"priority=100,in_port={p_dst},actions=output:{p_src}"
            ]
            bridge_flows[src_bridge].extend(of_rules)

            """
            # TC on host devices
            dev_src = port_map.host_dev[src_key]
            dev_dst = port_map.host_dev[dst_key]
            apply_tc(dev_src, delay, loss, bw)
            apply_tc(dev_dst, delay, loss, bw)
            """
        # 4) Default drop
        for br, flows in bridge_flows.items():
            if flows:
                flows.append("priority=0,actions=drop")
                _apply_openflow_rules(br, flows)
            else:
                logger.info(f"No flows to apply for bridge {br}.")


def serve_forever():
    """Starts the gRPC server and runs indefinitely."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    meco_pb2_grpc.add_MecoServiceServicer_to_server(MecoServiceServicer(), server)
    server.add_insecure_port("[::]:50051")
    server.start()
    logger.info("Meco gRPC server started on port 50051.")

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        logger.warning("Shutting down server...")
        server.stop(0)


def is_running(pid):
    """Check if the given PID is still alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_status():
    """Checks and prints the server status based on recorded PIDs."""
    if not os.path.exists(PID_LIST_FILE):
        logger.info("Meco server is not running (no PID list file found).")
        return

    try:
        with open(PID_LIST_FILE, "r") as f:
            pids = [line.strip() for line in f if line.strip()]
        running_pids = []
        for pid in pids:
            try:
                pid_int = int(pid)
                if is_running(pid_int):
                    running_pids.append(pid_int)
            except Exception as e:
                logger.error(f"Error checking PID {pid}: {e}")
        if running_pids:
            logger.info(
                f"Meco server is running with the following process: {running_pids}"
            )
            try:
                with open(ACTIVITY_FLAG, "r") as f:
                    running_file = f.read().strip()
                logger.info(f"Emulation based on {running_file} is running")
                result = subprocess.run(
                    ["incus", "list", "--format=json"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                names = [c["name"] for c in json.loads(result.stdout)]
                logger.info(f"Active containers: {names}")

            except FileNotFoundError:
                logger.info("Server is idle (no deployments running)")
        else:
            logger.info("Meco server is not running.")

    except Exception as e:
        logger.error(f"Error reading PID list file: {e}")


def server_on():
    """Turns the server ON (daemonizes it), creates the bridges, and tracks its correct PID."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            old_pid = int(f.read().strip())
        if is_running(old_pid):
            logger.warning(f"Meco server is already ON (PID: {old_pid}).")
            sys.exit(0)
        else:
            os.remove(PID_FILE)
    # -----------------------------------------------------------------
    # (A) Make sure the host-side bridges/networks/OVS switches exist
    #     BEFORE we fork into the background.  One call, at boot.
    # -----------------------------------------------------------------
    try:
        _setup_bridges()
        logger.info("Bridges initialised; server ready for deployments.")
    except Exception as e:
        logger.error(f"Failed to set up bridges: {e}")
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        sys.exit(0)  # Exit parent process to ensure daemonization

    os.setsid()  # Create a new session
    pid2 = os.fork()
    if pid2 > 0:
        sys.exit(0)  # Exit second parent process

    # Capture the actual PID after the final fork
    actual_pid = os.getpid()

    # Save the correct PID
    with open(PID_FILE, "w") as f:
        f.write(str(actual_pid) + "\n")

    with open(PID_LIST_FILE, "a") as f:
        f.write(f"{actual_pid}\n")

    logger.info(f"Meco server started in background (PID: {actual_pid}).")

    serve_forever()


def server_off(force=False):
    """Turns the server OFF by stopping only the recorded PIDs in the list."""
    logger.info("Stopping Meco server processes...")

    # If an emulation is active, require --force or bail out
    if os.path.exists(ACTIVITY_FLAG):
        if not force:
            logger.warning(
                "Active emulation detected. Please run 'client shutdown' first "
                "or retry with 'meco off --force' to force teardown."
            )
            return
        logger.info("Force flag set: tearing down active emulation first.")
        # teardown logic (delete MECO instances)...
        try:
            out = subprocess.run(
                ["incus", "list", "--format=json"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            # filter only MECO instances
            names = [
                inst["name"]
                for inst in json.loads(out)
                if inst.get("config", {}).get("user.meco") == "true"
            ]
            if names:
                max_workers = min(len(names), 32) or 1
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {
                        pool.submit(
                            subprocess.run,
                            ["incus", "delete", name, "--force"],
                            capture_output=True,
                            text=True,
                            check=True,
                        ): name
                        for name in names
                    }
                    for fut in as_completed(futures):
                        nm = futures[fut]
                        try:
                            fut.result()
                            logger.info(f"Deleted instance: {nm}")
                        except subprocess.CalledProcessError as ex:
                            logger.error(f"Failed deleting {nm}: {ex.stderr.strip()}")
                        except Exception as ex:
                            logger.error(f"Failed deleting {nm}: {ex}")
            else:
                logger.info("No active emulation instances found to delete.")

        except subprocess.CalledProcessError as e:
            logger.error(f"Error listing Incus instances: {e.stderr.strip()}")
        except Exception as e:
            logger.error(f"Error cleaning up emulation instances: {e}")

    if not os.path.exists(PID_LIST_FILE):
        logger.info("No recorded Meco server PIDs found.")
    else:
        killed_pids: List[int] = []
        pids: List[int] = []

        try:
            with open(PID_LIST_FILE, "r") as f:
                pids = [int(line.strip()) for line in f.readlines() if line.strip()]
        except FileNotFoundError:
            logger.info("No recorded Meco server PIDs found.")
        except (ValueError, Exception) as e:
            logger.error(f"Error reading PID list file: {e}")
            pids = []  # Clear PIDs to prevent a bad loop

        if pids:
            for pid in pids:
                if psutil.pid_exists(pid):
                    logger.info(f"Killing Meco server process (PID: {pid})")

                    # 1. Send SIGTERM for a graceful shutdown
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError as e:
                        logger.error(f"Error sending SIGTERM to PID {pid}: {e}")
                    
                    # 2. Wait for the process to terminate
                    timeout = 5
                    for _ in range(timeout):
                        if not psutil.pid_exists(pid):
                            break
                        time.sleep(1)
                    
                    # 3. If still running, send SIGKILL to force termination
                    if psutil.pid_exists(pid):
                        logger.warning(
                            f"Process (PID: {pid}) did not terminate. Sending SIGKILL."
                        )
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError as e:
                            logger.error(f"Error sending SIGKILL to PID {pid}: {e}")
                    
                    if not psutil.pid_exists(pid):
                        killed_pids.append(pid)
        else:
            logger.info("PID file was empty or contained invalid data.")

        # Section 3: Rebuild the PID file with any processes that survived.
        remaining_pids = [str(pid) for pid in pids if psutil.pid_exists(pid)]

        if remaining_pids:
            with open(PID_LIST_FILE, "w") as f:
                f.write("\n".join(remaining_pids) + "\n")
            logger.info(f"PID list file updated. {len(remaining_pids)} processes remain.")
        else:
            try:
                os.remove(PID_LIST_FILE)
                logger.info("All Meco server processes were stopped. PID list file removed.")
            except FileNotFoundError:
                pass # Already gone, no need to log an error

    # Section 4: Final cleanup steps, regardless of PID status.
    # This ensures profiles and bridges are always cleaned up at the end.
    try:
        if os.path.exists(ACTIVITY_FLAG):
            os.remove(ACTIVITY_FLAG)
            logger.info("Activity flag cleared.")
    except FileNotFoundError:
        pass



    try:
        subprocess.run(["incus", "profile", "delete", "meco-base"], check=True, capture_output=True, text=True)
        subprocess.run(["incus", "profile", "delete", "meco-vm"], check=True, capture_output=True, text=True)
        logger.info("Deleted Incus profiles: meco-base, meco-vm")
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to delete Incus profiles: {e.stderr.strip()}")
    except Exception as e:
        logger.warning(f"Failed to delete Incus profiles: {e}")

    try:
        _teardown_bridges()
        logger.info("All bridges torn down.")
    except Exception as e:
        logger.warning(f"Failed to clean up bridges: {e}")


def start_resource_descriptor(filename=None, file_content=None, save_as=None):
    """Sends either a file_path or file_content to the gRPC server, with optional save_as."""
    channel = grpc.insecure_channel("localhost:50051")
    stub = meco_pb2_grpc.MecoServiceStub(channel)

    if filename:
        if not os.path.exists(filename):
            logger.error(f'Error: File "{filename}" does not exist.')
            sys.exit(1)
        request = meco_pb2.ResourceDescriptor(file_path=filename)
    elif file_content:
        request = meco_pb2.ResourceDescriptor(
            file_content=file_content, save_as=save_as
        )
    else:
        logger.error("Error: No filename or file content provided.")
        sys.exit(1)

    response = stub.Start(request)

    if response.success:
        logger.info(f"Successfully processed resource: {response.message}")
    else:
        logger.error(f"Failed to process resource: {response.message}")


def create_parser():
    """Creates the argument parser."""
    parser = argparse.ArgumentParser(
        description="Emulates a LEO Mega Constellation", prog="meco"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Commands")

    subparsers.add_parser("on", help="Turn the Meco server ON")
    off_parser = subparsers.add_parser("off", help="Turn the Meco server OFF")
    off_parser.add_argument(
        "--force",
        action="store_true",
        help="Force teardown of active emulation before stopping the server",
    )
    subparsers.add_parser("status", help="Show server status")

    return parser


def handle_command(args, parser, parser_dict):
    """Handles CLI commands."""
    command = args.command
    if command in parser_dict:
        parser_dict[command](args)
    else:
        parser.print_help()
        sys.exit(1)


def signal_handler(sig, frame):
    logger.info("You pressed Ctrl+C!")
    try:
        if os.path.exists(PID_FILE):  # Remove the PID file if it exists
            os.remove(PID_FILE)
    except Exception as e:
        logger.error(f"Error removing PID file: {e}")
    sys.exit(0)  # Exit cleanly


def main():
    """Main function to parse arguments and execute commands."""
    parser = create_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    parser_dict = {
        "on": lambda _: server_on(),
        "off": lambda _: server_off(force=args.force),
        "status": lambda _: server_status(),
    }

    handle_command(args, parser, parser_dict)


if __name__ == "__main__":
    main()
