#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${PYTHON:-python3}

if [ "${ZELLIJ_TAB_NAMER_EDITABLE:-0}" = "1" ]; then
    if ! "$PYTHON_BIN" -m pip install -e "$SCRIPT_DIR"; then
        "$PYTHON_BIN" -m pip install "$SCRIPT_DIR"
    fi
else
    "$PYTHON_BIN" -m pip install "$SCRIPT_DIR"
fi

if [ -n "${ZELLIJ_TAB_NAMER_INSTALL_MODE:-}" ]; then
    set -- --mode "$ZELLIJ_TAB_NAMER_INSTALL_MODE" "$@"
fi

PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    exec "$PYTHON_BIN" -m zellij_tab_namer.cli install "$@"
