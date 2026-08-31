"""
إدارة المخاطر: تحديد حجم الصفقة، وحد الخسارة اليومي، وعدد المراكز المفتوحة.
البوت بيوقف نفسه تلقائياً لو الخسارة اليومية وصلت للحد الأقصى المسموح.
"""
import logging
from datetime import datetime, timezone

from .state import shared_state

logger = logging.getLogger("risk")


class RiskManager:
    def __init__(self):
        self.day = datetime.now(timezone.utc).date()
        self.daily_pnl_pct = 0.0
        self.starting_balance = None

    def _reset_if_new_day(self, balance: float):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.daily_pnl_pct = 0.0
            self.starting_balance = balance
            logger.info("يوم جديد: تصفير عداد الخسارة اليومية.")
        if self.starting_balance is None:
            self.starting_balance = balance

    def register_trade_result(self, pnl_usdt: float, balance: float):
        self._reset_if_new_day(balance)
        if self.starting_balance:
            self.daily_pnl_pct += (pnl_usdt / self.starting_balance) * 100

    def trading_allowed(self, balance: float) -> bool:
        self._reset_if_new_day(balance)
        max_daily_loss = shared_state.get("max_daily_loss_pct")
        if self.daily_pnl_pct <= -abs(max_daily_loss):
            logger.warning(
                f"🛑 تم إيقاف التداول: تعديت حد الخسارة اليومي "
                f"({self.daily_pnl_pct:.2f}% <= -{max_daily_loss}%)"
            )
            return False
        return True

    def can_open_new_position(self, open_positions_count: int) -> bool:
        return open_positions_count < shared_state.get("max_open_positions")
