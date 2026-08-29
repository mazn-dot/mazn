import unittest
from unittest.mock import patch

import formatter


class FormatterTests(unittest.TestCase):
    def test_single_transfer_is_visible(self):
        contract = "0x" + "a" * 40
        data = {
            "MEXC": {
                "tokens": {
                    "ABC": {"contract": contract, "symbol": "ABC", "name": "Alpha", "amount": 2.5, "count": 1}
                }
            }
        }
        with patch.object(formatter, "get_usd_prices", return_value={contract: 1.0}):
            text = formatter.format_report(data, "out", 1440)
        self.assertIn("ABC", text)
        self.assertIn("تحويل", text)
        self.assertNotIn("لا توجد عملات", text)


if __name__ == "__main__":
    unittest.main()
