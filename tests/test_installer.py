import json
from pathlib import Path
import tempfile
import unittest

from zellij_tab_namer.installer import (
    InstallError,
    InstallOptions,
    InstallPaths,
    install,
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
        plugins_dir=config_dir / "plugins",
        tab_namer_config_dir=tab_namer_dir,
        state_file=root_path / "state" / "state.json",
        permissions_file=root_path / "cache" / "permissions.kdl",
        backup_dir=tab_namer_dir / "backups",
        manifest_file=tab_namer_dir / "install-manifest.json",
    )


class InstallerTests(unittest.TestCase):
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
            self.assertIn(str(paths.plugins_dir / "zellij-tab-namer.wasm"), permissions)
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

    def test_permission_grants_escape_quoted_paths(self):
        updated, changed = update_permission_grants(
            "",
            Path('/tmp/plugins/zellij"tab.wasm'),
            ["ReadApplicationState"],
        )

        self.assertTrue(changed)
        normalized_path = str(Path('/tmp/plugins/zellij"tab.wasm').resolve(strict=False))
        self.assertIn(json.dumps(normalized_path), updated)

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
