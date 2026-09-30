"""Tests for tools/release/version.py: versions and image tags from git."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from packaging.version import Version

from tools.release import version
from tools.release.version import Release, compute, parse_tag

SHA = "0123456789abcdef0123456789abcdef01234567"
MAIN = "refs/heads/main"
PR = "refs/pull/7/merge"


def _tag_ref(tag):
    return "refs/tags/" + tag


class ParseTagTest(unittest.TestCase):
    def test_stable_and_rc(self):
        self.assertEqual(parse_tag("v0.1.0"), Release(0, 1, 0))
        self.assertEqual(parse_tag("v1.20.3-rc.2"), Release(1, 20, 3, 2))

    def test_rejects_other_shapes(self):
        for name in [
            "0.1.0",  # no v
            "v0.1",
            "v01.1.0",  # leading zero
            "v0.1.0rc1",  # PEP 440 spelling: sorts above 0.1.0 in SemVer (and Bazel)
            "v0.1.0-rc1",
            "v0.1.0-rc.0",
            "v0.1.0-beta.1",
            "v0.1.0+build",
            "latest",
        ]:
            self.assertIsNone(parse_tag(name), name)

    def test_forms(self):
        rc = parse_tag("v0.3.0-rc.1")
        self.assertEqual((rc.tag, rc.semver, rc.pep440), ("v0.3.0-rc.1", "0.3.0-rc.1", "0.3.0rc1"))
        final = parse_tag("v0.3.0")
        self.assertEqual((final.tag, final.semver, final.pep440), ("v0.3.0", "0.3.0", "0.3.0"))

    def test_semver_order(self):
        names = ["v0.10.0", "v0.3.0", "v0.3.0-rc.2", "v0.3.0-rc.10", "v0.2.9", "v1.0.0-rc.1"]
        self.assertEqual(
            [r.tag for r in version.release_tags(names + ["junk", "v0.3.0"])],
            ["v0.2.9", "v0.3.0-rc.2", "v0.3.0-rc.10", "v0.3.0", "v0.10.0", "v1.0.0-rc.1"],
        )


class DevelopmentVersionTest(unittest.TestCase):
    def test_no_tags(self):
        info = compute(sha=SHA, ref=PR, commit_count=42)
        self.assertEqual(info.pep440, "0.0.0.dev42+g0123456")
        self.assertEqual(info.version, info.pep440)
        self.assertFalse(info.release)
        self.assertTrue(info.prerelease)
        self.assertEqual(info.tag, "")
        self.assertEqual(info.base_tag, "")
        self.assertEqual(info.oci_tags, [])
        self.assertFalse(info.latest)

    def test_after_stable_release(self):
        info = compute(sha=SHA, ref=PR, base_tag="v0.2.1", distance=5, all_tags=["v0.2.1"])
        self.assertEqual(info.pep440, "0.2.2.dev5+g0123456")
        self.assertEqual(info.base_tag, "v0.2.1")

    def test_after_release_candidate(self):
        # Sorts after 0.3.0rc1 and before 0.3.0rc2 and 0.3.0.
        info = compute(sha=SHA, ref=PR, base_tag="v0.3.0-rc.1", distance=3)
        self.assertEqual(info.pep440, "0.3.0rc2.dev3+g0123456")

    def test_dirty_tree(self):
        info = compute(sha=SHA, ref=PR, base_tag="v0.2.1", distance=5, dirty=True)
        self.assertEqual(info.pep440, "0.2.2.dev5+g0123456.dirty")

    def test_dirty_tree_at_a_tag_is_not_a_release(self):
        info = compute(
            sha=SHA, ref=_tag_ref("v0.2.1"), head_tags=["v0.2.1"], base_tag="v0.2.1", dirty=True
        )
        self.assertFalse(info.release)
        self.assertEqual(info.pep440, "0.2.2.dev0+g0123456.dirty")
        self.assertEqual(info.oci_tags, [])

    def test_main_gets_edge_and_sha(self):
        info = compute(sha=SHA, ref=MAIN, commit_count=3)
        self.assertEqual(info.oci_tags, ["edge", "sha-0123456"])
        self.assertFalse(info.latest)

    def test_main_at_a_tagged_commit_still_gets_only_edge(self):
        info = compute(sha=SHA, ref=MAIN, head_tags=["v0.2.0"], all_tags=["v0.2.0"])
        self.assertTrue(info.release)
        self.assertEqual(info.pep440, "0.2.0")
        self.assertEqual(info.oci_tags, ["edge", "sha-0123456"])

    def test_invalid_base_tag(self):
        with self.assertRaises(ValueError):
            compute(sha=SHA, base_tag="v0.2.1foo", distance=1)

    def test_versions_are_pep440_and_sort_as_intended(self):
        # Every string is already in PEP 440 normal form, and the sequence of a
        # release cycle sorts in build order.
        sequence = [
            compute(sha=SHA, commit_count=9).pep440,  # before any release
            "0.1.0",
            compute(sha=SHA, base_tag="v0.1.0", distance=4).pep440,
            "0.2.0rc1",
            compute(sha=SHA, base_tag="v0.2.0-rc.1", distance=2).pep440,
            "0.2.0rc2",
            "0.2.0",
            compute(sha=SHA, base_tag="v0.2.0", distance=1).pep440,
        ]
        for text in sequence:
            self.assertEqual(str(Version(text)), text)
        self.assertEqual(sorted(sequence, key=Version), sequence)


class ReleaseVersionTest(unittest.TestCase):
    TAGS = ["v0.1.0", "v0.2.0", "v0.2.4", "v0.3.0-rc.1", "v0.3.0", "v0.3.1"]

    def _at(self, tag, all_tags=None, head_tags=None):
        return compute(
            sha=SHA,
            ref=_tag_ref(tag),
            head_tags=head_tags or [tag],
            base_tag=tag,
            all_tags=self.TAGS if all_tags is None else all_tags,
        )

    def test_highest_stable_release(self):
        info = self._at("v0.3.1")
        self.assertEqual((info.version, info.pep440, info.tag), ("0.3.1", "0.3.1", "v0.3.1"))
        self.assertTrue(info.release)
        self.assertFalse(info.prerelease)
        self.assertTrue(info.latest)
        # No major tag while the major version is 0.
        self.assertEqual(info.oci_tags, ["0.3.1", "0.3", "latest"])
        self.assertEqual(info.previous_stable, "v0.3.0")

    def test_backport_does_not_take_latest(self):
        info = self._at("v0.2.5", all_tags=self.TAGS + ["v0.2.5"])
        self.assertEqual(info.oci_tags, ["0.2.5", "0.2"])
        self.assertFalse(info.latest)
        self.assertEqual(info.previous_stable, "v0.2.4")

    def test_older_patch_does_not_move_the_minor_tag(self):
        info = self._at("v0.2.3", all_tags=self.TAGS + ["v0.2.3"])
        self.assertEqual(info.oci_tags, ["0.2.3"])

    def test_release_candidate(self):
        info = self._at("v0.4.0-rc.1", all_tags=self.TAGS + ["v0.4.0-rc.1"])
        self.assertEqual((info.version, info.pep440), ("0.4.0-rc.1", "0.4.0rc1"))
        self.assertTrue(info.release)
        self.assertTrue(info.prerelease)
        self.assertFalse(info.latest)
        self.assertEqual(info.oci_tags, ["0.4.0-rc.1"])
        # Release notes of an RC cover everything since the last stable release.
        self.assertEqual(info.previous_stable, "v0.3.1")

    def test_final_release_on_the_commit_of_its_last_rc(self):
        tags = self.TAGS + ["v0.4.0-rc.2", "v0.4.0"]
        final = self._at("v0.4.0", all_tags=tags, head_tags=["v0.4.0-rc.2", "v0.4.0"])
        self.assertEqual(final.tag, "v0.4.0")
        self.assertEqual(final.oci_tags, ["0.4.0", "0.4", "latest"])
        self.assertEqual(final.previous_stable, "v0.3.1")
        # Re-running the RC's workflow afterwards still builds the RC.
        rc = self._at("v0.4.0-rc.2", all_tags=tags, head_tags=["v0.4.0-rc.2", "v0.4.0"])
        self.assertEqual(rc.tag, "v0.4.0-rc.2")
        self.assertEqual(rc.oci_tags, ["0.4.0-rc.2"])

    def test_major_tag_from_one(self):
        tags = self.TAGS + ["v1.0.0", "v1.1.0", "v1.2.0"]
        self.assertEqual(
            self._at("v1.2.0", all_tags=tags).oci_tags, ["1.2.0", "1.2", "1", "latest"]
        )
        self.assertEqual(self._at("v1.1.1", all_tags=tags + ["v1.1.1"]).oci_tags, ["1.1.1", "1.1"])

    def test_first_release(self):
        info = self._at("v0.1.0", all_tags=["v0.1.0"])
        self.assertEqual(info.oci_tags, ["0.1.0", "0.1", "latest"])
        self.assertEqual(info.previous_stable, "")

    def test_outputs_are_strings(self):
        outputs = self._at("v0.3.1").outputs()
        self.assertEqual(outputs["oci_tags"], "0.3.1,0.3,latest")
        self.assertEqual(outputs["latest"], "true")
        self.assertEqual(outputs["prerelease"], "false")
        self.assertTrue(all(isinstance(v, str) for v in outputs.values()))


class GitTest(unittest.TestCase):
    """End to end against a scratch repository."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.env = dict(
            os.environ,
            HOME=self.repo,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_NAME="Test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        self._git("init", "-q")

    def tearDown(self):
        self._tmp.cleanup()

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", self.repo, *args],
            env=self.env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _commit(self, name):
        with open(os.path.join(self.repo, "file.txt"), "a", encoding="utf-8") as handle:
            handle.write(name + "\n")
        self._git("add", "file.txt")
        self._git("commit", "-q", "-m", name)

    def _info(self, ref=""):
        with mock.patch.dict(os.environ, self.env):
            return version.from_git(self.repo, ref)

    def test_lifecycle(self):
        self._commit("one")
        self._commit("two")
        info = self._info(MAIN)
        self.assertRegex(info.pep440, r"^0\.0\.0\.dev2\+g[0-9a-f]{7}$")
        self.assertEqual(info.oci_tags, ["edge", "sha-" + info.sha[:7]])

        self._git("tag", "-a", "v0.1.0-rc.1", "-m", "rc")
        self.assertEqual(self._info(_tag_ref("v0.1.0-rc.1")).oci_tags, ["0.1.0-rc.1"])
        self._git("tag", "-a", "v0.1.0", "-m", "final")
        final = self._info(_tag_ref("v0.1.0"))
        self.assertEqual(final.pep440, "0.1.0")
        self.assertEqual(final.oci_tags, ["0.1.0", "0.1", "latest"])

        self._commit("three")
        self._git("tag", "not-a-release")
        after = self._info(MAIN)
        self.assertEqual(after.base_tag, "v0.1.0")
        self.assertRegex(after.pep440, r"^0\.1\.1\.dev1\+g[0-9a-f]{7}$")

        # Tags that match describe's glob but are not release tags are skipped:
        # the nearest release tag below them is the base.
        self._commit("four")
        self._git("tag", "-a", "v0.2.0-beta.1", "-m", "not a release tag")
        self._git("tag", "v1.0.0-rc1")
        skipped = self._info(MAIN)
        self.assertEqual(skipped.base_tag, "v0.1.0")
        self.assertRegex(skipped.pep440, r"^0\.1\.1\.dev2\+g[0-9a-f]{7}$")
        self.assertEqual(skipped.oci_tags, ["edge", "sha-" + skipped.sha[:7]])

        with open(os.path.join(self.repo, "file.txt"), "a", encoding="utf-8") as handle:
            handle.write("uncommitted\n")
        self.assertTrue(self._info().pep440.endswith(".dirty"))

    def test_cli_expect_tag(self):
        self._commit("one")
        self._git("tag", "-a", "v0.2.0", "-m", "release")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env):
            self.assertEqual(
                version.main(["--repo", self.repo, "--ref", "", "--expect-tag", "v0.2.0"]), 0
            )
        self.assertEqual(json.loads(out.getvalue())["version"], "0.2.0")

        self._commit("two")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with mock.patch.dict(os.environ, self.env):
                self.assertEqual(
                    version.main(["--repo", self.repo, "--ref", "", "--expect-tag", "v0.2.0"]), 1
                )
        self.assertIn("not exactly v0.2.0", err.getvalue())

    def test_cli_github_output(self):
        self._commit("one")
        path = os.path.join(self.repo, "out.txt")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, self.env):
            version.main(["--repo", self.repo, "--ref", MAIN, "--github-output", path])
        with open(path, encoding="utf-8") as handle:
            lines = dict(line.rstrip("\n").split("=", 1) for line in handle)
        self.assertEqual(lines["release"], "false")
        self.assertTrue(lines["oci_tags"].startswith("edge,sha-"))


if __name__ == "__main__":
    unittest.main()
