import unittest

from main import parse_slot_numbers


class SlotNumberParsingTests(unittest.TestCase):
    def test_parses_space_separated_numbers_and_removes_duplicates(self):
        self.assertEqual(parse_slot_numbers("03 08 12 17 08"), [3, 8, 12, 17])

    def test_rejects_non_numeric_tokens(self):
        with self.assertRaises(ValueError):
            parse_slot_numbers("03 twelve 17")

    def test_rejects_out_of_range_numbers(self):
        with self.assertRaises(ValueError):
            parse_slot_numbers("00 03")

    def test_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            parse_slot_numbers("   ")


if __name__ == "__main__":
    unittest.main()