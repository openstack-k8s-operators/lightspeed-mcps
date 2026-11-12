"""Fake OpenStack + OpenShift cluster for the integration test suite.

Standalone CLI tool, runnable as a program and imported by nothing in the
suite (only its self-check test starts it, as a subprocess):

    uv run python -m tests.integration.fake_cluster \
        --host 0.0.0.0 --port 0 \
        --out-dir /tmp/fake-cluster \
        [--advertise-host host.containers.internal] \
        [--delay 0.7]

One threaded stdlib HTTPS server fakes Keystone and the Kubernetes/OpenShift
API on a single port (the paths don't collide):

- Keystone: version documents on `/` and `/v3`, `POST /v3/auth/tokens`
  (any credentials are accepted, a static token is returned along with a
  catalog pointing back at this server), `GET /v3/auth/tokens` (token
  validation for the mcp_token plugin, same token data), `GET */projects`,
  and enough of Cinder for `volume create`/`volume list`/`volume delete`.
- Kube API: `/version`, `/api`, `/apis`, `/api/v1`, `get pod`,
  `cluster-info`'s kube-system services, `whoami`, `create namespace` and
  `delete namespace` (namespaces are tracked: gettable by name and listed);
  anything else returns a 404 Status.

It writes `clouds.yaml`, `secure.yaml`, `kubeconfig`, and `fake-cluster.pid`
into `--out-dir`, all pointing at `https://<advertise-host>:<port>` so an MCP
server running in a container can reach it.  `--delay` sleeps that many
seconds before answering each request, to make concurrency measurements
meaningful.  The PID file makes it easy to kill a background run:
`kill $(cat <out-dir>/fake-cluster.pid)`.

Prints every request for debugging and runs until killed.
"""

import argparse
import json
import os
import ssl
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ADMIN_TOKEN = "static-token-123"  # noqa: S105
OC_TOKEN = "kubeadmin-token"  # noqa: S105
CLOUD_NAME = "fake"

PROJECTS = {
    "projects": [
        {
            "id": "pid1",
            "name": "admin",
            "domain_id": "default",
            "description": "admin project",
            "enabled": True,
            "is_domain": False,
            "parent_id": None,
            "options": {},
        }
    ]
}

PODS = [
    {
        "kind": "Pod",
        "apiVersion": "v1",
        "metadata": {"name": "nova-api-0", "namespace": "openstack"},
        "spec": {"containers": [{"name": "nova-api"}]},
        "status": {"phase": "Running"},
    },
    {
        "kind": "Pod",
        "apiVersion": "v1",
        "metadata": {
            "name": "apiserver-6d8c9f7b5-x4t2q",
            "namespace": "openshift-apiserver",
        },
        "spec": {"containers": [{"name": "openshift-apiserver"}]},
        "status": {"phase": "Running"},
    },
]

KUBE_USER = {
    "kind": "User",
    "apiVersion": "user.openshift.io/v1",
    "metadata": {"name": "kubeadmin"},
    "fullName": "Kube Admin",
    "identities": [],
}

KUBE_VERSION = {
    "major": "1",
    "minor": "32",
    "gitVersion": "v1.32.0",
    "gitCommit": "fake",
    "gitTreeState": "clean",
    "buildDate": "2026-01-01T00:00:00Z",
    "goVersion": "go1.23",
    "compiler": "gc",
    "platform": "linux/amd64",
}

# `services` is required in the list for `oc cluster-info` to work
API_V1 = {
    "kind": "APIResourceList",
    "groupVersion": "v1",
    "resources": [
        {
            "name": "pods",
            "singularName": "pod",
            "namespaced": True,
            "kind": "Pod",
            "verbs": ["get", "list"],
        },
        {
            "name": "namespaces",
            "singularName": "namespace",
            "namespaced": False,
            "kind": "Namespace",
            "verbs": ["get", "list", "create", "delete"],
        },
        {
            "name": "services",
            "singularName": "service",
            "namespaced": True,
            "kind": "Service",
            "verbs": ["get", "list"],
        },
        {
            "name": "events",
            "singularName": "event",
            "namespaced": True,
            "kind": "Event",
            "verbs": ["get", "list"],
        },
        {
            "name": "configmaps",
            "singularName": "configmap",
            "namespaced": True,
            "kind": "ConfigMap",
            "verbs": ["get", "list"],
        },
    ],
}

