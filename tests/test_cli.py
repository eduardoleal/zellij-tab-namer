import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from zellij_tab_namer.cli import run
from zellij_tab_namer.installer import InstallResult


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class FakeRunner:
    def __init__(self, panes=None, list_result=None, rename_result=None):
        self.panes = panes if panes is not None else []
        self.list_result = list_result
        self.rename_result = rename_result
        self.calls = []
        self.renames = []

    def __call__(self, args):
        self.calls.append(args)
        if args[:3] == ["zellij", "action", "list-panes"]:
            if self.list_result is not None:
                return self.list_result
            return completed(stdout=json.dumps(self.panes))
        if args[:3] == ["zellij", "action", "rename-tab-by-id"]:
            self.renames.append(args)
            if self.rename_result is not None:
                return self.rename_result
            return completed()
        raise AssertionError(f"unexpected command: {args}")


class CLITests(unittest.TestCase):
    def test_session_lock_mark_query_and_unmark_preserve_generated_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({"generated": {"1": "Build"}}, handle)

            for operation, expected, changed in (
                ("mark", True, True),
                ("query", True, None),
                ("mark", True, False),
                ("unmark", False, True),
                ("unmark", False, False),
            ):
                stdout = io.StringIO()
                self.assertEqual(
                    run(["session-lock", operation, "--state-file", state_file, "platform-review"], stdout=stdout),
                    0,
                )
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["locked"], expected)
                if changed is None:
                    self.assertNotIn("changed", result)
                else:
                    self.assertEqual(result["changed"], changed)

            with open(state_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["generated"], {"1": "Build"})

    def test_session_lock_rejects_unsafe_name_without_writing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            stdout = io.StringIO()
            self.assertEqual(
                run(["session-lock", "mark", "--state-file", state_file, "bad\nname"], stdout=stdout),
                2,
            )
            self.assertEqual(json.loads(stdout.getvalue()), {"error": "invalid session name"})
            self.assertFalse(os.path.exists(state_file))

    def test_session_lock_refuses_malformed_state_without_overwriting_it(self):
        malformed_states = ("[]", '{"generated": []}', '{"session_locks": []}')
        for original in malformed_states:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as tempdir:
                state_file = os.path.join(tempdir, "state.json")
                with open(state_file, "w", encoding="utf-8") as handle:
                    handle.write(original)
                stdout = io.StringIO()
                self.assertEqual(
                    run(
                        [
                            "session-lock",
                            "mark",
                            "--state-file",
                            state_file,
                            "work",
                        ],
                        stdout=stdout,
                    ),
                    1,
                )
                self.assertIn(
                    "must contain a JSON object",
                    json.loads(stdout.getvalue())["error"],
                )
                with open(state_file, encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), original)

    def test_install_propagates_native_llm_tri_state_and_dry_run(self):
        cases = (
            ([], None, None, None),
            (
                [
                    "--llm-base-url",
                    "http://localhost:11434/v1",
                    "--llm-model",
                    "llama3.2",
                ],
                None,
                "http://localhost:11434/v1",
                "llama3.2",
            ),
            (["--no-llm"], True, None, None),
            (
                [
                    "--no-llm",
                    "--llm-base-url",
                    "http://localhost:11434/v1",
                    "--llm-model",
                    "ignored",
                ],
                True,
                "http://localhost:11434/v1",
                "ignored",
            ),
        )
        for extra, expected_no_llm, expected_url, expected_model in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tempdir:
                captured = []

                def fake_install(options):
                    captured.append(options)
                    return InstallResult(status="complete", mode="wasm", dry_run=True)

                with patch("zellij_tab_namer.cli.install", side_effect=fake_install):
                    code = run(
                        [
                            "install",
                            "--mode",
                            "wasm",
                            "--dry-run",
                            "--zellij-config-dir",
                            os.path.join(tempdir, "zellij"),
                            *extra,
                        ],
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

                self.assertEqual(code, 0)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].no_llm, expected_no_llm)
                self.assertEqual(captured[0].llm_base_url, expected_url)
                self.assertEqual(captured[0].llm_model, expected_model)
                self.assertTrue(captured[0].dry_run)

    def test_dry_run_prints_planned_renames_without_applying_or_saving_state(self):
        runner = FakeRunner(
            panes=[
                {
                    "tab_id": 1,
                    "tab_name": "Tab #1",
                    "title": "Implement B3 slice 4 specifications",
                    "pane_command": "claude --resume",
                    "pane_cwd": "/Users/eduardoleal/Turno/backstage",
                    "is_focused": True,
                    "is_floating": False,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = run(
                ["once", "--dry-run", "--state-file", state_file, "--max-chars", "40"],
                command_runner=runner,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0)
            self.assertIn(
                "dry-run rename tab 1: Tab #1 -> Implement B3 slice 4 specifications",
                stdout.getvalue(),
            )
            self.assertEqual(runner.renames, [])
            self.assertFalse(os.path.exists(state_file))
            self.assertEqual(stderr.getvalue(), "")

    def test_once_applies_renames_and_saves_generated_state(self):
        runner = FakeRunner(
            panes=[
                {
                    "tab_id": 1,
                    "tab_name": "Tab #1",
                    "title": "Implement B3 slice 4 specifications",
                    "pane_command": "claude --resume",
                    "pane_cwd": "/Users/eduardoleal/Turno/backstage",
                    "is_focused": True,
                    "is_floating": False,
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            stdout = io.StringIO()

            code = run(
                ["once", "--state-file", state_file, "--max-chars", "40"],
                command_runner=runner,
                stdout=stdout,
                stderr=io.StringIO(),
            )

            self.assertEqual(code, 0)
            self.assertEqual(
                runner.renames,
                [
                    [
                        "zellij",
                        "action",
                        "rename-tab-by-id",
                        "1",
                        "Implement B3 slice 4 specifications",
                    ]
                ],
            )
            with open(state_file, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(
                state["generated"]["1"], "Implement B3 slice 4 specifications"
            )
            self.assertIn("renamed tab 1", stdout.getvalue())

    def test_once_preserves_session_locks_when_saving_generated_state(self):
        runner = FakeRunner(panes=[{"tab_id": 1, "tab_name": "Tab #1", "title": "Build", "pane_command": "fish", "pane_cwd": "/tmp", "is_focused": True, "is_floating": False}])
        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({"generated": {}, "session_locks": {"platform": True}}, handle)
            self.assertEqual(run(["once", "--state-file", state_file], command_runner=runner, stdout=io.StringIO(), stderr=io.StringIO()), 0)
            with open(state_file, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["session_locks"], {"platform": True})

    def test_invalid_json_returns_failure(self):
        runner = FakeRunner(list_result=completed(stdout="{not-json"))
        stderr = io.StringIO()

        code = run(
            ["once", "--dry-run"],
            command_runner=runner,
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertEqual(code, 1)
        self.assertIn("failed to parse zellij pane json", stderr.getvalue().lower())

    def test_command_failure_returns_failure(self):
        runner = FakeRunner(list_result=completed(stderr="zellij unavailable", returncode=2))
        stderr = io.StringIO()

        code = run(
            ["once", "--dry-run"],
            command_runner=runner,
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertEqual(code, 2)
        self.assertIn("zellij unavailable", stderr.getvalue())

    def test_rename_failure_returns_failure_without_saving_state(self):
        runner = FakeRunner(
            panes=[
                {
                    "tab_id": 1,
                    "tab_name": "Tab #1",
                    "title": "Implement release cleanup",
                    "pane_command": "claude",
                    "pane_cwd": "/Users/eduardoleal/Turno/backstage",
                    "is_focused": True,
                    "is_floating": False,
                }
            ],
            rename_result=completed(stderr="permission denied", returncode=3),
        )

        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            stderr = io.StringIO()

            code = run(
                ["once", "--state-file", state_file],
                command_runner=runner,
                stdout=io.StringIO(),
                stderr=stderr,
            )

            self.assertEqual(code, 3)
            self.assertIn("permission denied", stderr.getvalue())
            self.assertFalse(os.path.exists(state_file))

    def test_force_bypasses_manual_override(self):
        panes = [
            {
                "tab_id": 1,
                "tab_name": "prod deploy",
                "title": "Implement release cleanup",
                "pane_command": "claude",
                "pane_cwd": "/Users/eduardoleal/Turno/backstage",
                "is_focused": True,
                "is_floating": False,
            }
        ]

        with tempfile.TemporaryDirectory() as tempdir:
            state_file = os.path.join(tempdir, "state.json")
            with open(state_file, "w", encoding="utf-8") as handle:
                json.dump({"generated": {"1": "old generated"}}, handle)

            runner_without_force = FakeRunner(panes=panes)
            code = run(
                ["once", "--state-file", state_file],
                command_runner=runner_without_force,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            self.assertEqual(runner_without_force.renames, [])

            runner_with_force = FakeRunner(panes=panes)
            code = run(
                ["once", "--state-file", state_file, "--force"],
                command_runner=runner_with_force,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)
            self.assertEqual(len(runner_with_force.renames), 1)
            self.assertEqual(runner_with_force.renames[0][3:], ["1", "Implement release cleanup"])

    def test_install_both_dry_run_reports_cli_complete_and_wasm_unavailable(self):
        with tempfile.TemporaryDirectory() as tempdir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = run(
                [
                    "install",
                    "--mode",
                    "both",
                    "--dry-run",
                    "--zellij-config-dir",
                    os.path.join(tempdir, "zellij"),
                    "--tab-namer-config-dir",
                    os.path.join(tempdir, "zellij-tab-namer"),
                    "--permissions-file",
                    os.path.join(tempdir, "cache", "permissions.kdl"),
                    "--manifest-file",
                    os.path.join(tempdir, "zellij-tab-namer", "manifest.json"),
                    "--state-file",
                    os.path.join(tempdir, "state", "state.json"),
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0)
            self.assertIn("both status: complete", stdout.getvalue())
            self.assertIn("cli: complete", stdout.getvalue())
            self.assertIn("wasm: unavailable", stdout.getvalue())
            self.assertIn("verify: zellij-tab-namer once --dry-run", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")
            self.assertFalse(os.path.exists(os.path.join(tempdir, "zellij-tab-namer")))

    def test_install_wasm_without_artifact_returns_blocked(self):
        with tempfile.TemporaryDirectory() as tempdir:
            stdout = io.StringIO()
            stderr = io.StringIO()
            code = run(
                [
                    "install",
                    "--mode",
                    "wasm",
                    "--dry-run",
                    "--zellij-config-dir",
                    os.path.join(tempdir, "zellij"),
                    "--tab-namer-config-dir",
                    os.path.join(tempdir, "zellij-tab-namer"),
                    "--permissions-file",
                    os.path.join(tempdir, "cache", "permissions.kdl"),
                    "--manifest-file",
                    os.path.join(tempdir, "zellij-tab-namer", "manifest.json"),
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("wasm status: blocked", stderr.getvalue())
            self.assertIn("wasm: unavailable", stderr.getvalue())

    def test_install_wasm_uses_explicit_zellij_config_file(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_file = os.path.join(tempdir, "runtime", "zellij.kdl")
            os.makedirs(os.path.dirname(config_file))
            with open(config_file, "w", encoding="utf-8") as handle:
                handle.write("plugins {\n}\n\nload_plugins {\n}\n")
            wasm_source = os.path.join(tempdir, "source.wasm")
            with open(wasm_source, "wb") as handle:
                handle.write(b"\0asmfixture")
            stdout = io.StringIO()
            stderr = io.StringIO()

            code = run(
                [
                    "install",
                    "--mode",
                    "wasm",
                    "--dry-run",
                    "--zellij-config-dir",
                    os.path.join(tempdir, "zellij"),
                    "--zellij-config-file",
                    config_file,
                    "--wasm-source",
                    wasm_source,
                    "--tab-namer-config-dir",
                    os.path.join(tempdir, "zellij-tab-namer"),
                    "--permissions-file",
                    os.path.join(tempdir, "cache", "permissions.kdl"),
                    "--manifest-file",
                    os.path.join(tempdir, "zellij-tab-namer", "manifest.json"),
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0)
            self.assertIn(
                f"write zellij config: {os.path.realpath(config_file)}",
                stdout.getvalue(),
            )
            self.assertEqual(stderr.getvalue(), "")

    def test_rollback_dry_run_reports_manifest_actions(self):
        with tempfile.TemporaryDirectory() as tempdir:
            manifest = os.path.join(tempdir, "zellij-tab-namer", "manifest.json")
            config_dir = os.path.join(tempdir, "zellij-tab-namer")
            code = run(
                [
                    "install",
                    "--mode",
                    "cli",
                    "--tab-namer-config-dir",
                    config_dir,
                    "--manifest-file",
                    manifest,
                    "--state-file",
                    os.path.join(tempdir, "state", "state.json"),
                ],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            self.assertEqual(code, 0)

            stdout = io.StringIO()
            stderr = io.StringIO()
            code = run(
                [
                    "rollback",
                    "--dry-run",
                    "--tab-namer-config-dir",
                    config_dir,
                    "--manifest-file",
                    manifest,
                ],
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(code, 0)
            self.assertIn("rollback status: complete", stdout.getvalue())
            self.assertIn("[planned] rollback:", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
