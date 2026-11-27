#!/bin/sh
set -eu

REPO_URL="${QBOT_REPO_URL:-https://github.com/TheDarkRoot/qBot}"
BRANCH="${QBOT_BRANCH:-main}"

echo "=== qbot Universal Installer ==="

# 1. Platform tespiti
if [ -x /opt/bin/opkg ]; then
    PLATFORM="entware"
    INSTALL_DIR="${QBOT_DIR:-/opt/share/qbot}"
    PYTHON_BIN="/opt/bin/python3"
elif [ -x /sbin/opkg ] || [ -x /bin/opkg ]; then
    PLATFORM="openwrt"
    INSTALL_DIR="${QBOT_DIR:-/opt/qbot}"
    PYTHON_BIN="/usr/bin/python3"
elif command -v systemctl >/dev/null 2>&1; then
    PLATFORM="systemd"
    INSTALL_DIR="${QBOT_DIR:-/opt/qbot}"
    PYTHON_BIN="$(command -v python3 || echo /usr/bin/python3)"
else
    PLATFORM="generic"
    INSTALL_DIR="${QBOT_DIR:-$HOME/qbot}"
    PYTHON_BIN="$(command -v python3 || echo python3)"
fi

echo "[1/4] Platform detected: $PLATFORM (target: $INSTALL_DIR)"

# 2. Minimal Python 3 bagimliliklari
echo "[2/4] Installing minimal Python 3 packages..."
case "$PLATFORM" in
    entware)
        /opt/bin/opkg update
        /opt/bin/opkg install python3-light python3-urllib python3-openssl python3-logging python3-codecs python3-uuid ca-bundle
        /opt/bin/opkg install python3-email >/dev/null 2>&1 || true
        ;;
    openwrt)
        opkg update
        opkg install python3-light python3-urllib python3-openssl python3-logging python3-codecs python3-uuid ca-bundle
        opkg install python3-email >/dev/null 2>&1 || true
        ;;
    systemd)
        if ! command -v python3 >/dev/null 2>&1; then
            if command -v apt-get >/dev/null 2>&1; then
                sudo apt-get update && sudo apt-get install -y python3 ca-certificates
            else
                echo "Please install python3 manually and re-run."
                exit 1
            fi
        fi
        ;;
esac

# 3. Kaynak dosyalarini indir
echo "[3/4] Fetching qbot into $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
TMP_TAR="/tmp/qbot_src.tar.gz"
TAR_URL="$REPO_URL/archive/refs/heads/$BRANCH.tar.gz"

if command -v git >/dev/null 2>&1 && [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" pull --ff-only
elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$TAR_URL" -o "$TMP_TAR"
    tar -xzf "$TMP_TAR" --strip-components=1 -C "$INSTALL_DIR"
    rm -f "$TMP_TAR"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP_TAR" "$TAR_URL"
    tar -xzf "$TMP_TAR" --strip-components=1 -C "$INSTALL_DIR"
    rm -f "$TMP_TAR"
else
    echo "Error: curl, wget, or git is required to download qbot."
    exit 1
fi

# 4. Ilk yapilandirma ve servis kurulumu
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    chmod 600 "$INSTALL_DIR/config.json"
    echo "[4/4] Created $INSTALL_DIR/config.json (chmod 600). Edit it with your Telegram token & qBittorrent credentials."
else
    chmod 600 "$INSTALL_DIR/config.json"
    echo "[4/4] Existing config.json preserved."
fi

case "$PLATFORM" in
    entware)
        cp "$INSTALL_DIR/deploy/S99qbot" /opt/etc/init.d/S99qbot
        chmod +x /opt/etc/init.d/S99qbot
        echo "Done! Edit $INSTALL_DIR/config.json then run: /opt/etc/init.d/S99qbot start"
        ;;
    systemd)
        echo "Systemd template ready at: $INSTALL_DIR/deploy/qbot.service"
        echo "Install with: sudo cp $INSTALL_DIR/deploy/qbot.service /etc/systemd/system/ && sudo systemctl enable --now qbot"
        ;;
    *)
        echo "Done! Edit $INSTALL_DIR/config.json then run: $PYTHON_BIN $INSTALL_DIR/main.py"
        ;;
esac