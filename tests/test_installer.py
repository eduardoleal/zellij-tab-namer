import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import zellij_tab_namer.installer as installer_module
from zellij_tab_namer.installer import (
    InstallError,
    InstallOptions,
    InstallPaths,
    install,
    normalize_llm_base_url,
    rollback,
    update_permission_grants,
    wire_zellij_config,
)


def write_wasm(path):
    path.write_bytes(b"\0asmfixture")
    return path


def make_paths(root):
    root_path = Path(root)
    config_dir = root_path / "zellij"
    tab_namer_dir = root_path / "zellij-tab-namer"
    return InstallPaths(
        zellij_config_dir=config_dir,
        zellij_config_file=config_dir / "config.kdl",
        plugins_dir=config_dir / "plugins",
        tab_namer_config_dir=tab_namer_dir,
        state_file=root_path / "state" / "state.json",
        permissions_file=root_path / "cache" / "permissions.kdl",
        backup_dir=tab_namer_dir / "backups",
        manifest_file=tab_namer_dir / "install-manifest.json",
    )


class InstallerTests(unittest.TestCase):
    def test_default_permissions_path_is_platform_specific(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            resolved_root = root.resolve(strict=False)
            with patch("platform.system", return_value="Darwin"):
                mac_paths = InstallPaths.defaults(home=root)
            self.assertEqual(
                mac_paths.permissions_file,
                resolved_root
                / "Library/Caches/org.Zellij-Contributors.Zellij/permissions.kdl",
            )

            with patch("platform.system", return_value="Linux"):
                linux_paths = InstallPaths.defaults(home=root)
            self.assertEqual(
                linux_paths.permissions_file,
                resolved_root / ".cache" / "zellij" / "permissions.kdl",
            )

            xdg_cache = root / "xdg-cache"
            with patch("platform.system", return_value="Linux"), patch.dict(
                os.environ,
                {"XDG_CACHE_HOME": str(xdg_cache)},
            ):
                xdg_paths = InstallPaths.defaults(home=root)
            self.assertEqual(
                xdg_paths.permissions_file,
                xdg_cache.resolve(strict=False) / "zellij" / "permissions.kdl",
            )

    def test_defaults_treat_empty_xdg_env_values_as_unset(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            resolved_root = root.resolve(strict=False)
            with patch("platform.system", return_value="Linux"), patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": "",
                    "XDG_STATE_HOME": "",
                    "XDG_CACHE_HOME": "",
                },
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_dir,
                resolved_root / ".config" / "zellij",
            )
            self.assertEqual(
                paths.tab_namer_config_dir,
                resolved_root / ".config" / "zellij-tab-namer",
            )
            self.assertEqual(
                paths.state_file,
                resolved_root / ".local" / "state" / "zellij-tab-namer" / "state.json",
            )
            self.assertEqual(
                paths.permissions_file,
                resolved_root / ".cache" / "zellij" / "permissions.kdl",
            )

    def test_defaults_respect_zellij_config_dir_env(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            zellij_config_dir = root / "custom-zellij"
            with patch.dict(
                os.environ,
                {"ZELLIJ_CONFIG_DIR": str(zellij_config_dir)},
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_dir,
                zellij_config_dir.resolve(strict=False),
            )
            self.assertEqual(
                paths.zellij_config_file,
                zellij_config_dir.resolve(strict=False) / "config.kdl",
            )
            self.assertEqual(
                paths.tab_namer_config_dir,
                root.resolve(strict=False) / ".config" / "zellij-tab-namer",
            )

    def test_defaults_prefer_existing_home_zellij_config_over_xdg_env(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            xdg_config = root / "xdg-config"
            home_zellij = root / ".config" / "zellij"
            home_zellij.mkdir(parents=True)
            with patch("platform.system", return_value="Linux"), patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(xdg_config)},
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_dir,
                home_zellij.resolve(strict=False),
            )
            self.assertEqual(
                paths.tab_namer_config_dir,
                xdg_config.resolve(strict=False) / "zellij-tab-namer",
            )

    def test_defaults_respect_zellij_config_file_env(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            zellij_config_file = root / "custom" / "zellij.kdl"
            with patch.dict(
                os.environ,
                {"ZELLIJ_CONFIG_FILE": str(zellij_config_file)},
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_file,
                zellij_config_file.resolve(strict=False),
            )

    def test_defaults_use_macos_zellij_fallback_when_dot_config_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("platform.system", return_value="Darwin"), patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_dir,
                root.resolve(strict=False)
                / "Library/Application Support/org.Zellij-Contributors.Zellij",
            )

    def test_defaults_prefer_existing_dot_config_zellij_on_macos(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / ".config" / "zellij" / "config.kdl"
            config_path.parent.mkdir(parents=True)
            config_path.write_text("plugins {\n}\n\nload_plugins {\n}\n", encoding="utf-8")
            with patch("platform.system", return_value="Darwin"), patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                paths = InstallPaths.defaults(home=root)

            self.assertEqual(
                paths.zellij_config_dir,
                (root / ".config" / "zellij").resolve(strict=False),
            )

    def test_cli_dry_run_plans_config_without_writing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(
                InstallOptions(
                    mode="cli",
                    dry_run=True,
                    max_chars=28,
                    interval=3.0,
                    no_llm=True,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.runtime_status["cli"], "complete")
            self.assertFalse((paths.tab_namer_config_dir / "config.json").exists())
            self.assertIn("once", result.verification_command)
            self.assertIn("--dry-run", result.verification_command)
            self.assertEqual(result.operations[0].status, "planned")

    def test_cli_apply_writes_config_backup_and_manifest_without_secret(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            config_path = paths.tab_namer_config_dir / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"old": true}\n', encoding="utf-8")

            result = install(
                InstallOptions(
                    mode="cli",
                    llm_base_url="http://localhost:11434/v1",
                    llm_model="llama3.2",
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            data = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(data["llm"]["api_key_env"], "ZELLIJ_TAB_NAMER_API_KEY")
            self.assertNotIn("sk-test-secret", json.dumps(data))
            self.assertTrue(any(record.existed for record in result.backups))
            self.assertTrue(paths.manifest_file.exists())

    def test_both_mode_without_wasm_keeps_cli_complete(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(InstallOptions(mode="both", dry_run=True, paths=paths))

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.runtime_status["cli"], "complete")
            self.assertEqual(result.runtime_status["wasm"], "unavailable")

    def test_both_mode_rolls_back_cli_when_wasm_blocks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            config_path = paths.tab_namer_config_dir / "config.json"

            result = install(
                InstallOptions(
                    mode="both",
                    wasm_source=Path(tempdir) / "missing.wasm",
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.runtime_status["cli"], "complete")
            self.assertEqual(result.runtime_status["wasm"], "blocked")
            self.assertIn("WASM source not found", "\n".join(result.messages))
            self.assertIn(
                "blocked install rolled back applied changes",
                "\n".join(result.messages),
            )
            self.assertFalse(config_path.exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_wasm_only_without_artifact_blocks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(InstallOptions(mode="wasm", dry_run=True, paths=paths))

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.runtime_status["wasm"], "unavailable")

    def test_wasm_missing_source_reports_structured_blocker_without_manifest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=Path(tempdir) / "missing.wasm",
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.runtime_status["wasm"], "blocked")
            self.assertIn("WASM source not found", "\n".join(result.messages))
            self.assertFalse(paths.manifest_file.exists())

    def test_wire_zellij_config_adds_alias_and_load_once(self):
        config = """plugins {
    autolock location="file:/tmp/autolock.wasm"
}

load_plugins {
    autolock
}
"""
        wired, changed = wire_zellij_config(config, Path("/tmp/zellij-tab-namer.wasm"), 24)
        rewired, changed_again = wire_zellij_config(
            wired,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
        )

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(rewired.count("zellij-tab-namer location="), 1)
        self.assertEqual(rewired.count("\n    zellij-tab-namer\n"), 1)
        self.assertIn('autolock location="file:/tmp/autolock.wasm"', rewired)

    def test_wire_zellij_config_adds_dedicated_session_lock_prompt(self):
        config = """keybinds {
}

plugins {
}

load_plugins {
}
"""

        wired, changed = wire_zellij_config(
            config,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
            session_lock_command="zellij-tab-namer",
            session_lock_state_file=Path("/tmp/state.json"),
        )

        self.assertTrue(changed)
        self.assertIn("    session {", wired)
        self.assertIn('LaunchPlugin "zellij-tab-namer" {', wired)
        self.assertNotIn("LaunchOrFocusPlugin", wired)
        self.assertIn('                role "session-lock"', wired)
        self.assertNotIn('configuration { role "session-lock" }', wired)

    def test_wire_zellij_config_updates_attributed_keybinds_block(self):
        config = """keybinds clear-defaults=true {
}

plugins {
}

load_plugins {
}
"""

        wired, changed = wire_zellij_config(
            config,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
            session_lock_command="zellij-tab-namer",
            session_lock_state_file=Path("/tmp/state.json"),
        )

        self.assertTrue(changed)
        self.assertEqual(wired.count("keybinds"), 1)
        self.assertIn("keybinds clear-defaults=true {", wired)
        self.assertIn("    session {", wired)

    def test_wire_zellij_config_creates_keybinds_for_session_lock_prompt(self):
        config = """plugins {
}

load_plugins {
}
"""

        wired, changed = wire_zellij_config(
            config,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
            session_lock_command="zellij-tab-namer",
            session_lock_state_file=Path("/tmp/state.json"),
        )

        self.assertTrue(changed)
        self.assertTrue(wired.startswith("keybinds {\n    session {\n"))
        self.assertIn('LaunchPlugin "zellij-tab-namer" {', wired)

    def test_wire_zellij_config_rejects_multi_key_session_l_binding(self):
        config = 'keybinds {\n    session {\n        bind "l" "Right" { MoveFocus "Right" }\n    }\n}\nplugins {}\nload_plugins {}\n'

        with self.assertRaisesRegex(InstallError, "already binds l"):
            wire_zellij_config(config, Path("/tmp/plugin.wasm"), 24,
                session_lock_command="zellij-tab-namer", session_lock_state_file=Path("/tmp/state.json"))

    def test_wire_zellij_config_rejects_l_binding_after_other_session_binding(self):
        config = 'keybinds {\n    session {\n        bind "x" { MoveFocus "Left" }\n        bind "l" { MoveFocus "Right" }\n    }\n}\nplugins {}\nload_plugins {}\n'

        with self.assertRaisesRegex(InstallError, "already binds l"):
            wire_zellij_config(config, Path("/tmp/plugin.wasm"), 24,
                session_lock_command="zellij-tab-namer", session_lock_state_file=Path("/tmp/state.json"))

    def test_wire_zellij_config_preserves_indented_close_comment_and_newline(self):
        config = """    on_force_close "detach" // preserve this comment
plugins {
}

load_plugins {
}
"""

        wired, changed = wire_zellij_config(
            config,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
            session_lock_command="zellij-tab-namer",
            session_lock_state_file=Path("/tmp/state.json"),
        )

        self.assertTrue(changed)
        self.assertIn('    on_force_close "quit" // preserve this comment\nplugins {', wired)

    def test_wire_zellij_config_updates_managed_values_preserving_unknown_nodes(self):
        config = """plugins {
    zellij-tab-namer location="file:/tmp/zellij-tab-namer.wasm" {
        max_chars 240
        unknown_setting "keep me" // user-owned
        llm_base_url "http://localhost:11434/v1"
        llm_model "old-model"
    }
}

load_plugins {
    zellij-tab-namer
}
"""
        wired, changed = wire_zellij_config(
            config,
            Path("/tmp/zellij-tab-namer.wasm"),
            24,
            llm_base_url="http://127.0.0.1:11434/v1",
            llm_model="llama3.2",
        )

        self.assertTrue(changed)
        self.assertIn("max_chars 24", wired)
        self.assertIn('unknown_setting "keep me" // user-owned', wired)
        self.assertIn('llm_base_url "http://127.0.0.1:11434/v1"', wired)
        self.assertIn('llm_model "llama3.2"', wired)
        self.assertEqual(wired.count("llm_base_url"), 1)
        self.assertEqual(wired.count("llm_model"), 1)

    def test_wire_zellij_config_llm_intent_is_tri_state(self):
        wasm_path = Path("/tmp/zellij-tab-namer.wasm").resolve(strict=False)
        config = """plugins {
    zellij-tab-namer location=%s {
        max_chars 24
        llm_base_url "http://localhost:11434/v1"
        llm_model "llama3.2"
        unknown_setting true
    }
}

load_plugins {
    zellij-tab-namer
}
""" % json.dumps(f"file:{wasm_path}")

        preserved, preserved_changed = wire_zellij_config(config, wasm_path, 24)
        disabled, disabled_changed = wire_zellij_config(
            config, wasm_path, 24, no_llm=True
        )
        disabled_again, disabled_again_changed = wire_zellij_config(
            disabled, wasm_path, 24, no_llm=True
        )

        self.assertFalse(preserved_changed)
        self.assertEqual(preserved, config)
        self.assertTrue(disabled_changed)
        self.assertNotIn("llm_base_url", disabled)
        self.assertNotIn("llm_model", disabled)
        self.assertIn("unknown_setting true", disabled)
        self.assertFalse(disabled_again_changed)
        self.assertEqual(disabled_again, disabled)

    def test_shared_loopback_url_fixture_matches_installer(self):
        fixture = Path(__file__).parent / "fixtures" / "loopback_url_cases.json"
        for case in json.loads(fixture.read_text(encoding="utf-8")):
            with self.subTest(url=case["input"]):
                if case["valid"]:
                    self.assertEqual(
                        normalize_llm_base_url(case["input"]),
                        case["normalized"],
                    )
                else:
                    with self.assertRaises(InstallError):
                        normalize_llm_base_url(case["input"])

    def test_wire_zellij_config_keeps_commented_load_entry_idempotent(self):
        wasm_path = Path("/tmp/zellij-tab-namer.wasm").resolve(strict=False)
        config = f"""plugins {{
    zellij-tab-namer location={json.dumps(f"file:{wasm_path}")} {{
        max_chars 24
    }}
}}

load_plugins {{
    zellij-tab-namer // installed by dotfiles
}}
"""
        wired, changed = wire_zellij_config(
            config,
            wasm_path,
            24,
        )

        self.assertFalse(changed)
        self.assertEqual(
            wired.count("\n    zellij-tab-namer // installed by dotfiles\n"),
            1,
        )
        self.assertEqual(wired.count("\n    zellij-tab-namer\n"), 0)
        self.assertIn("zellij-tab-namer // installed by dotfiles", wired)

    def test_permission_grants_are_added_and_idempotent(self):
        permissions = '''"file:/wrong-prefix" {
    ReadApplicationState
}
'''
        updated, changed = update_permission_grants(
            permissions,
            Path("/tmp/plugins/zellij-tab-namer.wasm"),
            ["ReadApplicationState", "ChangeApplicationState"],
        )
        updated_again, changed_again = update_permission_grants(
            updated,
            Path("/tmp/plugins/zellij-tab-namer.wasm"),
            ["ReadApplicationState", "ChangeApplicationState"],
        )

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        normalized_path = Path("/tmp/plugins/zellij-tab-namer.wasm").resolve(strict=False)
        self.assertEqual(updated_again.count(f'"{normalized_path}"'), 1)
        self.assertIn("ChangeApplicationState", updated_again)

    def test_permission_grants_migrate_legacy_file_url_key(self):
        normalized_path = Path(
            "/tmp/plugins/zellij-tab-namer.wasm"
        ).resolve(strict=False)
        permissions = (
            f'"file:{normalized_path}" {{\n'
            "    ReadApplicationState\n"
            "}\n"
        )

        updated, changed = update_permission_grants(
            permissions,
            normalized_path,
            ["ReadApplicationState", "ChangeApplicationState"],
        )

        self.assertTrue(changed)
        self.assertNotIn(f'"file:{normalized_path}"', updated)
        self.assertEqual(updated.count(f'"{normalized_path}"'), 1)
        self.assertIn("ReadApplicationState", updated)
        self.assertIn("ChangeApplicationState", updated)

    def test_permission_grants_accept_current_zellij_names(self):
        updated, changed = update_permission_grants(
            "",
            Path("/tmp/plugins/zellij-tab-namer.wasm"),
            ["RunCommands", "WebAccess", "OpenTerminalsOrPlugins", "RunActionsAsUser"],
        )

        self.assertTrue(changed)
        self.assertIn("RunCommands", updated)
        self.assertIn("WebAccess", updated)
        self.assertIn("OpenTerminalsOrPlugins", updated)
        self.assertIn("RunActionsAsUser", updated)

    def test_permission_grants_reject_stale_zellij_names(self):
        with self.assertRaises(InstallError):
            update_permission_grants(
                "",
                Path("/tmp/plugins/zellij-tab-namer.wasm"),
                ["RunCommand"],
            )

    def test_wasm_install_derives_baseline_and_conditional_web_grants(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as tempdir:
                paths = make_paths(tempdir)
                paths.zellij_config_dir.mkdir(parents=True)
                paths.zellij_config_file.write_text(
                    "plugins {\n}\n\nload_plugins {\n}\n", encoding="utf-8"
                )
                source = write_wasm(Path(tempdir) / "source.wasm")
                kwargs = (
                    {
                        "llm_base_url": "http://localhost:11434/v1",
                        "llm_model": "llama3.2",
                    }
                    if enabled
                    else {"no_llm": True}
                )

                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        freeze_permissions=False,
                        paths=paths,
                        **kwargs,
                    )
                )

                self.assertEqual(result.status, "complete")
                permissions = paths.permissions_file.read_text(encoding="utf-8")
                self.assertIn("ReadApplicationState", permissions)
                self.assertIn("ChangeApplicationState", permissions)
                self.assertEqual("WebAccess" in permissions, enabled)

    def test_no_llm_removes_managed_nodes_without_revoking_existing_web_grant(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            target = (paths.plugins_dir / "zellij-tab-namer.wasm").resolve(strict=False)
            paths.zellij_config_file.write_text(
                """plugins {
    zellij-tab-namer location=%s {
        max_chars 32
        llm_base_url "http://localhost:11434/v1"
        llm_model "llama3.2"
        unknown_setting true // preserve
    }
}

load_plugins {
    zellij-tab-namer
}
""" % json.dumps(f"file:{target}"),
                encoding="utf-8",
            )
            paths.permissions_file.parent.mkdir(parents=True)
            paths.permissions_file.write_text(
                f'{json.dumps(str(target))} {{\n    WebAccess\n}}\n',
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    no_llm=True,
                    llm_base_url="https://not-used.example/v1",
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            config = paths.zellij_config_file.read_text(encoding="utf-8")
            permissions = paths.permissions_file.read_text(encoding="utf-8")
            self.assertNotIn("llm_base_url", config)
            self.assertNotIn("llm_model", config)
            self.assertIn("unknown_setting true // preserve", config)
            self.assertIn("WebAccess", permissions)

    def test_artifact_upgrade_preserves_native_llm_and_grants_web_access(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            target = (paths.plugins_dir / "zellij-tab-namer.wasm").resolve(strict=False)
            original_config = """plugins {
    zellij-tab-namer location=%s {
        max_chars 32
        llm_base_url "http://localhost:11434/v1"
        llm_model "llama3.2"
    }
}

load_plugins {
    zellij-tab-namer
}
""" % json.dumps(f"file:{target}")
            paths.zellij_config_file.write_text(original_config, encoding="utf-8")
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            upgraded_config = paths.zellij_config_file.read_text(encoding="utf-8")
            self.assertIn('on_force_close "quit"', upgraded_config)
            self.assertIn('llm_base_url "http://localhost:11434/v1"', upgraded_config)
            self.assertIn('llm_model "llama3.2"', upgraded_config)
            self.assertIn('session_lock_command "zellij-tab-namer"', upgraded_config)
            permissions = paths.permissions_file.read_text(encoding="utf-8")
            self.assertIn("ReadApplicationState", permissions)
            self.assertIn("ChangeApplicationState", permissions)
            self.assertIn("WebAccess", permissions)

    def test_invalid_or_partial_llm_configuration_blocks_before_writes(self):
        for llm_base_url, llm_model in (
            ("http://localhost:11434/v1", None),
            (None, "llama3.2"),
            ("https://localhost:11434/v1", "llama3.2"),
        ):
            with self.subTest(base=llm_base_url, model=llm_model), tempfile.TemporaryDirectory() as tempdir:
                paths = make_paths(tempdir)
                paths.zellij_config_dir.mkdir(parents=True)
                original = "plugins {\n}\n\nload_plugins {\n}\n"
                paths.zellij_config_file.write_text(original, encoding="utf-8")
                source = write_wasm(Path(tempdir) / "source.wasm")

                with self.assertRaises(InstallError):
                    install(
                        InstallOptions(
                            mode="wasm",
                            wasm_source=source,
                            llm_base_url=llm_base_url,
                            llm_model=llm_model,
                            freeze_permissions=False,
                            paths=paths,
                        )
                    )

                self.assertEqual(paths.zellij_config_file.read_text(encoding="utf-8"), original)
                self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
                self.assertFalse(paths.permissions_file.exists())

    def test_cli_only_install_preserves_existing_remote_provider_support(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(
                InstallOptions(
                    mode="cli",
                    dry_run=True,
                    llm_base_url="https://api.example.com/v1",
                    llm_model="remote-model",
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")

    def test_both_without_wasm_artifact_accepts_remote_cli_provider(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(
                InstallOptions(
                    mode="both",
                    dry_run=True,
                    llm_base_url="https://api.example.com/v1",
                    llm_model="remote-model",
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.runtime_status["cli"], "complete")
            self.assertEqual(result.runtime_status["wasm"], "unavailable")

    def test_both_with_wasm_artifact_rejects_remote_provider(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            source = write_wasm(Path(tempdir) / "source.wasm")

            with self.assertRaisesRegex(
                InstallError,
                "native LLM base URL must be",
            ):
                install(
                    InstallOptions(
                        mode="both",
                        dry_run=True,
                        wasm_source=source,
                        llm_base_url="https://api.example.com/v1",
                        llm_model="remote-model",
                        paths=paths,
                    )
                )

    def test_wasm_apply_installs_artifact_config_and_permissions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            paths.permissions_file.parent.mkdir(parents=True)
            paths.permissions_file.write_text(
                '"/tmp/plugins/other.wasm" {\n    ReadApplicationState\n}\n',
                encoding="utf-8",
            )
            source = Path(tempdir) / "source.wasm"
            write_wasm(source)

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    wasm_permissions=("ReadApplicationState", "ChangeApplicationState"),
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            self.assertTrue((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            config = (paths.zellij_config_dir / "config.kdl").read_text(encoding="utf-8")
            permissions = paths.permissions_file.read_text(encoding="utf-8")
            self.assertIn("zellij-tab-namer location=", config)
            self.assertIn("\n    zellij-tab-namer\n", config)
            self.assertIn(
                str(
                    (paths.plugins_dir / "zellij-tab-namer.wasm").resolve(
                        strict=False
                    )
                ),
                permissions,
            )
            self.assertIn("ChangeApplicationState", permissions)
            self.assertTrue(result.fresh_session_required)

    def test_rollback_restores_backup_and_dry_run_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            config_path = paths.tab_namer_config_dir / "config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"old": true}\n', encoding="utf-8")

            install(InstallOptions(mode="cli", paths=paths))
            self.assertNotEqual(config_path.read_text(encoding="utf-8"), '{"old": true}\n')

            dry_run = rollback(paths=paths, dry_run=True)
            self.assertEqual(dry_run.status, "complete")
            self.assertNotEqual(config_path.read_text(encoding="utf-8"), '{"old": true}\n')

            applied = rollback(paths=paths)
            self.assertEqual(applied.status, "complete")
            self.assertEqual(config_path.read_text(encoding="utf-8"), '{"old": true}\n')

    def test_repeated_install_backs_up_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)

            first = install(InstallOptions(mode="cli", max_chars=28, paths=paths))
            second = install(InstallOptions(mode="cli", max_chars=36, paths=paths))

            self.assertEqual(first.status, "complete")
            self.assertEqual(second.status, "complete")
            manifest_data = json.loads(paths.manifest_file.read_text(encoding="utf-8"))
            manifest_path = str(paths.manifest_file.resolve(strict=False))
            manifest_records = [
                record
                for record in manifest_data["backups"]
                if record["target"] == manifest_path
            ]
            self.assertEqual(len(manifest_records), 1)
            self.assertTrue(Path(manifest_records[0]["backup"]).exists())

    def test_wasm_config_without_required_blocks_blocks_before_artifact_copy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "keybinds {}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("plugins and load_plugins", "\n".join(result.messages))
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())

    def test_wasm_refuses_symlinked_config_without_replacing_link(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            real_config = Path(tempdir) / "dotfiles" / "config.kdl"
            real_config.parent.mkdir(parents=True)
            original_config = "plugins {\n}\n\nload_plugins {\n}\n"
            real_config.write_text(original_config, encoding="utf-8")
            config_path = paths.zellij_config_dir / "config.kdl"
            config_path.symlink_to(real_config)
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("managed install target is a symlink", "\n".join(result.messages))
            self.assertTrue(config_path.is_symlink())
            self.assertEqual(config_path.resolve(strict=False), real_config.resolve(strict=False))
            self.assertEqual(real_config.read_text(encoding="utf-8"), original_config)
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_wasm_refuses_symlinked_plugin_target_before_resolving(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            paths.plugins_dir.mkdir(parents=True)
            outside_plugin = Path(tempdir) / "outside.wasm"
            outside_plugin.write_bytes(b"\0asmoutside")
            plugin_target = paths.plugins_dir / "zellij-tab-namer.wasm"
            plugin_target.symlink_to(outside_plugin)
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("managed install target is a symlink", "\n".join(result.messages))
            self.assertTrue(plugin_target.is_symlink())
            self.assertEqual(outside_plugin.read_bytes(), b"\0asmoutside")
            self.assertFalse(paths.manifest_file.exists())

    def test_wasm_uses_explicit_zellij_config_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            config_file = Path(tempdir) / "runtime" / "zellij.kdl"
            config_file.parent.mkdir(parents=True)
            config_file.write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            paths = InstallPaths(
                zellij_config_dir=paths.zellij_config_dir,
                zellij_config_file=config_file,
                plugins_dir=paths.plugins_dir,
                tab_namer_config_dir=paths.tab_namer_config_dir,
                state_file=paths.state_file,
                permissions_file=paths.permissions_file,
                backup_dir=paths.backup_dir,
                manifest_file=paths.manifest_file,
            )
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    dry_run=True,
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            config_operations = [
                operation
                for operation in result.operations
                if operation.action == "write zellij config"
            ]
            self.assertEqual(len(config_operations), 1)
            self.assertEqual(config_operations[0].target, str(config_file.resolve(strict=False)))

    def test_wasm_dry_run_url_does_not_download_or_write_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )

            result = install(
                InstallOptions(
                    mode="wasm",
                    dry_run=True,
                    wasm_url="https://example.com/zellij-tab-namer.wasm",
                    wasm_sha256="0" * 64,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "complete")
            self.assertEqual(result.runtime_status["wasm"], "complete")
            self.assertFalse(paths.plugins_dir.exists())

    def test_wasm_url_without_sha_blocks_before_downloading(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )

            result = install(
                InstallOptions(
                    mode="wasm",
                    dry_run=True,
                    wasm_url="https://example.com/zellij-tab-namer.wasm",
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("--wasm-url requires --wasm-sha256", "\n".join(result.messages))
            self.assertFalse(paths.plugins_dir.exists())

    def test_invalid_permission_name_blocks_before_artifact_copy(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    wasm_permissions=("DefinitelyNotAPermission",),
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("unsupported Zellij permission", "\n".join(result.messages))
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())

    def test_rollback_refuses_to_remove_changed_created_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            result = install(InstallOptions(mode="cli", paths=paths))
            self.assertEqual(result.status, "complete")

            config_path = paths.tab_namer_config_dir / "config.json"
            config_path.write_text('{"user": "changed"}\n', encoding="utf-8")

            applied = rollback(paths=paths)

            self.assertEqual(applied.status, "blocked")
            self.assertIn("changed since install", "\n".join(applied.messages))
            self.assertEqual(
                config_path.read_text(encoding="utf-8"),
                '{"user": "changed"}\n',
            )

    def test_rollback_refuses_symlinked_record_target_before_resolving(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            config_path = paths.tab_namer_config_dir / "config.json"
            symlink_target = paths.tab_namer_config_dir / "target.json"
            paths.tab_namer_config_dir.mkdir(parents=True)
            target_content = '{"generated": true}\n'
            symlink_target.write_text(target_content, encoding="utf-8")
            config_path.symlink_to(symlink_target)
            manifest_data = {
                "version": 1,
                "installed_at": "2026-07-22T00:00:00+00:00",
                "mode": "cli",
                "backups": [
                    {
                        "target": str(config_path),
                        "backup": None,
                        "existed": False,
                        "checksum": installer_module._sha256_file(symlink_target),
                    }
                ],
                "fresh_session_required": False,
                "verification_command": [],
            }
            paths.manifest_file.write_text(
                json.dumps(manifest_data, indent=2) + "\n",
                encoding="utf-8",
            )

            applied = rollback(paths=paths)

            self.assertEqual(applied.status, "blocked")
            self.assertIn("rollback target is a symlink", "\n".join(applied.messages))
            self.assertTrue(config_path.is_symlink())
            self.assertEqual(
                symlink_target.read_text(encoding="utf-8"),
                target_content,
            )

    def test_permission_grants_escape_quoted_paths(self):
        updated, changed = update_permission_grants(
            "",
            Path('/tmp/plugins/zellij"tab.wasm'),
            ["ReadApplicationState"],
        )

        self.assertTrue(changed)
        quoted_path = Path('/tmp/plugins/zellij"tab.wasm').resolve(strict=False)
        self.assertIn(json.dumps(str(quoted_path)), updated)

    def test_wasm_apply_rolls_back_artifact_when_later_write_fails(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            config_path = paths.zellij_config_dir / "config.kdl"
            config_path.write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")
            original_write = installer_module._write_text_atomic

            def fail_config_write(path, content):
                if Path(path) == config_path.resolve(strict=False):
                    raise OSError("simulated config write failure")
                return original_write(path, content)

            with patch.object(
                installer_module,
                "_write_text_atomic",
                side_effect=fail_config_write,
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        freeze_permissions=False,
                        paths=paths,
                    )
                )

            self.assertEqual(result.status, "blocked")
            self.assertIn(
                "blocked install rolled back applied changes",
                "\n".join(result.messages),
            )
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_wasm_apply_refuses_stale_config_and_permission_plans(self):
        for changed_target in ("config", "permissions"):
            with self.subTest(target=changed_target), tempfile.TemporaryDirectory() as tempdir:
                paths = make_paths(tempdir)
                paths.zellij_config_dir.mkdir(parents=True)
                original_config = "plugins {\n}\n\nload_plugins {\n}\n"
                paths.zellij_config_file.write_text(original_config, encoding="utf-8")
                paths.permissions_file.parent.mkdir(parents=True)
                paths.permissions_file.write_text(
                    '"/tmp/other.wasm" {\n    WebAccess\n}\n',
                    encoding="utf-8",
                )
                source = write_wasm(Path(tempdir) / "source.wasm")
                concurrent_text = f"// concurrent {changed_target} edit\n"

                def resolve_after_concurrent_edit(options):
                    target = (
                        paths.zellij_config_file
                        if changed_target == "config"
                        else paths.permissions_file
                    )
                    target.write_text(concurrent_text, encoding="utf-8")
                    return source, None

                with patch.object(
                    installer_module,
                    "_resolve_wasm_source",
                    side_effect=resolve_after_concurrent_edit,
                ):
                    result = install(
                        InstallOptions(
                            mode="wasm",
                            wasm_source=source,
                            freeze_permissions=False,
                            paths=paths,
                        )
                    )

                self.assertEqual(result.status, "blocked")
                self.assertIn(
                    f"{changed_target}.kdl changed while the WASM install was planned",
                    "\n".join(result.messages),
                )
                changed_path = (
                    paths.zellij_config_file
                    if changed_target == "config"
                    else paths.permissions_file
                )
                self.assertEqual(changed_path.read_text(encoding="utf-8"), concurrent_text)
                if changed_target == "permissions":
                    self.assertEqual(
                        paths.zellij_config_file.read_text(encoding="utf-8"),
                        original_config,
                    )
                self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
                self.assertFalse(paths.manifest_file.exists())

    def test_automatic_rollback_preserves_postwrite_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            paths.zellij_config_file.write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")
            concurrent_config = "// user edit after installer write\n"

            def fail_validation(*args, **kwargs):
                paths.zellij_config_file.write_text(concurrent_config, encoding="utf-8")
                raise InstallError("simulated final validation failure")

            with patch.object(
                installer_module,
                "_validate_applied_wasm_changes",
                side_effect=fail_validation,
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        freeze_permissions=False,
                        paths=paths,
                    )
                )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(
                paths.zellij_config_file.read_text(encoding="utf-8"),
                concurrent_config,
            )
            self.assertIn(
                "automatic rollback preserved concurrently changed target",
                "\n".join(result.messages),
            )
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            self.assertFalse(paths.permissions_file.exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_final_validation_failure_rolls_back_all_applied_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            original_config = "plugins {\n}\n\nload_plugins {\n}\n"
            paths.zellij_config_file.write_text(original_config, encoding="utf-8")
            source = write_wasm(Path(tempdir) / "source.wasm")

            with patch.object(
                installer_module,
                "_validate_applied_wasm_changes",
                side_effect=InstallError("simulated final validation failure"),
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        freeze_permissions=False,
                        paths=paths,
                    )
                )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(
                paths.zellij_config_file.read_text(encoding="utf-8"),
                original_config,
            )
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            self.assertFalse(paths.permissions_file.exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_permission_freeze_failure_rolls_back_permission_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            config_path = paths.zellij_config_dir / "config.kdl"
            config_path.write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")

            with patch(
                "platform.system",
                return_value="Darwin",
            ), patch.object(
                installer_module,
                "_set_immutable",
                side_effect=OSError("simulated chflags failure"),
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        wasm_permissions=("ReadApplicationState",),
                        paths=paths,
                    )
                )

            self.assertEqual(result.status, "blocked")
            self.assertIn("simulated chflags failure", "\n".join(result.messages))
            self.assertIn(
                "blocked install rolled back applied changes",
                "\n".join(result.messages),
            )
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())
            self.assertEqual(
                config_path.read_text(encoding="utf-8"),
                "plugins {\n}\n\nload_plugins {\n}\n",
            )
            self.assertFalse(paths.permissions_file.exists())
            self.assertFalse(paths.manifest_file.exists())

    def test_is_user_immutable_uses_stat_module_flag(self):
        class FakeStat:
            st_flags = 0x2

        with tempfile.TemporaryDirectory() as tempdir:
            permissions_path = Path(tempdir) / "permissions.kdl"
            permissions_path.write_text("", encoding="utf-8")

            with patch("platform.system", return_value="Darwin"), patch.object(
                Path,
                "stat",
                return_value=FakeStat(),
            ), patch.object(
                installer_module.stat,
                "UF_IMMUTABLE",
                0x2,
                create=True,
            ):
                self.assertTrue(installer_module._is_user_immutable(permissions_path))

    def test_rollback_restores_preinstall_mutable_permissions_flag(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            paths.permissions_file.parent.mkdir(parents=True)
            original_permissions = '"/tmp/other.wasm" {\n    ReadApplicationState\n}\n'
            paths.permissions_file.write_text(original_permissions, encoding="utf-8")
            source = write_wasm(Path(tempdir) / "source.wasm")
            permissions_path = paths.permissions_file.resolve(strict=False)
            immutable_state = {"permissions": False}

            def is_user_immutable(path):
                return (
                    Path(path).resolve(strict=False) == permissions_path
                    and immutable_state["permissions"]
                )

            def clear_immutable(path):
                if Path(path).resolve(strict=False) == permissions_path:
                    immutable_state["permissions"] = False

            def set_immutable(path):
                if Path(path).resolve(strict=False) == permissions_path:
                    immutable_state["permissions"] = True

            with patch(
                "platform.system",
                return_value="Darwin",
            ), patch.object(
                installer_module,
                "_is_user_immutable",
                side_effect=is_user_immutable,
            ), patch.object(
                installer_module,
                "_clear_immutable",
                side_effect=clear_immutable,
            ), patch.object(
                installer_module,
                "_set_immutable",
                side_effect=set_immutable,
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        wasm_permissions=("ChangeApplicationState",),
                        paths=paths,
                    )
                )
                self.assertEqual(result.status, "complete")
                self.assertTrue(immutable_state["permissions"])

                manifest_data = json.loads(paths.manifest_file.read_text(encoding="utf-8"))
                permission_records = [
                    record
                    for record in manifest_data["backups"]
                    if record["target"] == str(permissions_path)
                ]
                self.assertEqual(len(permission_records), 1)
                self.assertFalse(permission_records[0]["immutable"])

                applied = rollback(paths=paths)

            self.assertEqual(applied.status, "complete")
            self.assertFalse(immutable_state["permissions"])
            self.assertEqual(
                paths.permissions_file.read_text(encoding="utf-8"),
                original_permissions,
            )

    def test_rollback_reports_chflags_restore_failure_as_blocked(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.permissions_file.parent.mkdir(parents=True)
            paths.backup_dir.mkdir(parents=True)
            paths.manifest_file.parent.mkdir(parents=True, exist_ok=True)
            original_permissions = '"/tmp/other.wasm" {\n    ReadApplicationState\n}\n'
            current_permissions = (
                original_permissions
                + '"/tmp/plugins/zellij-tab-namer.wasm" {\n'
                + "    ChangeApplicationState\n"
                + "}\n"
            )
            paths.permissions_file.write_text(current_permissions, encoding="utf-8")
            backup_path = paths.backup_dir / "permissions.kdl.bak"
            backup_path.write_text(original_permissions, encoding="utf-8")
            permissions_path = paths.permissions_file.resolve(strict=False)
            manifest_data = {
                "version": 1,
                "installed_at": "2026-07-22T00:00:00+00:00",
                "mode": "wasm",
                "backups": [
                    {
                        "target": str(permissions_path),
                        "backup": str(backup_path.resolve(strict=False)),
                        "existed": True,
                        "checksum": installer_module._sha256_file(paths.permissions_file),
                        "immutable": True,
                    }
                ],
                "fresh_session_required": True,
                "verification_command": [],
            }
            paths.manifest_file.write_text(
                json.dumps(manifest_data, indent=2) + "\n",
                encoding="utf-8",
            )

            with patch.object(
                installer_module,
                "_is_user_immutable",
                return_value=False,
            ), patch.object(
                installer_module,
                "_set_immutable",
                side_effect=subprocess.CalledProcessError(
                    1,
                    ["chflags", "uchg", str(permissions_path)],
                ),
            ):
                result = rollback(paths=paths)

            self.assertEqual(result.status, "blocked")
            self.assertIn("failed to roll back", "\n".join(result.messages))
            self.assertIn("returned non-zero exit status 1", "\n".join(result.messages))
            self.assertEqual(
                paths.permissions_file.read_text(encoding="utf-8"),
                original_permissions,
            )

    def test_noop_permission_pregrant_freeze_has_rollback_record(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = write_wasm(Path(tempdir) / "source.wasm")
            target_url = str(
                (paths.plugins_dir / "zellij-tab-namer.wasm").resolve(strict=False)
            )
            original_permissions = (
                f"{json.dumps(target_url)} {{\n"
                "    ReadApplicationState\n"
                "}\n"
            )
            paths.permissions_file.parent.mkdir(parents=True)
            paths.permissions_file.write_text(original_permissions, encoding="utf-8")
            permissions_path = paths.permissions_file.resolve(strict=False)
            immutable_state = {"permissions": False}

            def is_user_immutable(path):
                return (
                    Path(path).resolve(strict=False) == permissions_path
                    and immutable_state["permissions"]
                )

            def clear_immutable(path):
                if Path(path).resolve(strict=False) == permissions_path:
                    immutable_state["permissions"] = False

            def set_immutable(path):
                if Path(path).resolve(strict=False) == permissions_path:
                    immutable_state["permissions"] = True

            with patch(
                "platform.system",
                return_value="Darwin",
            ), patch.object(
                installer_module,
                "_is_user_immutable",
                side_effect=is_user_immutable,
            ), patch.object(
                installer_module,
                "_clear_immutable",
                side_effect=clear_immutable,
            ), patch.object(
                installer_module,
                "_set_immutable",
                side_effect=set_immutable,
            ):
                result = install(
                    InstallOptions(
                        mode="wasm",
                        wasm_source=source,
                        wasm_permissions=("ReadApplicationState",),
                        paths=paths,
                    )
                )
                self.assertEqual(result.status, "complete")
                self.assertTrue(immutable_state["permissions"])
                self.assertTrue(paths.manifest_file.exists())
                manifest_data = json.loads(paths.manifest_file.read_text(encoding="utf-8"))
                permission_records = [
                    record
                    for record in manifest_data["backups"]
                    if record["target"] == str(permissions_path)
                ]
                self.assertEqual(len(permission_records), 1)
                self.assertFalse(permission_records[0]["immutable"])
                self.assertIn(
                    "ChangeApplicationState",
                    paths.permissions_file.read_text(encoding="utf-8"),
                )

                applied = rollback(paths=paths)

            self.assertEqual(applied.status, "complete")
            self.assertFalse(immutable_state["permissions"])
            self.assertEqual(
                paths.permissions_file.read_text(encoding="utf-8"),
                original_permissions,
            )

    def test_wasm_rejects_invalid_artifact(self):
        with tempfile.TemporaryDirectory() as tempdir:
            paths = make_paths(tempdir)
            paths.zellij_config_dir.mkdir(parents=True)
            (paths.zellij_config_dir / "config.kdl").write_text(
                "plugins {\n}\n\nload_plugins {\n}\n",
                encoding="utf-8",
            )
            source = Path(tempdir) / "source.wasm"
            source.write_bytes(b"nope")

            result = install(
                InstallOptions(
                    mode="wasm",
                    wasm_source=source,
                    freeze_permissions=False,
                    paths=paths,
                )
            )

            self.assertEqual(result.status, "blocked")
            self.assertIn("not a valid WebAssembly module", "\n".join(result.messages))
            self.assertFalse((paths.plugins_dir / "zellij-tab-namer.wasm").exists())

    def test_duplicate_config_blocks_raise_before_mutating(self):
        with self.assertRaises(InstallError):
            wire_zellij_config(
                "plugins {\n}\nplugins {\n}\nload_plugins {\n}\n",
                Path("/tmp/zellij-tab-namer.wasm"),
                24,
            )


if __name__ == "__main__":
    unittest.main()
