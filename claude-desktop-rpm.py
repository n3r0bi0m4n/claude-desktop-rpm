#!/usr/bin/env python3
"""Install, update or uninstall Claude Desktop on RPM-based distributions.

Anthropic publishes Claude Desktop for Linux only as a .deb in an apt repository.
This script downloads the newest .deb from that repository, verifies it with the
same chain apt uses (pinned signing key -> signed InRelease -> Packages -> .deb),
repackages it as an RPM and installs it with dnf.

Supported: Fedora, RHEL, CentOS Stream, AlmaLinux, Rocky Linux (x86_64, aarch64).

Usage:
    claude-desktop-rpm.py [install|update|uninstall]

    curl -fsSL https://raw.githubusercontent.com/n3r0bi0m4n/claude-desktop-rpm/main/claude-desktop-rpm.py | python3 - install

`install` and `update` are the same: install if missing, upgrade if a newer
version exists, do nothing if already current. Without a command, `install`.
"""

# Keep this file parseable by Python 3.9 (the EL9 default) so the version check
# below can print a useful message instead of a SyntaxError.
from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    sys.exit(
        "claude-desktop-rpm: Python 3.12+ is required, found %d.%d.\n"
        "On EL9 install it with `sudo dnf install python3.12` and run the script with python3.12."
        % sys.version_info[:2]
    )

import datetime
import email.utils
import grp
import hashlib
import http.client
import io
import lzma
import os
import platform
import pwd
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import urllib.request
from pathlib import Path

NAME = "claude-desktop"
BASE_URL = "https://downloads.claude.ai/claude-desktop"
KEY_URL = f"{BASE_URL}/key.asc"
REPO_URL = f"{BASE_URL}/apt/stable"
SUITE, COMPONENT = "stable", "main"
# "Anthropic Claude Code Release Signing <security@anthropic.com>", the key the
# official docs and the package's own postinst pin.
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
# Our Release. An official RPM of the same version (Release >= 1) wins over it.
RELEASE = "0.repack"
# rpm arch -> Debian arch
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
# The deb is ~180 MB and unpacks to ~600 MB; staging, build root (hardlinks)
# and the RPM need a bit over 1 GB. /var/tmp, because /tmp is RAM on Fedora.
WORK_PARENT = "/var/tmp"
MIN_FREE_BYTES = 2 * 1024**3

# Debian Depends, as sonames where possible so one list resolves on Fedora and EL.
REQUIRES = [
    "glibc >= 2.34",
    "libgtk-3.so.0()(64bit)",
    "libnotify.so.4()(64bit)",
    "libnss3.so()(64bit)",
    "libatspi.so.0()(64bit)",
    "libdrm.so.2()(64bit)",
    "libgbm.so.1()(64bit)",
    "libxcb-dri3.so.0()(64bit)",
    "libsecret-1.so.0()(64bit)",
    "libXtst.so.6()(64bit)",
    "libuuid.so.1()(64bit)",
    "libpipewire-0.3.so.0()(64bit)",
    "xdg-utils",
    "xdg-desktop-portal",
    "(xdg-desktop-portal-gtk or xdg-desktop-portal-gnome or xdg-desktop-portal-kde)",
]
# Debian Recommends. dnf skips weak dependencies it cannot find, so names that
# exist only on Fedora (or only with EPEL) do not break the install elsewhere.
RECOMMENDS = [
    "libasound.so.2()(64bit)",
    "libayatana-appindicator3.so.1()(64bit)",
    "ca-certificates",
    "(gnome-keyring or kf6-kwallet or kf5-kwallet)",
    "bubblewrap",
    "socat",
    "openssh-clients",
    "virtiofsd",
]
RECOMMENDS_ARCH = {
    "x86_64": ["qemu-system-x86-core", "edk2-ovmf"],
    "aarch64": ["qemu-system-aarch64-core", "edk2-aarch64"],
}

