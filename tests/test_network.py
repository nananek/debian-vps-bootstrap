"""ネットワーク解決: netmask 変換・static 正規化・resolve_network の分岐。"""
import unittest

from _helpers import bootstrap


class TestMaskPrefix(unittest.TestCase):
    def test_netmask_to_prefix(self):
        self.assertEqual(bootstrap._netmask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(bootstrap._netmask_to_prefix("255.255.0.0"), 16)
        self.assertEqual(bootstrap._netmask_to_prefix("255.255.255.128"), 25)

    def test_prefix_to_netmask(self):
        self.assertEqual(bootstrap._prefix_to_netmask(24), "255.255.255.0")
        self.assertEqual(bootstrap._prefix_to_netmask(16), "255.255.0.0")
        self.assertEqual(bootstrap._prefix_to_netmask(25), "255.255.255.128")

    def test_roundtrip(self):
        for p in (8, 16, 22, 24, 25, 30, 32):
            self.assertEqual(
                bootstrap._netmask_to_prefix(bootstrap._prefix_to_netmask(p)), p)


class TestFinalizeStatic(unittest.TestCase):
    def test_cidr_in_address(self):
        out = bootstrap._finalize_static({"address": "203.0.113.10/24",
                                          "gateway": "203.0.113.1"})
        self.assertEqual(out["address"], "203.0.113.10")
        self.assertEqual(out["netmask"], "255.255.255.0")

    def test_numeric_netmask_converted(self):
        out = bootstrap._finalize_static({"address": "10.0.0.5", "netmask": "16",
                                          "gateway": "10.0.0.1"})
        self.assertEqual(out["netmask"], "255.255.0.0")

    def test_prefix_field(self):
        out = bootstrap._finalize_static({"address": "10.0.0.5", "prefix": 25,
                                          "gateway": "10.0.0.1"})
        self.assertEqual(out["netmask"], "255.255.255.128")

    def test_nameservers_default_to_gateway(self):
        out = bootstrap._finalize_static({"address": "10.0.0.5/24",
                                          "gateway": "10.0.0.1"})
        self.assertEqual(out["nameservers"], ["10.0.0.1"])

    def test_nameservers_default_when_no_gateway(self):
        out = bootstrap._finalize_static({"address": "10.0.0.5/24"})
        self.assertEqual(out["nameservers"], ["1.1.1.1", "8.8.8.8"])

    def test_explicit_nameservers_kept(self):
        out = bootstrap._finalize_static({"address": "10.0.0.5/24",
                                          "gateway": "10.0.0.1",
                                          "nameservers": ["9.9.9.9"]})
        self.assertEqual(out["nameservers"], ["9.9.9.9"])


class TestResolveNetwork(unittest.TestCase):
    def setUp(self):
        self._orig = bootstrap.detect_network

    def tearDown(self):
        bootstrap.detect_network = self._orig

    def test_dhcp_mode(self):
        cfg = {"network": {"mode": "dhcp"}}
        self.assertEqual(bootstrap.resolve_network(cfg), ("dhcp", {}))

    def test_static_mode(self):
        cfg = {"network": {"mode": "static", "address": "10.0.0.5/24",
                           "gateway": "10.0.0.1", "nameservers": ["1.1.1.1"]}}
        mode, p = bootstrap.resolve_network(cfg)
        self.assertEqual(mode, "static")
        self.assertEqual(p["address"], "10.0.0.5")
        self.assertEqual(p["netmask"], "255.255.255.0")

    def test_auto_detect_success_yields_static(self):
        bootstrap.detect_network = lambda: {
            "interface": "eth0", "address": "192.168.1.2", "netmask": "255.255.255.0",
            "gateway": "192.168.1.1", "nameservers": ["1.1.1.1"]}
        cfg = {"network": {"mode": "auto"}}
        mode, p = bootstrap.resolve_network(cfg)
        self.assertEqual(mode, "static")
        self.assertEqual(p["address"], "192.168.1.2")

    def test_auto_detect_failure_falls_back_to_dhcp(self):
        bootstrap.detect_network = lambda: None
        cfg = {"network": {"mode": "auto"}}
        self.assertEqual(bootstrap.resolve_network(cfg), ("dhcp", {}))

    def test_auto_detect_without_gateway_falls_back_to_dhcp(self):
        bootstrap.detect_network = lambda: {
            "interface": "eth0", "address": "192.168.1.2", "netmask": "255.255.255.0",
            "gateway": "", "nameservers": []}
        cfg = {"network": {"mode": "auto"}}
        self.assertEqual(bootstrap.resolve_network(cfg), ("dhcp", {}))

    def test_invalid_mode_exits(self):
        with self.assertRaises(SystemExit):
            bootstrap.resolve_network({"network": {"mode": "bogus"}})


if __name__ == "__main__":
    unittest.main()
