"""生成ロジック: effective_packages・validate・preseed・firstboot・sshd・service。"""
import unittest

from _helpers import bootstrap


def _cfg(**over):
    cfg = bootstrap.default_config()
    cfg["user"]["ssh_authorized_keys"] = ["ssh-ed25519 AAA user@x"]
    cfg["ansible"]["ssh_authorized_keys"] = ["ssh-ed25519 BBB ansible@x"]
    return bootstrap.deep_merge(cfg, over)


class TestEffectivePackages(unittest.TestCase):
    def test_python3_added_when_ansible_enabled(self):
        cfg = _cfg(ansible={"enabled": True}, packages={"include": ["openssh-server"]})
        self.assertIn("python3", bootstrap.effective_packages(cfg))

    def test_python3_not_duplicated(self):
        cfg = _cfg(ansible={"enabled": True},
                   packages={"include": ["openssh-server", "python3"]})
        self.assertEqual(bootstrap.effective_packages(cfg).count("python3"), 1)

    def test_no_python3_when_ansible_disabled(self):
        cfg = _cfg(ansible={"enabled": False}, packages={"include": ["openssh-server"]})
        self.assertNotIn("python3", bootstrap.effective_packages(cfg))


class TestValidateConfig(unittest.TestCase):
    def test_valid_config_no_errors(self):
        self.assertEqual(bootstrap.validate_config(_cfg()), [])

    def test_empty_user_name(self):
        errs = bootstrap.validate_config(_cfg(user={"name": ""}))
        self.assertTrue(any("user.name" in e for e in errs))

    def test_empty_user_keys(self):
        cfg = _cfg()
        cfg["user"]["ssh_authorized_keys"] = []
        self.assertTrue(any("ssh_authorized_keys" in e
                            for e in bootstrap.validate_config(cfg)))

    def test_bad_password_hash(self):
        errs = bootstrap.validate_config(_cfg(user={"password_hash": "plaintext"}))
        self.assertTrue(any("password_hash" in e for e in errs))

    def test_prompt_password_hash_ok(self):
        self.assertEqual(bootstrap.validate_config(_cfg(user={"password_hash": "prompt"})), [])

    def test_ansible_empty_keys(self):
        cfg = _cfg()
        cfg["ansible"]["ssh_authorized_keys"] = []
        self.assertTrue(any("ansible.ssh_authorized_keys" in e
                            for e in bootstrap.validate_config(cfg)))

    def test_disabled_ansible_skips_its_checks(self):
        cfg = _cfg(ansible={"enabled": False})
        cfg["ansible"]["ssh_authorized_keys"] = []
        cfg["ansible"]["password_hash"] = "garbage"
        self.assertEqual(bootstrap.validate_config(cfg), [])

    def test_static_requires_fields(self):
        cfg = _cfg(network={"mode": "static"})
        errs = bootstrap.validate_config(cfg)
        self.assertTrue(any("address" in e for e in errs))
        self.assertTrue(any("gateway" in e for e in errs))
        self.assertTrue(any("netmask" in e or "prefix" in e for e in errs))

    def test_static_with_cidr_address_ok_for_netmask(self):
        cfg = _cfg(network={"mode": "static", "address": "10.0.0.5/24",
                            "gateway": "10.0.0.1"})
        errs = bootstrap.validate_config(cfg)
        self.assertFalse(any("netmask" in e for e in errs))

    def test_invalid_network_mode(self):
        errs = bootstrap.validate_config(_cfg(network={"mode": "weird"}))
        self.assertTrue(any("network.mode" in e for e in errs))