# What the deb's postinst copies at install time; here they are plain RPM files.
APP_DIR = "usr/lib/claude-desktop"
EXTRA_FILES = {
    f"{APP_DIR}/resources/gnome-search-provider/com.anthropic.Claude.search-provider.ini":
        "usr/share/gnome-shell/search-providers/com.anthropic.Claude.search-provider.ini",
    f"{APP_DIR}/resources/gnome-search-provider/com.anthropic.Claude.SearchProvider.service":
        "usr/share/dbus-1/services/com.anthropic.Claude.SearchProvider.service",
    f"{APP_DIR}/resources/linux-mime/com.anthropic.Claude.xml":
        "usr/share/mime/packages/com.anthropic.Claude.xml",
}
CCD_LINK, CCD_TARGET = "usr/bin/ccd", "../lib/claude-desktop/resources/bin/ccd"
# Directories the package owns (everything below them too).
OWNED_DIRS = (APP_DIR, "usr/share/doc/claude-desktop")
DROP_DIRS = ("usr/share/lintian",)


class Error(Exception):
    pass


def log(msg: str) -> None:
    print(f"claude-desktop-rpm: {msg}", file=sys.stderr, flush=True)


# --- running commands ---------------------------------------------------------

def _terminal():
    """stdin for interactive children (sudo, dnf).

    Under `curl ... | python3 -` our stdin is the exhausted pipe, so prompts
    would read EOF. Use the controlling terminal instead, as rustup does.
    """
    if sys.stdin.isatty():
        return None
    try:
        return open("/dev/tty")
    except OSError:
        return None


def sudo(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return cmd
    if not shutil.which("sudo"):
        raise Error("this step needs root and sudo is not installed; run the script as root")
    return ["sudo", *cmd]


def dnf(args: list[str]) -> None:
    tty = _terminal()
    cmd = ["dnf", *args]
    if tty is None and not sys.stdin.isatty():
        cmd.insert(1, "-y")  # nobody to answer the prompt; the command itself is the consent
    try:
        subprocess.run(sudo(cmd), stdin=tty, check=True)
    except subprocess.CalledProcessError as e:
        raise Error(f"`{' '.join(cmd)}` failed with exit code {e.returncode}") from None
    finally:
        if tty is not None:
            tty.close()


def capture(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# --- downloading --------------------------------------------------------------

def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "claude-desktop-rpm"})
    try:
        return urllib.request.urlopen(req, timeout=60)
    except OSError as e:
        raise Error(f"cannot download {url}: {e}") from None


def fetch(url: str) -> bytes:
    with _open(url) as r:
        return r.read()


def download(url: str, dest: Path, size: int, sha256: str) -> None:
    h = hashlib.sha256()
    done, shown = 0, -1
    show = sys.stderr.isatty()
    with _open(url) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            pct = done * 100 // size if size else 0
            if show and pct != shown:
                print(f"\r  {done >> 20} / {size >> 20} MiB ({pct}%)", end="", file=sys.stderr, flush=True)
                shown = pct
    if show:
        print(file=sys.stderr)
    if done != size or h.hexdigest() != sha256:
        raise Error(f"{dest.name}: size or SHA256 does not match the signed index")


# --- apt repository -----------------------------------------------------------

def parse_deb822(text: str) -> list[dict[str, str]]:
    """Paragraphs of `Key: value` fields; continuation lines are joined with \\n."""
    paras = []
    for block in re.split(r"\n\s*\n", text.strip()):
        fields: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key:
                fields[key] += "\n" + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                key = key.strip()
                fields[key] = value.strip()
        if fields:
            paras.append(fields)
    return paras


