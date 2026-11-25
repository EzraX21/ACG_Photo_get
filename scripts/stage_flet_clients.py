#!/usr/bin/env python3
"""下载 flet 桌面客户端归档，供 PyInstaller 打包进可执行文件。

为什么需要这个脚本：

`flet_desktop.ensure_client_cached()` 在运行时的查找顺序是
「先找打包内置的归档，找不到才联网下载」。而 PyInstaller 的
`hook-flet.py` 只把**解压后的目录**塞进包里，运行时找的是**归档**
（`flet-windows.zip` / `flet-linux-<distro>-light-<arch>.tar.gz`），
于是每次都退回联网下载 —— 在没有受信 CA 的机器上就是
`SSL: CERTIFICATE_VERIFY_FAILED`。

Linux 还有个额外麻烦：归档名里带发行版，而运行时的
`get_artifact_filename()` 是按**用户机器**的 glibc 算这个名字的。
只打构建机那一个发行版的归档，用户机器 glibc 不同就匹配不上。
所以这里把所有可用的发行版构建都下载下来一起打进去。

版本、架构、发行版列表全部从装好的 `flet_desktop` 包里读，
不在脚本或 workflow 里写死 —— 升级 flet 后不会静默拿错版本。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

RELEASE_URL = "https://github.com/flet-dev/flet/releases/download/v{ver}/{name}"


def _flet_meta() -> tuple[str, str, str, list[str]]:
    """Return (version, arch, flavor, distro_ids) from installed flet_desktop."""
    import flet_desktop
    from flet_desktop import (
        _GLIBC_DISTRO_TABLE,
        __get_desktop_flavor,
        get_arch,
    )

    version = flet_desktop.version.version
    arch = get_arch()
    flavor = __get_desktop_flavor()

    if sys.platform == "win32":
        distros: list[str] = []
    elif sys.platform == "darwin":
        distros = []
    else:
        distros = [d for _, d, arches in _GLIBC_DISTRO_TABLE if arch in arches]

    return version, arch, flavor, distros


def _artifact_names(version: str, arch: str, flavor: str, distros: list[str]) -> list[str]:
    if sys.platform == "win32":
        return ["flet-windows.zip"]
    if sys.platform == "darwin":
        return ["flet-macos.tar.gz"]
    return [f"flet-linux-{d}-{flavor}-{arch}.tar.gz" for d in distros]


def _download(url: str, dest: Path) -> int:
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url) as resp, dest.open("wb") as f:
        while chunk := resp.read(1024 * 1024):
            f.write(chunk)
    size = dest.stat().st_size
    print(f"  -> {dest.name} ({size} bytes)", flush=True)
    return size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="flet-clients", help="staging directory")
    ap.add_argument(
        "--github-output",
        default=None,
        help="write `args=<pyinstaller --add-data flags>` to this file",
    )
    args = ap.parse_args()

    version, arch, flavor, distros = _flet_meta()
    names = _artifact_names(version, arch, flavor, distros)
    if not names:
        print("No client artifacts for this platform; nothing to do.", file=sys.stderr)
        return 1

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sep = ";" if sys.platform == "win32" else ":"
    add_data: list[str] = []
    for name in names:
        dest = out_dir / name
        url = RELEASE_URL.format(ver=version, name=name)
        if dest.exists():
            print(f"Using cached {dest.name} ({dest.stat().st_size} bytes)", flush=True)
        else:
            _download(url, dest)
        add_data.append(f"--add-data {dest}{sep}flet_desktop/app")

    flags = " ".join(add_data)
    print(f"\nStaged {len(add_data)} client archive(s) for flet {version} ({flavor}, {arch})")
    print(f"args={flags}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"args={flags}\n")
        print(f"Wrote args to {args.github_output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
