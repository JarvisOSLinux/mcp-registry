#!/usr/bin/env bash
# Setup for the JARVIS Computer Use — Linux Desktop Control MCP server.
# CWD = install directory (the cloned servers/computer-use-linux directory).
#
# What this script does:
#   1. Installs system packages: ydotool, python3-pyatspi, wl-clipboard, spectacle
#   2. Adds the current user to the 'input' group (needed for /dev/uinput)
#   3. Installs and enables the ydotoold systemd user unit (input-injection daemon)
#   4. Verifies that python3 can import pyatspi and run server.py
#
# Re-runnable: package installs and group adds are idempotent.
# Root is required for package installation and group management.
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo -e "${RED}Error: This setup script must run as root (sudo setup.sh).${NC}" >&2
        echo "It installs packages, adds the user to the 'input' group, and" >&2
        echo "installs a systemd user unit for ydotoold." >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Detect package manager and install packages
# ---------------------------------------------------------------------------

install_packages() {
    local -a pkgs=("$@")
    if command -v pacman &>/dev/null; then
        pacman -S --noconfirm --needed "${pkgs[@]}"
    elif command -v apt-get &>/dev/null; then
        apt-get install -y "${pkgs[@]}"
    elif command -v dnf &>/dev/null; then
        dnf install -y "${pkgs[@]}"
    elif command -v zypper &>/dev/null; then
        zypper install -y "${pkgs[@]}"
    else
        echo -e "${RED}No supported package manager found (pacman/apt/dnf/zypper).${NC}" >&2
        echo "Install manually: ${pkgs[*]}" >&2
        exit 1
    fi
}

# Map generic package names to distro-specific names
declare -A PKG_MAP_APT=(
    [ydotool]="ydotool"
    [python3-pyatspi]="python3-pyatspi"
    [wl-clipboard]="wl-clipboard"
    [spectacle]="kde-spectacle"
)

declare -A PKG_MAP_DNF=(
    [ydotool]="ydotool"
    [python3-pyatspi]="python3-pyatspi"
    [wl-clipboard]="wl-clipboard"
    [spectacle]="spectacle"
)

install_distro_packages() {
    if command -v pacman &>/dev/null; then
        pacman -S --noconfirm --needed ydotool python-pyatspi wl-clipboard spectacle
    elif command -v apt-get &>/dev/null; then
        apt-get install -y ydotool python3-pyatspi wl-clipboard kde-spectacle
    elif command -v dnf &>/dev/null; then
        dnf install -y ydotool python3-pyatspi wl-clipboard spectacle
    elif command -v zypper &>/dev/null; then
        zypper install -y ydotool python3-pyatspi wl-clipboard spectacle
    else
        echo -e "${RED}No supported package manager found.${NC}" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# ydotoold systemd user unit
# ---------------------------------------------------------------------------

install_ydotoold_unit() {
    local target_user="${SUDO_USER:-${USER}}"
    local user_home
    user_home="$(getent passwd "${target_user}" | cut -d: -f6)"
    local unit_dir="${user_home}/.config/systemd/user"
    mkdir -p "${unit_dir}"

    cat > "${unit_dir}/ydotoold.service" << 'UNITEOF'
[Unit]
Description=ydotool input-injection daemon
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/ydotoold
Restart=on-failure

[Install]
WantedBy=default.target
UNITEOF

    chown "${target_user}:${target_user}" "${unit_dir}/ydotoold.service"
    echo -e "${GREEN}Installed ~/.config/systemd/user/ydotoold.service for ${target_user}${NC}"
    echo ""
    echo "To enable and start ydotoold, run AS YOUR USER (not root):"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now ydotoold"
    echo ""
    echo "After enabling, you may need to log out and back in for the 'input'"
    echo "group membership to take effect."
}

# ---------------------------------------------------------------------------
# Add user to input group (needed for /dev/uinput write access)
# ---------------------------------------------------------------------------

add_user_to_input() {
    local target_user="${SUDO_USER:-${USER}}"
    if id -nG "${target_user}" | grep -qw input; then
        echo -e "${GREEN}User ${target_user} is already in the 'input' group${NC}"
    else
        usermod -aG input "${target_user}"
        echo -e "${GREEN}Added ${target_user} to the 'input' group${NC}"
        echo -e "${YELLOW}NOTE: Log out and back in (or reboot) for the group change to take effect.${NC}"
    fi
}

# ---------------------------------------------------------------------------
# Verify python3 + pyatspi
# ---------------------------------------------------------------------------

verify_python() {
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}python3 not found. Install Python 3.9+.${NC}" >&2
        exit 1
    fi
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        found="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        echo -e "${RED}Python >= 3.9 required (found ${found}).${NC}" >&2
        exit 1
    fi
    if ! python3 -c 'import pyatspi' 2>/dev/null; then
        echo -e "${RED}pyatspi import failed after package install.${NC}" >&2
        echo "Try: pip install pyatspi  or  pacman -S python-pyatspi" >&2
        exit 1
    fi
    echo -e "${GREEN}python3 + pyatspi: OK${NC}"
}

verify_server() {
    chmod +x server.py
    python3 -B -c "
import importlib.util
spec = importlib.util.spec_from_file_location('server', 'server.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
" || {
        echo -e "${RED}server.py failed to load (see traceback above).${NC}" >&2
        exit 1
    }
    echo -e "${GREEN}server.py loads cleanly.${NC}"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

need_root

echo "Installing system packages (ydotool, python-pyatspi, wl-clipboard, spectacle)..."
install_distro_packages
echo -e "${GREEN}Packages installed.${NC}"

echo "Adding user to 'input' group..."
add_user_to_input

echo "Installing ydotoold systemd user unit..."
install_ydotoold_unit

echo "Verifying Python and pyatspi..."
verify_python

echo "Verifying server.py..."
verify_server

echo ""
echo -e "${GREEN}Setup complete.${NC}"
echo ""
echo "IMPORTANT — manual steps after setup:"
echo "  1. As your user (not root):  systemctl --user daemon-reload && systemctl --user enable --now ydotoold"
echo "  2. Log out and back in so the 'input' group takes effect."
echo "  3. Verify ydotoold is running:  systemctl --user status ydotoold"
