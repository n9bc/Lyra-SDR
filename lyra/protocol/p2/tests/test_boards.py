"""Board-ID lookup tests."""
from __future__ import annotations

import unittest

from lyra.protocol.p2.boards import BOARDS, is_apache, lookup_board


class BoardsTest(unittest.TestCase):
    def test_saturn_g2_known(self) -> None:
        spec = lookup_board(10)
        self.assertIsNotNone(spec)
        assert spec is not None  # for type checker
        self.assertEqual(spec.short_name, "ANAN-G2")
        self.assertEqual(spec.family, "Apache")
        self.assertEqual(spec.n_adcs, 2)

    def test_hermes_lite_known(self) -> None:
        spec = lookup_board(6)
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.short_name, "Hermes Lite")
        self.assertEqual(spec.family, "HermesLite")
        self.assertEqual(spec.ddc_offset_for_rx1, 0,
                         "HermesLite uses DDC0 for RX1, unlike ORION2")

    def test_unknown_id_returns_none(self) -> None:
        self.assertIsNone(lookup_board(99))
        self.assertIsNone(lookup_board(255))  # full-form sentinel, not a real board

    def test_is_apache_classifier(self) -> None:
        self.assertTrue(is_apache(10))   # G2
        self.assertTrue(is_apache(5))    # ANAN-7000
        self.assertFalse(is_apache(6))   # HermesLite family (incl. Brick II)
        self.assertFalse(is_apache(0))   # Atlas is its own family
        self.assertFalse(is_apache(99))  # unknown

    def test_all_boards_have_unique_short_names(self) -> None:
        names = [s.short_name for s in BOARDS.values()]
        self.assertEqual(len(names), len(set(names)),
                         "duplicate short_name in BOARDS table")

    def test_id_field_matches_dict_key(self) -> None:
        for k, v in BOARDS.items():
            self.assertEqual(k, v.id, f"BOARDS[{k}].id != {k}")


if __name__ == "__main__":
    unittest.main()
