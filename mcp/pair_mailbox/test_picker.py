from __future__ import annotations

import unittest

from picker import pair_row_state, pair_row_visible, picker_open


class PickerDetectionTests(unittest.TestCase):
    def test_picker_chrome_and_filtered_row(self) -> None:
        text = "MCP servers\n› pair"
        self.assertTrue(picker_open(text))
        self.assertTrue(pair_row_visible(text))

    def test_negative_connected_marker_is_not_live(self) -> None:
        self.assertEqual("failed", pair_row_state("pair — Not connected"))

    def test_connected_marker_survives_wrapping(self) -> None:
        # `capture-pane -J` joins a row wrapped by the pane width back into one.
        self.assertEqual("connected", pair_row_state("› pair       Connected"))

    def test_absent_row_is_not_classified_as_connected(self) -> None:
        self.assertEqual("absent", pair_row_state("› context7 — Connected"))


if __name__ == "__main__":
    unittest.main()
