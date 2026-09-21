import os
import sqlite3
from datetime import datetime


# Railway:
# Set DATABASE_PATH to a persistent path such as /data/bot_database.db
# when a Railway Volume is attached.
DB_NAME = os.getenv("DATABASE_PATH", "bot_database.db")

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

ORDER_STATUSES = {
    "waiting_payment",
    "payment_review",
    "approved",
    "delivered",
    "rejected",
    "cancelled",
    "expired",
}


def now_text():
    return datetime.now().strftime(DATETIME_FORMAT)


def get_connection():
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        isolation_level=None,  # explicit transactions
    )
    conn.row_factory = sqlite3.Row

    # Better SQLite behavior for a bot running on Railway.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    return conn


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


def _ensure_column(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cur.fetchall()}

    if column not in columns:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                service_name TEXT,
                volume TEXT,
                price INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'waiting_payment',
                receipt_file_id TEXT,
                config TEXT,
                purchase_date TEXT,
                expiry_date TEXT,
                service_start_date TEXT,
                service_key TEXT,
                discount_code TEXT,
                discount_amount INTEGER NOT NULL DEFAULT 0,
                final_price INTEGER,
                renewal_for_order_code TEXT,
                reminder_3_sent INTEGER NOT NULL DEFAULT 0,
                reminder_1_sent INTEGER NOT NULL DEFAULT 0,
                expired_notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE,
                discount_type TEXT,
                discount_value INTEGER,
                max_uses INTEGER DEFAULT 0,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS services (
                service_key TEXT PRIMARY KEY,
                name TEXT,
                volume TEXT,
                price INTEGER,
                duration_days INTEGER DEFAULT 30,
                active INTEGER DEFAULT 1
            )
        """)

        # Safe migrations for existing databases.
        _ensure_column(cur, "orders", "service_key", "TEXT")
        _ensure_column(cur, "orders", "reminder_3_sent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "reminder_1_sent", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "expired_notified", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "discount_code", "TEXT")
        _ensure_column(cur, "orders", "discount_amount", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "orders", "final_price", "INTEGER")
        _ensure_column(cur, "orders", "renewal_for_order_code", "TEXT")
        _ensure_column(cur, "orders", "service_start_date", "TEXT")

        # Useful indexes for the queries used by the bot.
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_user_id
            ON orders(user_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status
            ON orders(status)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiry
            ON orders(status, expiry_date)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_renewal
            ON orders(renewal_for_order_code)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_orders_service_key
            ON orders(service_key)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_discount_code
            ON discount_codes(code)
        """)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


# ---------------- USERS ----------------

def save_user(user_id, username=None, first_name=None):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (user_id, username, first_name, now_text()))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_all_users():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM users
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_user_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        _close_quietly(conn)


# ---------------- ORDERS ----------------

def create_order(
    order_code,
    user_id,
    service_name,
    volume,
    price,
    service_key=None,
    discount_code=None,
    discount_amount=0,
    final_price=None,
    renewal_for_order_code=None,
):
    if final_price is None:
        final_price = max(0, int(price) - int(discount_amount))

    if int(price) < 0 or int(discount_amount) < 0 or int(final_price) < 0:
        raise ValueError("Invalid price/discount values.")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO orders (
                order_code,
                user_id,
                service_name,
                volume,
                price,
                status,
                service_key,
                discount_code,
                discount_amount,
                final_price,
                renewal_for_order_code,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'waiting_payment', ?, ?, ?, ?, ?, ?)
        """, (
            order_code,
            user_id,
            service_name,
            volume,
            int(price),
            service_key,
            discount_code.upper() if discount_code else None,
            int(discount_amount),
            int(final_price),
            renewal_for_order_code,
            now_text(),
        ))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_order(order_code):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code = ?
        """, (order_code,)).fetchone()
    finally:
        _close_quietly(conn)