SERVICES = {
    "kind": "ServiceList",
    "apiVersion": "v1",
    "metadata": {"resourceVersion": "1"},
    "items": [
        {
            "kind": "Service",
            "apiVersion": "v1",
            "metadata": {
                "name": "kubernetes",
                "namespace": "kube-system",
                "labels": {
                    "kubernetes.io/cluster-service": "true",
                    "provider": "kubernetes",
                },
            },
            "spec": {
                "clusterIP": "192.168.0.1",
                "ports": [{"port": 443, "protocol": "TCP", "name": "https"}],
            },
            "status": {},
        }
    ],
}

NAMESPACES = {
    "kind": "NamespaceList",
    "apiVersion": "v1",
    "metadata": {"resourceVersion": "1"},
    "items": [
        {
            "kind": "Namespace",
            "apiVersion": "v1",
            "metadata": {"name": "openstack"},
            "spec": {},
            "status": {"phase": "Active"},
        }
    ],
}


def version_doc(base_url: str) -> dict:
    return {
        "versions": {
            "values": [
                {
                    "id": "v3.14",
                    "status": "stable",
                    "updated": "2013-07-23T11:33:21Z",
                    "min_version": "3.0",
                    "max_version": "3.61",
                    "links": [{"rel": "self", "href": base_url}],
                    "media-types": [
                        {
                            "base": "application/json",
                            "type": "application/vnd.openstack.identity-v3+json",
                        }
                    ],
                }
            ]
        }
    }


def auth_response(base_url: str) -> dict:
    endpoints = [{"interface": "public", "region": "RegionOne", "url": base_url}]
    catalog = [
        {"type": "identity", "name": "keystone", "endpoints": endpoints},
        {"type": "volumev3", "name": "cinder", "endpoints": endpoints},
    ]
    return {
        "token": {
            "methods": ["password"],
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
            "user": {
                "name": "admin",
                "id": "uid1",
                "domain": {"id": "default", "name": "Default"},
            },
            "project": {
                "name": "admin",
                "id": "pid1",
                "domain": {"id": "default", "name": "Default"},
            },
            "catalog": catalog,
            "roles": [{"name": "admin"}],
        }
    }


def pod_list(namespace: str | None) -> dict:
    """PodList for one namespace (None: all of them)."""
    return {
        "kind": "PodList",
        "apiVersion": "v1",
        "metadata": {"resourceVersion": "1"},
        "items": [
            pod
            for pod in PODS
            if namespace is None or pod["metadata"]["namespace"] == namespace
        ],
    }


def kube_status(code: int, path: str) -> dict:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": f"the server could not find the requested resource: {path}",
        "reason": "NotFound",
        "code": code,
    }


def _protobuf_fields(data: bytes):
    """Iterate (field number, value) over length-delimited protobuf fields."""
    index = 0
    while index < len(data):
        key = data[index]
        index += 1
        if key & 0x07 != 2:  # only length-delimited fields
            break
        length = shift = 0
        while True:
            byte = data[index]
            index += 1
            length |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        yield key >> 3, data[index : index + length]
        index += length


def protobuf_metadata_name(body: bytes) -> str | None:
    """Extract metadata.name from a kubernetes protobuf-encoded object.

    oc posts built-in resources as application/vnd.kubernetes.protobuf (a
    JSON body would crash json.loads): the magic prefix ``k8s\\x00``, an
    Unknown message whose field 2 is the object, whose field 1 is the
    ObjectMeta, whose field 1 is the name.
    """
    data = body.removeprefix(b"k8s\x00")
    for _, raw in _protobuf_fields(data):
        for field, metadata in _protobuf_fields(raw):
            if field == 1:
                for field, name in _protobuf_fields(metadata):
                    if field == 1:
                        return name.decode()
    return None


def create_namespace(body: bytes) -> dict:
    name = None
    if body:
        if body.startswith(b"k8s\x00"):
            name = protobuf_metadata_name(body)
        else:
            name = json.loads(body).get("metadata", {}).get("name")
    return {
        "kind": "Namespace",
        "apiVersion": "v1",
        "metadata": {
            "name": name,
            "uid": uuid.uuid4().hex,
            "resourceVersion": "1",
            "creationTimestamp": "2026-01-01T00:00:00Z",
        },
        "spec": {"finalizers": ["kubernetes"]},
        "status": {"phase": "Active"},
    }


