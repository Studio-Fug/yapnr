"""The task and campaign schemas, the owner configuration, bundles and image references."""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

from yapnr.exp import bundle, config, image, spec, testing

EXAMPLE = Path(__file__).resolve().parents[3] / "docs" / "examples" / "cloud.toml.example"


class TaskSpecTest(unittest.TestCase):
    def task(self, **changes):
        task = testing.python_task("t/a", "pass")
        task.update(changes)
        return task

    def test_valid_task_and_stable_hash(self):
        task = self.task()
        self.assertEqual(spec.task_errors(task), [])
        reordered = dict(reversed(list(task.items())))
        self.assertEqual(spec.spec_hash(task), spec.spec_hash(reordered))
        self.assertEqual(spec.canonical_json({"b": 1, "a": "é"}), '{"a":"é","b":1}'.encode())

    def test_every_finding_is_listed(self):
        task = self.task(
            id="../x",
            visibility="secret",
            env={"GOOGLE_APPLICATION_CREDENTIALS": "x", "lower": "1"},
            command=["${PYTHON}", "${HOME}"],
            resources={"cpus": 0, "memory_gb": 1, "disk_gb": 1, "max_wall_s": 5},
        )
        task["extra"] = 1
        errors = spec.task_errors(task)
        self.assertIn("unknown field 'extra'", errors)
        task.pop("extra")
        errors = spec.task_errors(task)
        joined = "; ".join(errors)
        for text in (
            "is not a task id",
            "visibility",
            "looks like a credential",
            "not an environment",
            "${HOME}",
            "cpus",
            "max_wall_s",
        ):
            self.assertIn(text, joined)

    def test_private_tasks_need_opaque_ids(self):
        self.assertTrue(spec.task_errors(self.task(visibility="private")))
        self.assertEqual(spec.task_errors(self.task(visibility="private", id="p/" + "0" * 16)), [])

    def test_resume_needs_a_checkpoint(self):
        self.assertIn(
            "restart 'resume' needs a checkpoint", spec.task_errors(self.task(restart="resume"))
        )

    def test_campaign_errors(self):
        self.assertEqual(
            spec.campaign_errors({"schema": spec.CAMPAIGN_SCHEMA, "kind": "smoke"}), []
        )
        errors = spec.campaign_errors(
            {
                "schema": "x",
                "kind": "nope",
                "source": "main",
                "matrix": {"a": []},
                "placement": {"prefer": "x"},
            }
        )
        self.assertEqual(len(errors), 5, errors)

    def test_runtime_is_checked(self):
        base = {"schema": spec.CAMPAIGN_SCHEMA, "kind": "mc-eval"}
        ok = {"python": "/opt/openEMS/venv/bin/python", "entrypoint": ""}
        self.assertEqual(spec.campaign_errors(dict(base, runtime=ok)), [])
        self.assertEqual(spec.campaign_errors(dict(base, runtime={"entrypoint": "/bin/env"})), [])
        errors = spec.campaign_errors(
            dict(base, runtime={"python": "python3", "entrypoint": "env", "user": "x"})
        )
        self.assertEqual(len(errors), 3, errors)
        self.assertEqual(
            spec.campaign_errors(dict(base, runtime="x")),
            ["runtime is a table {python, entrypoint}"],
        )
        self.assertEqual(image.runtime({}), (image.YAPNR_PYTHON, image.YAPNR_ENTRYPOINT))
        self.assertEqual(image.runtime({"runtime": ok}), (ok["python"], None))

    def test_placement_keys_are_checked_not_ignored(self):
        def errors(**placement):
            return spec.campaign_errors(
                {"schema": spec.CAMPAIGN_SCHEMA, "kind": "smoke", "placement": placement}
            )

        self.assertEqual(errors(spot=False, shape="c4d-highcpu-8", region="us-west4"), [])
        # A key nothing reads would be silently dropped; a string "false" would mean Spot.
        self.assertEqual(errors(parallelism=8), ["unknown placement key 'parallelism'"])
        self.assertEqual(errors(spot="false"), ["placement.spot is a boolean"])
        self.assertEqual(len(errors(shape=16, region="US West")), 2)

    def test_image_pinning(self):
        self.assertTrue(spec.is_pinned("ghcr.io/studio-fug/yapnr@" + testing.DIGEST))
        self.assertFalse(spec.is_pinned("ghcr.io/studio-fug/yapnr:edge"))


