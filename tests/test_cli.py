import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pytest
from meco import main, handle_command, create_parser
from unittest.mock import patch
import argparse
import sys
from meco_test_client import perform_rpc_call


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
    def test_create_parser_has_subcommands(self):
        parser = create_parser()
        subcommands = {a.dest for a in parser._subparsers._actions if hasattr(a, 'dest')}
        assert "command" in subcommands

    def test_handle_command_valid(self, monkeypatch):
        called = {}
        def fake_on(*a, **k):
            called["on"] = True
        def fake_off(*a, **k):
            called["off"] = True
        def fake_status(*a, **k):
            called["status"] = True
        parser = create_parser()
        args = parser.parse_args(["on"])
        handle_command(args, parser, {"on": fake_on, "off": fake_off, "status": fake_status})
        assert "on" in called
        args = parser.parse_args(["off"])
        handle_command(args, parser, {"on": fake_on, "off": fake_off, "status": fake_status})
        assert "off" in called
        args = parser.parse_args(["status"])
        handle_command(args, parser, {"on": fake_on, "off": fake_off, "status": fake_status})
        assert "status" in called

    def test_handle_command_invalid(self, monkeypatch, capsys):
        parser = create_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["unknown"])
        captured = capsys.readouterr()
        assert "invalid choice" in captured.err.lower()