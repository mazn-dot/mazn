import unittest
from unittest.mock import patch

import formatter


class FormatterTests(unittest.TestCase):
    def test_less_than_ten_transfers_are_hidden(self):
        contract = "0x" + "a" * 40
        def report(count):
            return {"MEXC": {"tokens": {"ABC": {"contract": contract, "symbol": "ABC", "name": "Alpha", "amount": 2.5, "count": count}}}}
        with patch.object(formatter, "get_usd_prices", return_value={contract: 1.0}):
            hidden = formatter.format_report(report(9), "out", 1440)
            visible = formatter.format_report(report(10), "out", 1440)
        self.assertNotIn("ABC", hidden)
        self.assertIn("ABC", visible)
        self.assertIn("10 تحويل", visible)


if __name__ == "__main__":
    unittest.main()