class ConfigTest(unittest.TestCase):
    def test_example_config_parses(self):
        cfg = config.load(str(EXAMPLE))
        self.assertEqual(cfg.gcp.project, "example-project")
        self.assertEqual(cfg.gcp.runner_email.split("@")[0], "yapnr-runner")
        self.assertEqual(cfg.limits.hard_refuse_usd, 100)
        self.assertIn("example-site", cfg.slurm)

    def test_missing_default_file_gives_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(
            "os.environ", {"HOME": tmp}, clear=False
        ):
            import os

            os.environ.pop(config.ENV_CONFIG, None)
            cfg = config.load()
            self.assertIsNone(cfg.gcp)
            self.assertEqual(cfg.local.workers, 4)
            with self.assertRaises(config.ConfigError):
                cfg.require_gcp()

    def test_errors_are_collected(self):
        data = {
            "gcp": {
                "project": "Bad_Project",
                "home_region": "mars-north1",
                "regions": ["us-west4"],
                "inputs_bucket": "same",
                "runs_bucket": "same",
                "ranking": [["c4d", "europe-west4"]],
            },
            "limits": {"max_retries": 5, "confirm_usd": 50, "refuse_usd": 25},
            "local": {"workers": 0, "colour": "red"},
            "extra": {},
        }
        with self.assertRaises(config.ConfigError) as ctx:
            config.parse(data)
        joined = "; ".join(ctx.exception.errors)
        for text in (
            "unknown section [extra]",
            "not a project id",
            "home_region",
            "two buckets",
            "ranking names",
            "max_retries",
            "confirm_usd",
            "workers",
            "unknown key local.colour",
        ):
            self.assertIn(text, joined)


class BundleTest(unittest.TestCase):
    def test_source_bundle_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = testing.fixture_repo(Path(tmp))
            commit = testing.git(repo, "rev-parse", "HEAD")
            a, path = bundle.source_bundle(repo, commit, ["hardware/pnr"], Path(tmp) / "a")
            b, _ = bundle.source_bundle(repo, commit, ["hardware/pnr"], Path(tmp) / "b")
            self.assertEqual(a, b)
            dest = Path(tmp) / "x"
            bundle.extract(path, a, dest)
            self.assertTrue((dest / "hardware" / "pnr" / "regression" / "run.py").is_file())
            self.assertFalse((dest / "README.md").exists())
            with self.assertRaises(bundle.BundleError):
                bundle.extract(path, "0" * 64, dest)

    def test_data_bundle_of_a_directory_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "data"
            (src / "sub").mkdir(parents=True)
            (src / "sub" / "a.txt").write_text("a")
            first, _ = bundle.data_bundle(src, Path(tmp) / "b1")
            (src / "sub" / "a.txt").touch()
            second, _ = bundle.data_bundle(src, Path(tmp) / "b2")
            self.assertEqual(first, second)

    def test_unsafe_members_are_refused(self):
        for name in ("../escape", "/abs"):
            member = tarfile.TarInfo(name)
            with self.assertRaises(bundle.BundleError):
                bundle.unsafe_member(member)
        link = tarfile.TarInfo("ok")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        with self.assertRaises(bundle.BundleError):
            bundle.unsafe_member(link)


class FakeResponse(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImageTest(unittest.TestCase):
    def test_parse_and_mirror(self):
        ref = image.parse("edge")
        self.assertEqual(
            (ref.registry, ref.repository, ref.tag), ("ghcr.io", "studio-fug/yapnr", "edge")
        )
        pinned = image.pin("edge", testing.DIGEST)
        self.assertEqual(
            image.mirror(pinned, "us-west4-docker.pkg.dev/example-project/ghcr"),
            "us-west4-docker.pkg.dev/example-project/ghcr/studio-fug/yapnr@" + testing.DIGEST,
        )
        with self.assertRaises(image.ImageError):
            image.pin("edge", offline=True)
        with self.assertRaises(image.ImageError):
            image.pin("ghcr.io/studio-fug/yapnr@" + testing.DIGEST, "sha256:" + "cd" * 32)

    def test_resolve_asks_the_registry_for_the_digest(self):
        seen = []

        def opener(request):
            seen.append((request.get_method(), request.full_url, dict(request.header_items())))
            if "/token" in request.full_url:
                return FakeResponse(b'{"token": "anon"}')
            return FakeResponse(headers={"Docker-Content-Digest": testing.DIGEST})

        self.assertEqual(image.pin("edge", opener=opener, environ={}).digest, testing.DIGEST)
        self.assertEqual(seen[1][0], "HEAD")
        self.assertTrue(seen[1][1].endswith("/v2/studio-fug/yapnr/manifests/edge"))
        self.assertEqual(seen[1][2]["Authorization"], "Bearer anon")

    def test_private_package_explains_itself(self):
        def opener(request):
            if "/token" in request.full_url:
                return FakeResponse(b"{}")
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with self.assertRaises(image.ImageError) as ctx:
            image.pin("edge", opener=opener, environ={})
        self.assertIn("read:packages", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
