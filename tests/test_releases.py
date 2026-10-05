import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from telegram_bridge import releases


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


def native_bytes(native="x86_64"):
    result = bytearray(64)
    result[:6] = b"\x7fELF\x02\x01"
    result[18:20] = (62 if native == "x86_64" else 183).to_bytes(2, "little")
    return bytes(result) + b"unit test fixture only"


def archive_bytes(extra=None, native="x86_64", selected=True, payload=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        if selected:
            member = tarfile.TarInfo("omnirush/resources/sidecars/opencode-" + native + "-unknown-linux-gnu")
            body = native_bytes(native) if payload is None else payload
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        # An unrelated installer must not get extracted or executed.
        other = tarfile.TarInfo("omnirush/install.sh")
        other.size = 20
        archive.addfile(other, io.BytesIO(b"DO NOT EXECUTE THIS!!"))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isfile() else None)
    return output.getvalue()


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "private/runtimes"
        self.version = "3.1.1"
        self.asset_name = "omnirush-linux-x64-3.1.1.tar.gz"
        self.url = "https://github.com/omnirush-ai/omnirush-gui/releases/download/v3.1.1/" + self.asset_name
        self.architecture = patch.object(releases, "_architecture", return_value=("x64", "x86_64"))
        self.architecture.start()
        self.location = patch.object(releases, "RUNTIME_ROOT", self.root)
        self.location.start()

    def tearDown(self):
        self.location.stop()
        self.architecture.stop()
        self.temporary.cleanup()

    def fixture(self, body=None):
        body = archive_bytes() if body is None else body
        digest = hashlib.sha256(body).hexdigest()
        metadata = {"tag_name": "v3.1.1", "draft": False, "prerelease": False,
                    "published_at": "2026-01-01T00:00:00Z", "assets": [{
                        "name": self.asset_name, "size": len(body), "digest": "sha256:" + digest,
                        "browser_download_url": self.url}]}
        return body, digest, metadata

    def install_fixture(self, body=None, progress=None):
        body, digest, metadata = self.fixture(body)
        pins = {"3.1.1": {"x64": (len(body), digest)}}
        opener = Mock(side_effect=[Response(json.dumps(metadata).encode()),
                                  Response(body, {"Content-Length": str(len(body))})])
        with patch.object(releases, "PINNED_ASSETS", pins), patch.object(releases, "_open", opener):
            path = releases.ensure_runtime(progress=progress)
            again = releases.ensure_runtime()
            self.assertEqual(path, again)
        return path, opener, pins

    def test_atomic_private_install_exact_member_and_no_repeat_network(self):
        progress = Mock()
        path, opener, pins = self.install_fixture(progress=progress)
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.read_bytes(), native_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((path.parent / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "install.sh").exists())
        self.assertFalse(list(self.root.glob(".install-*")))
        self.assertEqual(opener.call_count, 2)
        progress.assert_called()
        self.assertEqual(progress.call_args.args[0], progress.call_args.args[1])
        with patch.object(releases, "PINNED_ASSETS", pins):
            self.assertEqual(releases.installed_runtime(), path)

    def test_previous_version_is_preserved(self):
        previous = self.root / "3.0.0/x64/old-runtime"
        previous.parent.mkdir(parents=True)
        previous.write_bytes(b"existing previous version")
        path, _, _ = self.install_fixture()
        self.assertTrue(path.exists())
        self.assertEqual(previous.read_bytes(), b"existing previous version")

    def test_bad_download_digest_does_not_extract_or_install(self):
        body, digest, metadata = self.fixture()
        corrupted = bytes([body[0] ^ 1]) + body[1:]
        with patch.object(releases, "PINNED_ASSETS", {"3.1.1": {"x64": (len(body), digest)}}), \
                patch.object(releases, "_open", side_effect=[Response(json.dumps(metadata).encode()), Response(corrupted)]), \
                patch.object(releases, "_extract") as extract, self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()
        extract.assert_not_called()
        self.assertFalse((self.root / "3.1.1/x64").exists())
        self.assertFalse(list(self.root.glob(".install-*")))

    def test_missing_api_digest_fails_before_download(self):
        body, digest, metadata = self.fixture()
        metadata["assets"][0]["digest"] = None
        with patch.object(releases, "PINNED_ASSETS", {"3.1.1": {"x64": (len(body), digest)}}), \
                patch.object(releases, "_open", return_value=Response(json.dumps(metadata).encode())) as opener, \
                self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()
        opener.assert_called_once()

    def test_size_cap_short_and_oversize_download(self):
        for response in (Response(b"short"), Response(b"too long"), Response(b"abc", {"Content-Length": "999"})):
            with self.subTest(response=response), patch.object(releases, "_open", return_value=response), \
                    tempfile.TemporaryDirectory() as directory, self.assertRaises(releases.ReleaseError):
                releases._download({"browser_download_url": self.url}, Path(directory) / "archive", 3,
                                   hashlib.sha256(b"abc").hexdigest(), None)

    def test_unsafe_archive_traversal_links_and_duplicate_are_rejected(self):
        for kind in ("../outside", "/absolute", "symlink", "hardlink", "duplicate"):
            member = tarfile.TarInfo(kind)
            if kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "elsewhere"
            elif kind == "hardlink":
                member.type = tarfile.LNKTYPE
                member.linkname = "elsewhere"
            elif kind == "duplicate":
                member.name = "omnirush/resources/sidecars/opencode-x86_64-unknown-linux-gnu"
            with self.subTest(kind=kind), self.assertRaises(releases.ReleaseError):
                self.install_fixture(archive_bytes(extra=member))
            self.assertFalse((self.root / "3.1.1/x64").exists())
        self.assertFalse((self.root.parent / "outside").exists())

    def test_missing_or_non_native_sidecar_fails(self):
        for body in (archive_bytes(selected=False), archive_bytes(payload=b"#!/bin/sh\n" * 10),
                     archive_bytes(payload=native_bytes("aarch64"))):
            with self.subTest(body=body[:10]), self.assertRaises(releases.ReleaseError):
                self.install_fixture(body)
        self.assertFalse((self.root / "3.1.1/x64").exists())

    def test_installation_tampering_fails_without_overwrite(self):
        path, _, pins = self.install_fixture()
        path.write_bytes(b"tampered installed executable")
        with patch.object(releases, "PINNED_ASSETS", pins), patch.object(releases, "_open") as opener, \
                self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()
        opener.assert_not_called()
        self.assertEqual(path.read_bytes(), b"tampered installed executable")

    def test_unsupported_version_arch_libc_and_missing_pin(self):
        with self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime("latest")
        with patch.object(releases, "PINNED_ASSETS", {"3.1.1": {"x64": (10, "")}}), self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()
        with patch.object(releases, "PINNED_ASSETS", {"3.1.1": {}}), self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()
        self.architecture.stop()
        for machine, libc in (("riscv64", "glibc"), ("x86_64", "musl")):
            with self.subTest(machine=machine), patch.object(releases.platform, "system", return_value="Linux"), \
                    patch.object(releases.platform, "machine", return_value=machine), \
                    patch.object(releases.platform, "libc_ver", return_value=(libc, "2.35")), \
                    self.assertRaises(releases.ReleaseError):
                releases.ensure_runtime()
        with patch.object(releases.platform, "system", return_value="Linux"), \
                patch.object(releases.platform, "machine", return_value="arm64"), \
                patch.object(releases.platform, "libc_ver", return_value=("glibc", "2.35")):
            self.assertEqual(releases._architecture(), ("arm64", "aarch64"))
        self.architecture.start()

    def test_unsafe_urls_and_redirects_are_rejected(self):
        for url in ("http://github.com/a", "https://github.com.evil.test/a", "https://evil.test/a",
                    "https://user:secret@github.com/a", "https://github.com:444/a"):
            with self.subTest(url=url), self.assertRaises(releases.ReleaseError):
                releases._safe_url(url)
        request = releases.urllib.request.Request(self.url)
        with self.assertRaises(releases.ReleaseError):
            releases._OfficialRedirect().redirect_request(request, None, 302, "", {}, "http://evil.test")

    def test_symlink_private_runtime_root_rejected(self):
        actual = Path(self.temporary.name) / "actual"
        actual.mkdir()
        self.root.parent.mkdir()
        self.root.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(releases.ReleaseError):
            releases.ensure_runtime()


if __name__ == "__main__":
    unittest.main()
