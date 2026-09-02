#!/usr/bin/env python3
"""Download and prepare Minecraft server runtimes for the admin panel."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

DATA_DIR = os.environ.get("MC_DIR", "/server/data")
IMAGE_JAR_DIR = "/server/jars"
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META_URL = "https://meta.fabricmc.net/v2"
MOJANG_PROFILE_URL = "https://api.mojang.com/users/profiles/minecraft/{name}"
FABRIC_INSTALLER_PIN = os.environ.get("FABRIC_INSTALLER_VERSION", "").strip()
FABRIC_LOADER_PIN = os.environ.get("FABRIC_LOADER_VERSION", "").strip()

VANILLA_125_SHA1 = "d8321edc9470e56b8ad5c67bbd16beba25843336"
VANILLA_125_URL = (
    f"https://launcher.mojang.com/v1/objects/{VANILLA_125_SHA1}/server.jar"
)

FORGE_BUILDS = {
    "1.7.10": {
        "installer": (
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "1.7.10-10.13.4.1614-1.7.10/"
            "forge-1.7.10-10.13.4.1614-1.7.10-installer.jar"
        ),
    },
    "1.12.2": {
        "installer": (
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "1.12.2-14.23.5.2859/forge-1.12.2-14.23.5.2859-installer.jar"
        ),
    },
    "1.16.5": {
        "installer": (
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "1.16.5-36.2.39/forge-1.16.5-36.2.39-installer.jar"
        ),
    },
}

CURATED_VANILLA = [
    "1.2.5",
    "1.5.2",
    "1.6.4",
    "1.7.10",
    "1.8.9",
    "1.12.2",
    "1.16.5",
    "1.20.1",
    "1.21.1",
    "26.2",
]
CURATED_FABRIC = ["1.20.1", "1.21.1", "26.2"]
WORLD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

JAVA_HOMES = {
    8: os.environ.get("JAVA_8_HOME", "/opt/java/8"),
    21: os.environ.get("JAVA_21_HOME", "/opt/java/21"),
    25: os.environ.get("JAVA_25_HOME", "/opt/java/25"),
}


class InstallError(Exception):
    pass


@dataclass
class RuntimeSpec:
    java_bin: str
    cwd: str
    jar: str
    extra_args: list[str] = field(default_factory=lambda: ["nogui"])
    jvm_args: list[str] = field(default_factory=list)
    prefix_args: list[str] = field(default_factory=list)
    version: str = "1.2.5"
    type: str = "vanilla"
    level_name: str = "world"
    java_major: int = 8


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in re.split(r"[.\-_]", version.strip()):
        if token.isdigit():
            parts.append(int(token))
        else:
            break
    if not parts:
        raise InstallError(f"Invalid Minecraft version: {version}")
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_year_version(version: str) -> bool:
    return parse_version(version)[0] >= 26


def is_legacy_protocol(version: str) -> bool:
    parsed = parse_version(version)
    return (not is_year_version(version)) and parsed < (1, 7, 0)


def uses_legacy_files(version: str) -> bool:
    parsed = parse_version(version)
    return (not is_year_version(version)) and parsed < (1, 8, 0)


def java_major_for_version(version: str) -> int:
    if is_year_version(version) or parse_version(version) >= (1, 21, 11):
        return 25 if os.path.isdir(JAVA_HOMES[25]) else 21
    if parse_version(version) >= (1, 17, 0):
        return 21
    return 8


def java_bin(major: int | None = None, version: str | None = None) -> str:
    if major is None:
        if version is None:
            raise InstallError("java_bin requires major or version")
        major = java_major_for_version(version)
    home = JAVA_HOMES.get(major, "")
    candidate = os.path.join(home, "bin", "java")
    if os.path.isfile(candidate):
        return candidate
    fallback = shutil.which("java")
    if fallback:
        return fallback
    raise InstallError(f"Java {major} is not installed")


def fabric_installer_version() -> str:
    if FABRIC_INSTALLER_PIN:
        return FABRIC_INSTALLER_PIN
    try:
        data = _http_json(f"{FABRIC_META_URL}/versions/installer")
        if isinstance(data, list):
            for item in data:
                if item.get("stable") and item.get("version"):
                    return str(item["version"])
            if data and data[0].get("version"):
                return str(data[0]["version"])
    except InstallError:
        pass
    return "1.1.2"


def fabric_loader_for(mc_version: str) -> str:
    if FABRIC_LOADER_PIN:
        return FABRIC_LOADER_PIN
    try:
        data = _http_json(f"{FABRIC_META_URL}/versions/loader/{mc_version}")
        if isinstance(data, list):
            for item in data:
                loader = item.get("loader") or {}
                if loader.get("stable") and loader.get("version"):
                    return str(loader["version"])
            if data:
                loader = data[0].get("loader") or {}
                if loader.get("version"):
                    return str(loader["version"])
    except InstallError:
        pass
    if is_year_version(mc_version) or parse_version(mc_version) >= (1, 21, 0):
        return "0.19.3"
    return "0.16.14"


def offline_uuid(name: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    hexed = bytes(digest).hex()
    return f"{hexed[0:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}"


def dashed_uuid(raw: str) -> str:
    compact = re.sub(r"[^0-9a-fA-F]", "", raw)
    if len(compact) != 32:
        raise InstallError("Invalid UUID")
    compact = compact.lower()
    return (
        f"{compact[0:8]}-{compact[8:12]}-{compact[12:16]}-"
        f"{compact[16:20]}-{compact[20:32]}"
    )


def player_uuid(name: str, online: bool = False) -> str:
    if online:
        try:
            data = _http_json(MOJANG_PROFILE_URL.format(name=name))
            if isinstance(data, dict) and data.get("id"):
                return dashed_uuid(str(data["id"]))
        except (InstallError, ValueError):
            pass
    return offline_uuid(name)


def paths() -> dict[str, str]:
    return {
        "data": DATA_DIR,
        "jars": os.path.join(DATA_DIR, "jars"),
        "worlds": os.path.join(DATA_DIR, "worlds"),
        "backups": os.path.join(DATA_DIR, "backups"),
        "instances": os.path.join(DATA_DIR, "instances"),
        "modpacks": os.path.join(DATA_DIR, "modpacks"),
        "config": os.path.join(DATA_DIR, "manager.json"),
        "properties": os.path.join(DATA_DIR, "server.properties"),
    }


def ensure_layout() -> None:
    for key in ("jars", "worlds", "backups", "instances", "modpacks"):
        os.makedirs(paths()[key], exist_ok=True)
    os.makedirs(os.path.join(paths()["worlds"], "world"), exist_ok=True)


def default_config() -> dict[str, Any]:
    return {
        "type": "vanilla",
        "minecraft_version": "1.2.5",
        "level_name": "world",
        "modpack": None,
        "instance": None,
    }


def load_config() -> dict[str, Any]:
    ensure_layout()
    cfg_path = paths()["config"]
    cfg = default_config()
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    env_type = os.environ.get("SERVER_TYPE", "").strip().lower()
    env_version = os.environ.get("MINECRAFT_VERSION", "").strip()
    if env_type and not os.path.isfile(cfg_path):
        cfg["type"] = env_type
    if env_version and not os.path.isfile(cfg_path):
        cfg["minecraft_version"] = env_version
    if cfg.get("type") not in {"vanilla", "fabric", "forge", "modpack"}:
        cfg["type"] = "vanilla"
    if not WORLD_NAME_RE.match(str(cfg.get("level_name") or "")):
        cfg["level_name"] = "world"
    return cfg


def save_config(cfg: dict[str, Any]) -> dict[str, Any]:
    ensure_layout()
    merged = default_config()
    merged.update(cfg)
    with open(paths()["config"], "w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")
    return merged


def sha1_file(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, dest: str, expected_sha1: str | None = None) -> str:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isfile(dest) and (
        expected_sha1 is None or sha1_file(dest) == expected_sha1
    ):
        return dest
    tmp = dest + ".tmp"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BetaServer-manager/1.2.5"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, open(
            tmp, "wb"
        ) as handle:
            shutil.copyfileobj(response, handle)
    except urllib.error.URLError as exc:
        raise InstallError(f"Download failed: {url} ({exc})") from exc
    if expected_sha1 and sha1_file(tmp) != expected_sha1:
        os.remove(tmp)
        raise InstallError(f"SHA-1 mismatch for {url}")
    os.replace(tmp, dest)
    return dest


def _http_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BetaServer-manager/1.2.5"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.URLError as exc:
        raise InstallError(f"Request failed: {url} ({exc})") from exc


def fetch_version_manifest() -> dict[str, Any]:
    return _http_json(MANIFEST_URL)


def vanilla_jar_path(version: str) -> str:
    return os.path.join(paths()["jars"], f"minecraft_server.{version}.jar")


def install_vanilla(version: str) -> str:
    dest = vanilla_jar_path(version)
    image_pin = os.path.join(IMAGE_JAR_DIR, "minecraft_server.1.2.5.jar")
    if version == "1.2.5":
        if os.path.isfile(image_pin):
            if not os.path.isfile(dest) or sha1_file(dest) != VANILLA_125_SHA1:
                shutil.copy2(image_pin, dest)
            if sha1_file(dest) != VANILLA_125_SHA1:
                raise InstallError("Pinned 1.2.5 jar failed SHA-1 check")
            return dest
        return download_file(VANILLA_125_URL, dest, VANILLA_125_SHA1)

    manifest = fetch_version_manifest()
    entry = next(
        (item for item in manifest.get("versions", []) if item.get("id") == version),
        None,
    )
    if not entry:
        raise InstallError(f"Minecraft {version} is not in the Mojang manifest")
    meta = _http_json(entry["url"])
    server = meta.get("downloads", {}).get("server")
    if not server:
        raise InstallError(f"No official server jar for {version}")
    return download_file(server["url"], dest, server.get("sha1"))


def instance_dir(server_type: str, version: str, name: str | None = None) -> str:
    if name:
        slug = name
    else:
        slug = f"{server_type}-{version}"
    return os.path.join(paths()["instances"], slug)


def install_fabric(
    version: str, dest: str | None = None, loader: str | None = None
) -> str:
    dest = dest or instance_dir("fabric", version)
    os.makedirs(dest, exist_ok=True)
    loader = loader or fabric_loader_for(version)
    marker = os.path.join(dest, ".fabric-ready")
    launch = os.path.join(dest, "fabric-server-launch.jar")
    if os.path.isfile(launch):
        return dest
    installer_ver = fabric_installer_version()
    installer = download_file(
        (
            "https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
            f"{installer_ver}/"
            f"fabric-installer-{installer_ver}.jar"
        ),
        os.path.join(paths()["jars"], f"fabric-installer-{installer_ver}.jar"),
    )
    java = java_bin(version=version)
    try:
        subprocess.run(
            [
                java,
                "-jar",
                installer,
                "server",
                "-mcversion",
                version,
                "-loader",
                loader,
                "-downloadMinecraft",
                "-dir",
                dest,
                "-noprofile",
            ],
            check=True,
            cwd=dest,
            timeout=300,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stdout", "") or str(exc)
        raise InstallError(f"Fabric install failed for {version}: {detail[-800:]}") from exc
    if not os.path.isfile(launch):
        raise InstallError("Fabric installer did not produce fabric-server-launch.jar")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(f"{version}\n{loader}\n")
    return dest


def _forge_installer_url(mc_version: str, forge_version: str | None = None) -> str:
    if mc_version in FORGE_BUILDS:
        return FORGE_BUILDS[mc_version]["installer"]
    if not forge_version:
        raise InstallError(
            f"Forge {mc_version} is not in the curated list "
            f"({', '.join(FORGE_BUILDS)})"
        )
    rev = f"{mc_version}-{forge_version}"
    return (
        "https://maven.minecraftforge.net/net/minecraftforge/forge/"
        f"{rev}/forge-{rev}-installer.jar"
    )


def install_forge(
    version: str, dest: str | None = None, forge_version: str | None = None
) -> str:
    dest = dest or instance_dir("forge", version)
    os.makedirs(dest, exist_ok=True)
    marker = os.path.join(dest, ".forge-ready")
    if _find_forge_launch(dest):
        return dest
    installer_url = _forge_installer_url(version, forge_version)
    installer = download_file(
        installer_url,
        os.path.join(paths()["jars"], os.path.basename(urlparse(installer_url).path)),
    )
    java = java_bin(version=version)
    try:
        subprocess.run(
            [java, "-jar", installer, "--installServer"],
            check=True,
            cwd=dest,
            timeout=300,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stdout", "") or str(exc)
        raise InstallError(f"Forge install failed for {version}: {detail[-800:]}") from exc
    if not _find_forge_launch(dest):
        raise InstallError("Forge installer did not produce a launch jar or args file")
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(version + "\n")
    return dest


def _find_forge_launch(directory: str) -> str | None:
    unix_args = []
    for root, _dirs, files in os.walk(os.path.join(directory, "libraries")):
        for name in files:
            if name == "unix_args.txt":
                unix_args.append(os.path.join(root, name))
    if unix_args:
        return unix_args[0]
    for name in os.listdir(directory):
        lower = name.lower()
        if (
            lower.startswith("forge-")
            and lower.endswith(".jar")
            and "installer" not in lower
        ):
            return os.path.join(directory, name)
    return None


def install_modpack_from_url(url: str, name: str) -> dict[str, Any]:
    if not WORLD_NAME_RE.match(name):
        raise InstallError("Invalid instance name")
    dest_archive = os.path.join(
        paths()["modpacks"],
        name + os.path.splitext(urlparse(url).path)[1] or ".zip",
    )
    download_file(url, dest_archive)
    return install_modpack_archive(dest_archive, name)


def install_modpack_archive(archive_path: str, name: str) -> dict[str, Any]:
    if not WORLD_NAME_RE.match(name):
        raise InstallError("Invalid instance name")
    dest = instance_dir("modpack", name, name=name)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    lower = archive_path.lower()
    if lower.endswith(".mrpack") or _zip_has(archive_path, "modrinth.index.json"):
        _install_mrpack(archive_path, dest)
    else:
        _safe_extract_zip(archive_path, dest)
    return {
        "type": "modpack",
        "minecraft_version": _detect_instance_version(dest),
        "level_name": "world",
        "modpack": name,
        "instance": name,
    }


def _zip_has(archive_path: str, inner: str) -> bool:
    try:
        with zipfile.ZipFile(archive_path) as zf:
            return any(item.filename.endswith(inner) for item in zf.infolist())
    except zipfile.BadZipFile:
        return False


def _safe_extract_zip(archive_path: str, dest: str) -> None:
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise InstallError(f"Refusing unsafe path in zip: {name}")
            zf.extract(info, dest)


def _install_mrpack(archive_path: str, dest: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _safe_extract_zip(archive_path, tmp)
        index_path = os.path.join(tmp, "modrinth.index.json")
        if not os.path.isfile(index_path):
            raise InstallError("mrpack is missing modrinth.index.json")
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
        for file_info in index.get("files", []):
            env = file_info.get("env") or {}
            if env.get("server") == "unsupported":
                continue
            rel = file_info.get("path") or ""
            if not rel or rel.startswith("/") or ".." in rel.split("/"):
                raise InstallError(f"Unsafe mrpack path: {rel}")
            urls = file_info.get("downloads") or []
            if not urls:
                continue
            hashes = file_info.get("hashes") or {}
            download_file(
                urls[0],
                os.path.join(dest, rel),
                hashes.get("sha1"),
            )
        for folder in ("overrides", "server-overrides"):
            override = os.path.join(tmp, folder)
            if os.path.isdir(override):
                shutil.copytree(override, dest, dirs_exist_ok=True)
        deps = index.get("dependencies") or {}
        meta_path = os.path.join(dest, "modpack-meta.json")
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "name": index.get("name"),
                    "dependencies": deps,
                },
                handle,
                indent=2,
            )
        _install_modpack_loader(dest, deps if isinstance(deps, dict) else {})


def _has_mod_jars(directory: str) -> bool:
    mods = os.path.join(directory, "mods")
    if not os.path.isdir(mods):
        return False
    return any(name.endswith(".jar") for name in os.listdir(mods))


def _install_modpack_loader(dest: str, deps: dict[str, Any]) -> None:
    mc_version = str(deps.get("minecraft") or "").strip()
    if not mc_version:
        detected = _detect_instance_version(dest)
        if detected and detected != "1.2.5":
            mc_version = detected
    if not mc_version:
        if _find_modpack_jar(dest) or _find_forge_launch(dest):
            return
        raise InstallError("Modpack does not declare a Minecraft version")
    if deps.get("quilt-loader"):
        raise InstallError("Quilt modpacks are not supported")
    if deps.get("neoforge"):
        raise InstallError("NeoForge modpacks are not supported")
    if deps.get("fabric-loader"):
        install_fabric(mc_version, dest=dest, loader=str(deps["fabric-loader"]))
        return
    if deps.get("forge"):
        install_forge(mc_version, dest=dest, forge_version=str(deps["forge"]))
        return
    if _find_modpack_jar(dest) or _find_forge_launch(dest):
        return
    if _has_mod_jars(dest):
        install_fabric(mc_version, dest=dest)
        return
    raise InstallError(
        "Modpack has no Fabric/Forge loader. Use a Modrinth .mrpack "
        "or a zip that already includes the server jar."
    )


def _detect_instance_version(directory: str) -> str:
    meta = os.path.join(directory, "modpack-meta.json")
    if os.path.isfile(meta):
        try:
            with open(meta, encoding="utf-8") as handle:
                deps = json.load(handle).get("dependencies") or {}
            if deps.get("minecraft"):
                return str(deps["minecraft"])
        except (OSError, json.JSONDecodeError):
            pass
    return "1.2.5"


def read_properties(path: str | None = None) -> dict[str, str]:
    props: dict[str, str] = {}
    target = path or paths()["properties"]
    if not os.path.isfile(target):
        return props
    with open(target, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            props[key.strip()] = value
    return props


def write_properties(values: dict[str, str], path: str | None = None) -> None:
    target = path or paths()["properties"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("# Generated by BetaServer manager\n")
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def properties_for(cfg: dict[str, Any]) -> dict[str, str]:
    version = str(cfg.get("minecraft_version") or "1.2.5")
    level = str(cfg.get("level_name") or "world")
    existing = read_properties()
    if uses_legacy_files(version):
        props = {
            "allow-nether": "true",
            "level-name": f"worlds/{level}",
            "enable-query": "false",
            "allow-flight": "false",
            "server-port": "25565",
            "level-type": "DEFAULT",
            "enable-rcon": "false",
            "level-seed": existing.get("level-seed", ""),
            "server-ip": "",
            "max-build-height": "256",
            "spawn-npcs": "true",
            "white-list": existing.get("white-list", "true"),
            "spawn-animals": "true",
            "online-mode": existing.get("online-mode", "false"),
            "pvp": existing.get("pvp", "true"),
            "difficulty": existing.get("difficulty", "1")
            if existing.get("difficulty", "1").isdigit()
            else "1",
            "gamemode": existing.get("gamemode", "0")
            if existing.get("gamemode", "0").isdigit()
            else "0",
            "max-players": existing.get("max-players", "20"),
            "spawn-monsters": "true",
            "generate-structures": "true",
            "view-distance": existing.get("view-distance", "8"),
            "spawn-protection": existing.get("spawn-protection", "0"),
            "motd": existing.get("motd", f"{version} friends server"),
        }
        if is_legacy_protocol(version):
            props["online-mode"] = existing.get("online-mode", "false")
            props["white-list"] = existing.get("white-list", "true")
        return props

    props = {
        "level-name": f"worlds/{level}",
        "server-port": "25565",
        "motd": existing.get("motd", f"{version} friends server"),
        "max-players": existing.get("max-players", "20"),
        "view-distance": existing.get("view-distance", "10"),
        "simulation-distance": existing.get("simulation-distance", "10"),
        "spawn-protection": existing.get("spawn-protection", "0"),
        "difficulty": "normal",
        "gamemode": "survival",
        "hardcore": "false",
        "pvp": existing.get("pvp", "true"),
        "online-mode": existing.get("online-mode", "true"),
        "white-list": existing.get("white-list", existing.get("enforce-whitelist", "false")),
        "allow-nether": "true",
        "enable-command-block": "false",
        "enable-status": "true",
        "enforce-secure-profile": "false",
        "sync-chunk-writes": "true",
        "server-ip": "",
        "enable-rcon": "false",
    }
    return props


def apply_properties(cfg: dict[str, Any]) -> None:
    props = properties_for(cfg)
    write_properties(props)
    try:
        run_dir = runtime_cwd(cfg)
    except InstallError:
        return
    if run_dir != DATA_DIR:
        write_properties(props, os.path.join(run_dir, "server.properties"))
        _relink_instance_world(cfg)
        sync_access_lists(cfg)


def world_path(name: str) -> str:
    if not WORLD_NAME_RE.match(name):
        raise InstallError("Invalid world name")
    return os.path.join(paths()["worlds"], name)


def list_worlds() -> list[dict[str, Any]]:
    ensure_layout()
    worlds_dir = paths()["worlds"]
    cfg = load_config()
    active = cfg.get("level_name") or "world"
    result = []
    for name in sorted(os.listdir(worlds_dir)):
        full = os.path.join(worlds_dir, name)
        if not os.path.isdir(full) or not WORLD_NAME_RE.match(name):
            continue
        size = 0
        mtime = os.path.getmtime(full)
        for root, _dirs, files in os.walk(full):
            for filename in files:
                try:
                    stat = os.stat(os.path.join(root, filename))
                except OSError:
                    continue
                size += stat.st_size
                mtime = max(mtime, stat.st_mtime)
        result.append(
            {
                "name": name,
                "active": name == active,
                "size": size,
                "modified": int(mtime),
                "has_level": os.path.isfile(os.path.join(full, "level.dat")),
            }
        )
    if not any(item["name"] == "world" for item in result):
        os.makedirs(world_path("world"), exist_ok=True)
        return list_worlds()
    return result


def create_world(name: str) -> str:
    dest = world_path(name)
    if os.path.exists(dest):
        raise InstallError(f"World {name} already exists")
    os.makedirs(dest, exist_ok=True)
    return name


def delete_world(name: str) -> None:
    cfg = load_config()
    if name == cfg.get("level_name"):
        raise InstallError("Cannot delete the active world")
    dest = world_path(name)
    if not os.path.isdir(dest):
        raise InstallError("World not found")
    shutil.rmtree(dest)


def select_world(name: str) -> dict[str, Any]:
    dest = world_path(name)
    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
    cfg = load_config()
    cfg["level_name"] = name
    save_config(cfg)
    apply_properties(cfg)
    return cfg


def _relink_instance_world(cfg: dict[str, Any]) -> None:
    run_dir = runtime_cwd(cfg)
    if run_dir == DATA_DIR:
        return
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(world_path(str(cfg.get("level_name") or "world")), exist_ok=True)
    target = paths()["worlds"]
    link = os.path.join(run_dir, "worlds")
    if os.path.islink(link):
        if os.path.realpath(link) != os.path.realpath(target):
            os.remove(link)
            os.symlink(os.path.relpath(target, run_dir), link)
    elif os.path.isdir(link):
        for name in os.listdir(link):
            src = os.path.join(link, name)
            dest = os.path.join(target, name)
            if os.path.exists(dest):
                continue
            shutil.move(src, dest)
        shutil.rmtree(link)
        os.symlink(os.path.relpath(target, run_dir), link)
    elif os.path.exists(link):
        os.remove(link)
        os.symlink(os.path.relpath(target, run_dir), link)
    else:
        os.symlink(os.path.relpath(target, run_dir), link)

    leftover = os.path.join(run_dir, "world")
    if os.path.islink(leftover):
        os.remove(leftover)


def runtime_cwd(cfg: dict[str, Any]) -> str:
    server_type = cfg.get("type") or "vanilla"
    version = str(cfg.get("minecraft_version") or "1.2.5")
    if server_type == "vanilla":
        return DATA_DIR
    if server_type == "modpack":
        name = cfg.get("instance") or cfg.get("modpack")
        if not name:
            raise InstallError("Modpack instance is not set")
        return instance_dir("modpack", version, name=str(name))
    return instance_dir(str(server_type), version)


def _write_eula(directory: str) -> None:
    with open(os.path.join(directory, "eula.txt"), "w", encoding="utf-8") as handle:
        handle.write("eula=true\n")


def prepare_runtime(cfg: dict[str, Any] | None = None) -> RuntimeSpec:
    cfg = cfg or load_config()
    ensure_layout()
    server_type = str(cfg.get("type") or "vanilla")
    version = str(cfg.get("minecraft_version") or "1.2.5")
    level = str(cfg.get("level_name") or "world")
    os.makedirs(world_path(level), exist_ok=True)
    major = java_major_for_version(version)
    java = java_bin(major=major)
    jvm = [
        "-Djava.awt.headless=true",
        "-Djava.net.preferIPv4Stack=true",
    ]

    if server_type == "vanilla":
        jar = install_vanilla(version)
        _write_eula(DATA_DIR)
        apply_properties(cfg)
        return RuntimeSpec(
            java_bin=java,
            cwd=DATA_DIR,
            jar=jar,
            extra_args=["nogui"],
            jvm_args=jvm,
            version=version,
            type="vanilla",
            level_name=level,
            java_major=major,
        )

    if server_type == "fabric":
        dest = install_fabric(version)
        _write_eula(dest)
        apply_properties(cfg)
        return RuntimeSpec(
            java_bin=java,
            cwd=dest,
            jar=os.path.join(dest, "fabric-server-launch.jar"),
            extra_args=["nogui"],
            jvm_args=jvm,
            version=version,
            type="fabric",
            level_name=level,
            java_major=major,
        )

    if server_type == "forge":
        dest = install_forge(version)
        _write_eula(dest)
        apply_properties(cfg)
        launch = _find_forge_launch(dest)
        if not launch:
            raise InstallError("Forge launch files are missing")
        if launch.endswith("unix_args.txt"):
            return RuntimeSpec(
                java_bin=java,
                cwd=dest,
                jar="",
                extra_args=["nogui"],
                jvm_args=jvm,
                prefix_args=[f"@{launch}"],
                version=version,
                type="forge",
                level_name=level,
                java_major=major,
            )
        return RuntimeSpec(
            java_bin=java,
            cwd=dest,
            jar=launch,
            extra_args=["nogui"],
            jvm_args=jvm,
            version=version,
            type="forge",
            level_name=level,
            java_major=major,
        )

    if server_type == "modpack":
        dest = runtime_cwd(cfg)
        if not os.path.isdir(dest):
            raise InstallError("Modpack instance is not installed")
        _write_eula(dest)
        apply_properties(cfg)
        launch = _find_forge_launch(dest)
        if launch and launch.endswith("unix_args.txt"):
            return RuntimeSpec(
                java_bin=java,
                cwd=dest,
                jar="",
                extra_args=["nogui"],
                jvm_args=jvm,
                prefix_args=[f"@{launch}"],
                version=version,
                type="modpack",
                level_name=level,
                java_major=major,
            )
        jar = _find_modpack_jar(dest)
        if not jar:
            raise InstallError("Could not find a server jar in the modpack instance")
        return RuntimeSpec(
            java_bin=java,
            cwd=dest,
            jar=jar,
            extra_args=["nogui"],
            jvm_args=jvm,
            version=version,
            type="modpack",
            level_name=level,
            java_major=major,
        )

    raise InstallError(f"Unknown server type: {server_type}")


def _find_modpack_jar(directory: str) -> str | None:
    names = []
    for name in os.listdir(directory):
        if name.endswith(".jar") and "installer" not in name.lower():
            names.append(name)
    for preferred in (
        "fabric-server-launch.jar",
        "server.jar",
        "minecraft_server.jar",
    ):
        path = os.path.join(directory, preferred)
        if os.path.isfile(path):
            return path
    forge = _find_forge_launch(directory)
    if forge and forge.endswith(".jar"):
        return forge
    if names:
        return os.path.join(directory, sorted(names)[0])
    return None


def apply_server(cfg: dict[str, Any]) -> RuntimeSpec:
    saved = save_config(cfg)
    return prepare_runtime(saved)


def list_available_versions() -> dict[str, Any]:
    vanilla = list(CURATED_VANILLA)
    try:
        manifest = fetch_version_manifest()
        releases = [
            item["id"]
            for item in manifest.get("versions", [])
            if item.get("type") == "release"
        ]
        for version in releases:
            if version not in vanilla:
                vanilla.append(version)
    except InstallError:
        pass
    return {
        "types": ["vanilla", "fabric", "forge", "modpack"],
        "vanilla": vanilla,
        "fabric": list(CURATED_FABRIC),
        "forge": list(FORGE_BUILDS.keys()),
        "curated_vanilla": list(CURATED_VANILLA),
    }


def list_backups() -> list[dict[str, Any]]:
    ensure_layout()
    items = []
    for name in sorted(os.listdir(paths()["backups"]), reverse=True):
        if not name.endswith(".tar.gz"):
            continue
        if ".." in name:
            continue
        full = os.path.join(paths()["backups"], name)
        if not os.path.isfile(full):
            continue
        stat = os.stat(full)
        items.append(
            {
                "name": name,
                "size": stat.st_size,
                "modified": int(stat.st_mtime),
            }
        )
    return items


def backup_path(name: str) -> str:
    base = os.path.basename(name)
    if base != name or ".." in base or not base.endswith(".tar.gz"):
        raise InstallError("Invalid backup name")
    return os.path.join(paths()["backups"], base)


def create_backup(level_name: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    world = level_name or str(cfg.get("level_name") or "world")
    source = world_path(world)
    os.makedirs(source, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    filename = f"{stamp}_{world}.tar.gz"
    dest = os.path.join(paths()["backups"], filename)
    with tarfile.open(dest, "w:gz") as archive:
        archive.add(source, arcname=f"worlds/{world}")
        for extra in (
            paths()["properties"],
            paths()["config"],
            os.path.join(DATA_DIR, "ops.txt"),
            os.path.join(DATA_DIR, "white-list.txt"),
            os.path.join(DATA_DIR, "whitelist.txt"),
            os.path.join(DATA_DIR, "ops.json"),
            os.path.join(DATA_DIR, "whitelist.json"),
        ):
            if os.path.isfile(extra):
                archive.add(extra, arcname=os.path.basename(extra))
    stat = os.stat(dest)
    return {"name": filename, "size": stat.st_size, "modified": int(stat.st_mtime)}


def restore_backup(name: str, select: bool = True) -> dict[str, Any]:
    source = backup_path(name)
    if not os.path.isfile(source):
        raise InstallError("Backup not found")
    with tarfile.open(source, "r:gz") as archive:
        world_name = None
        for member in archive.getmembers():
            if member.name.startswith("/") or ".." in member.name.split("/"):
                raise InstallError("Backup contains unsafe paths")
            if member.name.startswith("worlds/") and member.isdir():
                parts = member.name.split("/")
                if len(parts) >= 2 and WORLD_NAME_RE.match(parts[1]):
                    world_name = parts[1]
        if not world_name:
            match = re.search(r"_([A-Za-z0-9._-]+)\.tar\.gz$", name)
            world_name = match.group(1) if match else "world"
        try:
            archive.extractall(DATA_DIR, filter="data")
        except TypeError:
            archive.extractall(DATA_DIR)
    if select:
        select_world(world_name)
    return {"world": world_name, "name": name}


def delete_backup(name: str) -> None:
    path = backup_path(name)
    if not os.path.isfile(path):
        raise InstallError("Backup not found")
    os.remove(path)


def read_name_file(path: str) -> list[str]:
    if not os.path.isfile(path):
        return []
    if path.endswith(".json"):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return []
        names = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]))
                elif isinstance(item, str):
                    names.append(item)
        return names
    names = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if text and not text.startswith("#"):
                names.append(text)
    return names


def write_name_file(
    path: str, names: list[str], json_mode: bool, online: bool = False
) -> None:
    unique = []
    for name in names:
        clean = name.strip()
        if clean and clean not in unique:
            unique.append(clean)
    if json_mode:
        payload = [
            {"name": name, "uuid": player_uuid(name, online=online), "level": 4}
            if path.endswith("ops.json")
            else {"name": name, "uuid": player_uuid(name, online=online)}
            for name in unique
        ]
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        return
    with open(path, "w", encoding="utf-8") as handle:
        for name in unique:
            handle.write(name + "\n")


def _online_mode(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or load_config()
    return properties_for(cfg).get("online-mode") == "true"


def list_file_names(kind: str) -> tuple[str, list[str]]:
    cfg = load_config()
    version = str(cfg.get("minecraft_version") or "1.2.5")
    if kind == "ops":
        json_path = os.path.join(DATA_DIR, "ops.json")
        txt_path = os.path.join(DATA_DIR, "ops.txt")
        if uses_legacy_files(version):
            return txt_path, read_name_file(txt_path)
        names = read_name_file(json_path) or read_name_file(txt_path)
        return json_path, names
    json_path = os.path.join(DATA_DIR, "whitelist.json")
    txt_legacy = os.path.join(DATA_DIR, "white-list.txt")
    txt_alt = os.path.join(DATA_DIR, "whitelist.txt")
    if uses_legacy_files(version):
        names = read_name_file(txt_legacy) or read_name_file(txt_alt)
        return txt_legacy, names
    names = read_name_file(json_path) or read_name_file(txt_legacy) or read_name_file(txt_alt)
    return json_path, names


def sync_access_lists(cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config()
    try:
        run_dir = runtime_cwd(cfg)
    except InstallError:
        return
    version = str(cfg.get("minecraft_version") or "1.2.5")
    online = _online_mode(cfg)
    for kind in ("ops", "whitelist"):
        _path, names = list_file_names(kind)
        if uses_legacy_files(version):
            dest = os.path.join(
                run_dir, "ops.txt" if kind == "ops" else "white-list.txt"
            )
            write_name_file(dest, names, False)
            if kind != "ops":
                write_name_file(os.path.join(run_dir, "whitelist.txt"), names, False)
            continue
        dest = os.path.join(run_dir, "ops.json" if kind == "ops" else "whitelist.json")
        write_name_file(dest, names, True, online=online)


def update_name_list(kind: str, name: str, add: bool) -> list[str]:
    if not re.match(r"^[A-Za-z0-9_]{1,16}$", name):
        raise InstallError("Invalid player name")
    path, names = list_file_names(kind)
    if add and name not in names:
        names.append(name)
    if not add:
        names = [item for item in names if item.lower() != name.lower()]
    online = _online_mode()
    write_name_file(path, names, path.endswith(".json"), online=online)
    if kind != "ops" and path.endswith("white-list.txt"):
        alt = os.path.join(DATA_DIR, "whitelist.txt")
        write_name_file(alt, names, False)
    try:
        sync_access_lists()
    except InstallError:
        pass
    return names
