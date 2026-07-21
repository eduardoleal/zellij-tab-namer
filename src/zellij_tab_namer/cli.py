"""Command line interface for the Zellij tab namer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

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
            _save_state(args.state_file, next_state)
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

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print planned renames without applying them or saving state",
    )
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
        help="disable optional OpenAI-compatible label compression",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rename even when the current tab name looks manual",
    )


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"generated": {}}
    if not isinstance(data, dict):
        return {"generated": {}}
    generated = data.get("generated")
    if not isinstance(generated, dict):
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
