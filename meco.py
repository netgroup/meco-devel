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

# PID file for tracking the daemonized server
PID_FILE = "/tmp/meco_server.pid"
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

    def Start(self, request, context):
        """Handles Start RPC request with either file_path or file_content."""
        if request.HasField("file_path"):
            file_path = request.file_path
            logger.info(f"Start() received a file path: {file_path}")

            if not os.path.isfile(file_path):
                logger.error(f"File does not exist: {file_path}")
                return meco_pb2.StartResponse(success=False, message=f"File does not exist: {file_path}")

            with open(file_path, "r", encoding="utf-8") as f:
                file_content = f.read()
            logger.info(f"Successfully read file from path: {file_path}")

        elif request.HasField("file_content"):
            file_content = request.file_content
            logger.info("Start() received inline file content.")

            if request.HasField("save_as"):
                save_path = os.path.join(UPLOADS_DIR, request.save_as)
                os.makedirs(UPLOADS_DIR, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(file_content)
                logger.info(f"File content saved to: {save_path}")

        else:
            logger.error("Start() request missing both file_path and file_content.")
            return meco_pb2.StartResponse(success=False, message="No file_path or file_content provided.")

        logger.info(f"Processing file content: {file_content[:50]}...")  # Log first 50 chars
        return meco_pb2.StartResponse(success=True, message="File content processed successfully.")


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
    """Turns the server OFF."""
    if not os.path.exists(PID_FILE):
        logger.warning("No PID file found. Meco server might be OFF already.")
        return

    with open(PID_FILE, "r") as f:
        pid = int(f.read().strip())

    if not is_running(pid):
        logger.warning("Server process not found. Removing stale PID file.")
        os.remove(PID_FILE)
        return

    logger.info(f"Turning OFF Meco server (PID: {pid})...")
    os.kill(pid, signal.SIGTERM)
    os.remove(PID_FILE)
    logger.info("Meco server turned OFF.")


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
    start_parser.add_argument("--save-as", help="Filename to store the file on the server (if using content)")

    return parser


def handle_command(args, parser, parser_dict):
    """Handles CLI commands."""
    command = args.command
    if command in parser_dict:
        parser_dict[command](args)
    else:
        parser.print_help()
        sys.exit(1)


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


if __name__ == "__main__":
    main()
