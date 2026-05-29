"""その他のユーティリティ: arch 解決・ロケール判定・パスワードハッシュ。"""
import shutil
import unittest

from _helpers import bootstrap


class TestResolveArch(unittest.TestCase):
    def setUp(self):
        self._orig = bootstrap.platform.machine

    def tearDown(self):
        bootstrap.platform.machine = self._orig

    def test_explicit_values(self):
        self.assertEqual(bootstrap.resolve_arch("amd64"), "amd64")
        self.assertEqual(bootstrap.resolve_arch("arm64"), "arm64")

    def test_unsupported_explicit_exits(self):
        with self.assertRaises(SystemExit):
            bootstrap.resolve_arch("mips")

    def test_auto_x86_64(self):
        bootstrap.platform.machine = lambda: "x86_64"
        self.assertEqual(bootstrap.resolve_arch("auto"), "amd64")

    def test_auto_aarch64(self):
        bootstrap.platform.machine = lambda: "aarch64"
        self.assertEqual(bootstrap.resolve_arch("auto"), "arm64")

    def test_auto_unknown_exits(self):
        bootstrap.platform.machine = lambda: "sparc64"
        with self.assertRaises(SystemExit):
            bootstrap.resolve_arch("auto")


class TestMeaningfulLocale(unittest.TestCase):
    def test_rejects_non_regional(self):
        for v in ("", "C", "POSIX", "C.UTF-8", "c.utf-8", "en_US"):
            self.assertFalse(bootstrap._meaningful_locale(v), v)

    def test_accepts_regional(self):
        for v in ("en_US.UTF-8", "ja_JP.UTF-8", "de_DE.UTF-8"):
            self.assertTrue(bootstrap._meaningful_locale(v), v)


def _crypt_available():
    try:
        import crypt  # noqa: F401  (3.13 で削除済み。存在確認のためだけに import)
        return True
    except Exception:
        return False


_CAN_HASH = shutil.which("openssl") is not None or _crypt_available()


@unittest.skipUnless(_CAN_HASH, "crypt モジュールも openssl も無い環境ではスキップ")
class TestHashPassword(unittest.TestCase):
    def test_returns_sha512_crypt(self):
        h = bootstrap.hash_password("s3cret")
        self.assertTrue(h.startswith("$6$"))
        self.assertGreater(len(h), 20)


if __name__ == "__main__":
    unittest.main()