def update_order_status(order_code, status):
    if status not in ORDER_STATUSES:
        raise ValueError(f"Invalid order status: {status}")

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = ?
            WHERE order_code = ?
        """, (status, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def set_order_status_if_current(order_code, new_status, current_status):
    """Atomic compare-and-set status update."""
    if new_status not in ORDER_STATUSES or current_status not in ORDER_STATUSES:
        raise ValueError("Invalid order status.")

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = ?
            WHERE order_code = ?
              AND status = ?
        """, (new_status, order_code, current_status))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def save_receipt(order_code, receipt_file_id):
    """
    Attaches a receipt only to an order that is still waiting for payment.
    Returns True only when the status was changed successfully.
    """
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET receipt_file_id = ?,
                status = 'payment_review'
            WHERE order_code = ?
              AND status = 'waiting_payment'
        """, (receipt_file_id, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def save_config(order_code, config):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET config = ?
            WHERE order_code = ?
        """, (config, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def set_dates(order_code, purchase_date, expiry_date, service_start_date=None):
    if service_start_date is None:
        service_start_date = purchase_date

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET purchase_date = ?,
                expiry_date = ?,
                service_start_date = ?
            WHERE order_code = ?
        """, (purchase_date, expiry_date, service_start_date, order_code))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def finalize_order_delivery(
    order_code,
    config,
    purchase_date,
    expiry_date,
    service_start_date=None,
):
    """
    Atomically saves the config/dates and changes the order to delivered.

    Returns:
      True  -> finalization happened now
      False -> order was already delivered, or is not eligible
    """
    if service_start_date is None:
        service_start_date = purchase_date

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        order = conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code = ?
        """, (order_code,)).fetchone()

        if order is None:
            conn.rollback()
            return False

        if order["status"] == "delivered":
            conn.commit()
            return False

        if order["status"] != "approved":
            conn.rollback()
            return False

        conn.execute("""
            UPDATE orders
            SET config = ?,
                purchase_date = ?,
                expiry_date = ?,
                service_start_date = ?,
                status = 'delivered'
            WHERE order_code = ?
              AND status = 'approved'
        """, (
            config,
            purchase_date,
            expiry_date,
            service_start_date,
            order_code,
        ))

        # Discount consumption happens in the same transaction as delivery.
        # It is capped for max_uses to prevent over-consumption.
        if order["discount_code"]:
            code = order["discount_code"].upper()
            discount = conn.execute("""
                SELECT max_uses, used_count
                FROM discount_codes
                WHERE code = ?
            """, (code,)).fetchone()

            if discount:
                if discount["max_uses"] > 0:
                    conn.execute("""
                        UPDATE discount_codes
                        SET used_count = used_count + 1
                        WHERE code = ?
                          AND used_count < max_uses
                    """, (code,))
                else:
                    conn.execute("""
                        UPDATE discount_codes
                        SET used_count = used_count + 1
                        WHERE code = ?
                    """, (code,))

        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        _close_quietly(conn)


def get_user_orders(user_id):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
    finally:
        _close_quietly(conn)


def get_user_services(user_id):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE user_id = ?
              AND status IN ('delivered', 'expired')
              AND config IS NOT NULL
            ORDER BY created_at DESC
        """, (user_id,)).fetchall()
    finally:
        _close_quietly(conn)


def get_all_orders():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_pending_orders():
    # Payment review is the actual payment-review queue.
    # Approved orders are waiting for admin to deliver/configure.
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status IN ('payment_review', 'approved')
            ORDER BY created_at ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_active_services():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status = 'delivered'
            ORDER BY expiry_date ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_order_count():
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        _close_quietly(conn)


def get_order_count_by_status(status):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE status = ?
        """, (status,)).fetchone()[0]
    finally:
        _close_quietly(conn)


def get_total_sales():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT COALESCE(SUM(
                CASE
                    WHEN final_price IS NOT NULL THEN final_price
                    ELSE price
                END
            ), 0)
            FROM orders
            WHERE status IN ('delivered', 'expired')
        """).fetchone()[0]
    finally:
        _close_quietly(conn)


def search_orders(query):
    conn = get_connection()
    try:
        q = f"%{query}%"
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE order_code LIKE ?
               OR CAST(user_id AS TEXT) LIKE ?
               OR service_name LIKE ?
            ORDER BY created_at DESC
        """, (q, q, q)).fetchall()
    finally:
        _close_quietly(conn)


