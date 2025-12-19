import grpc
import yaml
import time
from concurrent import futures

try:
    import meco_pb2
    import meco_pb2_grpc
except ImportError:
    # Handle case where protos are not yet in path or need recompilation
    # For refactor context, we assume they are available or we mock imports if strictly needed for linting
    pass

from emulation.lifecycle import LifecycleManager
from config.validator import validate_topology

from utils.logger import setup_logging
from service.monitor import HypervisorMonitor

logger = setup_logging("meco.service")

lifecycle = LifecycleManager()
monitor = HypervisorMonitor()


class MecoService(meco_pb2_grpc.MecoServiceServicer):
    """
    gRPC Service implementation delegating to LifecycleManager.
    """

    def Start(self, request, context):
        try:
            start_time = time.time()
            # 1. Resolve content
            content = None
            if request.HasField("server_file_path"):
                with open(request.server_file_path, "r") as f:
                    content = f.read()
            elif request.HasField("client_file_content"):
                content = request.client_file_content
            else:
                yield meco_pb2.StartResponse(
                    success=False, message="No content provided"
                )
                return

            # 2. Parse & Validate
            parsed_yaml = yaml.safe_load(content)
            validation = validate_topology(parsed_yaml)
            if not validation["success"]:
                yield meco_pb2.StartResponse(
                    success=False, message=validation["message"]
                )
                return

            # 3. Save as (optional)
            if request.save_as:
                # Logic to save file on server
                pass

            # 4. Execute
            for item in lifecycle.start_emulation(parsed_yaml, dry_run=request.dry_run):
                if isinstance(item, str):
                    yield meco_pb2.StartResponse(success=True, log_message=item)
                elif isinstance(item, dict):
                    result = item
                    # Formulate message based on result
                    if result.get("dry_run"):
                        msg = "Validation passed (Dry run)."
                    elif result.get("flows_inserted"):
                        msg = "Emulation started successfully."
                    else:
                        msg = "Instances were deployed and reached Running state, but no flows were inserted (Port map empty)."

                    yield meco_pb2.StartResponse(success=True, message=msg)

                    # Log final result and duration
                    duration = time.time() - start_time
                    status = "Success" if result.get("success") else "Failed"
                    logger.info(
                        f"Emulation {status}. Duration: {duration:.2f}s. Result: {msg}"
                    )

        except Exception as e:
            duration = time.time() - start_time
            logger.error(f"Start RPC failed after {duration:.2f}s: {e}")
            yield meco_pb2.StartResponse(success=False, message=str(e))

    def Shutdown(self, request, context):
        try:
            for item in lifecycle.stop_emulation(force=True):
                if isinstance(item, str):
                    yield meco_pb2.ShutdownResponse(success=True, log_message=item)
                elif isinstance(item, bool):
                    success = item
                    msg = (
                        "Emulation stopped."
                        if success
                        else "Shutdown failed or no active emulation."
                    )
                    yield meco_pb2.ShutdownResponse(success=success, message=msg)

        except Exception as e:
            logger.error(f"Shutdown RPC failed: {e}")
            yield meco_pb2.ShutdownResponse(success=False, message=str(e))

    def MecoCall(self, request, context):
        return meco_pb2.MecoResponse(message=f"Echo: {request.message}")


def serve(port=50051, max_workers=10):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    meco_pb2_grpc.add_MecoServiceServicer_to_server(MecoService(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(f"Meco gRPC server started on port {port}.")

    # Start Monitor
    monitor.start()

    server.wait_for_termination()

    # Stop Monitor on exit
    monitor.stop()
