#!/usr/bin/env bash
set -euo pipefail
APP_SOURCE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
APP_DEST_DIR="$HOME/.local/share/sstp-vpn"
BIN_DEST_DIR="$HOME/.local/bin"
DESKTOP_DEST_DIR="$HOME/.local/share/applications"

echo 'Установка зависимостей SSTP VPN...'
sudo pacman -S --needed networkmanager network-manager-sstp python python-cryptography python-gobject gtk3 libayatana-appindicator yad
sudo systemctl enable --now NetworkManager
mkdir -p "$APP_DEST_DIR" "$BIN_DEST_DIR" "$DESKTOP_DEST_DIR"
install -m 755 "$APP_SOURCE_DIR/sstp_vpn_gui.py" "$APP_DEST_DIR/sstp_vpn_gui.py"
cat > "$BIN_DEST_DIR/sstp-vpn-gui" <<'LAUNCHER'
#!/usr/bin/env bash
exec /usr/bin/python3 "$HOME/.local/share/sstp-vpn/sstp_vpn_gui.py"
LAUNCHER
chmod 755 "$BIN_DEST_DIR/sstp-vpn-gui"
cat > "$DESKTOP_DEST_DIR/sstp-vpn.desktop" <<'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=SSTP VPN
Comment=Управление SSTP VPN
Exec=sstp-vpn-gui
Icon=network-vpn
Terminal=false
Categories=Network;Utility;
DESKTOP
printf '\nГотово. Запустите: sstp-vpn-gui\n'