def deleted_namespace(name: str) -> dict:
    namespace = create_namespace("")
    namespace["metadata"]["name"] = name
    namespace["status"] = {"phase": "Terminating"}
    return namespace


class FakeClusterState:
    """In-memory state for resources created through the fake APIs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.volumes: dict[str, dict] = {}
        # Seeded with the static namespaces so they are gettable like the
        # created ones
        self.namespaces: dict[str, dict] = {
            namespace["metadata"]["name"]: namespace
            for namespace in NAMESPACES["items"]
        }

    def create_volume(self, body: bytes) -> dict:
        spec = json.loads(body).get("volume", {}) if body else {}
        volume = {
            "id": uuid.uuid4().hex,
            "name": spec.get("name") or "unnamed",
            "size": spec.get("size", 1),
            "status": "available",
        }
        with self.lock:
            self.volumes[volume["id"]] = volume
        return {"volume": volume}

    def list_volumes(self) -> dict:
        with self.lock:
            return {"volumes": list(self.volumes.values())}

    def get_volume(self, key: str) -> dict | None:
        with self.lock:
            for volume in self.volumes.values():
                if key in (volume["id"], volume["name"]):
                    return volume
        return None

    def delete_volume(self, key: str) -> dict:
        with self.lock:
            self.volumes = {
                id: volume
                for id, volume in self.volumes.items()
                if key not in (id, volume["name"])
            }
        return {}

    def create_namespace(self, body: bytes) -> dict:
        namespace = create_namespace(body)
        with self.lock:
            self.namespaces[namespace["metadata"]["name"]] = namespace
        return namespace

    def get_namespace(self, name: str) -> dict | None:
        with self.lock:
            return self.namespaces.get(name)

    def delete_namespace(self, name: str) -> dict | None:
        """Terminating response, or None when the namespace doesn't exist."""
        with self.lock:
            existed = self.namespaces.pop(name, None) is not None
        return deleted_namespace(name) if existed else None

    def list_namespaces(self) -> dict:
        with self.lock:
            return {**NAMESPACES, "items": list(self.namespaces.values())}