class TestBuildPreseed(unittest.TestCase):
    def _preseed(self, cfg, disk="/dev/vda", net=("dhcp", {})):
        return bootstrap.build_preseed(cfg, disk, "$6$h$h", net).decode()

    def test_contains_disk_locale_keymap_user(self):
        cfg = _cfg(debian={"locale": "ja_JP.UTF-8", "keymap": "jp"},
                   user={"name": "alice"})
        out = self._preseed(cfg, disk="/dev/sda")
        self.assertIn("partman-auto/disk string /dev/sda", out)
        self.assertIn("grub-installer/bootdev string /dev/sda", out)
        self.assertIn("locale string ja_JP.UTF-8", out)
        self.assertIn("xkb-keymap select jp", out)
        self.assertIn("passwd/username string alice", out)
        self.assertIn("usermod -aG sudo alice", out)

    def test_static_netcfg(self):
        net = ("static", {"address": "10.0.0.5", "netmask": "255.255.255.0",
                          "gateway": "10.0.0.1", "nameservers": ["1.1.1.1", "8.8.8.8"]})
        out = self._preseed(_cfg(), net=net)
        self.assertIn("netcfg/disable_autoconfig boolean true", out)
        self.assertIn("netcfg/get_ipaddress string 10.0.0.5", out)
        self.assertIn("netcfg/get_nameservers string 1.1.1.1 8.8.8.8", out)

    def test_dhcp_netcfg(self):
        out = self._preseed(_cfg(), net=("dhcp", {}))
        self.assertNotIn("disable_autoconfig", out)
        self.assertIn("netcfg/choose_interface select auto", out)

    def test_include_lists_effective_packages(self):
        cfg = _cfg(ansible={"enabled": True}, packages={"include": ["openssh-server"]})
        out = self._preseed(cfg)
        self.assertIn("pkgsel/include string", out)
        self.assertIn("python3", out)


class TestBuildFirstboot(unittest.TestCase):
    def test_full_config_blocks_present(self):
        cfg = _cfg(firstboot={"docker": True, "tailscale": True,
                              "apt_packages": ["htop"], "run": ["echo hi"]},
                   ansible={"enabled": True, "name": "ansuser"},
                   user={"name": "alice"})
        out = bootstrap.build_firstboot(cfg)
        self.assertIn("docker-ce", out)
        self.assertIn("tailscale.com/install.sh", out)
        self.assertIn("apt-get install -y htop", out)
        self.assertIn("echo hi", out)
        self.assertIn('usermod -aG docker "alice"', out)
        self.assertIn('usermod -aG docker "ansuser"', out)
        # プレースホルダが残っていないこと
        self.assertNotIn("@@USER@@", out)
        self.assertNotIn("@@ANSIBLE@@", out)
        self.assertNotIn("@@ANSIBLE_DOCKER@@", out)

    def test_minimal_config_omits_blocks(self):
        cfg = _cfg(firstboot={"docker": False, "tailscale": False,
                              "apt_packages": [], "run": []},
                   ansible={"enabled": False})
        out = bootstrap.build_firstboot(cfg)
        self.assertNotIn("docker-ce", out)
        self.assertNotIn("tailscale", out)
        self.assertNotIn("ANSIBLE_PW_HASH", out)
        self.assertNotIn("@@", out)

    def test_main_user_keys_block_uses_username(self):
        out = bootstrap.build_firstboot(_cfg(user={"name": "bob"}))
        self.assertIn('id "bob"', out)
        self.assertIn("/home/bob/.ssh", out)


class TestSshdAndService(unittest.TestCase):
    def test_sshd_hardening_no(self):
        out = bootstrap.build_sshd_hardening(
            _cfg(ssh={"password_authentication": False, "permit_root_login": False})).decode()
        self.assertIn("PasswordAuthentication no", out)
        self.assertIn("PermitRootLogin no", out)

    def test_sshd_hardening_yes(self):
        out = bootstrap.build_sshd_hardening(
            _cfg(ssh={"password_authentication": True, "permit_root_login": True})).decode()
        self.assertIn("PasswordAuthentication yes", out)
        self.assertIn("PermitRootLogin yes", out)

    def test_service_unit(self):
        out = bootstrap.build_firstboot_service().decode()
        self.assertIn("Type=oneshot", out)
        self.assertIn("ExecStart=/var/lib/bootstrap/firstboot.sh", out)
        self.assertIn("WantedBy=multi-user.target", out)


if __name__ == "__main__":
    unittest.main()
