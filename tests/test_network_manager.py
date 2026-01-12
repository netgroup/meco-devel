import pytest
from unittest.mock import MagicMock, patch
from meco.network.manager import NetworkManager


@pytest.fixture
def net_manager():
    return NetworkManager()


@pytest.fixture
def mock_port_map():
    return {
        "1:0": ("hv1", "br-int", 1),  # Satellite 1 on hv1
        "2:0": ("hv1", "br-int", 2),  # Terminal 2 on hv1
        "3:0": ("hv1", "br-int", 3),  # Terminal 3 on hv1
        "4:0": ("hv2", "br-int", 4),  # Terminal 4 on hv2 (different HV)
    }


@pytest.fixture
def sample_topology():
    return {
        "nodes": [
            {"id": "1", "type": "Satellite"},
            {"id": "2", "type": "Terminal"},
            {"id": "3", "type": "Terminal"},
            {"id": "4", "type": "Terminal"},
        ],
        "visibility-ground": [
            {
                "time": 0,
                "connection": [
                    {"source": 1, "destination": 2},  # Valid pair on hv1
                    {"source": 1, "destination": 3},  # Valid pair on hv1
                    {"source": 1, "destination": 4},  # Invalid cross-domain
                ],
            }
        ],
    }


class TestNetworkManagerOF13:
    @patch("meco.network.manager.NetworkManager.build_port_map")
    def test_generate_visibility_rules(
        self, mock_build_map, net_manager, mock_port_map, sample_topology
    ):
        # Setup mock
        mock_build_map.return_value = mock_port_map

        # Run
        rules = net_manager.generate_visibility_rules(sample_topology, vlan_id=10)

        # Assertions
        assert "hv1" in rules
        assert "br-int" in rules["hv1"]

        flow_list = rules["hv1"]["br-int"]

        # 1. Check Baseline rules (ARP, tables 22, 20, 5, 9)
        # ARP for 1, 2, 3
        # Satellite 1 (port 1) -> outputs 2, 3
        # 1. Check Table 0 Downlink (Sat -> Table 5)
        # Satellite 1 (port 1) -> Table 0 -> resubmit(5)
        # Assuming regex "in_port=1.*resubmit\(,5\)"
        sat_down = next(
            (
                f
                for f in flow_list
                if "in_port=1" in f and "table=0" in f and "resubmit(,5)" in f
            ),
            None,
        )
        assert sat_down

        # 2. Check Table 22 Distribution (Sat -> Terminals)
        # Matches in_port=1 -> output:2, output:3 (and patch-tun for remote)
        sat_dist = next(
            (f for f in flow_list if "in_port=1" in f and "table=22" in f), None
        )
        assert sat_dist
        assert "output:2" in sat_dist
        assert "output:3" in sat_dist
        assert "output:patch-tun" in sat_dist

        # 3. Check Terminal Uplink (Term -> Sat)
        # Terminal 2 (port 2) -> Table 0 -> ... output:1
        term_up = next(
            (
                f
                for f in flow_list
                if "in_port=2" in f and "table=0" in f and "output:1" in f
            ),
            None,
        )
        assert term_up

        term2_up = next(
            (
                f
                for f in flow_list
                if "in_port=2" in f and "cookie" in f and "0x102" not in f
            ),
            None,
        )
        assert term2_up
        assert "output:1" in term2_up  # to sat
        assert "resubmit(,9)" in term2_up  # learn

        # Term 3
        term3_up = next(
            (f for f in flow_list if "in_port=3" in f and "cookie" in f), None
        )
        assert term3_up

    @patch("meco.network.manager.NetworkManager.build_port_map")
    def test_generate_visibility_rules_no_links(
        self, mock_build_map, net_manager, mock_port_map
    ):
        mock_build_map.return_value = mock_port_map
        topo = {"nodes": [], "visibility-ground": []}

        rules = net_manager.generate_visibility_rules(topo)
        assert rules == {}  # No links

    @patch("meco.network.manager.NetworkManager.build_port_map")
    def test_generate_visibility_rules_cross_domain(
        self, mock_build_map, net_manager, mock_port_map, sample_topology
    ):
        mock_build_map.return_value = mock_port_map

        # hv2 SHOULD now have rules (Scenario C for Terminal 4)
        rules = net_manager.generate_visibility_rules(sample_topology)

        assert "hv2" in rules
        assert "br-int" in rules["hv2"]
        # Term 4 is port 4. Should check for uplink to patch-tun
        assert any(
            "in_port=4" in f and "output:patch-tun" in f for f in rules["hv2"]["br-int"]
        )
