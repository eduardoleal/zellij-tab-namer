"""Command line interface for the Zellij tab namer."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from zellij_tab_namer.installer import (
    InstallOptions,
    InstallPaths,
    InstallResult,
    MANIFEST_FILENAME,
    MODE_BOTH,
    VALID_MODES,
    format_result,
    install,
    rollback,
)
from zellij_tab_namer.llm import compress_label, config_from_env
from zellij_tab_namer.naming import NamerConfig, RenameDecision, plan_renames


CommandRunner = Callable[[List[str]], subprocess.CompletedProcess]


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


def run(
    argv: Optional[Sequence[str]] = None,
    command_runner: Optional[CommandRunner] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    runner = command_runner or _run_command
    args = _build_parser().parse_args(argv)

    if args.command == "install":
        return _run_install(args, out, err)

    if args.command == "rollback":
        return _run_rollback(args, out, err)

    if args.command == "session-lock":
        return _run_session_lock(args, out)

    if args.command == "watch":
        while True:
            code = _run_once(args, runner, out, err)
            if code != 0:
                return code
            time.sleep(args.interval)

    return _run_once(args, runner, out, err)


def _run_once(
    args: argparse.Namespace,
    runner: CommandRunner,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    list_result = runner([args.zellij_bin, "action", "list-panes", "--json", "--all"])
    if list_result.returncode != 0:
        stderr.write(list_result.stderr or "zellij list-panes failed\n")
        if list_result.stderr and not list_result.stderr.endswith("\n"):
            stderr.write("\n")
        return list_result.returncode

    try:
        panes = json.loads(list_result.stdout or "[]")
    except json.JSONDecodeError as exc:
        stderr.write(f"failed to parse Zellij pane JSON: {exc}\n")
        return 1
    if not isinstance(panes, list):
        stderr.write("failed to parse Zellij pane JSON: expected a list\n")
        return 1

    try:
        state = _load_state(args.state_file)
    except OSError as exc:
        stderr.write(f"failed to read state file: {exc}\n")
        return 1
    except json.JSONDecodeError as exc:
        stderr.write(f"failed to parse state file: {exc}\n")
        return 1

    llm_config = config_from_env()
    compressor = None
    if not args.no_llm and llm_config.base_url:
        def compressor(label: str, max_chars: int) -> Optional[str]:
            return compress_label(label, max_chars, llm_config)

    decisions, next_state = plan_renames(
        panes,
        state=state,
        config=NamerConfig(max_chars=args.max_chars),
        compressor=compressor,
        force=args.force,
    )
    session_locks = state.get("session_locks")
    if isinstance(session_locks, dict):
        next_state["session_locks"] = dict(session_locks)

    applied = 0
    for decision in decisions:
        if decision.skipped:
            _print_skip(stdout, decision)
            continue

        if args.dry_run:
            stdout.write(
                f"dry-run rename tab {decision.tab_id}: "
                f"{decision.current_name} -> {decision.candidate}\n"
            )
            continue

        rename_result = runner(
            [
                args.zellij_bin,
                "action",
                "rename-tab-by-id",
                str(decision.tab_id),
                decision.candidate,
            ]
        )
        if rename_result.returncode != 0:
            stderr.write(rename_result.stderr or "zellij rename-tab-by-id failed\n")
            if rename_result.stderr and not rename_result.stderr.endswith("\n"):
                stderr.write("\n")
            return rename_result.returncode

        applied += 1
        stdout.write(
            f"renamed tab {decision.tab_id}: "
            f"{decision.current_name} -> {decision.candidate}\n"
        )

    if args.dry_run:
        if not decisions:
            stdout.write("dry-run no changes\n")
        return 0

    if applied:
        try:
            with _locked_state(args.state_file) as latest_state:
                latest_state["generated"] = next_state["generated"]
                _save_state(args.state_file, latest_state)
        except OSError as exc:
            stderr.write(f"failed to write state file: {exc}\n")
            return 1
    elif not decisions:
        stdout.write("no changes\n")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zellij-tab-namer",
        description="Name Zellij tabs from pane titles and lightweight context.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser("once", help="name tabs once and exit")
    _add_common_options(once)

    watch = subparsers.add_parser("watch", help="name tabs repeatedly")
    _add_common_options(watch)
    watch.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between watch iterations",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="set up CLI watcher or WASM plugin integration",
    )
    install_parser.add_argument(
        "--mode",
        choices=sorted(VALID_MODES),
        default=MODE_BOTH,
        help="runtime path to configure",
    )
    install_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between watch iterations for the CLI watcher",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned installer changes without writing files",
    )
    _add_runtime_options(install_parser, no_llm_default=None)
    _add_install_path_options(install_parser)
    install_parser.add_argument(
        "--llm-base-url",
        help="OpenAI-compatible chat completions base URL to record in config",
    )
    install_parser.add_argument(
        "--llm-model",
        help="model name to record in config",
    )
    install_parser.add_argument(
        "--command-name",
        default="zellij-tab-namer",
        help="command name used in generated watch and verification commands",
    )
    install_parser.add_argument(
        "--wasm-source",
        help="local WASM artifact to install into the Zellij plugins directory",
    )
    install_parser.add_argument(
        "--wasm-url",
        help="WASM artifact URL to download and install",
    )
    install_parser.add_argument(
        "--wasm-sha256",
        help="expected SHA-256 digest for --wasm-source or --wasm-url",
    )
    install_parser.add_argument(
        "--wasm-permission",
        action="append",
        default=[],
        help="permission grant to add for WASM mode; may be repeated",
    )
    install_parser.add_argument(
        "--no-freeze-permissions",
        action="store_true",
        help="do not restore the macOS immutable flag after editing permissions.kdl",
    )

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="restore files from the latest installer manifest",
    )
    rollback_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print rollback actions without mutating files",
    )
    _add_install_path_options(rollback_parser)

    session_lock = subparsers.add_parser(
        "session-lock", help="query or update durable session lock state"
    )
    session_lock.add_argument("operation", choices=("query", "mark", "unmark", "cleanup"))
    session_lock.add_argument("--state-file", default=_default_state_file())
    session_lock.add_argument("name")

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned renames without applying them or saving state",
    )
    _add_runtime_options(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="rename even when the current tab name looks manual",
    )


def _add_runtime_options(
    parser: argparse.ArgumentParser,
    no_llm_default: Optional[bool] = False,
) -> None:
    parser.add_argument(
        "--max-chars",
        type=int,
        default=32,
        help="maximum generated tab label length",
    )
    parser.add_argument(
        "--state-file",
        default=_default_state_file(),
        help="path to generated-name state JSON",
    )
    parser.add_argument(
        "--zellij-bin",
        default="zellij",
        help="zellij executable to invoke",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=no_llm_default,
        help="disable optional OpenAI-compatible label compression",
    )


def _add_install_path_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--zellij-config-dir",
        help="Zellij config directory, defaulting to XDG config or ~/.config/zellij",
    )
    parser.add_argument(
        "--zellij-config-file",
        help="Zellij config file to edit, defaulting to ZELLIJ_CONFIG_FILE or <zellij-config-dir>/config.kdl",
    )
    parser.add_argument(
        "--plugins-dir",
        help="Zellij plugin directory, defaulting to <zellij-config-dir>/plugins",
    )
    parser.add_argument(
        "--tab-namer-config-dir",
        help="config directory for zellij-tab-namer installer files",
    )
    parser.add_argument(
        "--permissions-file",
        help="Zellij permissions.kdl file to update for WASM grants",
    )
    parser.add_argument(
        "--backup-dir",
        help="directory for installer-created backups",
    )
    parser.add_argument(
        "--manifest-file",
        help="installer manifest used by rollback",
    )


def _run_install(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        paths = _install_paths_from_args(args)
        result = install(
            InstallOptions(
                mode=args.mode,
                dry_run=args.dry_run,
                max_chars=args.max_chars,
                interval=args.interval,
                no_llm=args.no_llm,
                llm_base_url=args.llm_base_url,
                llm_model=args.llm_model,
                zellij_bin=args.zellij_bin,
                command_name=args.command_name,
                wasm_source=Path(args.wasm_source).expanduser()
                if args.wasm_source
                else None,
                wasm_url=args.wasm_url,
                wasm_sha256=args.wasm_sha256,
                wasm_permissions=tuple(args.wasm_permission or ()),
                freeze_permissions=not args.no_freeze_permissions,
                paths=paths,
            )
        )
    except Exception as exc:
        stderr.write(f"install failed: {exc}\n")
        return 1

    return _emit_result(result, stdout, stderr)


def _run_rollback(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        result = rollback(paths=_install_paths_from_args(args), dry_run=args.dry_run)
    except Exception as exc:
        stderr.write(f"rollback failed: {exc}\n")
        return 1

    return _emit_result(result, stdout, stderr)


def _run_session_lock(args: argparse.Namespace, stdout: TextIO) -> int:
    name = args.name
    if not _valid_session_name(name):
        stdout.write(json.dumps({"error": "invalid session name"}) + "\n")
        return 2
    try:
        with _locked_state(args.state_file, require_object=True) as state:
            locks = state.setdefault("session_locks", {})
            if not isinstance(locks, dict):
                raise ValueError("state session_locks must contain a JSON object")
            if args.operation == "query":
                result = {"name": name, "locked": bool(locks.get(name))}
            elif args.operation == "mark":
                locks[name] = True
                _save_state(args.state_file, state)
                result = {"name": name, "locked": True}
            else:
                locks.pop(name, None)
                _save_state(args.state_file, state)
                result = {"name": name, "locked": False}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        stdout.write(json.dumps({"error": str(exc)}) + "\n")
        return 1
    stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def _valid_session_name(name: str) -> bool:
    return bool(name) and len(name) <= 64 and all(
        character.isalnum() or character in " ._-" for character in name
    )


class _locked_state:
    def __init__(self, state_file: str, require_object: bool = False) -> None:
        self.state_file = state_file
        self.require_object = require_object
        self.handle: Optional[TextIO] = None

    def __enter__(self) -> Dict[str, Any]:
        directory = os.path.dirname(self.state_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.handle = open(f"{self.state_file}.lock", "a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        try:
            return _load_state(
                self.state_file,
                require_object=self.require_object,
            )
        except Exception:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None
            raise

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _emit_result(
    result: InstallResult,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    output = format_result(result)
    if result.status == "blocked":
        stderr.write(output)
    else:
        stdout.write(output)
    return result.exit_code()


def _install_paths_from_args(args: argparse.Namespace) -> InstallPaths:
    defaults = InstallPaths.defaults()
    zellij_config_dir = _path_arg(args.zellij_config_dir, defaults.zellij_config_dir)
    default_zellij_config_file = (
        zellij_config_dir / "config.kdl"
        if args.zellij_config_dir and not args.zellij_config_file
        else defaults.zellij_config_file
    )
    zellij_config_file = _path_arg(
        args.zellij_config_file,
        default_zellij_config_file,
        file_target=True,
    )
    plugins_dir = _path_arg(
        args.plugins_dir,
        zellij_config_dir / "plugins",
    )
    tab_namer_config_dir = _path_arg(
        args.tab_namer_config_dir,
        defaults.tab_namer_config_dir,
    )
    state_file = _path_arg(
        getattr(args, "state_file", None),
        defaults.state_file,
        file_target=True,
    )
    permissions_file = _path_arg(
        args.permissions_file,
        defaults.permissions_file,
        file_target=True,
    )
    backup_dir = _path_arg(args.backup_dir, tab_namer_config_dir / "backups")
    manifest_file = _path_arg(
        args.manifest_file,
        tab_namer_config_dir / MANIFEST_FILENAME,
        file_target=True,
    )
    return InstallPaths(
        zellij_config_dir=zellij_config_dir,
        zellij_config_file=zellij_config_file,
        plugins_dir=plugins_dir,
        tab_namer_config_dir=tab_namer_config_dir,
        state_file=state_file,
        permissions_file=permissions_file,
        backup_dir=backup_dir,
        manifest_file=manifest_file,
    )


def _path_arg(
    value: Optional[str],
    default: Path,
    file_target: bool = False,
) -> Path:
    path = Path(value).expanduser() if value else default.expanduser()
    if not file_target:
        return path.resolve(strict=False)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve(strict=False) / path.name


def _load_state(path: str, require_object: bool = False) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"generated": {}}
    if not isinstance(data, dict):
        if require_object:
            raise ValueError("state file must contain a JSON object")
        return {"generated": {}}
    generated = data.get("generated")
    if not isinstance(generated, dict):
        if require_object and generated is not None:
            raise ValueError("state generated must contain a JSON object")
        data["generated"] = {}
    return data


def _save_state(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _default_state_file() -> str:
    root = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(root, "zellij-tab-namer", "state.json")


def _print_skip(stdout: TextIO, decision: RenameDecision) -> None:
    stdout.write(
        f"skip tab {decision.tab_id}: {decision.skip_reason} "
        f"({decision.current_name} -> {decision.candidate})\n"
    )


def _run_command(args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
