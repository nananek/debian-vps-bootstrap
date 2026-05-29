"""cpio (newc) 操作: エントリ生成・TRAILER 探索・initrd 再構築のラウンドトリップ。"""
import gzip
import unittest

from _helpers import bootstrap


def _parse_cpio(cpio):
    """newc cpio を {name: (mode, data)} に展開する（テスト用の素朴なパーサ）。"""
    out, off, n = {}, 0, len(cpio)
    while off < n:
        magic = cpio[off:off + 6]
        if magic not in (b"070701", b"070702"):
            raise ValueError(f"bad magic @ {off}")
        mode = int(cpio[off + 6 + 1 * 8: off + 6 + 2 * 8], 16)
        filesize = int(cpio[off + 6 + 6 * 8: off + 6 + 7 * 8], 16)
        namesize = int(cpio[off + 6 + 11 * 8: off + 6 + 12 * 8], 16)
        name = cpio[off + 110: off + 110 + namesize - 1].decode()
        hdrname = 110 + namesize
        data_start = off + hdrname + ((-hdrname) % 4)
        data = cpio[data_start:data_start + filesize]
        if name == "TRAILER!!!":
            break
        out[name] = (mode, data)
        off = data_start + filesize + ((-filesize) % 4)
    return out


def _make_cpio(entries):
    """{name: data} から最小 cpio + TRAILER を作る。"""
    blob = bytearray()
    ino = 1
    for name, data in entries.items():
        blob += bootstrap._cpio_entry(name, 0o100644, data, ino)
        ino += 1
    blob += bootstrap._cpio_entry("TRAILER!!!", 0, b"", 0)
    return bytes(blob)


class TestCpioEntry(unittest.TestCase):
    def test_header_and_padding_aligned(self):
        entry = bootstrap._cpio_entry("foo", 0o100644, b"abc", 5)
        self.assertTrue(entry.startswith(b"070701"))
        # ヘッダ+名前部とデータ部はそれぞれ 4 バイト境界に揃う
        self.assertEqual(len(entry) % 4, 0)

    def test_roundtrip_single_entry(self):
        cpio = _make_cpio({"hello.txt": b"world"})
        parsed = _parse_cpio(cpio)
        self.assertEqual(parsed["hello.txt"][1], b"world")


class TestFindTrailer(unittest.TestCase):
    def test_finds_trailer(self):
        cpio = _make_cpio({"a": b"1", "bb": b"22"})
        off = bootstrap._find_trailer(cpio)
        self.assertEqual(cpio[off:off + 6], b"070701")
        # TRAILER!!! が当該オフセットに名前として入っている
        self.assertIn(b"TRAILER!!!", cpio[off:off + 200])

    def test_bad_magic_raises(self):
        with self.assertRaises(ValueError):
            bootstrap._find_trailer(b"NOTCPIO" + b"\x00" * 200)

    def test_missing_trailer_raises(self):
        cpio = _make_cpio({"a": b"1"})
        # TRAILER エントリを切り落とす
        off = bootstrap._find_trailer(cpio)
        with self.assertRaises(ValueError):
            bootstrap._find_trailer(cpio[:off])


class TestBuildNewInitrd(unittest.TestCase):
    def test_injects_preseed_and_payload(self):
        orig = gzip.compress(_make_cpio({"init": b"#!/bin/sh\n"}))
        preseed = b"d-i foo bar\n"
        payload = {
            "payload/firstboot.sh": b"#!/bin/bash\necho hi\n",
            "payload/bootstrap.env": b"SECRET=1\n",
            "payload/user_authorized_keys": b"ssh-ed25519 AAA\n",
        }
        new_gz = bootstrap.build_new_initrd(orig, preseed, payload)
        parsed = _parse_cpio(gzip.decompress(new_gz))

        # 元エントリが残っている
        self.assertIn("init", parsed)
        # preseed と payload が注入されている
        self.assertEqual(parsed["preseed.cfg"][1], preseed)
        for name, data in payload.items():
            self.assertEqual(parsed[name][1], data)
        # 実行属性: .sh は 0755、bootstrap.env(秘密) は 0600
        self.assertEqual(parsed["payload/firstboot.sh"][0] & 0o777, 0o755)
        self.assertEqual(parsed["payload/bootstrap.env"][0] & 0o777, 0o600)
        self.assertEqual(parsed["payload/user_authorized_keys"][0] & 0o777, 0o644)

    def test_result_is_gzip(self):
        orig = gzip.compress(_make_cpio({"init": b"x"}))
        new_gz = bootstrap.build_new_initrd(orig, b"p", {})
        # gzip マジック
        self.assertEqual(new_gz[:2], b"\x1f\x8b")
        # 展開後に TRAILER を見つけられる（壊れていない）
        bootstrap._find_trailer(gzip.decompress(new_gz))


if __name__ == "__main__":
    unittest.main()
