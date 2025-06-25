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
import yaml
import uuid
import subprocess
from yaml import YAMLError
import psutil  # For process checking
from jsonschema import validate, ValidationError
import json

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
            subprocess.run(["incus", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def Start(self, request, context):
        """Handles Start requests with server_file_path or client_file_content."""
        try:
            file_content = None
            save_filename = None
            # 1. Load file content
            if request.HasField("server_file_path"):
                file_path = request.server_file_path
                save_filename = os.path.basename(file_path)
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
                save_filename = os.path.basename(save_path)
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
                    return meco_pb2.StartResponse(success=False, message=validation_result["message"])
                else:
                    if request.dry_run:
                        logger.info("YAML validation successful (dry run).")
                    else:
                        logger.info("YAML validation successful, proceeding to deployment.")
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
                    message=f"Emulation based on \"{running_file}\" is running. Please shut it down before starting a new one."
                )

            # 5. Check for dry_run
            if not request.dry_run:
                # 6. Check if 'incus' is installed
                if not self._check_incus():
                    logger.error("'incus' not found. Please install Incus first.")
                    return meco_pb2.StartResponse(
                        success=False,
                        message="Incus not found. Please install Incus before deploying."
                    )
                try:
                    self._emulate_deployment(parsed_yaml)
                    with open(ACTIVITY_FLAG, "w") as f:
                        f.write(save_filename)
                except Exception as e:
                    logger.error(f"Emulation failed: {str(e)}")
                    return meco_pb2.StartResponse(
                        success=False, message=f"Emulation failed: {str(e)}"
                    )

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
        if os.path.exists(ACTIVITY_FLAG):
            os.remove(ACTIVITY_FLAG)
            logger.info("Emulation shut down successfully.")
            return meco_pb2.ShutdownResponse(success=True, message="Emulation shut down.")
        else:
            logger.warning("No emulation was running.")
            return meco_pb2.ShutdownResponse(success=False, message="No active emulation.")


    def _get_save_path(self, filename):
        # Ensure filename ends with .yaml or .yml
        if not filename.lower().endswith((".yaml", ".yml")):
            filename += ".yaml"
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
        for node in data.get("nodes", []):
            name, config_yaml = self._generate_incus_config(node)
            ntype = node.get("type", "").lower()
            # app containers: all satellites & terminals
            if ntype == "terminal" or ntype.startswith("satellite"):
                self._create_container(name, config_yaml)
            # system containers: routers & gateways
            elif ntype in ("router", "gateway"):
                self._create_container(name, config_yaml)
            # full VMs: network orchestrator
            elif ntype == "networkorchestrator":
                self._create_vm(name, config_yaml)
            # fallback
            else:
                self._create_container(name, config_yaml)

    def _generate_incus_config(self, node):
        instance_uuid = str(uuid.uuid4())
        instance_name = f"{node['id']}-{node['type']}"

        config = {
            "architecture": "x86_64",
            "config": {
                "image.architecture": "amd64",
                "image.os": "Ubuntu",
                "image.release": "focal",
                "volatile.cloud-init.instance-id": instance_uuid,
                "volatile.uuid": instance_uuid
            },
            "devices": {
                "eth0": {
                    "name": "eth0",
                    "network": "incusbr0",
                    "type": "nic"
                },
                "root": {
                    "path": "/",
                    "pool": "default",
                    "type": "disk"
                }
            },
            "ephemeral": False,
            "profiles": ["default"],
            "stateful": False,
            "description": f"Container for {instance_name}"
        }

        return instance_name, yaml.dump(config, default_flow_style=False)

    def _instance_exists(self, name):
        result = subprocess.run(["incus", "list", "--format=json"], capture_output=True, text=True)
        return name in result.stdout

    def _create_container(self, name, config_yaml):
        if self._instance_exists(name):
            logger.info(f"Container {name} already exists. Skipping creation.")
            return
        logger.info(f"Creating container: {name}")
        subprocess.run([
             "incus", "launch", "images:ubuntu/20.04", name,
             "--storage", "default",
             "--config", f"user.user-data=@/tmp/{name}.yaml",
             "--config", "user.meco=true"
         ], check=True)
        logger.info(f"Container {name} is ready.")

    def _create_vm(self, name, config_yaml):
        if self._instance_exists(name):
            logger.info(f"VM {name} already exists. Skipping creation.")
            return
        logger.info(f"Creating VM: {name}")
        subprocess.run([
            "incus", "launch", "--vm", "images:ubuntu/20.04", name,
            "--storage", "default",
            "--config", f"user.user-data=@/tmp/{name}.yaml",
            "--config", "user.meco=true"
        ], check=True)
        logger.info(f"VM {name} is ready.")
        
    def Shutdown(self, request, context):
        if os.path.exists(ACTIVITY_FLAG):
            os.remove(ACTIVITY_FLAG)
            logger.info("Emulation shut down successfully.")
            return meco_pb2.ShutdownResponse(success=True, message="Emulation shut down.")
        else:
            logger.warning("No emulation was running.")
            return meco_pb2.ShutdownResponse(success=False, message="No active emulation.")


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
                    capture_output=True, text=True, check=True
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
    """Turns the server ON (daemonizes it) and tracks its correct PID."""
    if os.path.exists(PID_FILE):
        with open(PID_FILE, "r") as f:
            old_pid = int(f.read().strip())
        if is_running(old_pid):
            logger.warning(f"Meco server is already ON (PID: {old_pid}).")
            sys.exit(0)
        else:
            os.remove(PID_FILE)

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
                capture_output=True, text=True, check=True
            ).stdout
            for inst in json.loads(out):
                name = inst.get("name", "")
                parts = name.split("-", 1)
                if len(parts) == 2 and parts[0].isdigit():
                    logger.info(f"Deleting instance: {name}")
                    subprocess.run(["incus", "delete", name, "--force"], check=True)
        except Exception as e:
            logger.error(f"Error cleaning up emulation instances: {e}")
        finally:
            os.remove(ACTIVITY_FLAG)
            logger.info("Activity flag cleared.")

    if not os.path.exists(PID_LIST_FILE):
        logger.info("No recorded Meco server PIDs found.")
        return

    server_process_found = False
    killed_pids = []

    try:
        with open(PID_LIST_FILE, "r") as f:
            pids = [int(line.strip()) for line in f.readlines()]
    except FileNotFoundError:
        logger.info("No recorded Meco server PIDs found.")
        return
    except ValueError:
        logger.error("Error reading PID list file: Invalid PID format in file.")
        return
    except Exception as e:
        logger.error(f"Error reading PID list file: {e}")
        return

    for pid in pids:
        if psutil.pid_exists(pid):
            server_process_found = True
            logger.info(f"Killing Meco server process (PID: {pid})")

            try:
                # 1. SIGTERM (Graceful Shutdown)
                os.kill(pid, signal.SIGTERM)
            except OSError as e:
                logger.exception(f"Error sending SIGTERM: {e}")

            # 2. Wait for process to terminate
            timeout = 5
            process_still_running = True
            for _ in range(timeout):
                if not psutil.pid_exists(pid):
                    process_still_running = False
                    break
                time.sleep(1)

            # 3. Send SIGKILL if process is still running
            if process_still_running:
                logger.warning(
                    f"Process (PID: {pid}) did not terminate. Sending SIGKILL."
                )
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError as e:
                    logger.exception(f"Error sending SIGKILL: {e}")

            killed_pids.append(pid)

    # Rebuild PID list with remaining active PIDs
    remaining_pids = []
    for pid in pids:
        if psutil.pid_exists(pid):
            remaining_pids.append(str(pid))

    # Write remaining PIDs to file or remove file if empty
    if remaining_pids:
        with open(PID_LIST_FILE, "w") as f:
            f.write("\n".join(remaining_pids) + "\n")
    else:
        try:
            os.remove(PID_LIST_FILE)
            logger.info("PID list file removed.")
        except FileNotFoundError:
            pass

    if server_process_found:
        try:
            os.remove(PID_FILE)
            if os.path.exists(ACTIVITY_FLAG):
                os.remove(ACTIVITY_FLAG)
                logger.info("Activity flag cleared.")
            logger.info("PID file removed.")
        except FileNotFoundError:
            logger.info("No PID file found to remove.")
    else:
        logger.info("No active Meco server processes found.")


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
        help="Force teardown of active emulation before stopping the server"
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
