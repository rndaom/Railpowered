#!/usr/bin/env python3
"""Check or bump the Eclipse Temurin JREs pinned in the Dockerfile.

The Dockerfile pins each JRE (8, 21, 25) by its Adoptium release name and the
SHA-256 of the Linux tarball for amd64 and arm64:

    ARG JRE21_RELEASE=jdk-21.0.12.1+1
    ARG JRE21_SHA256_AMD64=...
    ARG JRE21_SHA256_ARM64=...

Usage:
    python3 .github/scripts/jre_versions.py            # report; exit 1 if outdated
    python3 .github/scripts/jre_versions.py --write    # update the Dockerfile in place

Exit codes: 0 up to date (or written), 1 newer releases exist, 2 error.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
MAJORS = (8, 21, 25)
ARCHES = {"AMD64": "x64", "ARM64": "aarch64"}
API = (
    "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
    "?architecture={arch}&image_type=jre&os=linux&vendor=eclipse"
)
ARG_RE = re.compile(r"^ARG (JRE(?:8|21|25)_(?:RELEASE|SHA256_AMD64|SHA256_ARM64))=(\S*)$", re.M)


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(2)


def pinned(text: str) -> dict[str, str]:
    """Return the pinned ARG values; a name used twice must have one value."""
    values: dict[str, str] = {}
    for name, value in ARG_RE.findall(text):
        if values.setdefault(name, value) != value:
            fail(f"Dockerfile pins {name} to two different values")
    expected = {f"JRE{m}_{k}" for m in MAJORS for k in ("RELEASE", "SHA256_AMD64", "SHA256_ARM64")}
    missing = sorted(expected - set(values))
    if missing:
        fail(f"Dockerfile is missing: {', '.join(missing)}")
    return values


def latest(major: int, arch: str) -> tuple[str, str]:
    """Return (release_name, sha256) of the newest GA Linux JRE tarball."""
    request = urllib.request.Request(
        API.format(major=major, arch=arch),
        headers={"User-Agent": "railpowered-jre-check"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assets = json.load(response)
    for asset in assets:
        package = asset.get("binary", {}).get("package", {})
        if package.get("name", "").endswith(".tar.gz") and package.get("checksum"):
            return asset["release_name"], package["checksum"]
    raise RuntimeError(f"No Linux {arch} JRE tarball found for Java {major}")


def wanted() -> dict[str, str]:
    values: dict[str, str] = {}
    for major in MAJORS:
        releases = set()
        for suffix, arch in ARCHES.items():
            release, checksum = latest(major, arch)
            releases.add(release)
            values[f"JRE{major}_SHA256_{suffix}"] = checksum
        if len(releases) != 1:
            # amd64 and arm64 builds can land days apart; wait for both.
            raise RuntimeError(
                f"Java {major}: amd64 and arm64 differ ({', '.join(sorted(releases))}); try again later"
            )
        values[f"JRE{major}_RELEASE"] = releases.pop()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="update the Dockerfile in place")
    args = parser.parse_args()

    with open(DOCKERFILE, encoding="utf-8") as handle:
        text = handle.read()
    current = pinned(text)
    try:
        target = wanted()
    except (OSError, urllib.error.URLError, RuntimeError, ValueError, KeyError) as exc:
        print(f"Could not query the Adoptium API: {exc}", file=sys.stderr)
        return 2

    changes = [name for name in sorted(target) if current[name] != target[name]]
    if not changes:
        print("The Temurin JREs pinned in the Dockerfile are the latest GA releases.")
        return 0

    print("Newer Eclipse Temurin JREs are available:\n")
    print("| Java | Pinned | Latest |")
    print("|---|---|---|")
    for major in MAJORS:
        name = f"JRE{major}_RELEASE"
        if any(change.startswith(f"JRE{major}_") for change in changes):
            print(f"| {major} | `{current[name]}` | `{target[name]}` |")
    print("\nUpdate with `python3 .github/scripts/jre_versions.py --write`, then let CI build the image.")

    if not args.write:
        return 1
    updated = ARG_RE.sub(lambda m: f"ARG {m.group(1)}={target[m.group(1)]}", text)
    with open(DOCKERFILE, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    print("\nDockerfile updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
