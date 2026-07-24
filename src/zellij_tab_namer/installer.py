"""Installer and rollback helpers for zellij-tab-namer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse
from urllib.request import urlopen


MODE_CLI = "cli"
MODE_WASM = "wasm"
MODE_BOTH = "both"
VALID_MODES = {MODE_CLI, MODE_WASM, MODE_BOTH}

PLUGIN_ALIAS = "zellij-tab-namer"
PLUGIN_FILENAME = "zellij-tab-namer.wasm"
CONFIG_FILENAME = "config.json"
MANIFEST_FILENAME = "install-manifest.json"
API_KEY_ENV = "ZELLIJ_TAB_NAMER_API_KEY"
WASM_MAGIC = b"\0asm"
URL_TIMEOUT_SECONDS = 10

KNOWN_ZELLIJ_PERMISSIONS = frozenset(
    {
        "ReadApplicationState",
        "ChangeApplicationState",
        "OpenFiles",
        "RunCommands",
        "OpenTerminalsOrPlugins",
        "WriteToStdin",
        "Reconfigure",
        "FullHdAccess",
        "StartWebServer",
        "InterceptInput",
        "ReadPaneContents",
        "RunActionsAsUser",
        "WriteToClipboard",
        "ReadSessionEnvironmentVariables",
        "WebAccess",
        "ReadCliPipes",
        "MessageAndLaunchOtherPlugins",
    }
)
_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")


class InstallError(RuntimeError):
    """Raised when the installer cannot safely continue."""


@dataclass(frozen=True)
class InstallPaths:
    zellij_config_dir: Path
    zellij_config_file: Path
    plugins_dir: Path
    tab_namer_config_dir: Path
    state_file: Path
    permissions_file: Path
    backup_dir: Path
    manifest_file: Path

    @classmethod
    def defaults(
        cls,
        home: Optional[Path] = None,
        xdg_config_home: Optional[Path] = None,
        xdg_state_home: Optional[Path] = None,
    ) -> "InstallPaths":
        resolved_home = _normalize_path(home or Path.home())
        config_root = _normalize_path(
            xdg_config_home or _env_path("XDG_CONFIG_HOME", resolved_home / ".config")
        )
        state_root = _normalize_path(
            xdg_state_home or _env_path("XDG_STATE_HOME", resolved_home / ".local/state")
        )
        zellij_config_dir = _default_zellij_config_dir(resolved_home, config_root)
        zellij_config_file = _default_zellij_config_file(zellij_config_dir)
        tab_namer_config_dir = config_root / "zellij-tab-namer"
        return cls(
            zellij_config_dir=zellij_config_dir,
            zellij_config_file=zellij_config_file,
            plugins_dir=zellij_config_dir / "plugins",
            tab_namer_config_dir=tab_namer_config_dir,
            state_file=state_root / "zellij-tab-namer" / "state.json",
            permissions_file=_default_permissions_file(resolved_home),
            backup_dir=tab_namer_config_dir / "backups",
            manifest_file=tab_namer_config_dir / MANIFEST_FILENAME,
        )


@dataclass(frozen=True)
class InstallOptions:
    mode: str = MODE_BOTH
    dry_run: bool = False
    max_chars: int = 32
    interval: float = 2.0
    no_llm: Optional[bool] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    zellij_bin: str = "zellij"
    command_name: str = "zellij-tab-namer"
    wasm_source: Optional[Path] = None
    wasm_url: Optional[str] = None
    wasm_sha256: Optional[str] = None
    wasm_permissions: Tuple[str, ...] = ()
    freeze_permissions: bool = True
    paths: InstallPaths = field(default_factory=InstallPaths.defaults)


@dataclass(frozen=True)
class BackupRecord:
    target: str
    backup: Optional[str]
    existed: bool
    checksum: Optional[str] = None
    immutable: Optional[bool] = None


@dataclass(frozen=True)
class Operation:
    action: str
    target: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class WasmChanges:
    config_path: Path
    original_config: str
    next_config: str
    config_changed: bool
    permissions_path: Path
    original_permissions: Optional[str] = None
    permissions_originally_immutable: bool = False
    next_permissions: Optional[str] = None
    permissions_changed: bool = False


@dataclass
class InstallResult:
    status: str
    mode: str
    dry_run: bool
    runtime_status: Dict[str, str] = field(default_factory=dict)
    operations: List[Operation] = field(default_factory=list)
    backups: List[BackupRecord] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    verification_command: List[str] = field(default_factory=list)
    fresh_session_required: bool = False
    manifest_path: Optional[str] = None

    def exit_code(self) -> int:
        return 1 if self.status == "blocked" else 0


def install(options: InstallOptions) -> InstallResult:
    if options.mode not in VALID_MODES:
        raise InstallError(f"unsupported install mode: {options.mode}")

    options = _normalize_options(options)
    result = InstallResult(
        status="complete",
        mode=options.mode,
        dry_run=options.dry_run,
        manifest_path=str(options.paths.manifest_file),
    )

    if _mode_includes(options.mode, MODE_CLI):
        _install_cli(options, result)

    if _mode_includes(options.mode, MODE_WASM):
        _install_wasm(options, result)

    _resolve_overall_status(options, result)
    if result.status == "blocked":
        _rollback_blocked_install(options, result)
        return result

    if not options.dry_run and _should_write_manifest(result):
        try:
            _write_manifest(options, result)
        except Exception as exc:
            fully_rolled_back = _rollback_applied_records(
                result.backups, options.paths, result
            )
            rollback_detail = (
                "rolled back applied changes"
                if fully_rolled_back
                else "rolled back non-conflicting changes; preserved divergent targets"
            )
            _block_install(
                result,
                f"failed to write installer manifest; {rollback_detail}: {exc}",
            )

    return result


def rollback(
    paths: Optional[InstallPaths] = None,
    dry_run: bool = False,
) -> InstallResult:
    resolved_paths = _normalize_paths(paths or InstallPaths.defaults())
    result = InstallResult(
        status="complete",
        mode="rollback",
        dry_run=dry_run,
        manifest_path=str(resolved_paths.manifest_file),
    )

    try:
        data = json.loads(resolved_paths.manifest_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result.status = "blocked"
        result.messages.append(f"manifest not found: {resolved_paths.manifest_file}")
        result.operations.append(
            Operation(
                action="read manifest",
                target=str(resolved_paths.manifest_file),
                status="blocked",
                detail="nothing to roll back",
            )
        )
        return result
    except (OSError, json.JSONDecodeError) as exc:
        result.status = "blocked"
        result.messages.append(f"failed to read rollback manifest: {exc}")
        return result

    records = data.get("backups", [])
    if not isinstance(records, list):
        result.status = "blocked"
        result.messages.append("rollback manifest has an unsupported shape")
        return result

    blocker = _rollback_preflight(records, resolved_paths)
    if blocker:
        result.status = "blocked"
        result.messages.append(blocker)
        return result

    ordered_records = _ordered_rollback_records(records, resolved_paths.manifest_file)
    for record in ordered_records:
        _rollback_record(record, resolved_paths, result, dry_run)
        if result.status == "blocked":
            return result

    if not _manifest_record_present(records, resolved_paths.manifest_file):
        _finish_created_manifest_rollback(resolved_paths, result, dry_run)

    return result


def format_result(result: InstallResult) -> str:
    lines = [f"{result.mode} status: {result.status}"]
    for runtime, status in sorted(result.runtime_status.items()):
        lines.append(f"{runtime}: {status}")
    for operation in result.operations:
        detail = f" - {operation.detail}" if operation.detail else ""
        lines.append(
            f"[{operation.status}] {operation.action}: {operation.target}{detail}"
        )
    for message in result.messages:
        lines.append(message)
    if result.verification_command:
        lines.append(f"verify: {shlex.join(result.verification_command)}")
    if result.fresh_session_required:
        lines.append("fresh Zellij session required for plugin permission/config reload")
    if result.manifest_path and not result.dry_run:
        lines.append(f"manifest: {result.manifest_path}")
    return "\n".join(lines) + "\n"


def build_watch_command(options: InstallOptions) -> List[str]:
    command = [
        options.command_name,
        "watch",
        "--interval",
        _format_number(options.interval),
        "--max-chars",
        str(options.max_chars),
        "--state-file",
        str(options.paths.state_file),
        "--zellij-bin",
        options.zellij_bin,
    ]
    if options.no_llm:
        command.append("--no-llm")
    return command


def build_verification_command(options: InstallOptions) -> List[str]:
    command = [
        options.command_name,
        "once",
        "--dry-run",
        "--max-chars",
        str(options.max_chars),
        "--state-file",
        str(options.paths.state_file),
        "--zellij-bin",
        options.zellij_bin,
    ]
    if options.no_llm:
        command.append("--no-llm")
    return command


def normalize_llm_base_url(base_url: str) -> str:
    value = base_url.strip()
    if (
        not value
        or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 for char in value)
    ):
        raise InstallError("native LLM base URL must be a bounded loopback HTTP URL")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise InstallError("native LLM base URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise InstallError("native LLM base URL must be unauthenticated loopback HTTP")
    host = parsed.hostname
    if host is None:
        raise InstallError("native LLM base URL must include a loopback host")
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host) in (
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("::1"),
        )
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback:
        raise InstallError("native LLM base URL must use localhost, 127.0.0.1, or [::1]")

    authority = host if ":" not in host else f"[{host}]"
    if port is not None:
        authority = f"{authority}:{port}"
    path = parsed.path.rstrip("/")
    if path not in ("", "/v1"):
        raise InstallError("native LLM base URL path must be empty or /v1")
    completion_path = f"{path}/chat/completions" if path else "/v1/chat/completions"
    return f"http://{authority}{completion_path}"


def wire_zellij_config(
    config_text: str,
    wasm_path: Path,
    max_chars: int,
    llm_base_url: Optional[str] = None,
    llm_model: Optional[str] = None,
    no_llm: Optional[bool] = None,
    session_lock_command: Optional[str] = None,
    session_lock_state_file: Optional[Path] = None,
) -> Tuple[str, bool]:
    if not _balanced_kdl(config_text):
        raise InstallError("config.kdl appears malformed; refusing to edit it")

    plugins_block = _find_named_block(config_text, "plugins")
    load_block = _find_named_block(config_text, "load_plugins")
    if plugins_block is None or load_block is None:
        raise InstallError(
            "config.kdl must already contain plugins and load_plugins blocks"
        )

    plugin_url = _plugin_url(wasm_path)
    llm_lines = ""
    if not no_llm and llm_base_url is not None and llm_model is not None:
        llm_lines = (
            f"        llm_base_url {_kdl_string(llm_base_url)}\n"
            f"        llm_model {_kdl_string(llm_model)}\n"
        )
    alias_entry = (
        f"    {PLUGIN_ALIAS} location={_kdl_string(plugin_url)} {{\n"
        f"        max_chars {max_chars}\n"
        + (
            f"        session_lock_command {_kdl_string(session_lock_command)}\n"
            f"        session_lock_state_file {_kdl_string(str(session_lock_state_file))}\n"
            if session_lock_command and session_lock_state_file
            else ""
        )
        + f"{llm_lines}"
        + "    }\n"
    )
    changed = False
    next_text = config_text

    alias_block = _find_plugin_alias_block(next_text, plugins_block)
    if alias_block is None:
        next_text = _insert_before_block_close(next_text, plugins_block, alias_entry)
        changed = True
    else:
        alias_text = next_text[alias_block[0] : alias_block[1]]
        alias_text = _reconcile_alias_location(alias_text, plugin_url)
        alias_text = _reconcile_managed_node(alias_text, "max_chars", str(max_chars))
        if session_lock_command and session_lock_state_file:
            alias_text = _reconcile_managed_node(
                alias_text, "session_lock_command", _kdl_string(session_lock_command)
            )
            alias_text = _reconcile_managed_node(
                alias_text,
                "session_lock_state_file",
                _kdl_string(str(session_lock_state_file)),
            )
        if no_llm:
            alias_text = _remove_managed_node(alias_text, "llm_base_url")
            alias_text = _remove_managed_node(alias_text, "llm_model")
        elif llm_base_url is not None and llm_model is not None:
            alias_text = _reconcile_managed_node(
                alias_text, "llm_base_url", _kdl_string(llm_base_url)
            )
            alias_text = _reconcile_managed_node(
                alias_text, "llm_model", _kdl_string(llm_model)
            )
        if alias_text != next_text[alias_block[0] : alias_block[1]]:
            next_text = (
                next_text[: alias_block[0]]
                + alias_text
                + next_text[alias_block[1] :]
            )
            changed = True

    load_block = _find_named_block(next_text, "load_plugins")
    if load_block is None:
        raise InstallError("load_plugins block disappeared while editing config.kdl")
    load_text = next_text[load_block[0] : load_block[1]]
    if not _kdl_bare_node_present(load_text, PLUGIN_ALIAS):
        next_text = _insert_before_block_close(
            next_text,
            load_block,
            f"    {PLUGIN_ALIAS}\n",
        )
        changed = True

    if session_lock_command and session_lock_state_file:
        next_text, close_changed = _reconcile_root_close_behavior(next_text)
        changed = changed or close_changed
        next_text, binding_changed = _reconcile_session_lock_binding(next_text)
        changed = changed or binding_changed

    return next_text, changed


def update_permission_grants(
    permissions_text: str,
    wasm_path: Path,
    grants: Sequence[str],
) -> Tuple[str, bool]:
    normalized_grants = _normalize_permissions(grants)
    if not normalized_grants:
        return permissions_text, False
    if not _balanced_kdl(permissions_text):
        raise InstallError("permissions.kdl appears malformed; refusing to edit it")

    # Zellij loads local plugins through a file: URL, but its permission cache
    # indexes grants by the normalized absolute filesystem path.
    key = _kdl_string(str(_normalize_path(wasm_path)))
    changed = False
    block = _find_exact_quoted_block(permissions_text, key)
    if block is None:
        legacy_key = _kdl_string(_plugin_url(wasm_path))
        legacy_block = _find_exact_quoted_block(permissions_text, legacy_key)
        if legacy_block is not None:
            legacy_text = permissions_text[legacy_block[0] : legacy_block[1]]
            migrated_text = legacy_text.replace(legacy_key, key, 1)
            permissions_text = (
                permissions_text[: legacy_block[0]]
                + migrated_text
                + permissions_text[legacy_block[1] :]
            )
            block = _find_exact_quoted_block(permissions_text, key)
            changed = True
    if block is None:
        grant_lines = "".join(f"    {permission}\n" for permission in normalized_grants)
        addition = f"{key} {{\n{grant_lines}}}\n"
        separator = "" if not permissions_text or permissions_text.endswith("\n") else "\n"
        return permissions_text + separator + addition, True

    block_text = permissions_text[block[0] : block[1]]
    missing = [
        permission
        for permission in normalized_grants
        if re.search(rf"(?m)^\s*{re.escape(permission)}\s*$", block_text) is None
    ]
    if not missing:
        return permissions_text, changed

    addition = "".join(f"    {permission}\n" for permission in missing)
    return _insert_before_block_close(permissions_text, block, addition), True


def _install_cli(options: InstallOptions, result: InstallResult) -> None:
    config_path = options.paths.tab_namer_config_dir / CONFIG_FILENAME
    config = {
        "runtime": "cli",
        "watch_command": build_watch_command(options),
        "verification_command": build_verification_command(options),
        "max_chars": options.max_chars,
        "interval": options.interval,
        "state_file": str(options.paths.state_file),
        "llm": {
            "base_url": options.llm_base_url,
            "model": options.llm_model,
            "api_key_env": API_KEY_ENV,
        },
    }
    result.verification_command = build_verification_command(options)
    if options.dry_run:
        result.runtime_status[MODE_CLI] = "complete"
        result.operations.append(
            Operation(
                action="write cli config",
                target=str(config_path),
                status="planned",
                detail="watcher config only; config.kdl is not changed for CLI mode",
            )
        )
        return

    record = _write_json_with_backup(config_path, config, options.paths.backup_dir)
    if record:
        result.backups.append(record)
        status = "complete"
        detail = "wrote watcher config"
    else:
        status = "unchanged"
        detail = "watcher config already current"
    result.runtime_status[MODE_CLI] = "complete"
    result.operations.append(
        Operation("write cli config", str(config_path), status, detail)
    )


def _install_wasm(options: InstallOptions, result: InstallResult) -> None:
    target = options.paths.plugins_dir / PLUGIN_FILENAME
    if options.wasm_source and options.wasm_url:
        _block_runtime(
            result,
            MODE_WASM,
            "provide either --wasm-source or --wasm-url, not both",
        )
        return
    if not options.wasm_source and not options.wasm_url:
        result.runtime_status[MODE_WASM] = "unavailable"
        result.operations.append(
            Operation(
                action="install wasm plugin",
                target=str(target),
                status="skipped",
                detail="provide --wasm-source or --wasm-url when the plugin artifact exists",
            )
        )
        return

    downloaded: Optional[Path] = None
    try:
        _refuse_symlink_target(target)
        wasm_source = _describe_wasm_source(options)
        changes = _plan_wasm_changes(options, target)
        if options.dry_run:
            result.runtime_status[MODE_WASM] = "complete"
            result.operations.extend(
                [
                    Operation(
                        action="install wasm plugin",
                        target=str(target),
                        status="planned",
                        detail=wasm_source,
                    ),
                    Operation(
                        action="write zellij config",
                        target=str(changes.config_path),
                        status="planned" if changes.config_changed else "unchanged",
                    ),
                    Operation(
                        action="write zellij permissions",
                        target=str(changes.permissions_path),
                        status=(
                            "planned"
                            if changes.permissions_changed
                            else "unchanged"
                        ),
                    ),
                ]
            )
            result.fresh_session_required = True
            return

        resolved_source, downloaded = _resolve_wasm_source(options)
        artifact_record = _copy_with_backup(
            resolved_source,
            target,
            options.paths.backup_dir,
        )
        if artifact_record:
            result.backups.append(artifact_record)

        _assert_planned_text_unchanged(
            changes.config_path,
            changes.original_config,
            "config.kdl",
        )
        config_record = _write_text_with_backup(
            changes.config_path,
            changes.next_config,
            options.paths.backup_dir,
        )
        if config_record:
            result.backups.append(config_record)

        if changes.next_permissions is not None:
            _assert_planned_text_unchanged(
                changes.permissions_path,
                changes.original_permissions,
                "permissions.kdl",
            )
            if (
                _is_user_immutable(changes.permissions_path)
                != changes.permissions_originally_immutable
            ):
                raise InstallError(
                    "permissions.kdl immutable state changed while the WASM install was planned"
                )
            permissions_record, originally_immutable = _write_permissions_change(
                changes.permissions_path,
                changes.next_permissions,
                options.paths.backup_dir,
            )
            should_freeze_permissions = options.freeze_permissions or originally_immutable
            if permissions_record is None:
                permissions_record = _record_permissions_metadata_change(
                    changes.permissions_path,
                    options.paths.backup_dir,
                    originally_immutable,
                    should_freeze_permissions,
                )
            if permissions_record:
                result.backups.append(permissions_record)
            _freeze_permissions_file(
                changes.permissions_path,
                should_freeze_permissions,
            )

            _validate_applied_wasm_changes(
                options,
                target,
                changes,
                should_freeze_permissions,
            )

        result.runtime_status[MODE_WASM] = "complete"
        result.operations.extend(
            [
                Operation("install wasm plugin", str(target), "complete"),
                Operation(
                    "write zellij config",
                    str(changes.config_path),
                    "complete" if changes.config_changed else "unchanged",
                ),
                Operation(
                    "write zellij permissions",
                    str(changes.permissions_path),
                    "complete" if changes.permissions_changed else "unchanged",
                ),
            ]
        )
        result.fresh_session_required = True
    except InstallError as exc:
        _block_runtime(result, MODE_WASM, str(exc))
    except Exception as exc:
        _block_runtime(
            result,
            MODE_WASM,
            f"failed to apply WASM install: {exc}",
        )
    finally:
        if downloaded:
            _unlink_missing_ok(downloaded)


def _plan_wasm_changes(options: InstallOptions, target: Path) -> WasmChanges:
    config_path = options.paths.zellij_config_file
    _refuse_symlink_target(config_path)
    try:
        config_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InstallError(f"config.kdl not found: {config_path}") from exc
    except OSError as exc:
        raise InstallError(f"failed to read config.kdl: {exc}") from exc

    next_config, config_changed = wire_zellij_config(
        config_text,
        target,
        options.max_chars,
        llm_base_url=options.llm_base_url,
        llm_model=options.llm_model,
        no_llm=options.no_llm,
        session_lock_command=options.command_name,
        session_lock_state_file=options.paths.state_file,
    )

    permissions_path = options.paths.permissions_file
    _refuse_symlink_target(permissions_path)
    try:
        permissions_text = permissions_path.read_text(encoding="utf-8")
        original_permissions = permissions_text
    except FileNotFoundError:
        permissions_text = ""
        original_permissions = None
    except OSError as exc:
        raise InstallError(f"failed to read permissions.kdl: {exc}") from exc
    effective_grants = [
        "ReadApplicationState",
        "ChangeApplicationState",
        "RunCommands",
        "Reconfigure",
        "OpenTerminalsOrPlugins",
    ]
    effective_grants.extend(options.wasm_permissions)
    if not options.no_llm and _native_llm_configured(next_config):
        effective_grants.append("WebAccess")
    next_permissions, permissions_changed = update_permission_grants(
        permissions_text,
        target,
        effective_grants,
    )
    return WasmChanges(
        config_path=config_path,
        original_config=config_text,
        next_config=next_config,
        config_changed=config_changed,
        permissions_path=options.paths.permissions_file,
        original_permissions=original_permissions,
        permissions_originally_immutable=_is_user_immutable(permissions_path),
        next_permissions=next_permissions,
        permissions_changed=permissions_changed,
    )


def _describe_wasm_source(options: InstallOptions) -> str:
    _validate_sha256_option(options.wasm_sha256)
    if options.wasm_url:
        _validate_wasm_url(options.wasm_url)
        _validate_wasm_url_hash(options.wasm_sha256)
        return f"download {options.wasm_url}"
    if options.wasm_source:
        source = _normalize_path(options.wasm_source)
        _validate_wasm_file(source, options.wasm_sha256)
        return f"copy {source}"
    raise InstallError("WASM source not provided")


def _validate_applied_wasm_changes(
    options: InstallOptions,
    target: Path,
    changes: WasmChanges,
    permissions_should_be_immutable: bool,
) -> None:
    _validate_wasm_file(target, None)
    try:
        actual_config = changes.config_path.read_text(encoding="utf-8")
        actual_permissions = changes.permissions_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"failed to verify applied WASM configuration: {exc}") from exc
    if actual_config != changes.next_config:
        raise InstallError("config.kdl changed while the WASM install was being verified")
    _, config_changed = wire_zellij_config(
        actual_config,
        target,
        options.max_chars,
        llm_base_url=options.llm_base_url,
        llm_model=options.llm_model,
        no_llm=options.no_llm,
    )
    if config_changed:
        raise InstallError("final config.kdl did not satisfy the managed plugin contract")
    if changes.next_permissions is None or actual_permissions != changes.next_permissions:
        raise InstallError("permissions.kdl changed while the WASM install was being verified")
    if (
        _permissions_freeze_supported()
        and _is_user_immutable(changes.permissions_path)
        != permissions_should_be_immutable
    ):
        raise InstallError("permissions.kdl immutable flag does not match requested state")


def _resolve_wasm_source(options: InstallOptions) -> Tuple[Path, Optional[Path]]:
    _validate_sha256_option(options.wasm_sha256)
    if options.wasm_url:
        downloaded = _download_wasm(options.wasm_url, options.wasm_sha256)
        return downloaded, downloaded
    if options.wasm_source:
        source = _normalize_path(options.wasm_source)
        _validate_wasm_file(source, options.wasm_sha256)
        return source, None
    raise InstallError("WASM source not provided")


def _download_wasm(url: str, expected_sha256: Optional[str]) -> Path:
    _validate_wasm_url(url)
    _validate_wasm_url_hash(expected_sha256)
    fd, temp_name = tempfile.mkstemp(prefix="zellij-tab-namer-", suffix=".wasm")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            with urlopen(url, timeout=URL_TIMEOUT_SECONDS) as response:
                shutil.copyfileobj(response, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_wasm_file(temp_path, expected_sha256)
        return temp_path
    except Exception:
        _unlink_missing_ok(temp_path)
        raise


def _validate_wasm_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise InstallError("WASM URL must be an HTTPS URL")


def _validate_sha256_option(expected_sha256: Optional[str]) -> None:
    if expected_sha256 and _SHA256_RE.fullmatch(expected_sha256) is None:
        raise InstallError("--wasm-sha256 must be a 64-character hex digest")


def _validate_wasm_file(path: Path, expected_sha256: Optional[str]) -> None:
    if not path.exists():
        raise InstallError(f"WASM source not found: {path}")
    if not path.is_file():
        raise InstallError(f"WASM source is not a regular file: {path}")
    with path.open("rb") as handle:
        prefix = handle.read(len(WASM_MAGIC))
    if prefix != WASM_MAGIC:
        raise InstallError(f"WASM source is not a valid WebAssembly module: {path}")
    if expected_sha256:
        actual = _sha256_file(path)
        if actual.lower() != expected_sha256.lower():
            raise InstallError(
                f"WASM SHA-256 mismatch for {path}: expected {expected_sha256}, got {actual}"
            )


def _write_manifest(options: InstallOptions, result: InstallResult) -> None:
    manifest_file = options.paths.manifest_file
    backups = list(result.backups)
    if manifest_file.exists():
        backups.append(_backup_file(manifest_file, options.paths.backup_dir))

    data = {
        "version": 1,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "mode": result.mode,
        "backups": [asdict(record) for record in backups],
        "fresh_session_required": result.fresh_session_required,
        "verification_command": result.verification_command,
    }
    _write_text_atomic(manifest_file, json.dumps(data, indent=2, sort_keys=True) + "\n")
    result.backups = backups
    result.operations.append(
        Operation("write manifest", str(manifest_file), "complete")
    )


def _write_json_with_backup(
    path: Path,
    data: Mapping[str, object],
    backup_dir: Path,
) -> Optional[BackupRecord]:
    return _write_text_with_backup(
        path,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        backup_dir,
    )


def _write_text_with_backup(
    path: Path,
    content: str,
    backup_dir: Path,
) -> Optional[BackupRecord]:
    _refuse_symlink_target(path)
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return None
    record = _backup_file(path, backup_dir) if path.exists() else None
    _write_text_atomic(path, content)
    return BackupRecord(
        target=str(path),
        backup=record.backup if record else None,
        existed=bool(record),
        checksum=_sha256_file(path),
    )


def _assert_planned_text_unchanged(
    path: Path,
    expected: Optional[str],
    label: str,
) -> None:
    _refuse_symlink_target(path)
    try:
        current = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        current = None
    except OSError as exc:
        raise InstallError(f"failed to re-read {label}: {exc}") from exc
    if current != expected:
        raise InstallError(f"{label} changed while the WASM install was planned")


def _write_permissions_change(
    path: Path,
    content: str,
    backup_dir: Path,
) -> Tuple[Optional[BackupRecord], bool]:
    _refuse_symlink_target(path)
    encoded = content.encode("utf-8")
    originally_immutable = _is_user_immutable(path)
    if path.exists() and path.read_bytes() == encoded:
        return None, originally_immutable
    record = _backup_file(path, backup_dir) if path.exists() else None

    if originally_immutable:
        _clear_immutable(path)
    try:
        _write_text_atomic(path, content)
    except Exception:
        if originally_immutable and path.exists():
            _set_immutable(path)
        raise

    return (
        BackupRecord(
            target=str(path),
            backup=record.backup if record else None,
            existed=bool(record),
            checksum=_sha256_file(path),
            immutable=originally_immutable,
        ),
        originally_immutable,
    )


def _freeze_permissions_file(path: Path, freeze: bool) -> None:
    if (
        freeze
        and _permissions_freeze_supported()
        and path.exists()
        and not _is_user_immutable(path)
    ):
        _set_immutable(path)


def _record_permissions_metadata_change(
    path: Path,
    backup_dir: Path,
    originally_immutable: bool,
    freeze_permissions: bool,
) -> Optional[BackupRecord]:
    if (
        not freeze_permissions
        or not _permissions_freeze_supported()
        or not path.exists()
        or _is_user_immutable(path)
    ):
        return None
    record = _backup_file(path, backup_dir)
    return BackupRecord(
        target=str(path),
        backup=record.backup,
        existed=True,
        checksum=_sha256_file(path),
        immutable=originally_immutable,
    )


def _copy_with_backup(
    source: Path,
    target: Path,
    backup_dir: Path,
) -> Optional[BackupRecord]:
    _refuse_symlink_target(target)
    _validate_wasm_file(source, None)
    if target.exists() and _sha256_file(target) == _sha256_file(source):
        return None
    record = _backup_file(target, backup_dir) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle, source.open("rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        _unlink_missing_ok(temp_path)
        raise
    return BackupRecord(
        target=str(target),
        backup=record.backup if record else None,
        existed=bool(record),
        checksum=_sha256_file(target),
    )


def _write_text_atomic(path: Path, content: str) -> None:
    _refuse_symlink_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        _unlink_missing_ok(temp_path)
        raise


def _backup_file(path: Path, backup_dir: Path) -> BackupRecord:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    backup_path = backup_dir / f"{timestamp}-{digest}-{path.name}"
    shutil.copy2(path, backup_path)
    return BackupRecord(target=str(path), backup=str(backup_path), existed=True)


def _rollback_preflight(records: Sequence[object], paths: InstallPaths) -> Optional[str]:
    for record in records:
        if not isinstance(record, dict):
            return "rollback manifest contains an invalid backup entry"
        try:
            target = _normalize_file_target_path(Path(str(record["target"])))
        except KeyError:
            return "rollback manifest contains a backup entry without target"
        if target.exists() and target.is_symlink():
            return f"rollback target is a symlink; refusing to mutate it: {target}"
        if not _target_allowed(target, paths):
            return f"rollback target is outside installer-managed paths: {target}"

        existed = bool(record.get("existed"))
        backup = record.get("backup")
        if existed:
            if not backup:
                return f"rollback entry for {target} is missing its backup"
            backup_path = _normalize_file_target_path(Path(str(backup)))
            if not backup_path.exists() or backup_path.is_symlink():
                return f"rollback backup is unavailable or unsafe: {backup_path}"
            if not _path_under(backup_path, paths.backup_dir):
                return f"rollback backup is outside installer backup dir: {backup_path}"
        elif backup:
            return f"rollback entry for created file should not include backup: {target}"

        checksum = record.get("checksum")
        if checksum and target.exists() and _sha256_file(target) != str(checksum):
            return f"rollback target changed since install: {target}"
    return None


def _ordered_rollback_records(
    records: Sequence[Mapping[str, object]],
    manifest_file: Path,
) -> List[Mapping[str, object]]:
    manifest_path = _normalize_file_target_path(manifest_file)
    reversed_records = list(reversed(records))
    ordinary = [
        record
        for record in reversed_records
        if _normalize_file_target_path(Path(str(record["target"]))) != manifest_path
    ]
    manifest = [
        record
        for record in reversed_records
        if _normalize_file_target_path(Path(str(record["target"]))) == manifest_path
    ]
    return ordinary + manifest


def _rollback_record(
    record: Mapping[str, object],
    paths: InstallPaths,
    result: InstallResult,
    dry_run: bool,
) -> None:
    target = _normalize_file_target_path(Path(str(record["target"])))
    existed = bool(record.get("existed"))
    backup = record.get("backup")
    restore_immutable = _permissions_restore_immutable(target, paths, record)

    if dry_run:
        detail = f"restore from {backup}" if existed else "remove created file"
        result.operations.append(
            Operation("rollback", str(target), "planned", detail)
        )
        return

    try:
        if existed:
            backup_path = _normalize_file_target_path(Path(str(backup)))
            _mutate_maybe_permissions_file(
                target,
                paths,
                lambda: _restore_backup(backup_path, target),
                restore_immutable=restore_immutable,
            )
            result.operations.append(
                Operation("rollback", str(target), "complete", f"restored {backup}")
            )
        else:
            _mutate_maybe_permissions_file(
                target,
                paths,
                lambda: _unlink_missing_ok(target),
                restore_immutable=restore_immutable,
            )
            result.operations.append(
                Operation("rollback", str(target), "complete", "removed created file")
            )
    except (OSError, subprocess.CalledProcessError) as exc:
        result.status = "blocked"
        result.messages.append(f"failed to roll back {target}: {exc}")


def _rollback_applied_records(
    records: Sequence[BackupRecord],
    paths: InstallPaths,
    result: InstallResult,
) -> bool:
    fully_rolled_back = True
    for record in reversed(records):
        target = _normalize_file_target_path(Path(record.target))
        if record.checksum and (
            not target.exists() or _sha256_file(target) != record.checksum
        ):
            result.messages.append(
                f"automatic rollback preserved concurrently changed target: {target}"
            )
            fully_rolled_back = False
            continue
        rollback_result = InstallResult(
            status="complete",
            mode="rollback-partial",
            dry_run=False,
        )
        _rollback_record(asdict(record), paths, rollback_result, dry_run=False)
        result.operations.extend(rollback_result.operations)
        if rollback_result.status == "blocked":
            result.messages.extend(rollback_result.messages)
            fully_rolled_back = False
    return fully_rolled_back


def _rollback_blocked_install(options: InstallOptions, result: InstallResult) -> None:
    if options.dry_run or not result.backups:
        return
    fully_rolled_back = _rollback_applied_records(
        result.backups, options.paths, result
    )
    result.messages.append(
        "blocked install rolled back applied changes"
        if fully_rolled_back
        else "blocked install rolled back non-conflicting changes and preserved divergent targets"
    )


def _restore_backup(backup_path: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle, backup_path.open("rb") as source:
            shutil.copyfileobj(source, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    except Exception:
        _unlink_missing_ok(temp_path)
        raise


def _finish_created_manifest_rollback(
    paths: InstallPaths,
    result: InstallResult,
    dry_run: bool,
) -> None:
    if dry_run:
        result.operations.append(
            Operation(
                "rollback",
                str(paths.manifest_file),
                "planned",
                "remove created manifest",
            )
        )
        return
    _unlink_missing_ok(paths.manifest_file)
    result.operations.append(
        Operation(
            "rollback",
            str(paths.manifest_file),
            "complete",
            "removed created manifest",
        )
    )


def _mutate_maybe_permissions_file(
    target: Path,
    paths: InstallPaths,
    action: Callable[[], None],
    restore_immutable: Optional[bool] = None,
) -> None:
    permissions_file = _normalize_path(paths.permissions_file)
    if target != permissions_file:
        action()
        return
    current_immutable = _is_user_immutable(target)
    if current_immutable:
        _clear_immutable(target)
    try:
        action()
    finally:
        should_be_immutable = (
            current_immutable if restore_immutable is None else restore_immutable
        )
        if should_be_immutable and target.exists() and not _is_user_immutable(target):
            _set_immutable(target)
        elif (
            not should_be_immutable
            and target.exists()
            and _is_user_immutable(target)
        ):
            _clear_immutable(target)


def _manifest_record_present(records: Sequence[object], manifest_file: Path) -> bool:
    manifest_path = _normalize_file_target_path(manifest_file)
    for record in records:
        if not isinstance(record, dict) or "target" not in record:
            continue
        if _normalize_file_target_path(Path(str(record["target"]))) == manifest_path:
            return True
    return False


def _should_write_manifest(result: InstallResult) -> bool:
    return result.status != "blocked" and bool(result.backups)


def _resolve_overall_status(options: InstallOptions, result: InstallResult) -> None:
    if any(status == "blocked" for status in result.runtime_status.values()):
        result.status = "blocked"
        return
    if options.mode == MODE_WASM and result.runtime_status.get(MODE_WASM) != "complete":
        result.status = "blocked"
        if not result.messages:
            result.messages.append("WASM mode requires a plugin artifact")
        return
    result.status = "complete"


def _block_runtime(result: InstallResult, runtime: str, message: str) -> None:
    result.runtime_status[runtime] = "blocked"
    result.status = "blocked"
    result.messages.append(message)
    result.operations.append(
        Operation("configure runtime", runtime, "blocked", message)
    )


def _block_install(result: InstallResult, message: str) -> None:
    result.status = "blocked"
    result.messages.append(message)
    result.operations.append(
        Operation("apply installer", result.mode, "blocked", message)
    )


def _mode_includes(mode: str, runtime: str) -> bool:
    return mode == runtime or mode == MODE_BOTH


def _normalize_options(options: InstallOptions) -> InstallOptions:
    base_url_provided = options.llm_base_url is not None
    model_provided = options.llm_model is not None
    llm_base_url = options.llm_base_url.strip() if base_url_provided else None
    llm_model = options.llm_model.strip() if model_provided else None
    if options.no_llm:
        llm_base_url = None
        llm_model = None
    elif base_url_provided != model_provided:
        raise InstallError("--llm-base-url and --llm-model must be provided together")
    elif base_url_provided:
        if not llm_base_url or not llm_model:
            raise InstallError("--llm-base-url and --llm-model must not be empty")
        native_artifact_requested = options.mode == MODE_WASM or (
            options.mode == MODE_BOTH
            and bool(options.wasm_source or options.wasm_url)
        )
        if native_artifact_requested:
            normalize_llm_base_url(llm_base_url)
    return InstallOptions(
        mode=options.mode,
        dry_run=options.dry_run,
        max_chars=options.max_chars,
        interval=options.interval,
        no_llm=options.no_llm,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        zellij_bin=options.zellij_bin,
        command_name=options.command_name,
        wasm_source=_normalize_path(options.wasm_source) if options.wasm_source else None,
        wasm_url=options.wasm_url,
        wasm_sha256=options.wasm_sha256,
        wasm_permissions=options.wasm_permissions,
        freeze_permissions=options.freeze_permissions,
        paths=_normalize_paths(options.paths),
    )


def _normalize_paths(paths: InstallPaths) -> InstallPaths:
    return InstallPaths(
        zellij_config_dir=_normalize_path(paths.zellij_config_dir),
        zellij_config_file=_normalize_file_target_path(paths.zellij_config_file),
        plugins_dir=_normalize_path(paths.plugins_dir),
        tab_namer_config_dir=_normalize_path(paths.tab_namer_config_dir),
        state_file=_normalize_file_target_path(paths.state_file),
        permissions_file=_normalize_file_target_path(paths.permissions_file),
        backup_dir=_normalize_path(paths.backup_dir),
        manifest_file=_normalize_file_target_path(paths.manifest_file),
    )


def _normalize_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _normalize_file_target_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.parent.resolve(strict=False) / expanded.name


def _plugin_url(path: Path) -> str:
    return f"file:{_normalize_path(path)}"


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


def _default_permissions_file(home: Path) -> Path:
    if platform.system() == "Darwin":
        return _normalize_file_target_path(
            home / "Library/Caches/org.Zellij-Contributors.Zellij/permissions.kdl"
        )
    cache_root = _env_path("XDG_CACHE_HOME", home / ".cache")
    return _normalize_file_target_path(cache_root / "zellij" / "permissions.kdl")


def _default_zellij_config_dir(home: Path, config_root: Path) -> Path:
    zellij_config_env = os.environ.get("ZELLIJ_CONFIG_DIR")
    if zellij_config_env:
        return _normalize_path(Path(zellij_config_env))

    config_dir = config_root / "zellij"
    home_config_dir = _normalize_path(home / ".config" / "zellij")
    if home_config_dir != _normalize_path(config_dir) and home_config_dir.exists():
        return home_config_dir

    if platform.system() == "Darwin" and not (config_dir / "config.kdl").exists():
        return _normalize_path(
            home / "Library/Application Support/org.Zellij-Contributors.Zellij"
        )
    return config_dir


def _default_zellij_config_file(zellij_config_dir: Path) -> Path:
    zellij_config_file_env = os.environ.get("ZELLIJ_CONFIG_FILE")
    if zellij_config_file_env:
        return _normalize_file_target_path(Path(zellij_config_file_env))
    return _normalize_file_target_path(zellij_config_dir / "config.kdl")


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(value)


def _normalize_permissions(grants: Sequence[str]) -> List[str]:
    normalized: List[str] = []
    for grant in grants:
        permission = grant.strip()
        if not permission:
            continue
        if permission not in KNOWN_ZELLIJ_PERMISSIONS:
            raise InstallError(f"unsupported Zellij permission: {permission}")
        if permission not in normalized:
            normalized.append(permission)
    return normalized


def _find_named_block(text: str, name: str) -> Optional[Tuple[int, int]]:
    argument = r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s{}]+)'
    matches = list(
        re.finditer(
            rf"(?m)^[ \t]*{re.escape(name)}(?:[ \t]+{argument})*[ \t]*\{{",
            text,
        )
    )
    if len(matches) > 1:
        raise InstallError(f"config contains multiple {name} blocks; edit manually")
    if not matches:
        return None
    start = matches[0].start()
    open_brace = text.find("{", matches[0].start(), matches[0].end())
    close_brace = _matching_brace(text, open_brace)
    if close_brace is None:
        raise InstallError(f"{name} block is not balanced")
    return start, close_brace + 1


def _reconcile_root_close_behavior(text: str) -> Tuple[str, bool]:
    pattern = re.compile(
        r'(?m)^(?P<indent>[ \t]*)on_force_close[ \t]+"(?:\\.|[^"\\])*"'
        r'(?P<suffix>[ \t]*(?://[^\r\n]*)?)$'
    )
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise InstallError("config contains multiple on_force_close nodes; edit manually")
    if matches:
        match = matches[0]
        replacement = f'{match["indent"]}on_force_close "quit"{match["suffix"]}'
        updated = text[: match.start()] + replacement + text[match.end() :]
        return updated, updated != text
    replacement = 'on_force_close "quit"'
    return replacement + "\n\n" + text, True


def _reconcile_session_lock_binding(text: str) -> Tuple[str, bool]:
    binding = (
        '        bind "l" {\n'
        f'            LaunchPlugin "{PLUGIN_ALIAS}" {{\n'
        '                floating true\n'
        '                move_to_focused_tab true\n'
        '                role "session-lock"\n'
        '            }\n'
        '            SwitchToMode "normal"\n'
        '        }\n'
    )
    keybinds = _find_named_block(text, "keybinds")
    if keybinds is None:
        keybinds_block = "keybinds {\n    session {\n" + binding + "    }\n}\n\n"
        return keybinds_block + text, True

    keybinds_text = text[keybinds[0] : keybinds[1]]
    session_in_keybinds = _find_named_block(keybinds_text, "session")
    if session_in_keybinds is None:
        session_block = "    session {\n" + binding + "    }\n"
        return _insert_before_block_close(text, keybinds, session_block), True

    session = (
        keybinds[0] + session_in_keybinds[0],
        keybinds[0] + session_in_keybinds[1],
    )
    block = text[session[0] : session[1]]
    binding_match = re.search(r'(?m)^\s*bind\s+"(?:\\.|[^"\\])*"(?:\s+"(?:\\.|[^"\\])*")*\s*\{', block)
    if binding_match:
        keys = re.findall(r'"((?:\\.|[^"\\])*)"', binding_match.group())
        if "l" not in keys:
            return _insert_before_block_close(text, session, binding), True
        open_brace = block.find("{", binding_match.start(), binding_match.end())
        close_brace = _matching_brace(block, open_brace)
        if close_brace is None:
            raise InstallError("session l binding is not balanced")
        existing_binding = block[binding_match.start() : close_brace + 1]
        if PLUGIN_ALIAS not in existing_binding:
            raise InstallError("session mode already binds l; refusing to replace it")
        return text, False
    return _insert_before_block_close(text, session, binding), True


def _find_plugin_alias_block(
    text: str,
    plugins_block: Tuple[int, int],
) -> Optional[Tuple[int, int]]:
    block_text = text[plugins_block[0] : plugins_block[1]]
    matches = list(
        re.finditer(rf"(?m)^\s*{re.escape(PLUGIN_ALIAS)}\s+location=", block_text)
    )
    if len(matches) > 1:
        raise InstallError("config contains multiple zellij-tab-namer aliases")
    if not matches:
        return None
    line_start = plugins_block[0] + matches[0].start()
    line_end = text.find("\n", line_start)
    if line_end == -1:
        line_end = len(text)
    opening = text.find("{", line_start, line_end)
    if opening == -1:
        raise InstallError("existing zellij-tab-namer alias must be a block")
    closing = _matching_brace(text, opening)
    if closing is None:
        raise InstallError("existing zellij-tab-namer alias is not balanced")
    return line_start, closing + 1


def _find_exact_quoted_block(text: str, quoted_key: str) -> Optional[Tuple[int, int]]:
    pattern = re.compile(rf"(?m)^\s*{re.escape(quoted_key)}\s*\{{")
    matches = list(pattern.finditer(text))
    if len(matches) > 1:
        raise InstallError(f"permissions contain multiple entries for {quoted_key}")
    if not matches:
        return None
    start = matches[0].start()
    open_brace = text.find("{", matches[0].start(), matches[0].end())
    close_brace = _matching_brace(text, open_brace)
    if close_brace is None:
        raise InstallError(f"permissions entry is not balanced: {quoted_key}")
    return start, close_brace + 1


def _insert_before_block_close(
    text: str,
    block: Tuple[int, int],
    addition: str,
) -> str:
    closing = block[1] - 1
    prefix = text[:closing]
    suffix = text[closing:]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + addition + suffix


def _reconcile_alias_location(alias_text: str, plugin_url: str) -> str:
    pattern = re.compile(
        r'(?m)^(\s*'
        + re.escape(PLUGIN_ALIAS)
        + r'\s+[^\n]*?\blocation=)"(?:\\.|[^"\\])*"'
    )
    matches = list(pattern.finditer(alias_text))
    if len(matches) != 1:
        raise InstallError("existing zellij-tab-namer alias has an unsupported location")
    return pattern.sub(r"\g<1>" + _kdl_string(plugin_url), alias_text, count=1)


def _managed_node_pattern(node: str) -> re.Pattern[str]:
    return re.compile(
        rf'(?m)^(?P<indent>[ \t]*){re.escape(node)}[ \t]+'
        r'(?P<value>"(?:\\.|[^"\\])*"|[^\s/]+)'
        r'(?P<suffix>[ \t]*(?://[^\n]*)?)(?P<newline>\n|$)'
    )


def _reconcile_managed_node(alias_text: str, node: str, value: str) -> str:
    pattern = _managed_node_pattern(node)
    matches = list(pattern.finditer(alias_text))
    if len(matches) > 1:
        raise InstallError(f"plugin alias contains multiple {node} nodes")
    if matches:
        match = matches[0]
        replacement = (
            f"{match.group('indent')}{node} {value}"
            f"{match.group('suffix')}{match.group('newline')}"
        )
        return alias_text[: match.start()] + replacement + alias_text[match.end() :]
    return _insert_before_block_close(
        alias_text,
        (0, len(alias_text)),
        f"        {node} {value}\n",
    )


def _remove_managed_node(alias_text: str, node: str) -> str:
    pattern = _managed_node_pattern(node)
    matches = list(pattern.finditer(alias_text))
    if len(matches) > 1:
        raise InstallError(f"plugin alias contains multiple {node} nodes")
    return pattern.sub("", alias_text, count=1)


def _native_llm_configured(config_text: str) -> bool:
    plugins_block = _find_named_block(config_text, "plugins")
    if plugins_block is None:
        return False
    alias_block = _find_plugin_alias_block(config_text, plugins_block)
    if alias_block is None:
        return False
    alias_text = config_text[alias_block[0] : alias_block[1]]
    base_matches = list(_managed_node_pattern("llm_base_url").finditer(alias_text))
    model_matches = list(_managed_node_pattern("llm_model").finditer(alias_text))
    if len(base_matches) != 1 or len(model_matches) != 1:
        return False
    try:
        base_url = json.loads(base_matches[0].group("value"))
        model = json.loads(model_matches[0].group("value"))
        normalize_llm_base_url(base_url)
    except (InstallError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(model, str) and bool(model.strip())


def _kdl_node_has_exact_value(text: str, node: str, value: str) -> bool:
    return (
        re.search(
            rf"(?m)^\s*{re.escape(node)}\s+{re.escape(value)}\s*$",
            text,
        )
        is not None
    )


def _kdl_bare_node_present(text: str, node: str) -> bool:
    return (
        re.search(
            rf"(?m)^\s*{re.escape(node)}(?:\s+//.*)?\s*$",
            text,
        )
        is not None
    )


def _matching_brace(text: str, open_index: int) -> Optional[int]:
    if open_index < 0 or open_index >= len(text) or text[open_index] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    in_line_comment = False
    index = open_index
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                return None
        index += 1
    return None


def _balanced_kdl(text: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    in_line_comment = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
        index += 1
    return depth == 0 and not in_string


def _kdl_string(value: str) -> str:
    return json.dumps(value)


def _validate_wasm_url_hash(expected_sha256: Optional[str]) -> None:
    if not expected_sha256:
        raise InstallError("--wasm-url requires --wasm-sha256")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_under(path: Path, root: Path) -> bool:
    path = _normalize_path(path)
    root = _normalize_path(root)
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _target_allowed(target: Path, paths: InstallPaths) -> bool:
    target = _normalize_path(target)
    if _path_under(target, paths.backup_dir):
        return False
    roots = [
        paths.zellij_config_dir,
        paths.plugins_dir,
        paths.tab_namer_config_dir,
    ]
    if any(_path_under(target, root) for root in roots):
        return True
    exact = {
        _normalize_path(paths.zellij_config_file),
        _normalize_path(paths.permissions_file),
        _normalize_path(paths.manifest_file),
    }
    return target in exact


def _refuse_symlink_target(path: Path) -> None:
    if path.is_symlink():
        raise InstallError(f"managed install target is a symlink: {path}")


def _permissions_restore_immutable(
    target: Path,
    paths: InstallPaths,
    record: Mapping[str, object],
) -> Optional[bool]:
    if target != _normalize_path(paths.permissions_file) or "immutable" not in record:
        return None
    immutable = record.get("immutable")
    if immutable is None:
        return None
    return bool(immutable)


def _is_user_immutable(path: Path) -> bool:
    if platform.system() != "Darwin" or not path.exists():
        return False
    flags = getattr(path.stat(), "st_flags", 0)
    return bool(flags & getattr(stat, "UF_IMMUTABLE", 0))


def _clear_immutable(path: Path) -> None:
    if platform.system() == "Darwin" and path.exists():
        subprocess.run(["chflags", "nouchg", str(path)], check=True)


def _set_immutable(path: Path) -> None:
    if platform.system() == "Darwin" and path.exists():
        subprocess.run(["chflags", "uchg", str(path)], check=True)


def _permissions_freeze_supported() -> bool:
    return platform.system() == "Darwin"


def _unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
