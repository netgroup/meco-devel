#!/usr/bin/env python

import grpc
import meco_pb2
import meco_pb2_grpc
import argparse
import os
import logging
import subprocess
import sys

# Configure logging for the client
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


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


def perform_rpc_call(  # Renamed from test_rpc_calls
    command, filename=None, localfile=None, saveas=None, dryrun=False, content=None
):
    """Tests the Meco gRPC service with file reference or inline content."""
    try:
        channel = grpc.insecure_channel("localhost:50051")  # Create a gRPC channel
        stub = meco_pb2_grpc.MecoServiceStub(channel)  # Create a stub for the service

        # Handle localfile: Read content from local file
        if localfile:
            with open(localfile, "r") as f:
                content = f.read()
            logger.info(f"Read content from local file: {localfile}")

        # Open editor and save content in a cache file when needed
        keep_file = bool(saveas or dryrun)  # Keep file if using --saveas or --dryrun
        if content == "":
            logger.info("Opening Nano editor for content input...")
            content = open_editor_for_content("edited_content.yaml", keep_file)
            if not content:
                logger.error("No valid input provided.")
                return  # Abort if no content was provided

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
        logger.info(f"Response: {response.message}")

    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            logger.error("Meco server is not running. Start it with 'meco on' first.")
        else:
            logger.error(f"gRPC Error: {e.details()}")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meco YAML Client")
    parser.add_argument("command", choices=["start"], help="Command to execute")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--filename", help="Server-side file path (existing on server)")
    group.add_argument(
        "--filepath", dest="localfile", help="Read local file and send as content"
    )
    group.add_argument(
        "--content",
        nargs="?",
        const="",
        help="Provide file content directly as a string or open an editor if empty",
    )

    parser.add_argument("--saveas", help="Specify remote filename to save the file as")
    parser.add_argument(
        "--dryrun", action="store_true", help="Validate without execution"
    )

    args = parser.parse_args()
    perform_rpc_call(
        args.command,
        filename=args.filename,
        localfile=args.localfile,
        saveas=args.saveas,
        dryrun=args.dryrun,
        content=args.content,
    )
