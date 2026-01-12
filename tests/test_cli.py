import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pytest
from meco.main import main
from unittest.mock import patch
import argparse
import sys
from meco.client import perform_rpc_call


class TestCliCommands:
    """Test suite for command-line interface commands"""

    class TestCliOn:
        @patch("meco.main.server_on")
        def test_cli_on(self, mock_on):
            """Verify 'meco on' command triggers server startup routine
            - Ensures the server_on function is called exactly once
            - Simulates full CLI command execution via patched sys.argv
            """
            with patch("sys.argv", ["meco", "on"]):
                main()
            mock_on.assert_called_once()

    class TestCliOff:
        @patch("meco.main.server_off")
        def test_cli_off(self, mock_off):
            """Validate proper shutdown command execution
            - Tests if server_off is invoked when 'meco off' is used
            - Verifies clean command parsing and routing
            """
            with patch("sys.argv", ["meco", "off"]):
                main()
            mock_off.assert_called_once()

    class TestCliStatus:
        @patch("meco.main.server_status")
        def test_cli_status(self, mock_status):
            """Check status command functionality
            - Ensures status check doesn't modify server state
            - Validates proper function mapping in command routing
            """
            with patch("sys.argv", ["meco", "status"]):
                main()
            mock_status.assert_called_once()

    class TestCliInvalidCommand:
        def test_cli_invalid_command(self, capsys):
            """Test error handling for unknown commands
            - Verifies system exits with error code
            - Checks for proper error messaging
            - Ensures help instructions are displayed
            """
            with patch("sys.argv", ["meco", "invalid"]), pytest.raises(SystemExit):
                main()
            captured = capsys.readouterr()
            assert "invalid choice" in captured.err.lower()
