import grpc
import meco_pb2
import meco_pb2_grpc
import argparse
import os

def test_rpc_calls(command, filename=None, localfile=None, saveas=None, dry_run=False):
    channel = grpc.insecure_channel("localhost:50051")
    stub = meco_pb2_grpc.MecoServiceStub(channel)

    if command == "start":
        if filename:
            # Sending a file path to the server
            start_req = meco_pb2.ResourceDescriptor(file_path=filename)
        elif localfile:
            # Reading the local file and sending its content
            if not os.path.exists(localfile):
                print(f"Error: File '{localfile}' does not exist.")
                return
            with open(localfile, "r", encoding="utf-8") as f:
                file_content = f.read()
            start_req = meco_pb2.ResourceDescriptor(file_content=file_content, save_as=saveas, dry_run=dry_run)
        else:
            print("Error: Either filename or localfile must be provided.")
            return

        response = stub.Start(start_req)
        print(f"Start({filename or localfile}) -> success = {response.success} ; message = {response.message}")
    else:
        print("Invalid command. Use 'start' with required parameters.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Meco gRPC Client with flexible file input")
    parser.add_argument("command", choices=["start"], help="Command to execute")
    parser.add_argument("filename", nargs="?", help="File path to send (for remote server access)")
    parser.add_argument("--file", dest="localfile", help="Read local file and send as content")
    parser.add_argument("--saveas", help="Specify remote filename to save the file as")
    parser.add_argument("--dry_run", action="store_true", help="Save the file without starting the emulation")
    
    args = parser.parse_args()

    test_rpc_calls(args.command, filename=args.filename, localfile=args.localfile, saveas=args.saveas, dry_run=args.dry_run)
