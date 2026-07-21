import unittest

from zellij_tab_namer.naming import NamerConfig, plan_renames, shorten_label


def pane(
    tab_id=1,
    tab_name="Tab #1",
    title="",
    command="/opt/homebrew/bin/fish",
    cwd="/Users/eduardoleal/Turno/platform-hub",
    focused=True,
    floating=False,
    plugin=False,
    exited=False,
):
    return {
        "tab_id": tab_id,
        "tab_name": tab_name,
        "title": title,
        "pane_command": command,
        "pane_cwd": cwd,
        "is_focused": focused,
        "is_floating": floating,
        "is_plugin": plugin,
        "exited": exited,
    }


class NamingPolicyTests(unittest.TestCase):
    def test_uses_useful_pane_title_instead_of_default_tab_name(self):
        decisions, state = plan_renames(
            [
                pane(
                    title="* Implement B3 slice 4 specifications",
                    command="claude --resume",
                )
            ],
            state={},
            config=NamerConfig(max_chars=40),
        )

        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0].skipped)
        self.assertEqual(decisions[0].candidate, "Implement B3 slice 4 specifications")
        self.assertEqual(state["generated"]["1"], "Implement B3 slice 4 specifications")

    def test_prefers_task_title_over_floating_shell_title(self):
        decisions, _ = plan_renames(
            [
                pane(title="~/T/backstage", command="/opt/homebrew/bin/fish", floating=True),
                pane(
                    title="Plan sprint planning meeting approach",
                    command="codex resume --yolo",
                    focused=False,
                    floating=False,
                ),
            ],
            state={},
            config=NamerConfig(max_chars=40),
        )

        self.assertEqual(decisions[0].candidate, "Plan sprint planning meeting approach")

    def test_shell_tab_falls_back_to_project_and_activity(self):
        decisions, _ = plan_renames(
            [
                pane(
                    title="fish",
                    command="/opt/homebrew/bin/fish",
                    cwd="/Users/eduardoleal/.config/zellij",
                )
            ],
            state={},
            config=NamerConfig(max_chars=40),
        )

        self.assertEqual(decisions[0].candidate, "zellij - fish")
        self.assertEqual(decisions[0].reason, "fallback")

    def test_local_shortening_keeps_long_title_useful(self):
        label = shorten_label("create monorepo for skills and agents", max_chars=24)

        self.assertEqual(label, "create monorepo for...")
        self.assertLessEqual(len(label), 24)

    def test_manual_override_is_skipped(self):
        decisions, state = plan_renames(
            [pane(tab_name="prod deploy", title="Implement release cleanup")],
            state={"generated": {"1": "old generated"}},
            config=NamerConfig(max_chars=32),
        )

        self.assertTrue(decisions[0].skipped)
        self.assertEqual(decisions[0].skip_reason, "manual_override")
        self.assertEqual(state["generated"]["1"], "old generated")

    def test_force_bypasses_manual_override(self):
        decisions, state = plan_renames(
            [pane(tab_name="prod deploy", title="Implement release cleanup")],
            state={"generated": {"1": "old generated"}},
            config=NamerConfig(max_chars=32),
            force=True,
        )

        self.assertFalse(decisions[0].skipped)
        self.assertEqual(decisions[0].candidate, "Implement release cleanup")
        self.assertEqual(state["generated"]["1"], "Implement release cleanup")

    def test_current_generated_name_is_noop(self):
        decisions, state = plan_renames(
            [pane(tab_name="Plan sprint", title="Plan sprint")],
            state={"generated": {"1": "Plan sprint"}},
            config=NamerConfig(max_chars=32),
        )

        self.assertTrue(decisions[0].skipped)
        self.assertEqual(decisions[0].skip_reason, "already_named")
        self.assertEqual(state["generated"]["1"], "Plan sprint")

    def test_ignores_plugin_and_exited_panes(self):
        decisions, _ = plan_renames(
            [
                pane(title="zjstatus", command="zjstatus", plugin=True),
                pane(title="old task", command="claude", exited=True),
                pane(title="", command="nvim", cwd="/Users/eduardoleal/Turno/backstage"),
            ],
            state={},
            config=NamerConfig(max_chars=32),
        )

        self.assertEqual(decisions[0].candidate, "backstage - nvim")


if __name__ == "__main__":
    unittest.main()
