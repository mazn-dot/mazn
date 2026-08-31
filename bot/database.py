"""
تسجيل الصفقات والإشارات في قاعدة بيانات Postgres (Railway).
Railway بيوفر متغير DATABASE_URL تلقائياً لما تضيف Postgres plugin للمشروع.
"""
import logging
import psycopg2
import psycopg2.extras

from .config import Config

logger = logging.getLogger("database")


class Database:
    def __init__(self):
        self.dsn = Config.DATABASE_URL
        self._init_schema()

    def _connect(self):
        return psycopg2.connect(self.dsn)

    def _init_schema(self):
        schema = """
        CREATE TABLE IF NOT EXISTS trades (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL,
            entry_price DOUBLE PRECISION NOT NULL,
            stop_loss DOUBLE PRECISION,
            take_profit DOUBLE PRECISION,
            exit_price DOUBLE PRECISION,
            pnl_usdt DOUBLE PRECISION,
            status TEXT NOT NULL DEFAULT 'open',
            dry_run BOOLEAN NOT NULL DEFAULT TRUE,
            reason TEXT,
            group_id TEXT,
            leg_index INTEGER,
            total_legs INTEGER,
            opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at TIMESTAMPTZ
        );
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS group_id TEXT;
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS leg_index INTEGER;
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS total_legs INTEGER;
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS plan_order_ids TEXT[];
        ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_plan_order_id TEXT;

        CREATE TABLE IF NOT EXISTS bot_channels (
            channel TEXT PRIMARY KEY,
            chat_id BIGINT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS signals_log (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            price DOUBLE PRECISION NOT NULL,
            reason TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS channel_signals (
            id SERIAL PRIMARY KEY,
            channel_id TEXT NOT NULL,
            message_id BIGINT NOT NULL,
            symbol_raw TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            entry_price DOUBLE PRECISION,
            stop_loss DOUBLE PRECISION,
            targets DOUBLE PRECISION[],
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT,
            trade_ids INTEGER[],
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (channel_id, message_id, symbol_raw)
        );
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(schema)
            conn.commit()

    def log_signal(self, symbol: str, action: str, price: float, reason: str):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO signals_log (symbol, action, price, reason) VALUES (%s,%s,%s,%s)",
                    (symbol, action, price, reason),
                )
            conn.commit()

    def open_trade(self, symbol, side, amount, entry_price, stop_loss, take_profit, reason, dry_run,
                   group_id: str = None, leg_index: int = None, total_legs: int = None) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO trades (symbol, side, amount, entry_price, stop_loss, take_profit,
                                            reason, dry_run, group_id, leg_index, total_legs)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (symbol, side, amount, entry_price, stop_loss, take_profit, reason, dry_run,
                     group_id, leg_index, total_legs),
                )
                trade_id = cur.fetchone()[0]
            conn.commit()
        return trade_id

    def close_fake_trade(self, trade_id: int, detail: str):
        """يغلق صفقة 'وهمية' سجلها في قاعدة البيانات بدون أن يكون لها رصيد فعلي
        في المحفظة (تجارب قديمة أو سجلات تالفة) - من غير بيع، ومع تسجيل السبب.
        بيفرق عن close_trade العادي إنه مش بيع حقيقي ولا يمسح حساب الأرباح."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE trades SET status='closed', exit_price=entry_price, pnl_usdt=0,
                          closed_at=now(), reason=COALESCE(reason, '') || ' | ' || %s
                       WHERE id=%s""",
                    (detail, trade_id),
                )
            conn.commit()

    def close_trade(self, trade_id: int, exit_price: float, pnl_usdt: float):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE trades SET exit_price=%s, pnl_usdt=%s, status='closed', closed_at=now()
                       WHERE id=%s""",
                    (exit_price, pnl_usdt, trade_id),
                )
            conn.commit()

    def open_trades(self):
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM trades WHERE status='open'")
                return cur.fetchall()

    # =========================================================================
    # مجموعات الصفقات (تقسيم 3 أهداف) - رفع الستوب تدريجياً بعد كل هدف يتحقق
    # =========================================================================
    def update_stop_loss(self, trade_id: int, new_stop: float):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE trades SET stop_loss=%s WHERE id=%s", (new_stop, trade_id))
            conn.commit()

    def raise_group_stop_loss(self, group_id: str, exclude_trade_id: int, new_stop: float):
        """يرفع الستوب لوس لكل المراكز المفتوحة الباقية في نفس المجموعة (نفس التوصية)
        ما عدا المركز اللي لسه قافل دلوقتي - يُستخدم بعد تحقيق هدف ربح."""
        if not group_id:
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE trades SET stop_loss=%s
                       WHERE group_id=%s AND status='open' AND id != %s""",
                    (new_stop, group_id, exclude_trade_id),
                )
            conn.commit()

    # =========================================================================
    # أوامر المنصة الشرطية (trigger / plan orders) - ربطها بالصفقات
    # =========================================================================
    def save_plan_order_ids(self, trade_id: int, plan_order_ids: list[str], sl_plan_order_id: str | None):
        """يحفظ أرقام أوامر المنصة الشرطية (TP لكل leg + SL واحد) مع الصفقة."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE trades SET plan_order_ids=%s, sl_plan_order_id=%s WHERE id=%s",
                    (plan_order_ids or [], sl_plan_order_id, trade_id),
                )
            conn.commit()

    def get_group_plan_order_ids(self, group_id: str):
        """يجيب كل أوامر المنصة الشرطية (TP + SL) لمجموعة صفقات معينة."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, plan_order_ids, sl_plan_order_id FROM trades WHERE group_id=%s",
                    (group_id,),
                )
                return cur.fetchall()

    def get_leg_take_profit(self, group_id: str, leg_index: int):
        """يجيب سعر هدف (take_profit) لِـ leg معين في نفس المجموعة، بغض النظر عن حالته
        (مفتوح أو مقفول) - يُستخدم لمعرفة سعر الهدف السابق عشان نرفع الستوب لسعره."""
        if not group_id:
            return None
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT take_profit FROM trades WHERE group_id=%s AND leg_index=%s LIMIT 1",
                    (group_id, leg_index),
                )
                row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    # =========================================================================
    # قنوات التوصيات (Telethon) - منع تنفيذ نفس التوصية مرتين
    # =========================================================================
    def claim_channel_signal(self, channel_id: str, message_id: int, symbol_raw: str) -> bool:
        """
        يحاول 'يحجز' التوصية دي (رسالة + رمز عملة معين جواها) قبل تنفيذها.
        بيرجع True لو دي أول مرة نشوفها (يبقى ينفذها)، أو False لو اتعالجت قبل كده
        (يبقى يتجاهلها - بيمنع التكرار حتى لو الرسالة اتعدلت وجت تاني بنفس الـ message_id).
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO channel_signals (channel_id, message_id, symbol_raw, status)
                       VALUES (%s,%s,%s,'pending')
                       ON CONFLICT (channel_id, message_id, symbol_raw) DO NOTHING
                       RETURNING id""",
                    (str(channel_id), message_id, symbol_raw),
                )
                row = cur.fetchone()
            conn.commit()
        return row is not None

    def update_channel_signal(self, channel_id: str, message_id: int, symbol_raw: str, **fields):
        """تحديث تفاصيل التوصية بعد المعالجة (status, detail, symbol, side, entry_price,
        stop_loss, targets, trade_ids...)."""
        if not fields:
            return
        columns = ", ".join(f"{k}=%s" for k in fields.keys())
        values = list(fields.values()) + [str(channel_id), message_id, symbol_raw]
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE channel_signals SET {columns}
                        WHERE channel_id=%s AND message_id=%s AND symbol_raw=%s""",
                    values,
                )
            conn.commit()

    # =========================================================================
    # قنوات التوصيات الدائمة - إضافة/حذف من تليجرام تبقى محفوظة بعد إعادة التشغيل
    # =========================================================================
    def get_persisted_channels(self) -> list[str]:
        """يجيب قائمة القنوات المحفوظة دائمًأ في قاعدة البيانات (بترتيب الإضافة)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel FROM bot_channels ORDER BY added_at ASC")
                return [row[0] for row in cur.fetchall()]

    def get_persisted_channels_with_chat_ids(self) -> list[tuple[str, int | None]]:
        """يجيب القنوات المحفوظة مع الآيدي الرقمي الحقيقي (لو اتسجل)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT channel, chat_id FROM bot_channels ORDER BY added_at ASC")
                return [(row[0], row[1]) for row in cur.fetchall()]

    def persist_channel(self, channel: str, chat_id=None) -> bool:
        """يحفظ قناة جديدة دائمًا (تُرجع True لو تمت إضافة جديدة)."""
        channel = (channel or "").strip()
        if not channel:
            return False
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_channels (channel, chat_id) VALUES (%s, %s) ON CONFLICT (channel) DO NOTHING RETURNING channel",
                    (channel, chat_id),
                )
                return cur.fetchone() is not None

    def remove_persisted_channel(self, channel: str) -> bool:
        """يحذف قناة من قائمة القنوات الدائمة."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_channels WHERE channel=%s", (channel,))
                return cur.rowcount > 0

    def update_channel_chat_id(self, channel: str, chat_id: int):
        """يحفظ الآيدي الرقمي الحقيقي للقناة (chat.id) عند التحقق منها."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bot_channels SET chat_id=%s WHERE channel=%s",
                    (chat_id, channel),
                )
            conn.commit()

    def recent_channel_signals(self, limit: int = 10):
        with self._connect() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM channel_signals ORDER BY created_at DESC LIMIT %s", (limit,)
                )
                return cur.fetchall()
