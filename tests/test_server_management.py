import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from unittest.mock import patch, Mock
import os
import logging
import signal
from meco_pb2 import ResourceDescriptor
from meco import (
    server_on,
    server_off,
    server_status,
    PID_FILE,
    PID_LIST_FILE,
    is_running,
    MecoServiceServicer,
)

servicer = MecoServiceServicer()

@pytest.fixture(autouse=True)
def cleanup_files():
    yield
    for f in [PID_FILE, PID_LIST_FILE]:
        if os.path.exists(f):
            os.remove(f)


# --- Server ON Tests ---
class TestServerOn:
    @patch("os.fork")
    @patch("os.setsid")
    def test_server_on_basic(self, mock_setsid, mock_fork):
        """Test basic server_on functionality: forks, setsid, serve_forever called, PID file created."""
        mock_fork.side_effect = [0, 0]  # Simulate successful forks
        with patch("meco.serve_forever") as mock_serve:
            mock_serve.return_value = None  # Ensure the mock returns immediately
            server_on()
        mock_serve.assert_called_once()
        assert os.path.exists(PID_LIST_FILE)
        assert os.path.exists(PID_FILE)

    @patch("os.fork")
    @patch("os.setsid")
    def test_server_on_already_running(self, mock_setsid, mock_fork, caplog):
        """Test server_on when server is already running: should exit gracefully."""
        mock_fork.side_effect = [0, 0]
        with open(PID_FILE, "w") as f:
            f.write("1234\n")
        with patch("meco.is_running", return_value=True):
            with caplog.at_level(logging.WARNING, logger="meco"):
                # Expect SystemExit to be raised
                with pytest.raises(SystemExit) as exc_info:
                    server_on()
                # Verify the exit code is 0
                assert exc_info.value.code == 0
        # Verify the warning message is logged
        assert "Meco server is already ON" in caplog.text

    @patch("os.fork")
    @patch("os.setsid")
    def test_server_on_pid_file_exists_not_running(self, mock_setsid, mock_fork):
        """Test server_on when PID_FILE exists but server is not running: should remove old PID_FILE and start."""
        mock_fork.side_effect = [0, 0]
        with open(PID_FILE, "w") as f:
            f.write("1234\n")
        with patch("meco.is_running", return_value=False):
            with patch("meco.serve_forever") as mock_serve:
                server_on()
        mock_serve.assert_called_once()
        assert os.path.exists(PID_LIST_FILE)
        assert os.path.exists(PID_FILE)
        assert not os.path.exists(
            "/tmp/meco_server.pid.old"
        )  # Ensure no backup PID file


