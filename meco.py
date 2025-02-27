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
from yaml import YAMLError
import psutil  # For process checking

import meco_pb2
import meco_pb2_grpc

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("meco")

PID_FILE = "/tmp/meco_server.pid"  # Tracks server process
UPLOADS_DIR = "/tmp/meco_uploads"  # Stores received files
PID_LIST_FILE = "/tmp/meco_pids.txt"  # File to track active Meco PIDs


class MecoServiceServicer(meco_pb2_grpc.MecoServiceServicer):
    """Handles gRPC service requests."""

    def MecoCall(self, request, context):
        logger.info(f"Received MecoCall: {request.message}")
        response_msg = f"Hello from M-E-C-O! You said: {request.message}"
        logger.info(f"Sending response: {response_msg}")
        return meco_pb2.MecoResponse(message=response_msg)

    def Start(self, request, context):
        """Handles Start requests with server_file_path or client_file_content."""
        try:
            file_content = None
            # Get content from server file or client input
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

            # Validate YAML
            validation_result = self._validate_yaml(file_content)
            if not validation_result["success"]:
                logger.error(f"YAML validation failed: {validation_result['message']}")
                return meco_pb2.StartResponse(**validation_result)

            # Handle save_as if requested
            if request.save_as:
                save_path = self._get_save_path(request.save_as)
                if os.path.exists(save_path):
                    logger.warning(f"File already exists: {save_path}")
                    return meco_pb2.StartResponse(
                        success=False, message=f"File already exists: {save_path}"
                    )

                logger.info(f"Saving file to {save_path}")
                self._save_yaml(file_content, save_path)

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

    def _get_save_path(self, filename):
        return os.path.join(UPLOADS_DIR, f"{filename}.yaml")

    def _save_yaml(self, content, save_path):
        """Saves YAML content to a file."""
        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "w") as f:
                f.write(content)
            logger.info(f"File saved successfully at {save_path}")
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to save file: {str(e)}")
            return {"success": False, "message": f"Save failed: {str(e)}"}

    def _validate_yaml(self, content):
        """Validates YAML syntax."""
        try:
            parsed_yaml = yaml.safe_load(content)
            if not isinstance(parsed_yaml, dict):
                return {
                    "success": False,
                    "message": "Invalid YAML: Root must be a mapping (dictionary)",
                }
            return {"success": True}
        except yaml.YAMLError as e:
            return {"success": False, "message": f"Invalid YAML: {str(e)}"}


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
                f"Meco server is running with the following process(es): {running_pids}"
            )
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


def server_off():
    """Turns the server OFF by stopping only the recorded PIDs in the list."""
    logger.info("Stopping Meco server processes...")

    if not os.path.exists(PID_LIST_FILE):
        logger.info("No recorded Meco server PIDs found.")
        return

    server_process_found = False
    killed_pids = []

    try:
        with open(PID_LIST_FILE, "r") as f:
            pids = [int(line.strip()) for line in f.readlines()]

        for pid in pids:
            if psutil.pid_exists(pid):
                server_process_found = True
                logger.info(f"Killing Meco server process (PID: {pid})")

                # 1️⃣ SIGTERM (Graceful Shutdown)
                os.kill(pid, signal.SIGTERM)

                # 2️⃣ Wait for process to terminate
                timeout = 5
                for _ in range(timeout):
                    if not psutil.pid_exists(pid):
                        break
                    time.sleep(1)
                else:
                    # 3️⃣ SIGKILL (Forceful Shutdown if still running)
                    logger.warning(
                        f"Process (PID: {pid}) did not terminate. Sending SIGKILL."
                    )
                    os.kill(pid, signal.SIGKILL)

                killed_pids.append(pid)

        # Remove stopped PIDs from the file
        with open(PID_LIST_FILE, "w") as f:
            remaining_pids = [str(pid) for pid in pids if pid not in killed_pids]
            f.write("\n".join(remaining_pids) + "\n")

    except Exception as e:
        logger.exception(f"Error while stopping the server: {e}")

    if server_process_found:
        try:
            os.remove(PID_FILE)
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
    subparsers.add_parser("off", help="Turn the Meco server OFF")
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
        "off": lambda _: server_off(),
        "status": lambda _: server_status(),
    }

    handle_command(args, parser, parser_dict)

    if args.command == "on":  # Only start server if the command is 'on'
        signal.signal(signal.SIGINT, signal_handler)  # Register the signal handler

        try:
            with open(PID_FILE, "w") as f:
                f.write(str(os.getpid()))

            server_on()  # Start the gRPC server

        finally:
            try:
                os.remove(PID_FILE)  # Remove the PID file when the server is stopped
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