def verified_inrelease(work: Path) -> dict[str, str]:
    """Download key and InRelease, check the signature, return the signed fields."""
    if not shutil.which("gpg"):
        raise Error("gpg not found; install it with `sudo dnf install gnupg2`")
    home = work / "gnupg"
    home.mkdir(mode=0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--no-tty"]
    key = work / "key.asc"
    key.write_bytes(fetch(KEY_URL))
    shown = capture([*gpg, "--with-colons", "--show-keys", str(key)])
    fprs = [l.split(":")[9] for l in shown.stdout.splitlines() if l.startswith("fpr:")]
    if not fprs or fprs[0] != FINGERPRINT:
        raise Error(f"signing key at {KEY_URL} is not {FINGERPRINT}; refusing to continue")
    if capture([*gpg, "--import", str(key)]).returncode != 0:
        raise Error("cannot import the signing key")

    signed = work / "InRelease"
    signed.write_bytes(fetch(f"{REPO_URL}/dists/{SUITE}/InRelease"))
    if not signed.read_bytes().startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
        raise Error("InRelease is not a clearsigned file")
    # --decrypt outputs only the signed text, so unsigned bytes around the
    # clearsigned block can never reach the parser.
    res = subprocess.run(
        [*gpg, "--trust-model", "always", "--status-fd", "2", "--decrypt", str(signed)],
        capture_output=True,
    )
    status = res.stderr.decode(errors="replace").splitlines()
    valid = any(
        l.startswith("[GNUPG:] VALIDSIG ") and l.split()[-1] == FINGERPRINT for l in status
    )
    good = any(l.startswith("[GNUPG:] GOODSIG ") for l in status)
    bad = any(l.split()[1:2] in (["BADSIG"], ["EXPKEYSIG"], ["REVKEYSIG"]) for l in status)
    if res.returncode != 0 or not (valid and good) or bad:
        raise Error("InRelease signature is not valid; refusing to continue")
    release = parse_deb822(res.stdout.decode())[0]

    until = release.get("Valid-Until")
    if until:
        try:
            expires = email.utils.parsedate_to_datetime(until)
        except (TypeError, ValueError):
            raise Error(f"cannot parse Valid-Until {until!r}") from None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.UTC)
        if expires < datetime.datetime.now(datetime.UTC):
            raise Error(f"repository metadata expired on {until} (stale mirror or replay)")
    return release


def signed_hash(release: dict[str, str], path: str) -> tuple[str, int]:
    for line in release.get("SHA256", "").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == path:
            return parts[0], int(parts[1])
    raise Error(f"{path} is not listed in the signed InRelease")


VERSION_RE = re.compile(r"\d+(\.\d+)*")


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def latest_package(work: Path, release: dict[str, str], deb_arch: str) -> dict[str, str]:
    path = f"{COMPONENT}/binary-{deb_arch}/Packages"
    sha, size = signed_hash(release, path)
    # by-hash: immune to the index changing between InRelease and Packages.
    url = f"{REPO_URL}/dists/{SUITE}/{COMPONENT}/binary-{deb_arch}/by-hash/SHA256/{sha}"
    data = fetch(url)
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
        raise Error("Packages index does not match the signed InRelease")
    pkgs = [
        p for p in parse_deb822(data.decode())
        if p.get("Package") == NAME and p.get("Architecture") == deb_arch
        # Plain dotted versions only: they compare the same in apt and rpm.
        and VERSION_RE.fullmatch(p.get("Version", ""))
    ]
    if not pkgs:
        raise Error(f"no {NAME} package for {deb_arch} in the repository")
    return max(pkgs, key=lambda p: version_key(p["Version"]))


# --- .deb unpacking -----------------------------------------------------------

class _Slice(io.RawIOBase):
    """A read-only window of `size` bytes from the current position of `f`."""

    def __init__(self, f, size: int):
        self.f, self.left = f, size

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = min(len(b), self.left)
        if n <= 0:
            return 0
        data = self.f.read(n)
        b[: len(data)] = data
        self.left -= len(data)
        return len(data)


def extract_deb_data(deb: Path, dest: Path) -> None:
    """Extract data.tar.* from a .deb (an `ar` archive) into dest."""
    with open(deb, "rb") as f:
        if f.read(8) != b"!<arch>\n":
            raise Error(f"{deb.name} is not a .deb")
        while header := f.read(60):
            if len(header) < 60:
                break
            name = header[:16].decode().strip().rstrip("/")
            size = int(header[48:58])
            if name.startswith("data.tar"):
                stream = io.BufferedReader(_Slice(f, size), 1 << 20)
                # "data" filter: no absolute paths, no escaping links, no setuid
                # bits, no ownership; chrome-sandbox gets 4755 back in the spec.
                with tarfile.open(fileobj=stream, mode="r|*") as tar:
                    tar.extractall(dest, filter="data")
                return
            f.seek(size + (size & 1), os.SEEK_CUR)
    raise Error(f"{deb.name} has no data.tar member")