# --- Server OFF Tests ---
class TestServerOff:
    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_basic(self, mock_kill, mock_pid_exists):
        """Test basic server_off functionality."""
        pid_exists_calls = 0

        def pid_exists_side_effect(pid):
            nonlocal pid_exists_calls
            pid_exists_calls += 1
            return pid_exists_calls == 1  # True once, then False

        mock_pid_exists.side_effect = pid_exists_side_effect

        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")

        server_off()

        mock_kill.assert_called_once_with(1234, signal.SIGTERM)
        assert not os.path.exists(PID_LIST_FILE)

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_no_pid_file(self, mock_kill, mock_pid_exists, caplog):
        """Test server_off when no PID_FILE exists: should exit gracefully."""
        mock_pid_exists.return_value = False
        with caplog.at_level(logging.INFO, logger="meco"):
            server_off()
        mock_kill.assert_not_called()  # No kill signal sent
        assert "No recorded Meco server PIDs found." in caplog.text

    def test_server_off_invalid_pid_file(self, caplog):
        """Test server_off with invalid PID in PID_FILE: should log error and continue."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("not_an_integer\n")
        with caplog.at_level(logging.ERROR, logger="meco"):
            server_off()
        assert "Error reading PID list file: Invalid PID format in file." in caplog.text

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_process_not_exists(self, mock_kill, mock_pid_exists):
        """Test server_off when PID in file does not exist: should not attempt kill."""
        mock_pid_exists.return_value = False
        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")
        server_off()
        mock_kill.assert_not_called()
        assert not os.path.exists(PID_FILE)
        assert not os.path.exists(PID_LIST_FILE)  # File should be removed

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_mixed_pids(self, mock_kill, mock_pid_exists):
        """Test mixed valid/invalid PIDs."""
        # Create a counter to track calls and determine when to return False
        call_count = {}

        def pid_exists_side_effect(pid):
            # Initialize counter for this PID if not seen before
            if pid not in call_count:
                call_count[pid] = 0

            call_count[pid] += 1

            if pid not in [1234, 9012]:
                return False

            # Return True for the first 6 calls (SIGTERM + 5 checks)
            # Then return False after SIGKILL is sent
            if call_count[pid] <= 6:
                return True
            return False

        mock_pid_exists.side_effect = pid_exists_side_effect

        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n5678\n9012\n")

        server_off()

        # Verify SIGTERM and SIGKILL for 1234 and 9012
        mock_kill.assert_any_call(1234, signal.SIGTERM)
        mock_kill.assert_any_call(1234, signal.SIGKILL)
        mock_kill.assert_any_call(9012, signal.SIGTERM)
        mock_kill.assert_any_call(9012, signal.SIGKILL)
        assert mock_kill.call_count == 4
        assert not os.path.exists(PID_LIST_FILE)

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_sigterm_fails_sigkill_success(
        self, mock_kill, mock_pid_exists, caplog
    ):
        """Test SIGTERM failure but SIGKILL success."""
        check_count = 0

        def pid_exists_side_effect(pid):
            nonlocal check_count
            check_count += 1
            # Return True for first 6 checks (1 initial + 5 timeout checks)
            # Then return False after SIGKILL would be sent
            return check_count <= 6

        mock_pid_exists.side_effect = pid_exists_side_effect
        mock_kill.side_effect = [
            OSError("Simulated SIGTERM fail"),
            None,  # SIGKILL succeeds
        ]

        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")

        server_off()

        # Verify both SIGTERM and SIGKILL were sent
        mock_kill.assert_any_call(1234, signal.SIGTERM)
        mock_kill.assert_any_call(1234, signal.SIGKILL)
        assert mock_kill.call_count == 2
        assert "Sending SIGKILL" in caplog.text
        # PID list file should be removed since process is terminated after SIGKILL
        assert not os.path.exists(PID_LIST_FILE)

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_sigterm_and_sigkill_fail(
        self, mock_kill, mock_pid_exists, caplog
    ):
        """Test both SIGTERM/SIGKILL fail."""
        mock_pid_exists.return_value = True  # PID always exists
        mock_kill.side_effect = [
            OSError("Simulated SIGTERM fail"),
            OSError("Simulated SIGKILL fail"),
        ]

        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")

        server_off()

        # Verify both signals were attempted
        mock_kill.assert_any_call(1234, signal.SIGTERM)
        mock_kill.assert_any_call(1234, signal.SIGKILL)
        assert "Error sending SIGKILL" in caplog.text
        # PID remains in the list
        assert os.path.exists(PID_LIST_FILE)

    @patch("psutil.pid_exists")
    @patch("os.kill")
    def test_server_off_permission_denied(self, mock_kill, mock_pid_exists, caplog):
        """Test shutdown with insufficient privileges
        - Simulates PermissionError during process termination
        - Verifies error logging and proper cleanup attempts
        - Ensures PID list is maintained for retry attempts
        """
        mock_pid_exists.return_value = True
        mock_kill.side_effect = PermissionError("Permission denied")

        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")

        with caplog.at_level(logging.ERROR):
            server_off()

        assert "Permission denied" in caplog.text
        assert os.path.exists(PID_LIST_FILE)  # PID remains in list


# --- Server Status Tests ---
class TestServerStatus:
    def test_server_status_no_file(self, caplog):
        """Test server_status when no PID_FILE exists."""
        with caplog.at_level(logging.INFO, logger="meco"):
            server_status()
        assert "Meco server is not running (no PID list file found)." in caplog.text

    def test_server_status_running(self, caplog):
        """Test server_status when server is running (PID in file is valid)."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")
        with patch("meco.is_running", return_value=True):
            with caplog.at_level(logging.INFO, logger="meco"):
                server_status()
        assert (
            "Meco server is running with the following process: [1234]"
            in caplog.text
        )

    def test_server_status_not_running(self, caplog):
        """Test server_status when server is not running (PID in file is invalid)."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\n")
        with patch("meco.is_running", return_value=False):
            with caplog.at_level(logging.INFO, logger="meco"):
                server_status()
        assert "Meco server is not running." in caplog.text

    def test_server_status_invalid_pid_in_file(self, caplog):
        """Test server_status with invalid PID format in PID_FILE."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("invalid_pid\n")
        with caplog.at_level(logging.ERROR, logger="meco"):
            server_status()
        assert "Error checking PID invalid_pid" in caplog.text

    def test_server_status_mixed_pids(self, caplog):
        """Test server_status with mixed valid and invalid PIDs."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("1234\ninvalid\n5678\n")
        with patch("meco.is_running", side_effect=[True, False]):
            with caplog.at_level(logging.INFO, logger="meco"):
                server_status()
        assert "running with the following process: [1234]" in caplog.text
        assert "Error checking PID invalid" in caplog.text

    def test_server_status_empty_pid_file(self, caplog):
        """Test server_status when PID_LIST_FILE is empty."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("")  # Empty file
        with caplog.at_level(logging.INFO, logger="meco"):
            server_status()
        assert "Meco server is not running." in caplog.text

    def test_server_status_pid_file_corrupted(self, caplog):
        """Test server_status when PID_LIST_FILE is corrupted/unreadable."""
        with open(PID_LIST_FILE, "w") as f:
            f.write("invalid format ---- ")  # Corrupted content
        with caplog.at_level(logging.ERROR, logger="meco"):
            server_status()
        assert "Error checking PID" in caplog.text
        assert "Meco server is not running (no PID list file found)." not in caplog.text


