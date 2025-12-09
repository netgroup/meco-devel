
import pytest
from unittest.mock import MagicMock, patch
from network.manager import NetworkManager

@pytest.fixture
def net_manager():
    return NetworkManager()

@pytest.fixture
def mock_port_map():
    return {
        "1:0": ("hv1", "br-int", 1),   # Satellite 1 on hv1
        "2:0": ("hv1", "br-int", 2),   # Terminal 2 on hv1
        "3:0": ("hv1", "br-int", 3),   # Terminal 3 on hv1
        "4:0": ("hv2", "br-int", 4),   # Terminal 4 on hv2 (different HV)
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
                    {"source": 1, "destination": 2}, # Valid pair on hv1
                    {"source": 1, "destination": 3}, # Valid pair on hv1
                    {"source": 1, "destination": 4}, # Invalid cross-domain
                ]
            }
        ]
    }

class TestNetworkManagerOF13:
    
    @patch("network.manager.NetworkManager.build_port_map")
    def test_generate_of13_rules(self, mock_build_map, net_manager, mock_port_map, sample_topology):
        # Setup mock
        mock_build_map.return_value = mock_port_map
        
        # Run
        rules = net_manager.generate_of13_rules_from_visibility(sample_topology, vlan_id=10)
        
        # Assertions
        assert "hv1" in rules
        assert "br-int" in rules["hv1"]
        
        flow_list = rules["hv1"]["br-int"]
        
        # 1. Check Baseline rules (ARP, tables 22, 20, 5, 9)
        # ARP for 1, 2, 3
        # Satellite 1 (port 1) -> outputs 2, 3
        arp_sat = next((f for f in flow_list if "in_port=1,arp" in f), None)
        assert arp_sat
        assert "output:2" in arp_sat and "output:3" in arp_sat
        
        # Learn rule
        assert any("table=9" in f and "learn(" in f for f in flow_list)
        
        # 2. Check Satellite Flows (Table 0 -> 20)
        # Port 1 (Sat)
        sat_ingress = next((f for f in flow_list if "in_port=1" in f and "cookie=0x102" in f), None)
        assert sat_ingress
        assert "load:0xa->NXM_OF_VLAN_TCI[]" in sat_ingress # VLAN 10
        
        # Downlink (Table 5 -> output terminals)
        downlink = next((f for f in flow_list if "table=5" in f and "cookie=0x103" in f), None)
        assert downlink
        assert "output:2" in downlink
        assert "output:3" in downlink
        
        # 3. Check Terminal Flows (Uplink: Term -> Sat)
        # Term 2
        term2_up = next((f for f in flow_list if "in_port=2" in f and "cookie" in f and "0x102" not in f), None)
        assert term2_up
        assert "output:1" in term2_up # to sat
        assert "resubmit(,9)" in term2_up # learn
        
        # Term 3
        term3_up = next((f for f in flow_list if "in_port=3" in f and "cookie" in f), None)
        assert term3_up
        
    @patch("network.manager.NetworkManager.build_port_map")
    def test_generate_of13_rules_no_links(self, mock_build_map, net_manager, mock_port_map):
        mock_build_map.return_value = mock_port_map
        topo = {"nodes": [], "visibility-ground": []}
        
        rules = net_manager.generate_of13_rules_from_visibility(topo)
        assert rules == {} # No links

    @patch("network.manager.NetworkManager.build_port_map")
    def test_generate_of13_rules_cross_domain_skip(self, mock_build_map, net_manager, mock_port_map, sample_topology):
        mock_build_map.return_value = mock_port_map
        
        # hv2 should NOT have rules because sat 1 is on hv1, and term 4 is on hv2
        rules = net_manager.generate_of13_rules_from_visibility(sample_topology)
        
        assert "hv2" not in rules
