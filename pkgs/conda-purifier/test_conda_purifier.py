# SPDX-License-Identifier: MIT
"""Unit tests of conda-purifier's lock run; the package build runs them."""

import base64
import hashlib
import json
import os
import tempfile
import unittest

import conda_purifier as cp


class TestLockRun(unittest.TestCase):
    def test_private_channel_token_stays_out_of_the_lock(self):
        url = "https://conda.anaconda.org/t/abc-123/private/linux-64/pkg-1.0-0.conda"
        entry = cp.conda_entry(url, "00" * 32, "pkg", "1.0")
        self.assertEqual(entry["url"], "https://conda.anaconda.org/private/linux-64/pkg-1.0-0.conda")
        self.assertEqual(entry["mirrors"], [])
        # The lock run itself still downloads with the token.
        self.assertEqual(cp.lock_run_url(entry["url"]), url)

    def test_sri(self):
        digest = hashlib.sha256(b"x")
        expected = "sha256-" + base64.b64encode(digest.digest()).decode()
        self.assertEqual(cp.sri(digest.hexdigest()), expected)

    def test_wheel_tags(self):
        self.assertFalse(cp.wheel_platform_specific("six-1.17.0-py2.py3-none-any.whl"))
        self.assertTrue(cp.wheel_platform_specific("msgpack-1.1.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"))
        self.assertFalse(cp.wheel_platform_specific("docopt-0.6.2.tar.gz"))

    def test_build_requires(self):
        self.assertEqual(cp.pyproject_build_requires(None), ["setuptools>=40.8.0"])
        self.assertEqual(cp.pyproject_build_requires("[project]\nname = 'x'\n"), ["setuptools>=40.8.0"])
        self.assertEqual(
            cp.pyproject_build_requires("[build-system]\nrequires = ['flit_core>=3.9']\n"),
            ["flit_core>=3.9"],
        )

    def test_manylinux_tags(self):
        cp.GLIBC = "2.40"
        tags = cp.manylinux_tags("linux-aarch64")
        self.assertEqual(tags[0], "manylinux_2_40_aarch64")
        self.assertIn("manylinux_2_17_aarch64", tags)
        self.assertEqual(tags[-2:], ["manylinux2014_aarch64", "linux_aarch64"])

    def test_macos_tags(self):
        cp.MACOS = "14.0"
        self.assertEqual(cp.macos_tags("osx-arm64"), ["macosx_14_0_arm64"])
        self.assertEqual(cp.macos_tags("osx-64"), ["macosx_14_0_x86_64"])

    def test_default_platforms(self):
        self.assertEqual(cp.default_platforms("linux-aarch64"), ["linux-64", "linux-aarch64"])
        self.assertEqual(cp.default_platforms("osx-arm64"), ["linux-64", "linux-aarch64", "osx-arm64"])

    def test_inputs_hash_keeps_line_endings(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "environment.yml"), "wb") as f:
                f.write(b"name: x\r\n")
            expected = hashlib.sha256(b"environment.yml\nname: x\r\n\n").hexdigest()
            self.assertEqual(cp.inputs_hash(root, ["environment.yml"]), expected)

    def test_lockfile_errors_say_what_to_run(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, cp.LOCKFILE)
            lock = {"version": cp.FORMAT, "platforms": {"linux-64": {"conda": []}}}
            lock["lockHash"] = cp.lock_hash(lock)
            with open(path, "w") as f:
                json.dump(lock, f)
            self.assertEqual(cp.read_lock(root)["version"], cp.FORMAT)

            lock["platforms"]["linux-64"]["conda"].append({"name": "added by hand"})
            with open(path, "w") as f:
                json.dump(lock, f)
            with self.assertRaisesRegex(cp.Failure, "changed after conda-purifier wrote it; run `conda-purifier lock`"):
                cp.read_lock(root)

            with open(path, "w") as f:
                f.write('{"version": 1,')
            with self.assertRaisesRegex(cp.Failure, "not valid JSON .*; run `conda-purifier lock`"):
                cp.read_lock(root)

            old = {"version": 1}
            old["lockHash"] = cp.lock_hash(old)
            with open(path, "w") as f:
                json.dump(old, f)
            with self.assertRaisesRegex(cp.Failure, "is format 1; .*Run `conda-purifier lock`"):
                cp.read_lock(root)

    def test_conda_lock_read_line_by_line(self):
        text = (
            "version: 1\n"
            "metadata:\n"
            "  platforms:\n"
            "  - linux-64\n"
            "package:\n"
            "- name: python_abi\n"
            "  version: '3.12'\n"
            "  manager: conda\n"
            "  platform: linux-64\n"
            "  dependencies:\n"
            "    sha256: not a hash\n"
            "  url: https://conda.anaconda.org/conda-forge/noarch/python_abi-3.12-9_cp312.conda\n"
            "  hash:\n"
            "    md5: 0123\n"
            "    sha256: abcd\n"
            "- name: six\n"
            "  manager: pip\n"
            "  platform: linux-64\n"
            "  url: https://files.pythonhosted.org/six.whl\n"
            "  hash:\n"
            "    sha256: ef01\n"
        )
        packages = cp.conda_lock_packages(text)
        self.assertEqual([p["name"] for p in packages], ["python_abi", "six"])
        self.assertEqual(packages[0]["version"], "3.12")
        self.assertEqual(packages[0]["hash"], {"md5": "0123", "sha256": "abcd"})
        self.assertEqual(packages[1]["manager"], "pip")

    def test_recorded_pip_fields(self):
        git = {"name": "x", "version": "1", "path": "pip-git/x", "hash": "h",
               "git": {"url": "u", "rev": "r", "submodules": True, "leaveDotGit": False}}
        self.assertEqual(cp.recorded_entry(git),
                         {"name": "x", "version": "1", "hash": "h", "git": {"url": "u", "rev": "r"}})
        wheel = cp.pip_file_entry("https://example.org/x-1-py3-none-any.whl", "00" * 32, "x", "1")
        self.assertEqual(sorted(cp.recorded_entry(wheel)), ["hash", "name", "url", "version"])

    def test_index_options(self):
        lines = ["six", "--extra-index-url https://example.org/simple", "-i=https://x/simple"]
        self.assertEqual(
            cp.index_options_of(lines),
            ["--extra-index-url", "https://example.org/simple", "-i", "https://x/simple"],
        )


if __name__ == "__main__":
    unittest.main()
