import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest
from unittest.mock import patch, mock_open, MagicMock
from meco.client import (
    open_editor_for_content,
    perform_rpc_call,
    get_running_instances,
    delete_instance,
)
import subprocess
import json


class TestOpenEditorForContent:
    @patch("os.remove")
    @patch("subprocess.call")
    @patch("builtins.open", new_callable=mock_open, read_data="")
    def test_editor_writes_nothing(
        self, mock_open_fn, mock_call, mock_remove, tmp_path
    ):
        result = open_editor_for_content(
            filename="foo.yaml", keep_file=False, cache_dir_base=str(tmp_path)
        )
        assert result is None

    @patch("os.remove")
    @patch("subprocess.call")
    @patch("builtins.open", new_callable=mock_open, read_data="some: value")
    def test_editor_writes_content_and_deletes(
        self, mock_open_fn, mock_call, mock_remove, tmp_path
    ):
        result = open_editor_for_content(
            filename="foo.yaml", keep_file=False, cache_dir_base=str(tmp_path)
        )
        assert result == "some: value"
        mock_remove.assert_called()

    @patch("os.remove")
    @patch("subprocess.call")
    @patch("builtins.open", new_callable=mock_open, read_data="some: value")
    def test_editor_writes_content_and_keeps(
        self, mock_open_fn, mock_call, mock_remove, tmp_path
    ):
        result = open_editor_for_content(
            filename="foo.yaml", keep_file=True, cache_dir_base=str(tmp_path)
        )
        assert result == "some: value"
        mock_remove.assert_not_called()


class TestGetRunningInstances:
    @patch("subprocess.run")
    def test_valid_json_stdout(self, mock_run):
        mock_run.return_value.stdout = json.dumps(
            [{"name": "foo", "config": {"user.meco": "true"}}]
        )
        result = get_running_instances()
        assert isinstance(result, list)
        assert result[0]["name"] == "foo"

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "incus"))
    def test_called_process_error(self, mock_run):
        result = get_running_instances()
        assert result == []

    @patch("subprocess.run")
    def test_json_decode_error(self, mock_run):
        mock_run.return_value.stdout = "not json"
        result = get_running_instances()
        assert result == []


class TestDeleteInstance:
    @patch("subprocess.run")
    def test_delete_ok(self, mock_run):
        mock_run.return_value = None
        assert delete_instance("foo")

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "incus"))
    def test_delete_fail(self, mock_run):
        assert not delete_instance("foo")


class TestPerformRPCCall:
    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    @patch("builtins.open", mock_open(read_data="key: value"))
    def test_start_with_filepath(self, mock_stub, mock_channel, tmp_path):
        test_file = tmp_path / "test.yaml"
        test_file.write_text("key: value")
        perform_rpc_call("start", localfile=str(test_file))
        args, _ = mock_stub.return_value.Start.call_args
        assert "key: value" in args[0].client_file_content

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    def test_start_with_content(self, mock_stub, mock_channel):
        perform_rpc_call("start", content="foo: bar")
        args, _ = mock_stub.return_value.Start.call_args
        assert "foo: bar" in args[0].client_file_content

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    @patch("meco.client.open_editor_for_content", return_value=None)
    def test_start_with_empty_content_editor_none(
        self, mock_editor, mock_stub, mock_channel
    ):
        perform_rpc_call("start", content="")
        mock_editor.assert_called()

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    @patch("meco.client.get_running_instances", return_value=[{"name": "foo"}])
    @patch("meco.client.delete_instance")
    def test_shutdown_success(self, mock_delete, mock_get, mock_stub, mock_channel):
        mock_stub.return_value.Shutdown.return_value.success = True
        perform_rpc_call("shutdown")
        mock_delete.assert_called_with("foo")

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    def test_shutdown_failure(self, mock_stub, mock_channel):
        mock_stub.return_value.Shutdown.return_value.success = False
        perform_rpc_call("shutdown")
        # Should not raise, just log warning

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    def test_start_grpc_unavailable(self, mock_stub, mock_channel):
        import grpc

        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.UNAVAILABLE
        mock_stub.return_value.Start.side_effect = error
        with pytest.raises(SystemExit):
            perform_rpc_call("start", content="foo")

    @patch("meco.client.grpc.insecure_channel")
    @patch("meco.client.meco_pb2_grpc.MecoServiceStub")
    def test_start_grpc_other_error(self, mock_stub, mock_channel):
        import grpc

        error = grpc.RpcError()
        error.details = lambda: "error details"
        error.code = lambda: grpc.StatusCode.UNKNOWN
        mock_stub.return_value.Start.side_effect = error
        with pytest.raises(SystemExit):
            perform_rpc_call("start", content="foo")
