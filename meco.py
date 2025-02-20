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
import json
import psutil  # For process checking

import meco_pb2
import meco_pb2_grpc

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)

logger = logging.getLogger("meco")


PID_FILE = "/tmp/meco_server.pid"   # PID file for tracking the daemonized server
UPLOADS_DIR = "/tmp/meco_uploads"  # Directory for storing received files


class MecoServiceServicer(meco_pb2_grpc.MecoServiceServicer):
    """
    Implements:
    1) MecoCall - a simple echo RPC.
    2) Start - supports both file_path and file_content with optional saving.
    """

    def MecoCall(self, request, context):
        logger.info(f"MecoCall received: {request.message}")
        response_msg = f"Hello from M-E-C-O! You said: {request.message}"
        return meco_pb2.MecoResponse(message=response_msg)

    def validate_and_respond(self, content):
        """Validates whether the input is a valid JSON."""
        try:
            json.loads(content)
            return meco_pb2.StartResponse(success=True, message="Content syntax is valid.")
        except json.JSONDecodeError:
            return meco_pb2.StartResponse(success=False, message="Error: Invalid JSON syntax.")

    def save_as_yaml(self, json_content, save_as):
        """Saves JSON content as YAML file."""
        try:
            data = json.loads(json_content)
            save_path = os.path.join(UPLOADS_DIR, f"{save_as}.yaml")
            os.makedirs(UPLOADS_DIR, exist_ok=True) # Create directory if needed

            with open(save_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False)

            logger.info(f"Saved JSON as YAML: {save_path}")
            return meco_pb2.StartResponse(success=True, message=f"Saved as {save_path}")

        except json.JSONDecodeError:
            return meco_pb2.StartResponse(success=False, message="Error: Invalid JSON syntax.")
        except Exception as e:
            logger.error(f"Error saving file: {e}")
            return meco_pb2.StartResponse(success=False, message=f"Error saving file: {e}")

    def Start(self, request, context):
        """Handles the Start RPC, processing file path or content."""
        try:
            file_content = None  # Initialize file_content

            if request.HasField("file_path"):
                file_path = request.file_path
                logger.info(f"Start() received a file path: {file_path}")

                if not os.path.isfile(file_path):
                    logger.error(f"File does not exist: {file_path}")
                    return meco_pb2.StartResponse(success=False, message=f"File does not exist: {file_path}")

                with open(file_path, "r", encoding="utf-8") as f:
                    file_content = f.read()  # Read as string
                logger.info(f"Successfully read file from {file_path} with size of {len(file_content)} bytes")

                if request.HasField("save_as"):
                    response = self.save_as_yaml(file_content, request.save_as)
                    if response.success:
                        response = self.validate_and_respond(file_content)
                        if response.success:
                            logger.info(f"Processed data (first 50 characters): {file_content[:50]}...")
                            return meco_pb2.StartResponse(success=True, message="File content processed and saved successfully.")
                        else:
                            return response
                    else:
                        return response
                    
            elif request.HasField("file_content"):
                file_content = request.file_content
                logger.info(f"Successfully received {len(file_content)} bytes inline")

            else:
                logger.error("Start() request missing both file_path and file_content.")
                return meco_pb2.StartResponse(success=False, message="No file_path or file_content provided.")

            if file_content is None: # Handle the case where no file content was received.
                return meco_pb2.StartResponse(success=False, message="No file content to process.")

            if request.HasField("save_as"):
                if request.dry_run:
                    response = self.validate_and_respond(file_content)
                    if response.success:
                        response = self.save_as_yaml(file_content, request.save_as)
                        if response.success:
                            return meco_pb2.StartResponse(success=True, message=f"File saved as {request.save_as}.yaml (dry run)")
                        else:
                            return response
                    else:
                        return response
                else:
                    response = self.save_as_yaml(file_content, request.save_as)
                    if response.success:
                        response = self.validate_and_respond(file_content)
                        if response.success:
                            logger.info(f"Processed data (first 50 characters): {file_content[:50]}...")
                            return meco_pb2.StartResponse(success=True, message="File content processed and saved successfully.")
                        else:
                            return response
                    else:
                        return response
            else:
                response = self.validate_and_respond(file_content)
                if response.success:
                    if request.dry_run:
                        return meco_pb2.StartResponse(success=True, message="File content validated (dry run).")
                    else:
                        logger.info(f"Processed data (first 50 characters): {file_content[:50]}...")
                        return meco_pb2.StartResponse(success=True, message="File content processed successfully.")
                else:
                    return response

        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            return meco_pb2.StartResponse(success=False, message=f"An unexpected error occurred: {e}")

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


def server_on():
    """Turns the server ON (daemonizes it)."""
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
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
        logger.info(f"Meco server turned ON in background (PID: {pid}).")
        sys.exit(0)
    else:
        os.setsid()
        pid2 = os.fork()
        if pid2 > 0:
            sys.exit(0)
        serve_forever()


def server_off():
    """Turns the server OFF (robust shutdown - kills ALL meco.py processes)."""

    # 1. Kill ALL meco.py processes
    killed_pids = []  # Keep track of killed PIDs to avoid double killing
    server_process_found = False  # Flag to track if any server process (except this one) is found
    
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid == os.getpid():
                continue    # Skip the current process to avoid killing the off command itself
            
            if "meco.py" in " ".join(proc.cmdline()):
                server_process_found = True
                logger.info(f"Killing meco.py process (PID: {proc.pid})")

                # 1. SIGTERM (Polite Shutdown) first
                os.kill(proc.pid, signal.SIGTERM)

                # 2. Wait for Termination (with timeout)
                timeout = 5  # seconds
                for _ in range(timeout):
                    if not psutil.pid_exists(proc.pid):
                        break  # Process terminated
                    time.sleep(1)
                else:  # If the loop finishes without breaking (timeout)
                # 3. SIGKILL (Forceful Kill)
                    logger.warning(f"Process (PID: {proc.pid}) did not respond to SIGTERM. Sending SIGKILL.")
                    os.kill(proc.pid, signal.SIGKILL)

                killed_pids.append(proc.pid)  # Add PID to list of killed ones

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass  # Process might have already exited

    # If no server processes (other than the current off command) are found, log that info.
    if not server_process_found:
        logger.info("All server-related processes are off.")

    # 2. Remove PID file (if it exists – it might not if the server crashed)
    try:
        os.remove(PID_FILE)
        logger.info("PID file removed.")
    except FileNotFoundError:
        pass  # It's okay if the file wasn't there

    sys.exit(0)  # Exit after killing processes


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
        request = meco_pb2.ResourceDescriptor(file_content=file_content, save_as=save_as)
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
        description="Emulates a LEO Mega Constellation",
        prog="meco"
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Available commands")

    subparsers.add_parser("on", help="Turn the Meco gRPC server ON (daemon mode)")
    subparsers.add_parser("off", help="Turn the Meco gRPC server OFF")

    start_parser = subparsers.add_parser("start", help="Send a resource descriptor file to the Meco server")
    start_parser.add_argument("filename", nargs="?", help="Path to the resource descriptor file")
    start_parser.add_argument("--content", help="Provide file content directly as a string")
    start_parser.add_argument("--saveas", dest="save_as", help="Filename to store the file on the server (if using content)")

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
    logger.info('You pressed Ctrl+C!')
    try:
        if os.path.exists(PID_FILE): # Remove the PID file if it exists
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
        "start": lambda args: start_resource_descriptor(args.filename, args.content, args.save_as),
    }

    handle_command(args, parser, parser_dict)
    
    if args.command == 'on': # Only start server if the command is 'on'
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
