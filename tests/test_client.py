import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import patch, mock_open
from meco_test_client import open_editor_for_content, perform_rpc_call
import os


class TestOpenEditorForContent:
    @patch("subprocess.call")
    @patch("builtins.open", mock_open(read_data="key: value"))
    @patch("os.remove")
    def test_open_editor_for_content_basic(self, mock_remove, mock_call, tmp_path):
        """Validate editor workflow from file creation to cleanup
        - Tests temporary file creation in designated cache directory
        - Verifies editor process invocation
        - Ensures proper cleanup when keep_file=False
        - Checks directory persistence after file deletion
        """
        cache_dir = tmp_path / "cache"
        test_filename = str(cache_dir / "edited_content.yaml")

        content = open_editor_for_content(
            keep_file=False, filename=test_filename, cache_dir_base=str(tmp_path)
        )

        mock_call.assert_called_once()
        assert "key: value" in content
        mock_remove.assert_called_once_with(test_filename)
        assert os.path.exists(cache_dir)  # Directory should persist


class TestPerformRPCCall:
    @patch("meco_test_client.grpc.insecure_channel")
    @patch("meco_test_client.meco_pb2_grpc.MecoServiceStub")
    @patch("builtins.open", mock_open(read_data="key: value"))
    def test_rpc_calls_with_localfile(self, mock_stub, mock_channel, tmp_path):
        """End-to-end test for local file processing workflow
        - Simulates file input through --filepath argument
        - Verifies proper file reading and content transmission
        - Checks gRPC payload contains expected content
        """
        test_file = tmp_path / "test.yaml"
        test_file.write_text("key: value")

        perform_rpc_call("start", localfile=str(test_file))

        args, _ = mock_stub.return_value.Start.call_args
        assert "key: value" in args[0].client_file_content
        assert mock_stub.return_value.Start.call_count == 1
