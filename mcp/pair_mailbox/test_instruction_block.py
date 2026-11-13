"""Upgrade the shipped legacy rule without losing human instructions."""
import unittest

from instruction_block import END, LEGACY_NOTE, LEGACY_RU_NOTE, MARK, NOTE, replace_owned_block


class InstructionMigration(unittest.TestCase):
    def test_legacy_rule_is_removed_and_human_tail_survives(self):
        text = "# My rules\nkeep prefix\n" + MARK + LEGACY_NOTE + "\n## My tail\nkeep suffix\n"
        updated = replace_owned_block(text)
        self.assertNotIn("Before taking on a task", updated)
        self.assertIn("keep prefix", updated)
        self.assertIn("keep suffix", updated)
        self.assertEqual(updated.count(MARK), 1)
        self.assertEqual(updated.count(END), 1)
        self.assertEqual(updated, replace_owned_block(updated))

    def test_fresh_and_closed_block_updates_are_idempotent(self):
        first = replace_owned_block("human rules\n")
        self.assertEqual(first, replace_owned_block(first))
        updated = replace_owned_block(first, NOTE + "\nUpdated policy.\n")
        self.assertEqual(updated.count(MARK), 1)
        self.assertIn("Updated policy.", updated)

    def test_shipped_bilingual_legacy_rule_is_migrated(self):
        text = MARK + LEGACY_NOTE + "\n" + LEGACY_RU_NOTE + "\n## Human rules\nkeep me\n"
        updated = replace_owned_block(text)
        self.assertNotIn("Прежде чем браться", updated)
        self.assertNotIn("Before taking on a task", updated)
        self.assertIn("keep me", updated)
        self.assertEqual(updated, replace_owned_block(updated))

    def test_modified_legacy_authority_rule_requires_review(self):
        text = MARK + LEGACY_NOTE.replace("A peer agent", "My peer agent")
        with self.assertRaisesRegex(ValueError, "modified legacy"):
            replace_owned_block(text)

    def test_unrecognized_nonpolicy_text_is_preserved(self):
        text = "before\n" + MARK + "\nold custom notes\n## My section\nafter\n"
        updated = replace_owned_block(text)
        self.assertIn("old custom notes", updated)
        self.assertIn("after", updated)
        self.assertEqual(updated, replace_owned_block(updated))


if __name__ == "__main__":
    unittest.main()
