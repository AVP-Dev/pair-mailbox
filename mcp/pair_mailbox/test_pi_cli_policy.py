import unittest

import pi_cli_policy


class PiCliPolicyTests(unittest.TestCase):
    def test_writer_trust_is_forced_and_conflicting_tool_overrides_are_removed(self):
        result = pi_cli_policy.safe_pi_command(
            ["pi", "--no-approve", "--no-tools", "--tools", "read",
             "--exclude-tools=bash", "--model", "provider/model", "task"],
            is_writer=True)

        self.assertEqual(result, ["pi", "--model", "provider/model", "task", "--approve"])

    def test_critic_disables_builtin_file_tools_but_keeps_other_args(self):
        result = pi_cli_policy.safe_pi_command(
            ["pi", "--approve", "--tools", "read,write", "--exclude-tools", "bash",
             "--resume", "thread"], is_writer=False)

        self.assertEqual(result, ["pi", "--resume", "thread", "--approve",
                                  "--exclude-tools", "edit,write"])

    def test_incomplete_tool_override_is_rejected(self):
        for option in ("--tools", "-t", "--exclude-tools", "-xt"):
            with self.subTest(option=option):
                with self.assertRaisesRegex(ValueError, "needs a value"):
                    pi_cli_policy.safe_pi_command(["pi", option], is_writer=True)


if __name__ == "__main__":
    unittest.main()
