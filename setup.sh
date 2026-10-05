#!/bin/sh
# No downloads, privilege changes, or credentials occur in this bootstrap.
set -eu
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "Python 3.10 or newer is required. Nothing was installed." >&2
    distro=unknown
    if [ -r /etc/os-release ]; then
        distro=$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')
    fi
    case "$distro" in
        ubuntu|debian|linuxmint|pop) echo "Ask your administrator to install a supported Python: apt install python3 (on a release shipping Python >=3.10)." >&2 ;;
        fedora|rhel|centos|rocky|almalinux) echo "Ask your administrator: dnf install python3 (verify the version is >=3.10)." >&2 ;;
        arch|manjaro) echo "Ask your administrator: pacman -S python." >&2 ;;
        opensuse*|sles) echo "Ask your administrator: zypper install python311." >&2 ;;
        alpine) echo "Ask your administrator: apk add python3. Note: the pinned native runtime requires a supported glibc platform." >&2 ;;
        *) echo "Install Python >=3.10 using your distribution package manager, then rerun ./setup.sh." >&2 ;;
    esac
    exit 1
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$script_dir/omnirush.py" setup "$@"
