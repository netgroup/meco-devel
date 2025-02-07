import grpc
import meco_pb2
import meco_pb2_grpc

def test_rpc_calls():
    channel = grpc.insecure_channel('localhost:50051')
    stub = meco_pb2_grpc.MecoServiceStub(channel)

    # Test MecoCall
    response = stub.MecoCall(meco_pb2.MecoRequest(message="Hello from the client"))
    print("MecoCall() ->", response.message)

    # Test Start
    start_req = meco_pb2.ResourceDescriptor(file_path="/path/to/somefile.txt")
    start_resp = stub.Start(start_req)
    print("Start() -> success =", start_resp.success, "; message =", start_resp.message)

if __name__ == '__main__':
    test_rpc_calls()
