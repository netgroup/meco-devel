import pytest
from unittest.mock import MagicMock, patch
from meco.emulation.lifecycle import LifecycleManager
from meco.emulation.scheduler import Scheduler


@pytest.fixture
def lifecycle():
    with (
        patch("meco.emulation.lifecycle.IncusClient"),
        patch("meco.emulation.lifecycle.local_incus_client"),
        patch("meco.emulation.lifecycle.CONFIG") as mock_config,
    ):
        # Mock config to have some hypervisors
        mock_config.get.return_value = {
            "hv1": {"ip": "1.2.3.4"},
            "hv2": {"ip": "5.6.7.8"},
        }
        lm = LifecycleManager()
        # Ensure clients are initialized (hv1, hv2, local)
        # Lifecycle always adds local
        return lm


def test_scheduler_round_robin():
    """Verify Scheduler logic independently."""
    instances = ["node1", "node2", "node3", "node4"]
    hvs = ["hv1", "hv2"]
    assignments = Scheduler.schedule(instances, hvs)

    assert assignments["node1"] == "hv1"
    assert assignments["node2"] == "hv2"
    assert assignments["node3"] == "hv1"
    assert assignments["node4"] == "hv2"


def test_lifecycle_scheduler_integration(lifecycle):
    """Verify LifecycleManager uses Scheduler for unassigned nodes."""

    # Setup data
    topology_data = {
        "nodes": [
            {"id": "1", "type": "Satellite"},  # Unassigned
            {"id": "2", "type": "Terminal"},  # Unassigned
            {"id": "3", "type": "GroundStation"},  # Unassigned
        ]
    }

    # Mock _launch_node to avoid actual logic
    lifecycle._launch_node = MagicMock()
    lifecycle._wait_for_deployment = MagicMock()

    # Verify initial state: no explicit locations
    assert not lifecycle.node_locations

    # Run deployment
    lifecycle._deploy_nodes(topology_data)

    # Check that locations were populated
    assert len(lifecycle.node_locations) == 3
    assert "1-Satellite" in lifecycle.node_locations
    assert "2-Terminal" in lifecycle.node_locations
    assert "3-GroundStation" in lifecycle.node_locations

    # Check distribution (Round Robin across hv1, hv2 ONLY - local excluded)
    # Available clients keys: local, hv1, hv2
    # Remote only: hv1, hv2
    # Sorted: hv1, hv2
    # 1-Sat -> hv1
    # 2-Term -> hv2
    # 3-GS -> hv1 (wrapped around)

    assert lifecycle.node_locations["1-Satellite"] == "hv1"
    assert lifecycle.node_locations["2-Terminal"] == "hv2"
    assert lifecycle.node_locations["3-GroundStation"] == "hv1"

    # Check that _launch_node was called 3 times
    assert lifecycle._launch_node.call_count == 3


def test_lifecycle_mixed_assignment(lifecycle):
    """Verify explicit assignment is respected and mixed with dynamic."""

    # Explicitly assign one node
    lifecycle.node_locations["1-Satellite"] = "hv2"

    topology_data = {
        "nodes": [
            {"id": "1", "type": "Satellite"},  # Assigned to hv2
            {"id": "2", "type": "Terminal"},  # Unassigned
        ]
    }

    lifecycle._launch_node = MagicMock()
    lifecycle._wait_for_deployment = MagicMock()

    lifecycle._deploy_nodes(topology_data)

    # 1-Sat should still be hv2
    assert lifecycle.node_locations["1-Satellite"] == "hv2"

    # 2-Terminal should be scheduled.
    # Scheduler sees keys: hv1, hv2. (Local excluded)
    # First unassigned is 2-Terminal -> hv1
    assert lifecycle.node_locations["2-Terminal"] == "hv1"


def test_lifecycle_fallback_to_local():
    """Verify fallback to local if no remotes exist."""
    with (
        patch("meco.emulation.lifecycle.IncusClient"),
        patch("meco.emulation.lifecycle.local_incus_client"),
        patch("meco.emulation.lifecycle.CONFIG") as mock_config,
    ):
        mock_config.get.return_value = {}  # No remotes
        lm = LifecycleManager()

        topology_data = {"nodes": [{"id": "1", "type": "Sat"}]}
        lm._launch_node = MagicMock()
        lm._wait_for_deployment = MagicMock()

        lm._deploy_nodes(topology_data)

        # Should be local (None)
        assert lm.node_locations["1-Sat"] is None
