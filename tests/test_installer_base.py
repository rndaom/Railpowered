"""Shared fixtures for the installer tests (no test cases of its own).

Every test runs against a fresh temporary data volume with the network
replaced by :class:`FakeNet`, fake Java installs, a scrubbed environment and
reset module caches, so nothing leaks between tests or reaches the internet.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import installer  # noqa: E402

# Environment variables that change installer behaviour; tests start without them.
_ISOLATED_ENV = (
    "MC_DIR",
    "SERVER_TYPE",
    "MINECRAFT_VERSION",
    "IDLE_TIMEOUT",
    "AUTO_START",
    "BACKUP_KEEP",
    "MAX_UPLOAD_MB",
    "MAX_EXTRACT_MB",
    "FABRIC_LOADER_VERSION",
    "FABRIC_INSTALLER_VERSION",
    "ADMIN_KEY",
)

_SYMLINK_OK: bool | None = None


def can_symlink() -> bool:
    """True when this process may create directory symlinks (Windows needs
    Developer Mode or admin rights)."""
    global _SYMLINK_OK
    if _SYMLINK_OK is None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "target")
            os.mkdir(target)
            try:
                os.symlink(target, os.path.join(tmp, "link"), target_is_directory=True)
                _SYMLINK_OK = True
            except (OSError, NotImplementedError):
                _SYMLINK_OK = False
    return _SYMLINK_OK


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeResponse:
    def __init__(self, body: bytes = b"", status: int = 200, headers: dict | None = None):
        self._buffer = io.BytesIO(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNet:
    """Stands in for installer._urlopen. Unknown URLs fail like a dead network."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.calls: list[str] = []

    def add(self, url: str, body: object = b"", status: int = 200) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.routes[url] = (status, body)

    def add_error(self, url: str, error: BaseException) -> None:
        self.routes[url] = error

    def count(self, fragment: str = "") -> int:
        return sum(1 for url in self.calls if fragment in url)

    def __call__(self, request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            raise urllib.error.URLError("network disabled in tests")
        if isinstance(route, BaseException):
            raise route
        if callable(route):
            return route(url)
        status, body = route
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(body))
        return FakeResponse(body, status)


class InstallerTestCase(unittest.TestCase):
    """Fresh data volume, fake network and Java, scrubbed env, reset caches."""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)

        env = {k: v for k, v in os.environ.items() if k not in _ISOLATED_ENV}
        self._patch(mock.patch.dict(os.environ, env, clear=True))

        self.net = FakeNet()
        self.java_homes = {}
        for major in (8, 21, 25):
            home = os.path.join(self.tmp, "java", str(major))
            os.makedirs(os.path.join(home, "bin"))
            with open(os.path.join(home, "bin", "java"), "w", encoding="utf-8") as handle:
                handle.write("")
            self.java_homes[major] = home
        self.sleeps: list[float] = []

        self._patch(mock.patch.object(installer, "DATA_DIR", self.data))
        self._patch(mock.patch.object(installer, "_urlopen", self.net))
        self._patch(mock.patch.object(installer, "_sleep", self.sleeps.append))
        self._patch(mock.patch.object(installer, "JAVA_HOMES", dict(self.java_homes)))
        self._patch(mock.patch.object(installer, "FABRIC_LOADER_PIN", ""))
        self._patch(mock.patch.object(installer, "FABRIC_INSTALLER_PIN", ""))
        self._patch(mock.patch.object(installer.shutil, "which", return_value=None))
        self._patch(mock.patch.object(installer, "_log_handler", None))
        installer._reset_caches()
        self.addCleanup(installer._reset_caches)

    def _patch(self, patcher) -> object:
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    # -- helpers -----------------------------------------------------------

    def path(self, *parts: str) -> str:
        return os.path.join(self.data, *parts)

    def write(self, rel: str, content: str | bytes = "") -> str:
        full = self.path(*rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
        with open(full, mode, **kwargs) as handle:
            handle.write(content)
        return full

    def read(self, rel: str) -> str:
        with open(self.path(*rel.split("/")), encoding="utf-8") as handle:
            return handle.read()

    def read_json(self, rel: str):
        return json.loads(self.read(rel))

    def make_world(self, name: str, last_version: str | None = None, extra: dict | None = None) -> str:
        directory = self.path("worlds", name)
        os.makedirs(os.path.join(directory, "region"), exist_ok=True)
        with open(os.path.join(directory, "level.dat"), "wb") as handle:
            handle.write(b"level-" + name.encode())
        with open(os.path.join(directory, "region", "r.0.0.mca"), "wb") as handle:
            handle.write(b"region-data")
        for rel, content in (extra or {}).items():
            full = os.path.join(directory, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(content)
        if last_version:
            with open(os.path.join(directory, installer.WORLD_META_FILE), "w", encoding="utf-8") as handle:
                json.dump({"last_version": last_version, "last_type": "vanilla", "last_played": None}, handle)
        return directory

    def vanilla_jar(self, version: str) -> str:
        """Put a vanilla server jar on the volume (so no download is needed)."""
        return self.write(f"jars/minecraft_server.{version}.jar", b"jar-" + version.encode())

    def manifest(self, latest: str = "26.3", releases=None, snapshots=("26.4-snapshot-1",)) -> dict:
        releases = list(releases or ("26.3", "1.21.1", "1.20.1", "1.12.2", "1.7.10", "1.2.5", "1.0"))
        versions = [
            {
                "id": v,
                "type": "release",
                "url": f"https://piston-meta.mojang.com/v1/packages/{v}.json",
                "releaseTime": "2020-01-01T00:00:00+00:00",
            }
            for v in releases
        ]
        versions += [
            {"id": v, "type": "snapshot", "url": f"https://piston-meta.mojang.com/v1/packages/{v}.json"}
            for v in snapshots
        ]
        data = {"latest": {"release": latest, "snapshot": latest}, "versions": versions}
        self.net.add(installer.MANIFEST_URL, data)
        return data

    def version_json(self, version: str, java: int | None, jar: bytes | None = None) -> bytes:
        jar = jar if jar is not None else b"server-" + version.encode()
        jar_url = f"https://piston-data.mojang.com/v1/objects/{sha1(jar)}/server.jar"
        data = {
            "id": version,
            "downloads": {"server": {"url": jar_url, "sha1": sha1(jar), "size": len(jar)}},
        }
        if java is not None:
            data["javaVersion"] = {"component": "java-runtime", "majorVersion": java}
        self.net.add(f"https://piston-meta.mojang.com/v1/packages/{version}.json", data)
        self.net.add(jar_url, jar)
        return jar

    def make_zip(self, entries: dict[str, bytes | str], name: str = "pack.zip") -> str:
        path = os.path.join(self.tmp, name)
        with zipfile.ZipFile(path, "w") as archive:
            for arcname, content in entries.items():
                if isinstance(content, str):
                    content = content.encode("utf-8")
                archive.writestr(arcname, content)
        return path

    def make_mod_jar(self, marker: str = "fabric.mod.json") -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(marker, "{}")
        return buffer.getvalue()

    def tar_members(self, path: str) -> list[str]:
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
