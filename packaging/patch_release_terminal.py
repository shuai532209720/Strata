#!/usr/bin/env python3
"""Build a Terminal (Control) archive from a published release archive plus this checkout's commits on top of it.

A release's Terminal archive (strata-control-<ver>-<platform>.zip / .tar.gz) carries a private Python runtime and locked wheels,
and it can only be built on its own platform (package_terminal.py). When the commits since the release change only the
application's Python and data files, the release's archive can be used as it is with those files swapped in. That needs no
interpreter or wheels of the target platform, so a Windows archive can be made on Linux.

    python packaging/patch_release_terminal.py --release strata-control-0.1.55-windows-x86_64.zip --out dist --label gpulease

What it does, and what it refuses:
  * reads the archive's COMMIT (the commit the release was built from) and checks that every application file in it is byte for byte
    that commit's file (line ends aside): an archive that is not that commit's is refused;
  * takes `git diff --name-status <COMMIT> HEAD` and applies it to the application folders the archive carries (package_terminal.PAYLOAD
    and the installer's files): a changed file is replaced with HEAD's, a deleted one removed, a new one under a payload folder added;
  * refuses when HEAD changes something the archive cannot get this way: the runtime lock, the launchers in bin/, the requirements
    (a new wheel would be missing from the private runtime);
  * refuses uncommitted changes in those folders (the archive is HEAD's, nothing else);
  * writes COMMIT = HEAD and a PATCHES.txt (base, head, the files changed); VERSION stays the release's, so `strata update` compares
    it with the feed as before. A zip is written as a zip, a tar.gz as a tar.gz, and its SHA-256 goes into SHA256SUMS.txt beside it.

The result is not signed with the release key: install it from a file (packaging/install-local.ps1 on Windows), not through a feed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
import package_terminal as pt  # noqa: E402

# what a patch cannot bring along: the runtime itself and how it starts
REFUSED = ("packaging/locks/", "bin/", "requirements.txt", "requirements-monitor.txt")


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


class Archive:
    """The release archive's members by their path inside the bundle (zip: rooted at the bundle; tar.gz: one top folder)."""

    def __init__(self, path: Path):
        self.path, self.zip = path, path.suffix == ".zip"
        self.files: dict[str, bytes] = {}
        self.prefix = ""
        if self.zip:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if not n.endswith("/"):
                        self.files[n] = z.read(n)
        else:
            with tarfile.open(path) as t:
                members = [m for m in t.getmembers() if m.isfile()]
                tops = {m.name.split("/", 1)[0] for m in members}
                self.prefix = tops.pop() + "/" if len(tops) == 1 else ""
                for m in members:
                    self.files[m.name[len(self.prefix):]] = t.extractfile(m).read()

    def write(self, out: Path, files: dict[str, bytes]) -> None:
        if self.zip:
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                for n in sorted(files):
                    z.writestr(n, files[n])
            return
        with tarfile.open(out, "w:gz") as t:
            for n in sorted(files):
                info = tarfile.TarInfo(self.prefix + n)
                info.size, info.mode = len(files[n]), 0o755 if n.startswith(("bin/", "python/bin/")) else 0o644
                t.addfile(info, io.BytesIO(files[n]))


def app_path(name: str) -> bool:
    """A file the archive takes from the source tree (not the runtime): the payload, the tools subset and the installer."""
    roots = [p.rstrip("/") for p in pt.PAYLOAD] + ["tools", "setup.py", "ref", pt.INSTALLER_DATA]
    return any(name == r or name.startswith(r + "/") for r in roots)


def same(a: bytes, b: bytes) -> bool:
    return a.replace(b"\r\n", b"\n") == b.replace(b"\r\n", b"\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", type=Path, required=True, help="the published Terminal archive (strata-control-<ver>-<platform>.zip or .tar.gz)")
    ap.add_argument("--out", type=Path, required=True, help="folder to write the new archive into")
    ap.add_argument("--label", default="patched", help="a short word for the file name, e.g. gpulease")
    a = ap.parse_args(argv)

    arc = Archive(a.release)
    base = arc.files.get("COMMIT", b"").decode().strip()
    version = arc.files.get("VERSION", b"").decode().strip()
    if len(base) != 40:
        raise SystemExit(f"{a.release.name} has no COMMIT file: the commit it was built from is unknown")
    head = git("rev-parse", "HEAD").decode().strip()
    try:
        git("cat-file", "-e", base + "^{commit}")
    except subprocess.CalledProcessError:
        raise SystemExit(f"this checkout does not have the release's commit {base[:12]}: fetch it first") from None

    checked = 0
    for name, data in arc.files.items():
        if not app_path(name) or name.endswith(".pyc") or "/__pycache__/" in name:
            continue
        try:
            src = git("show", f"{base}:{name}")
        except subprocess.CalledProcessError:
            continue                                     # a generated file (none today), not in git
        if not same(src, data):
            raise SystemExit(f"{name} in the archive is not the file of commit {base[:12]}: this is not that commit's archive")
        checked += 1

    dirty = git("status", "--porcelain").decode().splitlines()
    dirty = [l[3:] for l in dirty if app_path(l[3:].strip('"'))]
    if dirty:
        raise SystemExit("uncommitted changes in the application folders (commit them first): " + ", ".join(dirty[:8]))

    changes = []
    for line in git("diff", "--name-status", "--no-renames", base, head).decode().splitlines():
        status, name = line.split("\t", 1)
        if any(name == r or name.startswith(r) for r in REFUSED):
            raise SystemExit(f"{name} changed since the release: a patched archive cannot carry it (build a full one with "
                             "package_terminal.py on the target platform)")
        if name in arc.files or (status == "A" and app_path(name)):
            changes.append((status, name))

    files = dict(arc.files)
    for status, name in changes:
        if status == "D":
            files.pop(name, None)
        else:
            files[name] = git("show", f"{head}:{name}")
    files["COMMIT"] = (head + "\n").encode()
    files["PATCHES.txt"] = (
        f"This Terminal is release {version} (commit {base}) with the commits up to {head} applied.\n"
        f"It was made by packaging/patch_release_terminal.py from {a.release.name}; it is not signed with the release key.\n"
        "Changed files:\n" + "".join(f"  {s} {n}\n" for s, n in changes)).encode()

    a.out.mkdir(parents=True, exist_ok=True)
    try:
        import json
        platform = json.loads(arc.files["BUNDLE.json"])["target"]
    except (KeyError, ValueError):
        platform = "unknown"
    out = a.out / f"strata-control-{version}-{a.label}-{head[:8]}-{platform}{'.zip' if arc.zip else '.tar.gz'}"
    arc.write(out, files)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    sums = a.out / "SHA256SUMS.txt"
    lines = [l for l in (sums.read_text().splitlines() if sums.exists() else []) if not l.endswith("  " + out.name)]
    sums.write_text("\n".join(lines + [f"{digest}  {out.name}"]) + "\n")
    print(f"release {version} @ {base[:12]}: {checked} application files match that commit")
    for s, n in changes:
        print(f"  {s} {n}")
    print(f"-> {out} ({out.stat().st_size // 1048576} MB)\n   sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
