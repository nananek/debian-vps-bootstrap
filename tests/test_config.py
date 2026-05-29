"""設定の読み書き: default/merge・自前 TOML パーサ・tomllib との一致・load/dump。"""
import os
import sys
import tempfile
import unittest

from _helpers import EXAMPLES, bootstrap


class TestDeepMergeDefault(unittest.TestCase):
    def test_default_config_has_core_sections(self):
        cfg = bootstrap.default_config()
        for sect in ("debian", "target", "network", "host", "user",
                     "ansible", "packages", "firstboot", "ssh"):
            self.assertIn(sect, cfg)

    def test_deep_merge_nested_override(self):
        base = bootstrap.default_config()
        over = {"host": {"hostname": "newhost"}, "ssh": {"permit_root_login": True}}
        out = bootstrap.deep_merge(base, over)
        self.assertEqual(out["host"]["hostname"], "newhost")
        self.assertTrue(out["ssh"]["permit_root_login"])
        # 上書きしていないキーは既定のまま残る
        self.assertEqual(out["debian"]["keymap"], "us")

    def test_deep_merge_replaces_lists(self):
        base = {"packages": {"include": ["a", "b"]}}
        out = bootstrap.deep_merge(base, {"packages": {"include": ["x"]}})
        self.assertEqual(out["packages"]["include"], ["x"])

    def test_deep_merge_does_not_mutate_base(self):
        base = bootstrap.default_config()
        bootstrap.deep_merge(base, {"host": {"hostname": "zzz"}})
        self.assertEqual(base["host"]["hostname"], "debian-vps")


class TestTomlFallbackPieces(unittest.TestCase):
    def test_strip_comment_respects_quotes(self):
        self.assertEqual(bootstrap._toml_strip_comment('a = "x#y"  # c').strip(),
                         'a = "x#y"')
        self.assertEqual(bootstrap._toml_strip_comment("# whole line").strip(), "")

    def test_unescape(self):
        self.assertEqual(bootstrap._toml_unescape(r'"a\nb"'), "a\nb")
        self.assertEqual(bootstrap._toml_unescape(r'"a\"b"'), 'a"b')

    def test_value_types(self):
        self.assertEqual(bootstrap._toml_value("true"), True)
        self.assertEqual(bootstrap._toml_value("false"), False)
        self.assertEqual(bootstrap._toml_value("42"), 42)
        self.assertEqual(bootstrap._toml_value('"hi"'), "hi")
        self.assertEqual(bootstrap._toml_value('["a", "b"]'), ["a", "b"])

    def test_split_array_nested_and_quoted_commas(self):
        self.assertEqual(bootstrap._toml_split_array('"a,b", "c"'),
                         ['"a,b"', ' "c"'])

    def test_fallback_nested_table_and_multiline_array(self):
        text = (
            "[a.b]\n"
            "x = 1\n"
            "list = [\n"
            '  "one",\n'
            '  "two",\n'
            "]\n"
        )
        data = bootstrap._toml_loads_fallback(text)
        self.assertEqual(data["a"]["b"]["x"], 1)
        self.assertEqual(data["a"]["b"]["list"], ["one", "two"])

    def test_fallback_bad_line_raises(self):
        with self.assertRaises(ValueError):
            bootstrap._toml_loads_fallback("not a kv line\n")


def _both_parsers_agree(test, text):
    """tomllib（あれば）と自前 fallback が同じ dict を返すことを検証。"""
    fallback = bootstrap._toml_loads_fallback(text)
    if sys.version_info >= (3, 11):
        import tomllib
        ref = tomllib.loads(text)
        test.assertEqual(fallback, ref)
    return fallback


class TestTomllibParity(unittest.TestCase):
    def test_sample_config_parity(self):
        with open(os.path.join(EXAMPLES, "config.sample.toml")) as fh:
            _both_parsers_agree(self, fh.read())

    def test_minimal_config_parity(self):
        with open(os.path.join(EXAMPLES, "config.minimal.toml")) as fh:
            _both_parsers_agree(self, fh.read())

    def test_dump_config_parity(self):
        cfg = bootstrap.default_config()
        cfg["user"]["password_hash"] = "$6$abc$def"
        cfg["firstboot"]["run"] = ["timedatectl set-ntp true"]
        text = bootstrap.dump_config(cfg)
        _both_parsers_agree(self, text)


class TestLoadDump(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".toml")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_load_fills_defaults(self):
        path = self._write('[host]\nhostname = "h1"\n')
        cfg = bootstrap.load_config(path)
        self.assertEqual(cfg["host"]["hostname"], "h1")
        # 未指定キーは既定で補完される
        self.assertEqual(cfg["debian"]["keymap"], "us")
        self.assertTrue(cfg["ansible"]["enabled"])

    def test_load_missing_path_exits(self):
        with self.assertRaises(SystemExit):
            bootstrap.load_config("/nonexistent/path/to/config.toml")

    def test_dump_then_load_roundtrip(self):
        cfg = bootstrap.default_config()
        cfg["host"]["hostname"] = "round"
        cfg["user"]["ssh_authorized_keys"] = ["ssh-ed25519 AAA test@x"]
        cfg["firstboot"]["apt_packages"] = ["htop", "tmux"]
        path = self._write(bootstrap.dump_config(cfg))
        loaded = bootstrap.load_config(path)
        self.assertEqual(loaded["host"]["hostname"], "round")
        self.assertEqual(loaded["user"]["ssh_authorized_keys"],
                         ["ssh-ed25519 AAA test@x"])
        self.assertEqual(loaded["firstboot"]["apt_packages"], ["htop", "tmux"])


if __name__ == "__main__":
    unittest.main()
