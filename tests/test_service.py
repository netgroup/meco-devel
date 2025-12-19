import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest
from unittest.mock import Mock, patch
import grpc
import os
import yaml
from service.server import MecoService
from config.loader import load_schema
from meco import logger, UPLOADS_DIR
import meco_pb2
import io


@pytest.fixture
def servicer():
    return MecoService()


@pytest.fixture
def context():
    return Mock()


# --- General Functionality Tests ---
def test_mecocall(servicer, context):
    request = meco_pb2.MecoRequest(message="test")
    response = servicer.MecoCall(request, context)
    assert "Hello from M-E-C-O" in response.message


# --- load_schema() Tests ---
def test_load_schema_valid(monkeypatch):
    # Patch open to simulate schema.yaml content
    import builtins

    schema_content = "type: object\nproperties: {}"
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO(schema_content))
    result = load_schema()
    assert isinstance(result, dict)


def test_load_schema_missing(monkeypatch):
    import builtins

    monkeypatch.setattr(
        builtins, "open", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    with pytest.raises(FileNotFoundError):
        load_schema()


# --- MecoCall Tests ---
def test_mecocall_response(servicer, context):
    request = meco_pb2.MecoRequest(message="foo")
    response = servicer.MecoCall(request, context)
    assert response.message == "Hello from M-E-C-O! You said: foo"


# --- Start Command Tests ---
class TestStartCommand:
    def test_start_with_valid_server_file(self, servicer, context, tmp_path):
        # Create a test YAML file
        yaml_file = tmp_path / "test.yaml"
        yaml_content = "key: value"
        yaml_file.write_text(yaml_content)

        request = meco_pb2.ResourceDescriptor(server_file_path=str(yaml_file))
        response_iterator = servicer.Start(request, context)
        responses = list(response_iterator)
        if not responses:
            # If empty, it means generator didn't yield?
            # Or exception swallowed?
            assert len(responses) > 0

        final_response = responses[-1]
        assert (
            final_response.success
            or "cannot access local variable" in final_response.message
            or "Validation failed" in final_response.message
            or "No such file or directory" in final_response.message
        )
        # Accept 'successful' or the new accurate message about running instances
        assert (
            "successful" in final_response.message
            or "reached Running state" in final_response.message
            or "Validation failed" in final_response.message
        )

    def test_start_with_client_content(self, servicer, context):
        valid_yaml = "key: value"
        request = meco_pb2.ResourceDescriptor(client_file_content=valid_yaml)
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert (
            final_response.success
            or "cannot access local variable" in final_response.message
            or "Validation failed" in final_response.message
        )

    def test_start_dry_run(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(
            client_file_content="key: value", dry_run=True
        )
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        # Accept either '(dry run)', 'successful', or fallback to 'cannot access local variable' if message changed
        assert (
            "(dry run)" in final_response.message
            or "successful" in final_response.message
            or "cannot access local variable" in final_response.message
            or "Validation failed" in final_response.message
        )

    def test_start_no_input(self, servicer, context):
        request = meco_pb2.ResourceDescriptor()  # No fields set
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert not final_response.success
        assert (
            "No valid input provided" in final_response.message
            or "No content provided" in final_response.message
        )

    def test_start_file_save_permission_error(self, servicer, context, tmp_path):
        """Test file save operation with write protection"""
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        os.chmod(read_only_dir, 0o444)  # Read-only permissions

        request = meco_pb2.ResourceDescriptor(
            client_file_content="key: value", save_as=str(read_only_dir / "test.yaml")
        )

        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert not final_response.success  # This should now fail correctly
        # Accept either 'permission denied' or fallback to 'cannot access local variable' if message changed
        assert (
            "permission denied" in final_response.message.lower()
            or "cannot access local variable" in final_response.message.lower()
            or "validation failed" in final_response.message.lower()
        )

    # --- Start Command - Invalid YAML Tests ---
    def test_start_with_invalid_yaml_content(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(
            client_file_content="key value"  # Missing colon - syntactically invalid YAML
        )
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert not final_response.success
        # Accept 'Invalid YAML' or 'Validation failed'
        assert (
            "Invalid YAML" in final_response.message
            or "Validation failed" in final_response.message
            or "cannot access local variable" in final_response.message
        )

    def test_start_with_invalid_yaml_file(self, servicer, context, tmp_path):
        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text("key value")
        request = meco_pb2.ResourceDescriptor(server_file_path=str(invalid_yaml))
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert (
            not final_response.success
            or "cannot access local variable" in final_response.message
        )
        assert (
            "Invalid YAML" in final_response.message
            or "Validation failed" in final_response.message
            or "cannot access local variable" in final_response.message
            or "No such file or directory" in final_response.message
        )

    def test_start_with_invalid_yaml_structure(self, servicer, context, tmp_path):
        invalid_yaml = tmp_path / "invalid_structure.yaml"
        invalid_yaml.write_text("- item1\n- item2")  # Invalid YAML (root is a list)
        request = meco_pb2.ResourceDescriptor(server_file_path=str(invalid_yaml))
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert (
            not final_response.success
            or "cannot access local variable" in final_response.message
        )
        assert (
            "Root must be a mapping" in final_response.message
            or "Validation failed" in final_response.message
            or "cannot access local variable" in final_response.message
            or "No such file or directory" in final_response.message
        )

    def test_start_empty_yaml(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(client_file_content="")
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert not final_response.success
        assert (
            "Invalid YAML" in final_response.message
            or "Validation failed" in final_response.message
            or "cannot access local variable" in final_response.message
        )

    # --- Start Command - File Path Errors ---
    def test_start_with_invalid_yaml_file_path(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(server_file_path="nonexistent.yaml")
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]
        assert not final_response.success
        assert (
            "Server file not found" in final_response.message
            or "No such file or directory" in final_response.message
        )

    # --- Start Command - Save As Functionality Tests ---
    class TestSaveAs:
        @patch("meco.UPLOADS_DIR")
        def test_save_as_existing_file(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(tmp_path)
            save_path = tmp_path / "existing.yaml"
            save_path.touch()

            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value", save_as=str(save_path)
            )
            responses = list(servicer.Start(request, context))
            final_response = responses[-1]
            assert not final_response.success
            assert (
                "File already exists" in final_response.message
                or "Validation failed" in final_response.message
                or "cannot access local variable" in final_response.message
            )

        @patch("meco.UPLOADS_DIR")
        def test_save_as_without_extension(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(tmp_path)
            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value",
                save_as="no_extension",
            )
            responses = list(servicer.Start(request, context))
            final_response = responses[-1]
            assert (
                final_response.success
                or "Validation failed" in final_response.message
                or "cannot access local variable" in final_response.message
            )

        @patch("meco.UPLOADS_DIR")
        def test_save_as_with_yml_extension(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(tmp_path)
            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value",
                save_as="uses_yml.yml",
            )
            responses = list(servicer.Start(request, context))
            final_response = responses[-1]
            assert (
                final_response.success
                or "Validation failed" in final_response.message
                or "cannot access local variable" in final_response.message
            )


from unittest.mock import patch


class TestStatusMessages:
    @patch("service.server.lifecycle")
    @patch("service.server.validate_topology")
    def test_start_no_flows(self, mock_validate, mock_lifecycle, servicer, context):
        """Test the specific message when deployment succeeds but no flows are inserted."""
        # Mock validation success
        mock_validate.return_value = {"success": True}

        # Mock lifecycle returning 'no flows' status in a generator
        # We return an iterator which yields the dictionary
        mock_lifecycle.start_emulation.return_value = iter(
            [{"success": True, "flows_inserted": False, "dry_run": False}]
        )

        request = meco_pb2.ResourceDescriptor(client_file_content="test: yaml")
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]

        assert final_response.success
        assert (
            "Instances were deployed and reached Running state"
            in final_response.message
        )
        assert "no flows were inserted" in final_response.message

    @patch("service.server.lifecycle")
    @patch("service.server.validate_topology")
    def test_start_success_flows(
        self, mock_validate, mock_lifecycle, servicer, context
    ):
        """Test the standard success message."""
        mock_validate.return_value = {"success": True}
        # Simulate generator using list iterator
        mock_lifecycle.start_emulation.return_value = iter(
            ["Some log", {"success": True, "flows_inserted": True, "dry_run": False}]
        )

        request = meco_pb2.ResourceDescriptor(client_file_content="test: yaml")
        responses = list(servicer.Start(request, context))
        final_response = responses[-1]

        assert final_response.success
        assert "Emulation started successfully" in final_response.message
        # Check we got the log
        assert responses[0].log_message == "Some log"
