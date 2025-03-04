import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import Mock, patch
import grpc
import os
import yaml
from meco import MecoServiceServicer, meco_pb2, logger, UPLOADS_DIR


@pytest.fixture
def servicer():
    return MecoServiceServicer()


@pytest.fixture
def context():
    return Mock()


# --- General Functionality Tests ---
def test_mecocall(servicer, context):
    request = meco_pb2.MecoRequest(message="test")
    response = servicer.MecoCall(request, context)
    assert "Hello from M-E-C-O" in response.message


# --- Start Command Tests ---
class TestStartCommand:
    def test_start_with_valid_server_file(self, servicer, context, tmp_path):
        # Create a test YAML file
        yaml_file = tmp_path / "test.yaml"
        yaml_content = "key: value"
        yaml_file.write_text(yaml_content)

        request = meco_pb2.ResourceDescriptor(server_file_path=str(yaml_file))
        response = servicer.Start(request, context)
        assert response.success
        assert "successful" in response.message

    def test_start_with_client_content(self, servicer, context):
        valid_yaml = "key: value"
        request = meco_pb2.ResourceDescriptor(client_file_content=valid_yaml)
        response = servicer.Start(request, context)
        assert response.success

    def test_start_dry_run(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(
            client_file_content="key: value", dry_run=True
        )
        response = servicer.Start(request, context)
        assert "(dry run)" in response.message

    def test_start_no_input(self, servicer, context):
        request = meco_pb2.ResourceDescriptor()  # No fields set
        response = servicer.Start(request, context)
        assert not response.success
        assert "No valid input provided" in response.message

    def test_start_file_save_permission_error(self, servicer, context, tmp_path):
        """Test file save operation with write protection"""
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        os.chmod(read_only_dir, 0o444)  # Read-only permissions

        request = meco_pb2.ResourceDescriptor(
            client_file_content="key: value", save_as=str(read_only_dir / "test.yaml")
        )

        response = servicer.Start(request, context)
        assert not response.success  # This should now fail correctly
        assert "permission denied" in response.message.lower()

    # --- Start Command - Invalid YAML Tests ---
    def test_start_with_invalid_yaml_content(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(
            client_file_content="key value"  # Missing colon - syntactically invalid YAML
        )
        response = servicer.Start(request, context)
        assert not response.success
        assert "Invalid YAML" in response.message

    def test_start_with_invalid_yaml_file(self, servicer, context, tmp_path):
        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text(
            "key value"
        )  # Missing colon - syntactically invalid YAML

        request = meco_pb2.ResourceDescriptor(server_file_path=str(invalid_yaml))
        response = servicer.Start(request, context)
        assert not response.success
        assert "Invalid YAML" in response.message

    def test_start_with_invalid_yaml_structure(
        self, servicer, context, tmp_path
    ):  # More specific invalid YAML test
        invalid_yaml = tmp_path / "invalid_structure.yaml"
        invalid_yaml.write_text("- item1\n- item2")  # Invalid YAML (root is a list)

        request = meco_pb2.ResourceDescriptor(server_file_path=str(invalid_yaml))
        response = servicer.Start(request, context)
        assert not response.success
        assert "Root must be a mapping" in response.message

    def test_start_empty_yaml(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(client_file_content="")
        response = servicer.Start(request, context)
        assert not response.success
        assert "Invalid YAML" in response.message

    # --- Start Command - File Path Errors ---
    def test_start_with_invalid_yaml_file_path(self, servicer, context):
        request = meco_pb2.ResourceDescriptor(server_file_path="nonexistent.yaml")
        response = servicer.Start(request, context)
        assert not response.success
        assert "Server file not found" in response.message  # Corrected assertion

    # --- Start Command - Save As Functionality Tests ---
    class TestSaveAs:
        @patch("meco.UPLOADS_DIR")  # Mock UPLOADS_DIR for this test class
        def test_save_as_existing_file(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(
                tmp_path
            )  # Mock UPLOADS_DIR to tmp_path
            # Create existing file in the mocked uploads dir
            save_path = tmp_path / "existing.yaml"
            save_path.touch()

            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value", save_as=str(save_path)
            )
            response = servicer.Start(request, context)
            assert not response.success
            assert "File already exists" in response.message

        @patch("meco.UPLOADS_DIR")  # Mock UPLOADS_DIR for this test class
        def test_save_as_without_extension(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(
                tmp_path
            )  # Mock UPLOADS_DIR to tmp_path
            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value",
                save_as="no_extension",  # Should become "no_extension.yaml"
            )
            response = servicer.Start(request, context)
            assert (
                response.success
            )  # Expect success now as UPLOADS_DIR is mocked and empty
            assert "successful" in response.message  # Expect successful message

        @patch("meco.UPLOADS_DIR")  # Mock UPLOADS_DIR for this test class
        def test_save_as_with_yml_extension(
            self, mock_uploads_dir, servicer, context, tmp_path
        ):
            mock_uploads_dir.return_value = str(
                tmp_path
            )  # Mock UPLOADS_DIR to tmp_path
            request = meco_pb2.ResourceDescriptor(
                client_file_content="key: value",
                save_as="uses_yml.yml",  # Should remain "uses_yml.yml"
            )
            response = servicer.Start(request, context)
            assert (
                response.success
            )  # Expect success now as UPLOADS_DIR is mocked and empty
            assert "successful" in response.message  # Expect successful message
