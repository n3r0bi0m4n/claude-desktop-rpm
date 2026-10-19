# claude-desktop-rpm

Install and update the [Claude desktop app](https://code.claude.com/docs/en/desktop-linux) on RPM-based
distributions: Fedora, RHEL, CentOS Stream, AlmaLinux and Rocky Linux (x86_64, aarch64).

Anthropic ships Claude Desktop for Linux only as a Debian package. This script takes the official `.deb` from
Anthropic's apt repository, verifies it, repackages it as an RPM on your machine and installs it with `dnf`.
Nothing is rebuilt or modified: the RPM contains the same files.

> Unofficial. Not built or supported by Anthropic. Do not redistribute the RPMs it builds.

## Usage

Without cloning:

```bash
curl -fsSL https://raw.githubusercontent.com/n3r0bi0m4n/claude-desktop-rpm/main/claude-desktop-rpm.py | python3 - install
```

Or from a checkout:

```bash
./claude-desktop-rpm.py install
```

| Command | What it does |
|---|---|
| `install` (default) | Installs the newest version, or upgrades to it, or does nothing if already current |
| `update` | Same as `install` |
| `uninstall` | `dnf remove claude-desktop`. Settings and sign-in stay in `~/.config/Claude` |

Run it as your normal user; it calls `sudo` only for `dnf`. Running it as root works too. When there is no
terminal at all (cron, CI), `dnf` runs with `-y`.

## Requirements

- Python 3.12+. On EL9 the default `python3` is 3.9: `sudo dnf install python3.12` and use `python3.12`.
- `gpg` (installed on every Fedora/EL system), `dnf`. `rpm-build` is installed automatically when missing.
- About 2 GiB free in `/var/tmp` during the build. It is cleaned up afterwards.

## How it works

1. Downloads Anthropic's signing key and checks it is `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`, the
   fingerprint from the official docs.
2. Verifies the signed `InRelease` of the apt repository and rejects it if expired (`Valid-Until`).
3. Fetches the `Packages` index by its signed SHA256 and picks the newest `claude-desktop` for your
   architecture.
4. Compares it with the installed version. If current, stops here.
5. Downloads the `.deb` and checks its SHA256 against the index.
6. Unpacks it and adds what the Debian `postinst` would create: the GNOME search provider, the MIME types
   and the `ccd` launcher link. The apt repository and AppArmor parts are Debian-only and left out.
7. Builds `claude-desktop-<version>-0.repack` with `rpmbuild` and installs it with `dnf`.

Dependencies are declared by library soname, so the same RPM resolves on Fedora and EL. The Cowork stack
(QEMU, UEFI firmware, `virtiofsd`), the keyring and the tray library are weak dependencies: `dnf` installs
them when available and skips them otherwise.

If an official Anthropic RPM appears, it supersedes this one (`Release` 0 is lower than any official
release), and the script leaves a non-repacked `claude-desktop` alone.

## Cowork

Cowork runs tasks in a QEMU/KVM virtual machine. After installing, the script tells you if something is
missing:

- join the `kvm` group: `sudo usermod -aG kvm $USER`, then log out and back in;
- load `vhost_vsock`: `sudo modprobe vhost_vsock` (and add it to `/etc/modules-load.d/` for every boot).

On EL, QEMU is packaged as `qemu-kvm` rather than `qemu-system-x86`, so Cowork may not find it there.
