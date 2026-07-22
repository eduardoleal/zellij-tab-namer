# zellij-tab-namer

`zellij-tab-namer` names Zellij tabs from the state Zellij already exposes. It
ships both a dependency-free Python watcher and a native headless WASM plugin.
Both read pane titles first, protect manual tab names, and use deterministic
local fallbacks when no useful title is available. The Python watcher can also
ask an OpenAI-compatible endpoint to compress long labels.

## Install

Because the repository is private, authenticate the GitHub CLI before cloning.
Run `gh auth login` first if `gh auth status` reports that you are not logged in:

```sh
gh auth status
gh repo clone eduardoleal/zellij-tab-namer
cd zellij-tab-namer
```

### Native WASM plugin

Use the authenticated GitHub CLI session to download the prebuilt `v0.1.0`
plugin and its checksum.

```sh
mkdir -p /tmp/zellij-tab-namer-v0.1.0
gh release download v0.1.0 \
  --repo eduardoleal/zellij-tab-namer \
  --pattern 'zellij-tab-namer.wasm*' \
  --dir /tmp/zellij-tab-namer-v0.1.0 \
  --clobber

(cd /tmp/zellij-tab-namer-v0.1.0 && \
  shasum -a 256 -c zellij-tab-namer.wasm.sha256)

./install.sh --mode wasm \
  --wasm-source /tmp/zellij-tab-namer-v0.1.0/zellij-tab-namer.wasm \
  --wasm-sha256 31b074209af582f580f5c1af0763739a4b3ca9d9c60828823ad49d8505aee5b3 \
  --wasm-permission ReadApplicationState \
  --wasm-permission ChangeApplicationState
```

The checksum command and installer both verify the release before changing
Zellij configuration. The release assets are available from the
[`v0.1.0` release](https://github.com/eduardoleal/zellij-tab-namer/releases/tag/v0.1.0).
Start a fresh Zellij session after installation so the plugin and pre-granted
permissions are loaded.

### Python watcher

Install the dependency-free Python watcher instead:

```sh
./install.sh --mode cli
```

The installer script installs the Python package and then runs the selected
setup mode. For local development, set `ZELLIJ_TAB_NAMER_EDITABLE=1` to request
an editable package install. The package has no runtime dependencies outside
the Python standard library.

`zellij-tab-namer install` defaults to `--mode both`, but the WASM path is
skipped unless `--wasm-source` or `--wasm-url` is provided.

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

To build instead of using a release, compile the native plugin with Cargo and
install the resulting artifact:

```sh
rustup target add wasm32-wasip1
cargo test --lib
cargo build --release --target wasm32-wasip1 --bin zellij-tab-namer
```

```sh
zellij-tab-namer install --mode wasm \
  --wasm-source ./target/wasm32-wasip1/release/zellij-tab-namer.wasm \
  --wasm-permission ReadApplicationState \
  --wasm-permission ChangeApplicationState
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
elsewhere). Zellij loads the plugin from a `file:/...` URL in `config.kdl`, but
its permission cache keys the grant by the normalized absolute WASM path with
no `file:` prefix.

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

## Native Plugin

The Rust plugin subscribes to Zellij pane and tab updates, debounces bursts of
events, and renames tabs by stable ID. It uses the best eligible non-plugin pane
title, queries Zellij for that pane's working directory and running command for
the deterministic `project - activity` fallback, and never reads pane contents
or scrollback. Existing non-default names are preserved as manual overrides;
clearing a tab name
re-enables automatic naming for that tab.

The plugin requests `ReadApplicationState` and `ChangeApplicationState` and is
designed to run headlessly through `load_plugins`. A fresh Zellij server is
required after pre-granting those permissions. Native LLM compression is not
yet included; long labels are shortened locally without network access.

## Releases

GitHub Actions builds and checks the native plugin for pull requests, pushes to
`main`, and manual workflow runs. Each successful run publishes a workflow
artifact named `zellij-tab-namer-wasm` containing:

- `zellij-tab-namer.wasm`
- `zellij-tab-namer.wasm.sha256`

To publish those files on a GitHub Release, update the package version in
`Cargo.toml`, create the matching `v<version>` tag, and push it. For example,
version `0.1.0` is released from tag `v0.1.0`. Published release assets are
immutable: rebuilding an existing release tag does not replace them. Publish a
new version and tag when the artifact changes.
