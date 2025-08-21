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


    """
    Tear down and remove Incus and OVS bridges.
    - Deletes all OpenFlow rules.
    - Removes all non-bridge ports from each OVS bridge.
    - Deletes Incus network objects for each bridge.
    """
    for br in ("incus-br-int", "incus-br-tun"):
        # Remove all OpenFlow rules from the bridge.
        subprocess.run(["sudo", "ovs-ofctl", "del-flows", br], check=False)
         # List all ports on the bridge.
        ports = subprocess.run(["sudo", "ovs-vsctl", "list-ports", br],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        for p in ports:
            if p == br:
                continue    # Skip the bridge itself.
            # Remove the port from OVS and delete the network device.
            subprocess.run(["sudo", "ovs-vsctl", "--if-exists",
                           "del-port", br, p], check=False)
            subprocess.run(["sudo", "ip", "link", "del", p], check=False)

        # Remove the Incus network object.
        subprocess.run(["sudo", "incus", "network", "delete", br],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info(f"Deleted Incus network object: {br}")


def _apply_openflow_rules(bridge: str, flows: List[str]):
    """
    Apply a set of OpenFlow rules to a given bridge.
    - Clears any existing flows before applying new ones.
    - Logs each flow addition.
    - Raises/logs errors as needed.
    """
    logger.info(f"Applying OpenFlow rules to bridge {bridge}...")
    try:
        # Delete all existing OpenFlow rules.
        subprocess.run(["sudo", "ovs-ofctl", "del-flows", bridge], check=False)
        logger.info(f"Cleared existing flows from bridge {bridge}.")

        # Apply each flow rule.
        for rule in flows:
            subprocess.run(["sudo", "ovs-ofctl", "add-flow",
                           bridge, rule], check=True)
            logger.info(f"OF rule added: {rule}")
        logger.info("All flows installed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to apply OpenFlow rules: {e.stderr.strip()}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while applying flows: {e}")
        raise


def _build_port_map():
    """
    Build a mapping from instance interface (instance_id:ethX) to OVS bridge and OpenFlow port number.
    - Iterates over all running MECO instances.
    - For each eth* interface, finds the OVS bridge and ofport.
    - Returns a dictionary: { "<instance_id>:<iface_idx>": (bridge, ofport) }
    """
    port_map = {}
    logger.info("Starting port mapping process...")

    try:
        # Get all Incus instances as JSON.
        out = subprocess.run(
            ["incus", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        instances = json.loads(out)

        # Filter to only running MECO instances.
        meco_instances = [
            inst for inst in instances
            if inst.get("config", {}).get("user.meco") == "true"
            and inst.get("status") == "Running"
        ]

        if not meco_instances:
            logger.warning("No running MECO instances found.")
            return port_map

        for inst in meco_instances:
            name = inst["name"]
            instance_id = name.split('-')[0]    # Assumes convention: <id>-...

            try:
                # Get runtime state for the instance.
                state_out = subprocess.run(
                    ["incus", "query", f"/1.0/instances/{name}/state"],
                    capture_output=True, text=True, check=True
                ).stdout

                state = json.loads(state_out)
                network_info = state.get("network", {})

                for iface_name, iface_data in network_info.items():
                    # Only consider ethernet interfaces (eth*)
                    if not iface_name.startswith("eth"):
                        continue

                    try:
                        idx = int(iface_name.replace("eth", ""))
                        host_dev = iface_data.get("host_name", "")
                        if not host_dev:
                            logger.warning(f"No host device found for {
                                           name}.{iface_name}")
                            continue

                        logger.info(f"Found host device {
                                    host_dev} for {name}.{iface_name}")

                        # Get the OVS bridge this device is attached to.
                        check_result = subprocess.run(
                            ["sudo", "ovs-vsctl", "port-to-br", host_dev],
                            capture_output=True, text=True, check=False
                        )

                        ovs_bridge = check_result.stdout.strip()
                        if not ovs_bridge:
                            logger.warning(
                                f"{host_dev} is not attached to any OVS bridge. Ignoring.")
                            continue

                        # Get OpenFlow port number for the host device.
                        port_num = subprocess.run(
                            ["sudo", "ovs-vsctl", "get",
                                "Interface", host_dev, "ofport"],
                            capture_output=True, text=True, check=True
                        ).stdout.strip()

                        if port_num.isdigit():
                            port_map[f"{instance_id}:{idx}"] = (
                                ovs_bridge, int(port_num))
                            logger.info(f"Mapped {instance_id}:{
                                        idx} → {ovs_bridge}:{port_num}")
                        else:
                            logger.warning(f"Invalid port number for {
                                           host_dev}: {port_num}")

                    except Exception as e:
                        logger.error(f"Error processing interface {
                                     name}.{iface_name}: {e}")
                        continue

            except Exception as e:
                logger.error(f"Error processing instance {name}: {e}")
                continue

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