# ---------------- EXPIRY ----------------

def get_services_for_expiry_check():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM orders
            WHERE status = 'delivered'
              AND expiry_date IS NOT NULL
        """).fetchall()
    finally:
        _close_quietly(conn)


def mark_reminder_sent(order_code, reminder_number):
    if reminder_number == 3:
        column = "reminder_3_sent"
    elif reminder_number == 1:
        column = "reminder_1_sent"
    else:
        return False

    conn = get_connection()
    try:
        cur = conn.execute(f"""
            UPDATE orders
            SET {column} = 1
            WHERE order_code = ?
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def mark_expired(order_code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET status = 'expired'
            WHERE order_code = ?
              AND status = 'delivered'
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def mark_expired_notified(order_code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE orders
            SET expired_notified = 1
            WHERE order_code = ?
        """, (order_code,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def has_scheduled_renewal(order_code, reference_date, now_text_value=None):
    if now_text_value is None:
        now_text_value = now_text()

    conn = get_connection()
    try:
        count = conn.execute("""
            SELECT COUNT(*)
            FROM orders
            WHERE renewal_for_order_code = ?
              AND status IN ('payment_review', 'approved', 'delivered')
              AND service_start_date >= ?
              AND (
                  expiry_date IS NULL
                  OR expiry_date > ?
              )
        """, (
            order_code,
            reference_date,
            now_text_value,
        )).fetchone()[0]
        return count > 0
    finally:
        _close_quietly(conn)


# ---------------- SETTINGS ----------------

def get_setting(key, default=None):
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key = ?
        """, (key,)).fetchone()
        return default if row is None else row["value"]
    finally:
        _close_quietly(conn)


def set_setting(key, value):
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value
        """, (key, str(value)))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_next_support_admin(admin_ids):
    if not admin_ids:
        return None

    normalized_ids = [int(x) for x in admin_ids]
    last = get_setting("last_support_admin")

    try:
        last_id = int(last) if last is not None else None
    except (TypeError, ValueError):
        last_id = None

    if last_id in normalized_ids:
        index = normalized_ids.index(last_id)
        selected = normalized_ids[(index + 1) % len(normalized_ids)]
    else:
        selected = normalized_ids[0]

    set_setting("last_support_admin", selected)
    return selected


# ---------------- DISCOUNTS ----------------

def create_discount_code(
    code,
    discount_type,
    discount_value,
    max_uses=0,
    expires_at=None,
):
    code = code.strip().upper()

    if discount_type not in {"percent", "fixed"}:
        raise ValueError("discount_type must be 'percent' or 'fixed'.")

    if int(discount_value) < 0:
        raise ValueError("discount_value cannot be negative.")

    if int(max_uses) < 0:
        raise ValueError("max_uses cannot be negative.")

    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO discount_codes (
                code,
                discount_type,
                discount_value,
                max_uses,
                used_count,
                expires_at,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, 0, ?, 1, ?)
        """, (
            code,
            discount_type,
            int(discount_value),
            int(max_uses),
            expires_at,
            now_text(),
        ))
        conn.commit()
    finally:
        _close_quietly(conn)


