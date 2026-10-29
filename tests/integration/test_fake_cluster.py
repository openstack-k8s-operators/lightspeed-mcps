"""Self-check for the fake cluster tool.

This is the only test allowed to start the fake cluster: everywhere else it
is the developer's (or CI's) job to run it, the suite only consumes it.
"""

import json
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClusterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out_dir = Path(tempfile.mkdtemp(prefix="fake-cluster-selfcheck-"))
        cls.process = subprocess.Popen(  # noqa: S603 -- fixed argv, our own module
            [
                sys.executable,
                "-m",
                "tests.integration.fake_cluster",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--out-dir",
                str(cls.out_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=REPO_ROOT,
        )
        clouds_file = cls.out_dir / "clouds.yaml"
        deadline = time.monotonic() + 15
        while not clouds_file.exists():
            if cls.process.poll() is not None:
                cls.fail(
                    "fake cluster died on startup:\n"
                    + (cls.process.stdout.read() if cls.process.stdout else "")
                )
            if time.monotonic() > deadline:
                cls.process.kill()
                cls.fail("fake cluster did not write clouds.yaml in time")
            time.sleep(0.1)
        clouds = yaml.safe_load(clouds_file.read_text())
        cls.base_url = clouds["clouds"]["fake"]["auth"]["auth_url"].rsplit("/v3", 1)[0]
        cls.context = ssl.create_default_context()
        cls.context.check_hostname = False
        cls.context.verify_mode = ssl.CERT_NONE

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)

    def request(self, path, method="GET", data=None, headers=None):
        request = urllib.request.Request(  # noqa: S310 -- https to our fake cluster
            self.base_url + path, data=data, method=method
        )
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            response = urllib.request.urlopen(  # noqa: S310
                request, context=self.context, timeout=10
            )
        except urllib.error.HTTPError as error:
            response = error  # 4xx/5xx responses are valid fake answers
        status = getattr(response, "status", None) or response.code
        return status, dict(response.headers), json.loads(response.read())

    def test_version_document(self):
        for path in ("/", "/v3"):
            with self.subTest(path=path):
                status, _, payload = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(payload["versions"]["values"][0]["id"], "v3.14")

    def test_authentication_and_projects(self):
        status, headers, payload = self.request(
            "/v3/auth/tokens", method="POST", data=b"{}"
        )
        self.assertEqual(status, 201)
        self.assertEqual(headers.get("X-Subject-Token"), "static-token-123")
        catalog = payload["token"]["catalog"]
        self.assertEqual(
            [service["type"] for service in catalog], ["identity", "volumev3"]
        )

        # Token self-validation used by the mcp_token plugin to fetch the
        # service catalog for token+endpoint (dynamic) credentials
        status, _, payload = self.request(
            "/v3/auth/tokens",
            headers={
                "X-Auth-Token": "static-token-123",
                "X-Subject-Token": "static-token-123",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [service["type"] for service in payload["token"]["catalog"]],
            ["identity", "volumev3"],
        )

        status, _, payload = self.request("/v3/projects")
        self.assertEqual(status, 200)
        self.assertEqual(payload["projects"][0]["name"], "admin")

    def test_kube_api(self):
        status, _, payload = self.request("/version")
        self.assertEqual(status, 200)
        self.assertEqual(payload["gitVersion"], "v1.32.0")

        status, _, payload = self.request("/api/v1/namespaces/openstack/pods")
        self.assertEqual(status, 200)
        self.assertEqual(payload["kind"], "PodList")
        self.assertEqual(payload["items"][0]["metadata"]["name"], "nova-api-0")

        status, _, payload = self.request(
            "/api/v1/namespaces/kube-system/services?labelSelector=kubernetes.io"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["metadata"]["name"], "kubernetes")

    def test_namespace_creation_and_deletion(self):
        status, _, payload = self.request(
            "/api/v1/namespaces",
            method="POST",
            data=json.dumps({"metadata": {"name": "itest"}}).encode(),
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["metadata"]["name"], "itest")

        # oc posts namespaces as kubernetes protobuf, not JSON
        protobuf = (
            b"k8s\x00\n\x0f\n\x02v1\x12\tNamespace\x12\x1f\n\x17\n\x07repro-x"
            b'\x12\x00\x1a\x00"\x00*\x002\x008\x00B\x00\x12\x00\x1a\x02\n\x00'
            b'\x1a\x00"\x00'
        )
        status, _, payload = self.request(
            "/api/v1/namespaces", method="POST", data=protobuf
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["metadata"]["name"], "repro-x")

        # Created namespaces are gettable and listed, like the seeded ones
        for name in ("itest", "repro-x", "openstack"):
            with self.subTest(namespace=name):
                status, _, payload = self.request(f"/api/v1/namespaces/{name}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["metadata"]["name"], name)

        status, _, payload = self.request("/api/v1/namespaces")
        listed = [ns["metadata"]["name"] for ns in payload["items"]]
        for name in ("itest", "repro-x", "openstack"):
            self.assertIn(name, listed)

        status, _, payload = self.request("/api/v1/namespaces/missing")
        self.assertEqual(status, 404)
        self.assertEqual(payload["kind"], "Status")

        status, _, payload = self.request("/api/v1/namespaces/itest", method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"]["phase"], "Terminating")

        status, _, _ = self.request("/api/v1/namespaces/itest")
        self.assertEqual(status, 404)

        # Deleting an unknown namespace fails like the real API
        status, _, payload = self.request("/api/v1/namespaces/missing", method="DELETE")
        self.assertEqual(status, 404)
        self.assertEqual(payload["kind"], "Status")

    def test_volume_lifecycle(self):
        status, _, payload = self.request(
            "/v3/volumes",
            method="POST",
            data=json.dumps({"volume": {"name": "selfcheck-vol", "size": 1}}).encode(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["volume"]["name"], "selfcheck-vol")

        status, _, payload = self.request("/v3/volumes/selfcheck-vol")
        self.assertEqual(status, 200)
        self.assertEqual(payload["volume"]["name"], "selfcheck-vol")

        status, _, payload = self.request("/v3/volumes")
        self.assertEqual(status, 200)
        self.assertTrue(any(v["name"] == "selfcheck-vol" for v in payload["volumes"]))

        status, _, payload = self.request("/v3/volumes/selfcheck-vol", method="DELETE")
        self.assertEqual(status, 202)
        status, _, _ = self.request("/v3/volumes/selfcheck-vol")
        self.assertEqual(status, 404)

    def test_unknown_kube_path_returns_404_status(self):
        status, _, payload = self.request("/api/v1/frobnicate")
        self.assertEqual(status, 404)
        self.assertEqual(payload["kind"], "Status")

    def test_generated_credentials(self):
        kubeconfig = yaml.safe_load((self.out_dir / "kubeconfig").read_text())
        cluster = kubeconfig["clusters"][0]["cluster"]
        self.assertEqual(cluster["server"], self.base_url)
        self.assertTrue(cluster["insecure-skip-tls-verify"])
        secure = yaml.safe_load((self.out_dir / "secure.yaml").read_text())
        self.assertIn("password", secure["clouds"]["fake"]["auth"])


if __name__ == "__main__":
    unittest.main()
