import pytest
from unittest.mock import MagicMock, patch
from meco.network.manager import NetworkManager
import time


@pytest.fixture
def net_manager():
    return NetworkManager()


@patch("meco.network.manager.NetworkManager._scan_ports")
def test_build_port_map_immediate_success(mock_scan, net_manager):
    """Test that it returns immediately if expected count is met."""
    expected = {"1:0": ("hv1", "br-int", 1), "2:0": ("hv1", "br-int", 2)}
    mock_scan.return_value = expected

    start = time.time()
    result = net_manager.build_port_map(expected_count=2, timeout=5)
    elapsed = time.time() - start

    assert result == expected
    assert mock_scan.call_count >= 1
    assert elapsed < 2  # Should be fast


@patch("meco.network.manager.NetworkManager._scan_ports")
def test_build_port_map_retry_success(mock_scan, net_manager):
    """Test that it retries until expected count is met."""
    # First call: empty, Second: 1 node, Third: 2 nodes (success)
    mock_scan.side_effect = [
        {},
        {"1:0": ("hv1", "br-int", 1)},
        {"1:0": ("hv1", "br-int", 1), "2:0": ("hv1", "br-int", 2)},
    ]

    start = time.time()
    result = net_manager.build_port_map(expected_count=2, timeout=10)

    assert len(result) == 2
    assert mock_scan.call_count == 3


@patch("meco.network.manager.NetworkManager._scan_ports")
def test_build_port_map_timeout(mock_scan, net_manager):
    """Test that it returns what it has after timeout."""
    mock_scan.return_value = {"1:0": ("hv1", "br-int", 1)}

    start = time.time()
    result = net_manager.build_port_map(expected_count=2, timeout=2)
    elapsed = time.time() - start

    assert len(result) == 1
    assert elapsed >= 2
