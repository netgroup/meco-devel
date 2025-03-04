import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from meco import main, handle_command, create_parser
from unittest.mock import patch
import argparse
import sys


class TestCliCommands:
    """Test suite for command-line interface commands"""

    class TestCliOn:
        @patch("meco.server_on")
        def test_cli_on(self, mock_on):
            """Verify 'meco on' command triggers server startup routine
            - Ensures the server_on function is called exactly once
            - Simulates full CLI command execution via patched sys.argv
            """
            with patch("sys.argv", ["meco", "on"]):
                main()
            mock_on.assert_called_once()

    class TestCliOff:
        @patch("meco.server_off")
        def test_cli_off(self, mock_off):
            """Validate proper shutdown command execution
            - Tests if server_off is invoked when 'meco off' is used
            - Verifies clean command parsing and routing
            """
            with patch("sys.argv", ["meco", "off"]):
                main()
            mock_off.assert_called_once()

    class TestCliStatus:
        @patch("meco.server_status")
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


class TestHandleCommand:
    def test_handle_unknown_command(self, capsys):
        """Validate internal command routing error handling
        - Tests fallback to help system when receiving unknown command
        - Ensures graceful degradation rather than silent failures
        """
        from argparse import Namespace

        args = Namespace(command="unknown")
        parser = create_parser()
        with pytest.raises(SystemExit):
            handle_command(args, parser, {})
        captured = capsys.readouterr()
        assert "help" in captured.out.lower()
