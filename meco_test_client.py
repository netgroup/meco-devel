import grpc
import meco_pb2
import meco_pb2_grpc
import argparse
import os
import logging

# Configure logging for the client
logging.basicConfig(
    level=logging.INFO,  # Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],  # Log to the console
)

logger = logging.getLogger(__name__)  # Get a logger instance for this module

def test_rpc_calls(command, filename=None, localfile=None, saveas=None, dry_run=False):
    """Tests the Meco gRPC service."""
    try:
        channel = grpc.insecure_channel("localhost:50051")  # Create a gRPC channel
        stub = meco_pb2_grpc.MecoServiceStub(channel)  # Create a stub for the service

        if command == "start":
            start_req = None  # Initialize the request variable

            if filename:
                logger.info(f"Sending file path: {filename}")
                start_req = meco_pb2.ResourceDescriptor(file_path=filename, dry_run=dry_run)
            elif localfile:
                if not os.path.exists(localfile):
                    logger.error(f"Error: File '{localfile}' does not exist.")
                    return  # Exit the function if the file doesn't exist

                try:
                    with open(localfile, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    logger.info(f"Sending local file content (first 50 chars): {file_content[:50]}...")
                    start_req = meco_pb2.ResourceDescriptor(file_content=file_content, save_as=saveas, dry_run=dry_run)
                except Exception as e: # Catch file reading errors
                    logger.error(f"Error reading local file: {e}")
                    return

            else:
                logger.error("Error: Either filename or localfile must be provided for 'start' command.")
                return  # Exit if no file info is given

            if start_req is None: # Exit if the request is still None
                logger.error("Error: No request was created.")
                return

            try:  # Try making the gRPC call; handle connection errors
                response = stub.Start(start_req)
                if response.success:
                    logger.info(f"Start({filename or localfile}) -> Success: {response.message}")
                else:
                    logger.error(f"Start({filename or localfile}) -> Error: {response.message}")

            except grpc.RpcError as e:  # Catch gRPC errors (including connection failures)
                logger.error(f"gRPC Error (likely server offline): {e}")
                if e.code() == grpc.StatusCode.UNAVAILABLE: # Check if the server is unavailable
                    logger.error("The server is likely offline or unreachable.")
                return  # Exit the function after reporting the error
            
        else:
            logger.error("Invalid command. Use 'start'.")

    except Exception as e:
        logger.exception(f"Client-side Error: {e}")  # Handle other client-side errors



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Meco gRPC Client with flexible file input")
    parser.add_argument("command", choices=["start"], help="Command to execute")
    parser.add_argument("filename", nargs="?", help="File path to send (for remote server access)")
    parser.add_argument("--file", dest="localfile", help="Read local file and send as content")
    parser.add_argument("--saveas", help="Specify remote filename to save the file as")
    parser.add_argument("--dry_run", action="store_true", help="Save the file without starting the emulation")
    
    args = parser.parse_args()

    test_rpc_calls(args.command, filename=args.filename, localfile=args.localfile, saveas=args.saveas, dry_run=args.dry_run)
