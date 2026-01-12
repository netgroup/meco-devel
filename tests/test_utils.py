import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest
from unittest.mock import patch
import signal
import errno
from meco.main import is_running, signal_handler, PID_FILE


class TestIsRunning:
    @patch("sys.exit")
    def test_is_running_valid_pid(self, mock_exit):
        """Test is_running with a valid PID (os.kill succeeds)."""
        with patch("psutil.pid_exists", return_value=True):
            assert is_running(1234) is True

    @patch("sys.exit")
    def test_is_running_invalid_pid(self, mock_exit):
        """Test is_running with an invalid PID (os.kill raises OSError)."""
        with patch("psutil.pid_exists", return_value=False):
            assert is_running(9999) is False

    @patch("sys.exit")
    def test_is_running_zombie_process(self, mock_exit):
        """Test PID check for zombie process
        - Simulates ESRCH error (zombie process)
        - Verifies proper false return for non-existent PID
        """
        with patch("psutil.pid_exists", return_value=False):
            assert is_running(9999) is False


class TestSignalHandler:
    @patch("sys.exit")
    def test_signal_handler_basic(self, mock_exit, caplog):
        """Test signal_handler removes PID_FILE and logs message on SIGINT."""
        with (
            patch("os.path.exists", return_value=True),
            patch("os.remove") as mock_remove,
        ):
            signal_handler(signal.SIGINT, None)
            assert "Signal received" in caplog.text
            mock_remove.assert_called_with(PID_FILE)
            # mock_exit.assert_called_with(0) # Optionally assert sys.exit is called

    @patch("sys.exit")
    def test_signal_handler_no_pid_file(self, mock_exit, caplog):
        """Test signal_handler when PID_FILE does not exist."""
        with (
            patch("os.path.exists", return_value=False),
            patch("os.remove") as mock_remove,
        ):  # mock_remove still needed, even if not expected to be called in this test, for context
            signal_handler(signal.SIGINT, None)
            assert "Signal received" in caplog.text
            mock_remove.assert_not_called()  # Verify os.remove is NOT called
            # mock_exit.assert_called_with(0) # Optionally assert sys.exit is called

    @patch("sys.exit")
    def test_signal_handler_remove_fails(self, mock_exit, caplog):
        """Test signal_handler when removing PID_FILE fails."""
        with (
            patch("os.path.exists", return_value=True),
            patch(
                "os.remove", side_effect=OSError("Simulated remove error")
            ) as mock_remove,
        ):
            signal_handler(signal.SIGINT, None)
            assert "Ctrl+C" in caplog.text
            assert "Error removing PID file" in caplog.text
            mock_remove.assert_called_with(
                PID_FILE
            )  # Verify os.remove is still called, even if it fails
            # mock_exit.assert_called_with(0) # Optionally assert sys.exit is called
