# zellij-tab-namer

`zellij-tab-namer` names Zellij tabs from state Zellij already exposes. It has
two runtime paths:

- The dependency-free **Python watcher** runs as an external process. It can
  use any OpenAI-compatible endpoint configured through environment variables.
- The **native headless WASM plugin** runs inside Zellij. Its optional v0.2.0
  refinement targets unauthenticated Ollama on the local loopback interface.

Both paths prefer pane titles, protect manual tab names, and fall back to
deterministic local labels when model refinement is disabled or unavailable.

## Install

### Homebrew

```sh
brew install eduardoleal/tap/zellij-tab-namer

zellij-tab-namer install --mode wasm \
  --wasm-source "$(brew --prefix zellij-tab-namer)/libexec/plugin/zellij-tab-namer.wasm"
zellij delete-all-sessions --force
```

The formula installs the checksum-pinned v0.2.0 source archive and published
WASM release asset, then prints the one-time Zellij setup command as its caveat.
The setup command edits Zellij configuration and pre-grants the headless plugin
permissions; run the final command from outside Zellij so a fresh server loads
them.

### Source checkout for non-Homebrew installs

The released-WASM and Python-watcher paths below run `install.sh` from an
source checkout. Clone it once before using either path:

```sh
git clone https://github.com/eduardoleal/zellij-tab-namer.git
cd zellij-tab-namer
```

### Released native WASM plugin

To install without Homebrew, download the prebuilt `v0.2.0` plugin and its
checksum.

```sh
mkdir -p /tmp/zellij-tab-namer-v0.2.0
curl -fL \
  -o /tmp/zellij-tab-namer-v0.2.0/zellij-tab-namer.wasm \
  https://github.com/eduardoleal/zellij-tab-namer/releases/download/v0.2.0/zellij-tab-namer.wasm
curl -fL \
  -o /tmp/zellij-tab-namer-v0.2.0/zellij-tab-namer.wasm.sha256 \
  https://github.com/eduardoleal/zellij-tab-namer/releases/download/v0.2.0/zellij-tab-namer.wasm.sha256

(cd /tmp/zellij-tab-namer-v0.2.0 && \
  shasum -a 256 -c zellij-tab-namer.wasm.sha256)

./install.sh --mode wasm \
  --wasm-source /tmp/zellij-tab-namer-v0.2.0/zellij-tab-namer.wasm \
  --wasm-sha256 e6f4eb2f404a86dd27cb318d4ce4365869e44ca578b30e1a33932bc73682465b \
  --wasm-permission ReadApplicationState \
  --wasm-permission ChangeApplicationState
```

