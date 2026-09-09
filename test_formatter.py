import unittest
from unittest.mock import patch

import formatter


class FormatterV3Tests(unittest.TestCase):
    def report(self, count):
        contract = "0x" + "a" * 40
        item = {
            "chain": "bsc",
            "contract": contract,
            "symbol": "ABC",
            "name": "Alpha",
            "amount": 25.0,
            "count": count,
            "score": 25.0,
            "reason": "نشاط",
        }
        return {"_meta": {"chains": ["bsc"]}, "_combined": [item]}

    def test_less_than_ten_transfers_are_hidden(self):
        contract = "0x" + "a" * 40
        with patch.object(formatter, "get_usd_prices", return_value={contract: 1.0}):
            hidden = formatter.format_report(self.report(9), "out", 1440)
            visible = formatter.format_report(self.report(10), "out", 1440)
        self.assertNotIn("ABC", hidden)
        self.assertIn("ABC", visible)
        self.assertNotIn("لا توجد توكنات", visible)


if __name__ == "__main__":
    unittest.main()
