import sys
import unittest
from unittest.mock import MagicMock, patch

# Adjust path to find modules
sys.path.append("/home/ubuntu/meco-devel")

from meco.network.manager import NetworkManager


class TestRuleGeneration(unittest.TestCase):
    def setUp(self):
        self.nm = NetworkManager()
        # Mock valid port map to avoid scanning
        # 1: Sat on hv1
        # 2: Term on hv1 (Scenario A)
        # 3: Term on hv2 (Scenario B for sat, C for term)
        self.mock_port_map = {
            "1:0": ("hv1", "br-int", 10),
            "2:0": ("hv1", "br-int", 20),
            "3:0": ("hv2", "br-int", 30),
        }
        self.nm.build_port_map = MagicMock(return_value=self.mock_port_map)

    def test_generate_visibility_rules(self):
        topo = {
            "nodes": [
                {"id": 1, "type": "Satellite"},
                {"id": 2, "type": "Terminal"},
                {"id": 3, "type": "Terminal"},
            ],
            "visibility-ground": [
                {
                    "connection": [
                        {"source": 1, "destination": 2},  # Local-Local
                        {"source": 1, "destination": 3},  # Local-Remote
                    ]
                }
            ],
        }

        # Override config if needed, but defaults (br-int, br-tun) should be fine

        rules = self.nm.generate_visibility_rules(topo)

        # Verify result structure
        self.assertIn("hv1", rules)
        self.assertIn("hv2", rules)

        # --- HV1 (Sat Side) ---
        hv1_rules = rules["hv1"]
        self.assertIn("br-int", hv1_rules)
        self.assertIn("br-tun", hv1_rules)

        # Check Scenario A (Sat 1 <-> Term 2) on br-int
        # Sat Port 10, Term Port 20
        # Search for downlink rule: priority=100,in_port=10... actions=...resubmit(,5)
        found_dl_a = any(
            "in_port=10" in r and "resubmit(,5)" in r for r in hv1_rules["br-int"]
        )
        self.assertTrue(found_dl_a, "Failed to find Scenario A downlink rule on HV1")

        # Check Scenario B (Sat 1 <-> Term 3) on br-int
        # Flood to patch-tun
        found_flood_b = any(
            "output:patch-tun" in r and "table=22" in r for r in hv1_rules["br-int"]
        )
        self.assertTrue(
            found_flood_b, "Failed to find Scenario B flood rule (to patch-tun) on HV1"
        )

        # Check Scenario B on br-tun
        # Encap to remote
        # We expect "output:vxlan-hv2" approximately
        found_encap_b = any(
            "output:vxlan-hv2" in r and "table=22" in r for r in hv1_rules["br-tun"]
        )
        self.assertTrue(found_encap_b, "Failed to find Scenario B encap rule on HV1")

        # --- HV2 (Remote Term Side) ---
        hv2_rules = rules["hv2"]
        # Check Scenario C (Term 3 <-> Sat 1) on br-int
        # Term Port 30
        # Uplink to patch-tun
        found_ul_c = any(
            "in_port=30" in r and "output:patch-tun" in r for r in hv2_rules["br-int"]
        )
        self.assertTrue(found_ul_c, "Failed to find Scenario C uplink rule on HV2")

        # Check Scenario C decaps on br-tun
        # incoming from sat (vxlan-hv1 ?)
        # Actually logic generates rule for incoming 'tun_id=...' -> mod_vlan -> output:patch-int
        found_decap_c = any(
            "tun_id=" in r and "output:patch-int" in r for r in hv2_rules["br-tun"]
        )
        self.assertTrue(found_decap_c, "Failed to find Scenario C decap rule on HV2")


if __name__ == "__main__":
    unittest.main()
