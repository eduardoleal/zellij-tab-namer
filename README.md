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

The repository also includes an installer script that installs the package and
then runs the setup command:

```sh
./install.sh --dry-run
./install.sh --mode cli
```

For local development, set `ZELLIJ_TAB_NAMER_EDITABLE=1` before running the
script to request an editable install.

By default `zellij-tab-namer install` uses `--mode both`: it configures the CLI
watcher path today and reports the native WASM path as unavailable until a
plugin artifact is provided.

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

## Installer

The installer is safe to run as a dry run first:

```sh
zellij-tab-namer install --mode both --dry-run
```

CLI mode writes local installer config under
`~/.config/zellij-tab-namer/config.json`, including the generated watch command
and verification command. It does not edit `config.kdl` for the Python watcher;
automatic headless startup belongs to the native WASM plugin path.

```sh
zellij-tab-namer install --mode cli --max-chars 28 --interval 2 --no-llm
```

WASM mode expects a plugin artifact. Until releases publish
`zellij-tab-namer.wasm`, use `--wasm-source` for a local artifact:

```sh
zellij-tab-namer install --mode wasm \
  --wasm-source ./target/wasm32-wasip1/release/zellij-tab-namer.wasm \
  --wasm-permission ReadApplicationState \
  --wasm-permission ChangeApplicationState
```

Remote artifact installs require HTTPS plus an expected digest:

```sh
zellij-tab-namer install --mode wasm \
  --wasm-url https://example.com/zellij-tab-namer.wasm \
  --wasm-sha256 "<64-character-sha256>"
```

WASM setup installs the artifact to the Zellij plugins directory, adds a
`zellij-tab-namer` plugin alias, adds it to `load_plugins`, and can pre-grant
known permissions in `permissions.kdl`. File mutations create backups and write
a rollback manifest. Local and remote artifacts are validated before config is
mutated. The installer reports when a fresh Zellij session is needed; it does
not delete sessions or restart Zellij for you.

Permission grants are written to Zellij's platform cache path by default
(`~/Library/Caches/org.Zellij-Contributors.Zellij/permissions.kdl` on macOS,
`$XDG_CACHE_HOME/zellij/permissions.kdl` or `~/.cache/zellij/permissions.kdl`
elsewhere) and keyed by the same `file:/.../zellij-tab-namer.wasm` plugin URL
that `config.kdl` loads.

Rollback uses the latest manifest:

```sh
zellij-tab-namer rollback --dry-run
zellij-tab-namer rollback
```

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

For installer config, pass endpoint metadata without secrets:

```sh
zellij-tab-namer install --mode cli \
  --llm-base-url "http://localhost:11434/v1" \
  --llm-model "llama3.2"
```

The API key remains an environment variable; it is not written into installer
config.

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
the behavior. Installer support for artifact placement, `config.kdl` startup
wiring, permission grants, backups, and rollback is present now; the native
plugin artifact itself is still a future deliverable.
