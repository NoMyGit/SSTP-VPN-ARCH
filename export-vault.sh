#!/usr/bin/env bash
set -euo pipefail
BUNDLE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
VAULT_SOURCE="${XDG_DATA_HOME:-$HOME/.local/share}/sstp-vpn/connections.vault"
if [[ ! -f "$VAULT_SOURCE" ]]; then
  echo "Vault не найден: $VAULT_SOURCE" >&2
  exit 1
fi
install -m 600 "$VAULT_SOURCE" "$BUNDLE_DIR/connections.vault"
printf 'Vault добавлен в переносимую папку: %s\n' "$BUNDLE_DIR/connections.vault"
