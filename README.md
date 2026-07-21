# zellij-tab-namer

`zellij-tab-namer` is a dependency-free Python watcher that names Zellij tabs
from the state Zellij already exposes. It reads pane titles first, falls back to
`project - activity`, and can optionally ask an OpenAI-compatible endpoint to
compress long labels.

The first slice is a companion CLI, not a native Zellij plugin. That keeps the
naming policy easy to test before any headless plugin permission wiring is
added to the dot-config repo.

## Install

From this checkout:

```sh
python3 -m pip install -e .
```

The package has no runtime dependencies outside the Python standard library.

## Usage

Start with a dry run:

```sh
python3 -m zellij_tab_namer.cli once --dry-run
```

Apply names once:

```sh
python3 -m zellij_tab_namer.cli once
```

Run continuously:

```sh
python3 -m zellij_tab_namer.cli watch --interval 2
```

Useful options:

```sh
python3 -m zellij_tab_namer.cli once --max-chars 28 --no-llm
python3 -m zellij_tab_namer.cli once --force
python3 -m zellij_tab_namer.cli once --state-file ~/.local/state/zellij-tab-namer/state.json
```

`--dry-run` reads live Zellij JSON and prints the planned rename decisions
without calling `rename-tab-by-id` or updating the generated-name state.

## LLM Compression

LLM use is optional. When no endpoint is configured, labels are shortened
locally.

The client uses OpenAI-compatible chat completions:

```sh
export ZELLIJ_TAB_NAMER_BASE_URL="http://localhost:11434/v1"
export ZELLIJ_TAB_NAMER_MODEL="llama3.2"
export ZELLIJ_TAB_NAMER_API_KEY="ollama"
export ZELLIJ_TAB_NAMER_TIMEOUT="0.8"
```

Only the chosen pane title or pushed metadata text is sent to the endpoint,
along with the maximum label size. The watcher does not send visible pane text
or scrollback.

## Manual Overrides

The watcher stores the last generated name per tab. If the current tab name is
non-default and differs from the stored generated value, it is treated as a
manual override and skipped. Use `--force` for a deliberate one-off refresh.

If the state file is lost, the watcher errs on the side of preserving clear
non-default tab names rather than overwriting them.

## Native Plugin Path

The future Zellij WASM plugin can reuse this naming policy after the CLI proves
the behavior. Plugin packaging, permission grants, and dot-config startup
wiring are intentionally deferred.