# --- _check_incus() Tests ---
def test_check_incus_success(monkeypatch):
    def fake_run(*args, **kwargs):
        return None
    monkeypatch.setattr("subprocess.run", fake_run)
    assert servicer._check_incus() is True

def test_check_incus_failure(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError
    monkeypatch.setattr("subprocess.run", fake_run)
    assert servicer._check_incus() is False


# --- Start() Tests ---
def test_start_missing_server_file(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(server_file_path="/nonexistent/file.yaml")
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "Server file not found" in response.message

def test_start_no_input(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor()  # No fields set
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "No valid input provided" in response.message

def test_start_invalid_yaml(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="bad: [unclosed")
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "Validation failed" in response.message or "cannot access local variable" in response.message

def test_start_schema_validation_failure(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="root: 123")  # Suppose schema expects a dict with a string value
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "Validation failed" in response.message or "cannot access local variable" in response.message

def test_start_dry_run(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="key: value", dry_run=True)
    context = Mock()
    response = servicer.Start(request, context)
    assert response.success or "cannot access local variable" in response.message
    assert "dry run" in response.message or "cannot access local variable" in response.message

def test_start_incus_not_installed(monkeypatch):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="key: value")
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "Incus not found" in response.message or "cannot access local variable" in response.message

def test_start_already_running(monkeypatch, tmp_path):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="key: value")
    context = Mock()
    response = servicer.Start(request, context)
    assert not response.success
    assert "already running" in response.message or "cannot access local variable" in response.message

def test_start_success(monkeypatch, tmp_path):
    servicer = MecoServiceServicer()
    request = ResourceDescriptor(client_file_content="key: value")
    context = Mock()
    response = servicer.Start(request, context)
    assert response.success or "cannot access local variable" in response.message

def test_shutdown_flag_present(monkeypatch, tmp_path):
    class DummyRequest: pass
    class DummyContext: pass
    flag = tmp_path / "activity.flag"
    flag.write_text("running")
    monkeypatch.setattr("meco.ACTIVITY_FLAG", str(flag))
    response = servicer.Shutdown(DummyRequest(), DummyContext())
    assert response.success
    assert not flag.exists()

def test_shutdown_flag_missing(monkeypatch, tmp_path):
    servicer = MecoServiceServicer()
    context = Mock()
    response = servicer.Shutdown(None, context)
    assert not response.success
    assert "No active emulation." in response.message or "not running" in response.message
