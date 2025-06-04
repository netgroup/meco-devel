#!/usr/bin/env python

import grpc
import meco_pb2
import meco_pb2_grpc
import yaml
import argparse
import os
import logging
import subprocess
import sys
import json
from google.protobuf.empty_pb2 import Empty


# === Colored Log Setup with [Client-LEVEL] Format ===
class LogColors:
    RESET = "\033[0m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

class ClientColorFormatter(logging.Formatter):
    def format(self, record):
        level_color = {
            "DEBUG": LogColors.GRAY,
            "INFO": LogColors.GREEN,
            "WARNING": LogColors.YELLOW,
            "ERROR": LogColors.RED,
            "CRITICAL": LogColors.RED,
        }.get(record.levelname, LogColors.RESET)

        record.levelname = f"[Client-{record.levelname}]"
        record.msg = f"{level_color}{record.msg}{LogColors.RESET}"
        return super().format(record)

handler = logging.StreamHandler()
handler.setFormatter(ClientColorFormatter("%(levelname)s %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[handler],
)

logger = logging.getLogger("meco-client")


def open_editor_for_content(
    filename="edited_content.yaml", keep_file=False, cache_dir_base="/tmp/meco_uploads"
):
    """Opens nano for input, saves content in a cache file, and returns the content."""
    cache_dir = os.path.join(cache_dir_base, "cache")
    os.makedirs(cache_dir, exist_ok=True)  # Ensure directory exists
    file_path = os.path.join(cache_dir, filename)

    # Create file if it doesn't exist
    with open(file_path, "w") as f:
        pass  # Just create an empty file

    # Open Nano editor
    subprocess.call([os.environ.get("EDITOR", "nano"), file_path])

    # Read content after editing
    with open(file_path, "r") as f:
        content = f.read().strip()

    if not content:
        logger.warning("No content entered. Aborting operation.")
        return None

    if not keep_file:
        os.remove(file_path)  # Remove only if not meant to be saved

    return content


def get_running_containers():
    """Get list of running containers from incus in JSON format."""
    try:
        result = subprocess.run(
            ["incus", "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error(f"Error listing containers: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing incus output: {e}")
        return []

def is_meco_container(name):
    """Check if container name matches MECO container pattern (id-type)."""
    try:
        # Split name into id and type parts
        parts = name.split('-')
        if len(parts) != 2:
            return False
            
        # Check if id part is a number and type is a valid node type
        node_id, node_type = parts
        if not node_id.isdigit():
            return False
            
        # List of valid node types from our schema
        valid_types = ['SatelliteBig', 'SatelliteSmall', 'Terminal', 'Gateway', 'Router']
        return node_type in valid_types
    except:
        return False

def delete_container(name):
    """Delete a container with proper error handling."""
    try:
        logger.info(f"Deleting container: {name}")
        subprocess.run(
            ["incus", "delete", name, "--force"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to delete container {name}: {e}")
        return False


def perform_rpc_call(command, filename=None, localfile=None, saveas=None, dryrun=False, content=None):
    """Tests the Meco gRPC service with file reference or inline content."""
    try:
        channel = grpc.insecure_channel("localhost:50051")
        stub = meco_pb2_grpc.MecoServiceStub(channel)

        if command == "start":
            # Handle localfile
            if localfile:
                with open(localfile, "r") as f:
                    content = f.read()
                logger.info(f"Read content from local file: {localfile}")

            keep_file = bool(saveas or dryrun)
            if content == "":
                logger.info("Opening Nano editor for content input...")
                content = open_editor_for_content("edited_content.yaml", keep_file)
                if not content:
                    logger.error("No valid input provided.")
                    return

            if filename:
                logger.info(f"Using server-side file: {filename}")
                request = meco_pb2.ResourceDescriptor(
                    server_file_path=filename, save_as=saveas, dry_run=dryrun
                )
            elif content:
                logger.info(f"Sending YAML content (first 50 chars): {content[:50]}...")
                request = meco_pb2.ResourceDescriptor(
                    client_file_content=content, save_as=saveas, dry_run=dryrun
                )
            else:
                logger.error("No valid input provided.")
                return

            response = stub.Start(request)
            
            msg = response.message.lower()
            
            if "incus not found" in msg or "install incus" in msg:
                logger.error("Deployment failed: Incus is not installed on the server. Please install Incus (e.g. `sudo apt install incus`) and try again.")
            elif "running" in msg:
                # e.g. “Emulation based on X is running…”
                logger.warning(msg)
            elif "processing successful" in msg:
                if "dry run" in msg:
                    logger.info("Dry run passed: YAML is valid.")
                else:
                    logger.info("Deployment started successfully on Incus.")
            else:
                # Any other error message from the server
                logger.error(f"Deployment failed: {msg}")
                
        elif command == "shutdown":
            response = stub.Shutdown(Empty())
            if response.success:
                logger.info(response.message)
                try:
                    # Get list of running containers in JSON format
                    containers = get_running_containers()
                    
                    # Delete containers that match our naming pattern (id-type)
                    for container in containers:
                        name = container.get("name", "")
                        if is_meco_container(name):
                            delete_container(name)
                except Exception as e:
                    logger.error(f"Error during container cleanup: {e}")
            else:
                logger.warning(response.message)

    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            logger.error("Meco server is not running. Start it with 'meco on' first.")
        else:
            logger.error(f"gRPC Error: {e.details()}")
        sys.exit(1)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meco YAML Client")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # START command
    start_parser = subparsers.add_parser("start", help="Start processing the input topology and deploy it")
    group = start_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--filename", help="Server-side file path (existing on server)")
    group.add_argument("--filepath", dest="localfile", help="Read local file and send as content")
    group.add_argument("--content", nargs="?", const="", help="Provide file content directly as a string or open an editor if empty")
    start_parser.add_argument("--saveas", help="Specify remote filename to save the file as")
    start_parser.add_argument("--dryrun", action="store_true", help="Validate without execution")

    # SHUTDOWN command
    shutdown_parser = subparsers.add_parser("shutdown", help="Shut down running deployment")


    args = parser.parse_args()
    if args.command == "shutdown":
        perform_rpc_call("shutdown")
    else:
        perform_rpc_call(
            args.command,
            filename=args.filename,
            localfile=args.localfile,
            saveas=args.saveas,
            dryrun=args.dryrun,
            content=args.content,
        )
