import grpc
import meco_pb2
import meco_pb2_grpc

def test_rpc_calls():
    channel = grpc.insecure_channel("localhost:50051")
    stub = meco_pb2_grpc.MecoServiceStub(channel)

    # Test MecoCall
    response = stub.MecoCall(meco_pb2.MecoRequest(message="Hello from the client"))
    print("MecoCall() ->", response.message)

    # Test Start with a file path
    start_req_path = meco_pb2.ResourceDescriptor(file_path="/path/to/somefile.txt")
    start_resp_path = stub.Start(start_req_path)
    print("Start(file_path) -> success =", start_resp_path.success, "; message =", start_resp_path.message)

    # Test Start with inline file content
    file_content = "This is a test file content passed directly."
    start_req_content = meco_pb2.ResourceDescriptor(file_content=file_content)
    start_resp_content = stub.Start(start_req_content)
    print("Start(file_content) -> success =", start_resp_content.success, "; message =", start_resp_content.message)

    # Test Start with inline content and save_as filename
    save_as_filename = "saved_file.txt"
    start_req_save = meco_pb2.ResourceDescriptor(file_content=file_content, save_as=save_as_filename)
    start_resp_save = stub.Start(start_req_save)
    print("Start(file_content + save_as) -> success =", start_resp_save.success, "; message =", start_resp_save.message)

if __name__ == "__main__":
    test_rpc_calls()