class FakeClusterHandler(BaseHTTPRequestHandler):
    cluster = FakeClusterState()
    delay = 0.0

    def _respond(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        print(f"REQ: {method} {self.path}", flush=True)
        # The Host header carries the address the client used to reach us, so
        # it works with random ports and host.containers.internal alike.
        base_url = f"https://{self.headers.get('Host')}"
        path = self.path.split("?", 1)[0].rstrip("/")
        status, headers, payload = self.route(method, path, body, base_url)
        if self.delay:
            time.sleep(self.delay)
        data = json.dumps(payload).encode()
        self.send_response(status)
        for header, value in headers:
            self.send_header(header, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def route(
        self, method: str, path: str, body: bytes, base_url: str
    ) -> tuple[int, list[tuple[str, str]], dict]:
        # Keystone
        if method == "POST" and path.endswith("/auth/tokens"):
            return 201, [("X-Subject-Token", ADMIN_TOKEN)], auth_response(base_url)
        # Token self-validation, what the mcp_token plugin calls to get the
        # service catalog for token+endpoint credentials
        if method == "GET" and path.endswith("/auth/tokens"):
            return 200, [], auth_response(base_url)
        if path in ("", "/v3"):
            return 200, [], version_doc(base_url)
        if path.endswith("/projects"):
            return 200, [], PROJECTS
        # Cinder
        if method == "POST" and path.endswith("/volumes"):
            return 200, [], self.cluster.create_volume(body)
        if path.endswith(("/volumes", "/volumes/detail")):
            return 200, [], self.cluster.list_volumes()
        if "/volumes/" in path:
            key = path.rsplit("/", 1)[1]
            if method == "DELETE":
                return 202, [], self.cluster.delete_volume(key)
            volume = self.cluster.get_volume(key)
            if volume is None:
                return (
                    404,
                    [],
                    {
                        "itemNotFound": {
                            "message": f"Volume {key} could not be found.",
                            "code": 404,
                        }
                    },
                )
            return 200, [], {"volume": volume}
        # Kubernetes / OpenShift
        if path == "/version":
            return 200, [], KUBE_VERSION
        if path == "/api":
            return (
                200,
                [],
                {
                    "kind": "APIVersions",
                    "versions": ["v1"],
                    "serverAddressByClientCIDRs": [],
                },
            )
        if path == "/apis":
            return 200, [], {"kind": "APIGroupList", "apiVersion": "v1", "groups": []}
        if path == "/api/v1":
            return 200, [], API_V1
        if path.endswith("/users/~"):
            return 200, [], KUBE_USER
        if path.endswith("/pods"):
            # oc rejects objects whose namespace differs from the requested one
            namespace = path.partition("/namespaces/")[2].split("/")[0] or None
            return 200, [], pod_list(namespace)
        if path.endswith("/services"):
            return 200, [], SERVICES
        if path == "/api/v1/namespaces":
            if method == "POST":
                return 201, [], self.cluster.create_namespace(body)
            return 200, [], self.cluster.list_namespaces()
        if path.startswith("/api/v1/namespaces/"):
            name = path.rsplit("/", 1)[1]
            if method == "DELETE":
                deleted = self.cluster.delete_namespace(name)
                if deleted is None:
                    return 404, [], kube_status(404, path)
                return 200, [], deleted
            namespace = self.cluster.get_namespace(name)
            if namespace is None:
                return 404, [], kube_status(404, path)
            return 200, [], namespace
        if path.startswith(("/api", "/apis", "/version", "/openapi", "/metrics")):
            return 404, [], kube_status(404, path)
        # Unknown OpenStack-ish path: answer with the version document (the
        # shape validated by the probes) so discovery variations keep working.
        return 200, [], version_doc(base_url)

    def do_GET(self) -> None:
        self._respond("GET")

    def do_POST(self) -> None:
        self._respond("POST")

    def do_DELETE(self) -> None:
        self._respond("DELETE")

    def log_message(self, *args) -> None:
        pass


def generate_cert(out_dir: Path) -> tuple[Path, Path]:
    cert = out_dir / "cert.pem"
    key = out_dir / "key.pem"
    subprocess.run(  # noqa: S603 -- fixed argv
        [  # noqa: S607 -- openssl resolved from PATH
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-nodes",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def write_credentials(out_dir: Path, base_url: str) -> None:
    (out_dir / "clouds.yaml").write_text(
        f"""clouds:
  {CLOUD_NAME}:
    auth:
      auth_url: {base_url}/v3
      username: admin
      project_name: admin
      user_domain_name: Default
      project_domain_name: Default
    verify: false
"""
    )
    (out_dir / "secure.yaml").write_text(
        f"""clouds:
  {CLOUD_NAME}:
    auth:
      password: secret
"""
    )
    (out_dir / "kubeconfig").write_text(
        f"""apiVersion: v1
kind: Config
clusters:
- name: {CLOUD_NAME}
  cluster:
    server: {base_url}
    insecure-skip-tls-verify: true
contexts:
- name: {CLOUD_NAME}
  context:
    cluster: {CLOUD_NAME}
    user: {CLOUD_NAME}
current-context: {CLOUD_NAME}
users:
- name: {CLOUD_NAME}
  user:
    token: {OC_TOKEN}
"""
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fake OpenStack + OpenShift cluster for integration tests"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Address to bind to")
    parser.add_argument(
        "--port", type=int, default=0, help="Port to bind to (0: random)"
    )
    parser.add_argument(
        "--out-dir", required=True, help="Directory for the generated credentials"
    )
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="Host the credentials point at (default: --host, or 127.0.0.1 "
        "when binding to 0.0.0.0); use host.containers.internal to serve "
        "containers",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to sleep before answering each request",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cert, key = generate_cert(out_dir)

    FakeClusterHandler.delay = args.delay
    server = ThreadingHTTPServer((args.host, args.port), FakeClusterHandler)
    port = server.server_address[1]
    advertise_host = args.advertise_host or (
        "127.0.0.1" if args.host == "0.0.0.0" else args.host  # noqa: S104
    )
    base_url = f"https://{advertise_host}:{port}"

    write_credentials(out_dir, base_url)
    (out_dir / "fake-cluster.pid").write_text(f"{os.getpid()}\n")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)

    print(f"Fake cluster listening on {base_url} (delay={args.delay}s)")
    print(f"Credentials written to {out_dir}: clouds.yaml, secure.yaml, kubeconfig")
    print(f"PID {os.getpid()} written to {out_dir / 'fake-cluster.pid'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