def get_discount_codes():
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM discount_codes
            ORDER BY created_at DESC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_discount_code(code):
    if not code:
        return None

    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM discount_codes
            WHERE code = ?
        """, (code.strip().upper(),)).fetchone()
    finally:
        _close_quietly(conn)


def calculate_discount(code, price):
    if not code:
        return 0, None

    price = int(price)
    if price < 0:
        return 0, "قیمت نامعتبر است."

    discount = get_discount_code(code)

    if not discount:
        return 0, "کد تخفیف پیدا نشد."

    if not discount["active"]:
        return 0, "این کد تخفیف غیرفعال است."

    if (
        discount["max_uses"] > 0
        and discount["used_count"] >= discount["max_uses"]
    ):
        return 0, "ظرفیت استفاده از این کد تخفیف تمام شده است."

    if discount["expires_at"]:
        try:
            expires = datetime.strptime(
                discount["expires_at"],
                DATETIME_FORMAT,
            )
            if datetime.now() > expires:
                return 0, "این کد تخفیف منقضی شده است."
        except ValueError:
            return 0, "تاریخ انقضای کد تخفیف نامعتبر است."

    if discount["discount_type"] == "percent":
        amount = int(price * discount["discount_value"] / 100)
    else:
        amount = int(discount["discount_value"])

    amount = max(0, min(amount, price))
    return amount, None


def increment_discount_usage(code):
    """
    Kept for compatibility with the existing bot.
    New delivery flow should use finalize_order_delivery(), which
    increments usage inside the same DB transaction.
    """
    if not code:
        return False

    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE discount_codes
            SET used_count = used_count + 1
            WHERE code = ?
              AND (
                  max_uses = 0
                  OR used_count < max_uses
              )
        """, (code.strip().upper(),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


def toggle_discount(code):
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE discount_codes
            SET active = CASE
                WHEN active = 1 THEN 0
                ELSE 1
            END
            WHERE code = ?
        """, (code.strip().upper(),))
        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)


# ---------------- SERVICES ----------------

def seed_services(services):
    """
    IMPORTANT:
    Existing service records are NOT overwritten on every restart.
    This prevents admin-edited prices/settings from being reset by Railway
    redeploys/restarts.

    Missing services are inserted with the default values.
    """
    conn = get_connection()
    try:
        for service in services:
            conn.execute("""
                INSERT INTO services (
                    service_key,
                    name,
                    volume,
                    price,
                    duration_days,
                    active
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(service_key) DO NOTHING
            """, (
                service["key"],
                service["name"],
                service["volume"],
                int(service["price"]),
                int(service.get("duration_days", 30)),
                int(service.get("active", 1)),
            ))

        conn.commit()
    finally:
        _close_quietly(conn)


def get_services(active_only=False):
    conn = get_connection()
    try:
        if active_only:
            return conn.execute("""
                SELECT *
                FROM services
                WHERE active = 1
                ORDER BY price ASC
            """).fetchall()

        return conn.execute("""
            SELECT *
            FROM services
            ORDER BY price ASC
        """).fetchall()
    finally:
        _close_quietly(conn)


def get_service(service_key):
    conn = get_connection()
    try:
        return conn.execute("""
            SELECT *
            FROM services
            WHERE service_key = ?
        """, (service_key,)).fetchone()
    finally:
        _close_quietly(conn)


def update_service(
    service_key,
    name=None,
    volume=None,
    price=None,
    duration_days=None,
    active=None,
):
    conn = get_connection()
    try:
        service = conn.execute("""
            SELECT *
            FROM services
            WHERE service_key = ?
        """, (service_key,)).fetchone()

        if not service:
            return False

        new_name = name if name is not None else service["name"]
        new_volume = volume if volume is not None else service["volume"]
        new_price = price if price is not None else service["price"]
        new_duration = (
            duration_days
            if duration_days is not None
            else service["duration_days"]
        )
        new_active = active if active is not None else service["active"]

        if int(new_price) < 0:
            raise ValueError("price cannot be negative.")
        if int(new_duration) <= 0:
            raise ValueError("duration_days must be greater than zero.")
        if int(new_active) not in (0, 1):
            raise ValueError("active must be 0 or 1.")

        cur = conn.execute("""
            UPDATE services
            SET name = ?,
                volume = ?,
                price = ?,
                duration_days = ?,
                active = ?
            WHERE service_key = ?
        """, (
            new_name,
            new_volume,
            int(new_price),
            int(new_duration),
            int(new_active),
            service_key,
        ))

        conn.commit()
        return cur.rowcount == 1
    finally:
        _close_quietly(conn)