The checksum command and installer both verify the release before changing
Zellij configuration. The release assets are available from the
[`v0.2.0` release](https://github.com/eduardoleal/zellij-tab-namer/releases/tag/v0.2.0).
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

## Python watcher

Preview one naming pass:

```sh
python3 -m zellij_tab_namer.cli once --dry-run
```

Apply once or watch continuously:

```sh
python3 -m zellij_tab_namer.cli once
python3 -m zellij_tab_namer.cli watch --interval 2
```

Useful watcher options include:

```sh
python3 -m zellij_tab_namer.cli once --max-chars 28 --no-llm
python3 -m zellij_tab_namer.cli once --force
python3 -m zellij_tab_namer.cli once \
  --state-file ~/.local/state/zellij-tab-namer/state.json
```

`--dry-run` reads live Zellij JSON and prints planned decisions without calling
`rename-tab-by-id` or updating generated-name state. The watcher stores its last
generated name per tab. A non-default current name that differs from that value
is treated as a manual override; `--force` deliberately bypasses that protection
for one run. If the state file is lost, the watcher preserves clear non-default
names.

### Optional watcher LLM configuration

The watcher uses OpenAI-compatible chat completions and reads its configuration
from the process environment:

```sh
export ZELLIJ_TAB_NAMER_BASE_URL="http://localhost:11434/v1"
export ZELLIJ_TAB_NAMER_MODEL="llama3.2"
export ZELLIJ_TAB_NAMER_API_KEY="ollama"
export ZELLIJ_TAB_NAMER_TIMEOUT="0.8"
```

The API key remains an environment variable; it is not written into installer
configuration. Watcher configuration and behavior are independent of the
native plugin settings described below.

Install watcher configuration with:

```sh
zellij-tab-namer install --mode cli --max-chars 28 --interval 2 \
  --llm-base-url "http://localhost:11434/v1" \
  --llm-model "llama3.2"
```

CLI mode writes local installer configuration under
`~/.config/zellij-tab-namer/config.json`, including generated watch and
verification commands. It does not edit Zellij's `config.kdl`.

## Native WASM plugin

The native plugin subscribes to Zellij pane and tab metadata and renames tabs
by stable ID. It never reads pane contents or scrollback. It applies a
deterministic shortened label immediately; when native Ollama is enabled, a
valid current model result may refine that label asynchronously.

A model failure, rejected result, denied permission, stopped server, or two
hung requests leaves deterministic naming active. Only one nonempty output line
within `max_chars` is accepted. Late, stale, cross-tab, and closed-tab results
are ignored. A manual rename always wins, including while a request is in
flight. Native manual protection lasts for that tab's lifetime; open a new tab
to resume automatic naming for that work.

### Build and install without native Ollama

To build instead of using a release, compile the native plugin with Cargo and
install the resulting artifact:

```sh
rustup target add wasm32-wasip1
cargo test --locked --lib
cargo build --locked --release --target wasm32-wasip1 \
  --bin zellij-tab-namer

zellij-tab-namer install --mode wasm \
  --wasm-source ./target/wasm32-wasip1/release/zellij-tab-namer.wasm \
  --no-llm
```

Without complete native LLM configuration, the plugin requests only
`ReadApplicationState` and `ChangeApplicationState`, makes no web request, and
uses deterministic naming.

### Enable local Ollama refinement

Install Ollama, start its local server, and fetch the configured model:

```sh
ollama serve
ollama pull llama3.2
```

In another terminal, install the native artifact with a complete loopback
configuration:

```sh
zellij-tab-namer install --mode wasm \
  --wasm-source ./target/wasm32-wasip1/release/zellij-tab-namer.wasm \
  --llm-base-url "http://localhost:11434/v1" \
  --llm-model "llama3.2"
```

The native endpoint is fixed to OpenAI-compatible `/v1/chat/completions`.
Installer validation accepts only unauthenticated `http` URLs using
`localhost`, `127.0.0.1`, or `[::1]`, with no query or fragment. Native mode
does not accept or require an API key. Supplying only one LLM option is an
error. Omitting both options preserves existing managed LLM nodes during an
artifact upgrade; explicit `--no-llm` removes them.

The only variable metadata in a native request is the selected pane title or
already-derived fallback label, the configured model name, and the maximum
label length. The JSON body also contains fixed prompt text and bounded
generation parameters. It does not contain pane contents, scrollback, raw
working directories, full commands, environment data, manual tab names, tab
IDs, or local paths. Working directory and command metadata may be used locally
to derive a fallback, but the raw values are not transmitted.

Zellij 0.44 follows HTTP redirects before returning a response to the plugin.
Consequently, a loopback Ollama-compatible server can redirect the disclosed
title/fallback metadata to another host; only run a server you trust. The
plugin rejects response bodies above 64 KiB when it receives them, but that
limit does not prevent Zellij's host from buffering a larger redirected
response first.

### Permissions and fresh-server restart

WASM setup installs the artifact, maintains one `zellij-tab-namer` alias and
`load_plugins` entry, and pre-grants the plugin's baseline permissions. A
complete native Ollama configuration also pre-grants `WebAccess`. Permissions
are keyed by the normalized absolute WASM path, without the `file:` prefix used
in `config.kdl`:

- macOS: `~/Library/Caches/org.Zellij-Contributors.Zellij/permissions.kdl`
- Linux/XDG: `$XDG_CACHE_HOME/zellij/permissions.kdl` or
  `~/.cache/zellij/permissions.kdl`

Headless plugins have no focusable permission prompt. On macOS the installer
therefore safely unfreezes an existing permissions file when necessary, writes
and verifies the grants transactionally, and freezes it with the `uchg` flag by
default so a running Zellij server cannot overwrite the pre-grant on exit.
Backups and a rollback manifest are created for mutations. Use
`--no-freeze-permissions` only if you intentionally manage that lifecycle
yourself.

Zellij caches plugin configuration and grants in the server process. After an
install or configuration/permission change, exit Zellij and run this from a
terminal outside Zellij before starting a new session:

```sh
zellij delete-all-sessions --force
```

`--no-llm` prevents new automatic `WebAccess` grants but does not remove an
existing grant from the shared permission cache: the installer cannot know
whether another configuration still relies on it. To revoke it manually on
macOS, first stop all Zellij sessions, then:

```sh
permissions_file="$HOME/Library/Caches/org.Zellij-Contributors.Zellij/permissions.kdl"
chflags nouchg "$permissions_file"
# Edit only this plugin's absolute-path entry and remove its WebAccess line.
chflags uchg "$permissions_file"
```

Keep the installer's backup, preserve valid KDL, and restart with a fresh
Zellij server afterward. On other platforms, edit the applicable cache path and
preserve its existing ownership and mode. Do not remove `WebAccess` if another
installation sharing that exact plugin-path entry still needs it.

### Native diagnostics

Native failures keep the deterministic label and emit only bounded category
names: `IncompleteConfiguration`, `PermissionDenied`, `TransportFailure`,
`HttpStatus`, `ValidationRejected`, or `SchedulerSaturated`. Diagnostics never
include source text, request prompts, response bodies, or complete endpoint
URLs, and each category is emitted at most once for a naming generation. Two
hung host requests saturate the fixed scheduler until a response arrives or a
fresh server is started; tab naming itself continues locally.

## Installer safety and rollback

Run a dry run before either installation mode:

```sh
zellij-tab-namer install --mode both --dry-run
```

Remote artifact installs require HTTPS plus an expected digest:

```sh
zellij-tab-namer install --mode wasm \
  --wasm-url https://example.com/zellij-tab-namer.wasm \
  --wasm-sha256 "<64-character-sha256>"
```

Local and remote artifacts are validated before configuration is mutated.
Configuration and permission writes are backed up, verified, and rolled back
on failure. The installer refuses unsafe symlink targets and reports whether a
fresh session is required; it never deletes sessions for you.

Rollback uses the latest manifest:

```sh
zellij-tab-namer rollback --dry-run
zellij-tab-namer rollback
```

## Releases

GitHub Actions checks, tests, builds, packages, and checksums the native plugin
for pull requests, pushes to `main`, and manual workflow runs. Each successful
build uploads a workflow artifact named `zellij-tab-namer-wasm` containing:

- `zellij-tab-namer.wasm`
- `zellij-tab-namer.wasm.sha256`

Publishing is separate from preparing v0.2.0. The workflow publishes a GitHub
Release only for a pushed `v<version>` tag matching the Cargo package version,
and it refuses to replace an existing release's immutable assets. This work
does not create or push `v0.2.0`; release publication requires an explicit
follow-up action.