def stage_tree(root: Path) -> None:
    """Turn the deb's tree into what the RPM ships (what postinst would add)."""
    for d in DROP_DIRS:
        shutil.rmtree(root / d, ignore_errors=True)
    for src, dst in EXTRA_FILES.items():
        if (root / src).is_file():
            (root / dst).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / src, root / dst)
    ccd = root / CCD_LINK
    if os.access(root / APP_DIR / "resources/bin/ccd", os.X_OK) and not (ccd.exists() or ccd.is_symlink()):
        (root / CCD_LINK).parent.mkdir(parents=True, exist_ok=True)
        (root / CCD_LINK).symlink_to(CCD_TARGET)
    if not (root / APP_DIR / NAME).is_file():
        raise Error(f"unexpected package layout: /{APP_DIR}/{NAME} is missing")


# --- RPM building -------------------------------------------------------------

def _spec_path(rel: str) -> str:
    """A path for %files: escape rpm globs and macros, quote if needed."""
    p = "/" + rel
    p = p.replace("%", "%%")
    p = re.sub(r"([\[\]*?{}\\])", r"\\\1", p)
    return f'"{p}"' if " " in p or '"' in p else p


def files_list(root: Path) -> list[str]:
    lines = ["%defattr(-,root,root,755)"]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir != "." and any(rel_dir == d or rel_dir.startswith(d + "/") for d in OWNED_DIRS):
            lines.append("%dir " + _spec_path(rel_dir))
        # Symlinks to directories show up in dirnames; os.walk does not follow them.
        links = [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        for name in sorted(filenames + links):
            rel = Path(dirpath, name).relative_to(root).as_posix()
            if rel == f"{APP_DIR}/chrome-sandbox":
                lines.append("%attr(4755,root,root) " + _spec_path(rel))
            else:
                lines.append(_spec_path(rel))
    return lines


def write_spec(path: Path, version: str, arch: str, stage: Path) -> None:
    recommends = RECOMMENDS + RECOMMENDS_ARCH[arch]
    header = textwrap.dedent(f"""\
        # Generated by claude-desktop-rpm.py from Anthropic's official .deb.
        %global debug_package %{{nil}}
        %global __os_install_post %{{nil}}
        %global _build_id_links none
        %define _binary_payload w3.zstdio

        Name:           {NAME}
        Version:        {version}
        Release:        {RELEASE}
        Summary:        Claude desktop app (repackaged from Anthropic's .deb)
        License:        Proprietary
        URL:            https://claude.ai
        ExclusiveArch:  {arch}
        AutoReqProv:    no
        """)
    deps = "".join(f"Requires:       {r}\n" for r in REQUIRES)
    deps += "".join(f"Recommends:     {r}\n" for r in recommends)
    body = textwrap.dedent(f"""\

        %description
        Claude desktop app: Chat, Cowork and Claude Code.
        Repackaged locally from Anthropic's official Debian package by
        claude-desktop-rpm.py; not built or supported by Anthropic.

        %install
        cp -al {stage}/. %{{buildroot}}/

        %files
        """)
    path.write_text(header + deps + body + "\n".join(files_list(stage)) + "\n")


def build_rpm(work: Path, version: str, arch: str, stage: Path) -> Path:
    if not shutil.which("rpmbuild"):
        log("rpmbuild not found, installing rpm-build")
        dnf(["install", "rpm-build"])
    top = work / "rpmbuild"
    for d in ("BUILD", "BUILDROOT", "RPMS", "SRPMS", "SOURCES", "SPECS"):
        (top / d).mkdir(parents=True, exist_ok=True)
    spec = work / f"{NAME}.spec"
    write_spec(spec, version, arch, stage)
    res = capture([
        "rpmbuild", "-bb", "--quiet",
        "--define", f"_topdir {top}",
        str(spec),
    ], env={**os.environ, "QA_RPATHS": "0x00ff"})  # Electron's $ORIGIN rpaths are fine
    if res.returncode != 0:
        raise Error("rpmbuild failed:\n" + (res.stderr or res.stdout).strip())
    rpms = list((top / "RPMS").rglob(f"{NAME}-{version}-*.rpm"))
    if len(rpms) != 1:
        raise Error(f"rpmbuild produced {len(rpms)} packages, expected 1")
    return rpms[0]


# --- commands -----------------------------------------------------------------

def installed() -> tuple[str, str] | None:
    res = capture(["rpm", "-q", "--qf", "%{VERSION}\n%{RELEASE}", NAME])
    if res.returncode != 0:
        return None
    version, release = res.stdout.split("\n", 1)
    return version, release


def preflight() -> str:
    if not sys.platform.startswith("linux") or not shutil.which("dnf") or not shutil.which("rpm"):
        raise Error("this script is for RPM-based distributions with dnf (Fedora, RHEL, CentOS, Alma, Rocky)")
    arch = platform.machine()
    if arch not in ARCHES:
        raise Error(f"unsupported architecture {arch}; Anthropic publishes x86_64 and aarch64 only")
    return arch


def cowork_hints() -> None:
    user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    hints = []
    try:
        kvm = grp.getgrnam("kvm")
        if user != "root" and user not in kvm.gr_mem and pwd.getpwnam(user).pw_gid != kvm.gr_gid:
            hints.append(f"add yourself to the kvm group: sudo usermod -aG kvm {user}  (then log out and back in)")
    except KeyError:
        pass
    if not Path("/dev/vhost-vsock").exists():
        hints.append("load vhost_vsock: sudo modprobe vhost_vsock  "
                     "(on every boot: echo vhost_vsock | sudo tee /etc/modules-load.d/vhost_vsock.conf)")
    if hints:
        log("for Cowork (virtual machine tasks):")
        for h in hints:
            print(f"  - {h}", file=sys.stderr)


def cmd_install() -> None:
    arch = preflight()
    current = installed()
    if current and not current[1].endswith("repack"):
        log(f"{NAME} {'-'.join(current)} is installed from another source "
            "(official RPM?); leaving it alone")
        return

    os.makedirs(WORK_PARENT, exist_ok=True)
    if shutil.disk_usage(WORK_PARENT).free < MIN_FREE_BYTES:
        raise Error(f"need at least {MIN_FREE_BYTES >> 30} GiB free in {WORK_PARENT}")
    with tempfile.TemporaryDirectory(prefix="claude-desktop-rpm-", dir=WORK_PARENT) as tmp:
        work = Path(tmp)
        log("checking the repository signature")
        release = verified_inrelease(work)
        pkg = latest_package(work, release, ARCHES[arch])
        version = pkg["Version"]

        if current and VERSION_RE.fullmatch(current[0]) and version_key(current[0]) >= version_key(version):
            log(f"{NAME} {current[0]} is up to date")
            return
        log(f"{'updating ' + current[0] + ' -> ' if current else 'installing '}{version}")

        deb = work / Path(pkg["Filename"]).name
        log(f"downloading {deb.name}")
        download(f"{REPO_URL}/{pkg['Filename']}", deb, int(pkg["Size"]), pkg["SHA256"])

        log("unpacking")
        stage = work / "stage"
        stage.mkdir()
        extract_deb_data(deb, stage)
        deb.unlink()
        stage_tree(stage)

        log("building the RPM (takes a minute)")
        rpm = build_rpm(work, version, arch, stage)
        shutil.rmtree(stage)

        dnf(["install", str(rpm)])
    log(f"{NAME} {version} installed; launch Claude from the app menu or run `claude-desktop`")
    cowork_hints()


def cmd_uninstall() -> None:
    preflight()
    if not installed():
        log(f"{NAME} is not installed")
        return
    dnf(["remove", NAME])
    log("removed; your settings and sign-in stay in ~/.config/Claude")


COMMANDS = {"install": cmd_install, "update": cmd_install, "uninstall": cmd_uninstall}


def main(argv: list[str]) -> int:
    if len(argv) > 1 or (argv and argv[0] not in COMMANDS):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        COMMANDS[argv[0] if argv else "install"]()
    except Error as e:
        log(f"error: {e}")
        return 1
    except (OSError, ValueError, tarfile.TarError, lzma.LZMAError, http.client.HTTPException) as e:
        log(f"error: {type(e).__name__}: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
