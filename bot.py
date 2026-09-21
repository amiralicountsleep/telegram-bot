import os
import asyncio
import logging
import uuid
import sqlite3
import html

from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramRetryAfter,
)

from database import (
    init_db,
    save_user,
    create_order,
    get_order,
    update_order_status,
    set_order_status_if_current,
    save_receipt,
    finalize_order_delivery,
    get_user_orders,
    get_user_services,
    get_all_users,
    get_all_orders,
    get_pending_orders,
    get_order_count,
    get_user_count,
    get_order_count_by_status,
    get_total_sales,
    search_orders,
    get_services_for_expiry_check,
    mark_reminder_sent,
    mark_expired,
    mark_expired_notified,
    has_scheduled_renewal,
    get_setting,
    set_setting,
    get_next_support_admin,
    calculate_discount,
    create_discount_code,
    get_discount_codes,
    toggle_discount,
    seed_services,
    get_services,
    get_service,
)


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set.")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
try:
    ADMIN_IDS = [
        int(x.strip())
        for x in ADMIN_IDS_RAW.split(",")
        if x.strip()
    ]
except ValueError as exc:
    raise RuntimeError(
        "ADMIN_IDS must contain only numeric Telegram user IDs."
    ) from exc

if len(ADMIN_IDS) < 2:
    raise RuntimeError("ADMIN_IDS must contain at least two Telegram user IDs.")

CARD_NUMBER = os.getenv("CARD_NUMBER", "")
if not CARD_NUMBER:
    raise RuntimeError("CARD_NUMBER is not set.")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher()


# =========================================================
# SERVICES
# =========================================================

DEFAULT_SERVICES = [
    {
        "key": "1gb",
        "name": "۱ گیگ - تک کاربره",
        "volume": "۱ گیگ",
        "price": 10000,
        "duration_days": 30,
        "active": 0,
    },
    {
        "key": "2gb",
        "name": "۲ گیگ - تک کاربره",
        "volume": "۲ گیگ",
        "price": 20000,
        "duration_days": 30,
        "active": 0,
    },
    {
        "key": "5gb",
        "name": "۵ گیگ - تک کاربره",
        "volume": "۵ گیگ",
        "price": 50000,
        "duration_days": 30,
        "active": 0,
    },
    {
        "key": "10gb",
        "name": "۱۰ گیگ - تک کاربره",
        "volume": "۱۰ گیگ",
        "price": 100000,
        "duration_days": 30,
        "active": 1,
    },
    {
        "key": "20gb",
        "name": "۲۰ گیگ - 🌐 IP ثابت - تک کاربره",
        "volume": "۲۰ گیگ",
        "price": 195000,
        "duration_days": 30,
        "active": 1,
    },
    {
        "key": "30gb_multi",
        "name": "🌍 ۳۰ گیگ - Multi-Location - تک کاربره",
        "volume": "۳۰ گیگ",
        "price": 190000,
        "duration_days": 30,
        "active": 1,
    },
    {
        "key": "50gb",
        "name": "۵۰ گیگ - 🌐 IP ثابت - تک کاربره",
        "volume": "۵۰ گیگ", 
        "price": 450000,
        "duration_days": 30,
        "active": 1,
    },
    {
        "key": "unlimited",
        "name": "♾️ نامحدود - تک کاربره",
        "volume": "نامحدود",
        "price": 195000,
        "duration_days": 30,
        "active": 1,
    },
]


# =========================================================
# STATES
# =========================================================

class PurchaseState(StatesGroup):
    waiting_for_discount = State()
    waiting_for_receipt = State()


class SupportState(StatesGroup):
    waiting_for_message = State()


class AdminConfigState(StatesGroup):
    waiting_for_config = State()


class AdminSearchState(StatesGroup):
    waiting_for_query = State()


class BroadcastState(StatesGroup):
    waiting_for_message = State()


# =========================================================
# CONSTANTS / HELPERS
# =========================================================

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# This lock is useful inside one bot process.
# The database compare-and-set below is what provides the real protection.
approval_locks = set()


def esc(value):
    return html.escape(str(value if value is not None else ""))


def generate_order_code():
    return "ORD-" + uuid.uuid4().hex[:8].upper()


def format_money(amount):
    return f"{int(amount):,}".replace(",", "٬")


def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_service_duration(service_key):
    service = get_service(service_key)
    if not service:
        return 30
    return int(service["duration_days"] or 30)


def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, DATETIME_FORMAT)
    except (TypeError, ValueError):
        return None


def calculate_renewal_dates(source_order):
    now = datetime.now()
    expiry = parse_datetime(source_order["expiry_date"])

    if expiry is None:
        return None, None

    start = expiry if expiry > now else now
    duration_days = get_service_duration(source_order["service_key"])
    end = start + timedelta(days=duration_days)

    return (
        start.strftime(DATETIME_FORMAT),
        end.strftime(DATETIME_FORMAT),
    )


def get_order_final_price(order):
    if order["final_price"] is not None:
        return order["final_price"]
    return order["price"]


def has_open_renewal(user_id, source_order_code):
    """Prevent duplicate renewal orders for the same source service."""
    for order in get_user_orders(user_id):
        if (
            order["renewal_for_order_code"] == source_order_code
            and order["status"] in (
                "waiting_payment",
                "payment_review",
                "approved",
                "delivered",
            )
        ):
            return True
    return False


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🛒 خرید سرویس"),
                KeyboardButton(text="📡 سرویس‌های من"),
            ],
            [
                KeyboardButton(text="📋 سفارش‌های من"),
                KeyboardButton(text="🎧 پشتیبانی"),
            ],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📦 سفارش‌های جدید"),
                KeyboardButton(text="💳 پرداخت‌های در انتظار"),
            ],
            [
                KeyboardButton(text="👥 کاربران"),
                KeyboardButton(text="📡 مدیریت سرویس‌ها"),
            ],
            [
                KeyboardButton(text="📊 آمار فروش"),
                KeyboardButton(text="🔍 جستجوی سفارش"),
            ],
            [
                KeyboardButton(text="🎟 کدهای تخفیف"),
                KeyboardButton(text="📢 پیام همگانی"),
            ],
        ],
        resize_keyboard=True,
    )


def service_keyboard():
    services = get_services(active_only=True)
    buttons = []

    for service in services:
        buttons.append([
            InlineKeyboardButton(
                text=(
                    f"{service['name']} | "
                    f"{format_money(service['price'])} تومان"
                ),
                callback_data=f"buy:{service['service_key']}",
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def discount_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎟 وارد کردن کد تخفیف",
                    callback_data="discount:yes",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ بدون کد تخفیف",
                    callback_data="discount:no",
                )
            ],
        ]
    )


def payment_keyboard(order_code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ لغو سفارش",
                    callback_data=f"cancel_order:{order_code}",
                )
            ]
        ]
    )


def admin_payment_keyboard(order_code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ تایید پرداخت",
                    callback_data=f"approve:{order_code}",
                ),
                InlineKeyboardButton(
                    text="❌ رد پرداخت",
                    callback_data=f"reject:{order_code}",
                ),
            ]
        ]
    )


def renew_keyboard(order_code):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 تمدید سرویس",
                    callback_data=f"renew:{order_code}",
                )
            ]
        ]
    )


# =========================================================
# START / ADMIN
# =========================================================

@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    save_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
    )

    text = """🌐 سلام و خوش اومدی!

به ربات فروش و مدیریت سرویس خوش اومدی 👋

📦 سرویس‌های مختلف با حجم‌های متنوع
👤 تمامی سرویس‌ها تک کاربره
⏳ اعتبار سرویس‌ها طبق مدت سرویس
⚡️ تحویل کانفیگ بعد از تأیید پرداخت
🎧 پشتیبانی در صورت نیاز

🛒 برای مشاهده سرویس‌ها و ثبت سفارش، روی «خرید سرویس» بزن.

از همراهی شما ممنونیم ❤️"""

    await message.answer(text, reply_markup=main_keyboard())


@dp.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext):
    await state.clear()

    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "🛠 پنل مدیریت باز شد.",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# BUY
# =========================================================

@dp.message(F.text == "🛒 خرید سرویس")
async def buy_service_handler(message: Message, state: FSMContext):
    await state.clear()

    services = get_services(active_only=True)
    if not services:
        await message.answer("در حال حاضر سرویسی برای فروش فعال نیست.")
        return

    await message.answer(
        "🛒 سرویس موردنظرت رو انتخاب کن:",
        reply_markup=service_keyboard(),
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery, state: FSMContext):
    service_key = callback.data.split(":", 1)[1]
    service = get_service(service_key)

    if not service or not service["active"]:
        await callback.answer(
            "این سرویس در حال حاضر فعال نیست.",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(
        service_key=service_key,
        service_name=service["name"],
        volume=service["volume"],
        price=service["price"],
        renewal_for_order_code=None,
    )
    await state.set_state(PurchaseState.waiting_for_discount)

    await callback.message.answer(
        f"🎟 آیا کد تخفیف داری؟\n\n"
        f"سرویس: {esc(service['name'])}\n"
        f"قیمت: {format_money(service['price'])} تومان",
        reply_markup=discount_keyboard(),
    )
    await callback.answer()


# =========================================================
# RENEW
# =========================================================

@dp.callback_query(F.data.startswith("renew:"))
async def renew_callback(callback: CallbackQuery, state: FSMContext):
    order_code = callback.data.split(":", 1)[1]
    order = get_order(order_code)

    if not order:
        await callback.answer("سرویس پیدا نشد.", show_alert=True)
        return

    if order["user_id"] != callback.from_user.id:
        await callback.answer("این سرویس متعلق به شما نیست.", show_alert=True)
        return

    if order["status"] not in ("delivered", "expired"):
        await callback.answer(
            "این سرویس در حال حاضر قابل تمدید نیست.",
            show_alert=True,
        )
        return

    if has_open_renewal(callback.from_user.id, order_code):
        await callback.answer(
            "برای این سرویس یک تمدید فعال یا در حال بررسی وجود دارد.",
            show_alert=True,
        )
        return

    service_key = order["service_key"]
    if not service_key:
        await callback.answer(
            "اطلاعات سرویس این سفارش کامل نیست.",
            show_alert=True,
        )
        return

    service = get_service(service_key)
    if not service or not service["active"]:
        await callback.answer(
            "این سرویس در حال حاضر برای تمدید فعال نیست.",
            show_alert=True,
        )
        return

    start, expiry = calculate_renewal_dates(order)
    if not start or not expiry:
        await callback.answer(
            "تاریخ سرویس قابل محاسبه نیست.",
            show_alert=True,
        )
        return

    await state.clear()
    await state.update_data(
        service_key=service_key,
        service_name=service["name"],
        volume=service["volume"],
        price=service["price"],
        renewal_for_order_code=order_code,
    )
    await state.set_state(PurchaseState.waiting_for_discount)

    await callback.message.answer(
        f"🔄 تمدید سرویس\n\n"
        f"📦 سرویس: {esc(service['name'])}\n"
        f"💰 قیمت: {format_money(service['price'])} تومان\n"
        f"📅 شروع تمدید: {start}\n"
        f"⏳ پایان تمدید: {expiry}\n\n"
        f"🎟 آیا کد تخفیف داری؟",
        reply_markup=discount_keyboard(),
    )
    await callback.answer()


# =========================================================
# DISCOUNT / CREATE ORDER
# =========================================================

@dp.callback_query(
    PurchaseState.waiting_for_discount,
    F.data == "discount:no",
)
async def no_discount(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    order_code = await create_payment_order(
        callback.message,
        callback.from_user.id,
        data,
        None,
        0,
    )

    if order_code:
        await state.update_data(order_code=order_code)
        await state.set_state(PurchaseState.waiting_for_receipt)

    await callback.answer()


@dp.callback_query(
    PurchaseState.waiting_for_discount,
    F.data == "discount:yes",
)
async def yes_discount(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "🎟 کد تخفیف رو ارسال کن.\n\n"
        "اگر منصرف شدی /cancel رو بفرست."
    )
    await state.update_data(waiting_discount_code=True)
    await callback.answer()


@dp.message(PurchaseState.waiting_for_discount)
async def discount_text_handler(message: Message, state: FSMContext):
    if not message.text:
        return

    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer(
            "❌ عملیات لغو شد.",
            reply_markup=main_keyboard(),
        )
        return

    data = await state.get_data()
    if not data.get("waiting_discount_code"):
        return

    code = message.text.strip().upper()
    discount_amount, error = calculate_discount(code, data["price"])

    if error:
        await message.answer(
            f"❌ {esc(error)}\n\n"
            "کد دیگری وارد کن یا /cancel بزن."
        )
        return

    order_code = await create_payment_order(
        message,
        message.from_user.id,
        data,
        code,
        discount_amount,
    )

    if order_code:
        await state.update_data(order_code=order_code)
        await state.set_state(PurchaseState.waiting_for_receipt)


async def create_payment_order(
    message,
    user_id,
    data,
    discount_code,
    discount_amount=0,
):
    service = get_service(data["service_key"])
    if not service or not service["active"]:
        await message.answer("❌ این سرویس دیگر فعال نیست.")
        return None

    price = int(service["price"])
    discount_amount = max(0, min(int(discount_amount), price))
    final_price = price - discount_amount

    order_code = generate_order_code()

    try:
        create_order(
            order_code=order_code,
            user_id=user_id,
            service_name=service["name"],
            volume=service["volume"],
            price=price,
            service_key=service["service_key"],
            discount_code=discount_code,
            discount_amount=discount_amount,
            final_price=final_price,
            renewal_for_order_code=data.get("renewal_for_order_code"),
        )
    except sqlite3.IntegrityError:
        logger.exception("Could not create order %s", order_code)
        await message.answer("❌ ایجاد سفارش ناموفق بود. دوباره تلاش کن.")
        return None

    discount_text = ""
    if discount_code:
        discount_text = (
            f"\n🎟 کد تخفیف: <code>{esc(discount_code)}</code>"
            f"\n💸 تخفیف: {format_money(discount_amount)} تومان"
        )

    text = f"""💳 پرداخت سفارش

📦 سرویس: {esc(service['name'])}
💰 قیمت اصلی: {format_money(price)} تومان
{discount_text}
💵 مبلغ قابل پرداخت: {format_money(final_price)} تومان

💳 شماره کارت:
<code>{esc(CARD_NUMBER)}</code>

بعد از واریز، لطفاً عکس رسید پرداخت را همینجا ارسال کن.

⏳ پرداخت شما پس از بررسی تأیید خواهد شد."""

    try:
        await message.answer(
            text,
            reply_markup=payment_keyboard(order_code),
        )
        await message.answer(
            f"🔖 کد سفارش شما:\n<code>{order_code}</code>\n\n"
            "این کد را نگه دار."
        )
    except Exception:
        logger.exception("Could not send payment instructions for %s", order_code)

    # IMPORTANT: Admins are notified only after the receipt is uploaded.
    return order_code


# =========================================================
# RECEIPT
# =========================================================

@dp.message(PurchaseState.waiting_for_receipt, F.photo)
async def receipt_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    order_code = data.get("order_code")

    if not order_code:
        await message.answer(
            "❌ سفارش فعال پیدا نشد. لطفاً دوباره سفارش ثبت کن."
        )
        await state.clear()
        return

    order = get_order(order_code)

    if not order or order["user_id"] != message.from_user.id:
        await message.answer("❌ سفارش معتبر پیدا نشد.")
        await state.clear()
        return

    if order["status"] != "waiting_payment":
        await message.answer(
            "❌ این سفارش دیگر منتظر رسید نیست."
        )
        await state.clear()
        return

    receipt_file_id = message.photo[-1].file_id

    if not save_receipt(order_code, receipt_file_id):
        await message.answer(
            "❌ این سفارش قبلاً پردازش شده یا قابل ثبت رسید نیست."
        )
        await state.clear()
        return

    await message.answer(
        "✅ رسید دریافت شد.\n\n"
        "⏳ پرداخت شما برای بررسی ارسال شد."
    )

    await send_payment_to_admins(
        order_code,
        receipt_file_id,
    )

    await state.clear()


@dp.message(PurchaseState.waiting_for_receipt)
async def receipt_wrong_type(message: Message):
    await message.answer("📸 لطفاً عکس رسید پرداخت را ارسال کن.")


# =========================================================
# ADMIN PAYMENT NOTIFICATION
# =========================================================

async def send_payment_to_admins(order_code, receipt_file_id=None):
    order = get_order(order_code)
    if not order:
        return

    user_id = order["user_id"]

    text = f"""💳 پرداخت جدید

🔖 سفارش: <code>{esc(order_code)}</code>
👤 کاربر: <code>{user_id}</code>

📦 سرویس: {esc(order['service_name'])}
💰 مبلغ: {format_money(get_order_final_price(order))} تومان
"""

    if order["discount_code"]:
        text += (
            f"\n🎟 تخفیف: <code>{esc(order['discount_code'])}</code>"
            f"\n💸 مبلغ تخفیف: {format_money(order['discount_amount'])} تومان"
        )

    for admin_id in ADMIN_IDS:
        try:
            if receipt_file_id:
                await bot.send_photo(
                    admin_id,
                    receipt_file_id,
                    caption=text,
                    reply_markup=admin_payment_keyboard(order_code),
                )
            else:
                await bot.send_message(
                    admin_id,
                    text,
                    reply_markup=admin_payment_keyboard(order_code),
                )
        except TelegramForbiddenError:
            logger.warning("Admin %s blocked the bot.", admin_id)
        except Exception as exc:
            logger.warning(
                "Could not send payment %s to admin %s: %s",
                order_code,
                admin_id,
                exc,
            )


# =========================================================
# CANCEL ORDER
# =========================================================

@dp.callback_query(F.data.startswith("cancel_order:"))
async def cancel_order_callback(
    callback: CallbackQuery,
    state: FSMContext,
):
    order_code = callback.data.split(":", 1)[1]
    order = get_order(order_code)

    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    if order["user_id"] != callback.from_user.id:
        await callback.answer("این سفارش متعلق به شما نیست.", show_alert=True)
        return

    if order["status"] != "waiting_payment":
        await callback.answer(
            "این سفارش دیگر قابل لغو نیست.",
            show_alert=True,
        )
        return

    changed = set_order_status_if_current(
        order_code,
        "cancelled",
        "waiting_payment",
    )

    if not changed:
        await callback.answer(
            "سفارش قبلاً پردازش شده است.",
            show_alert=True,
        )
        return

    await state.clear()

    await callback.message.answer(
        "❌ سفارش لغو شد.",
        reply_markup=main_keyboard(),
    )
    await callback.answer()


# =========================================================
# APPROVE / REJECT PAYMENT
# =========================================================

@dp.callback_query(F.data.startswith("approve:"))
async def approve_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    order_code = callback.data.split(":", 1)[1]

    if order_code in approval_locks:
        await callback.answer("در حال پردازش...", show_alert=True)
        return

    approval_locks.add(order_code)

    try:
        order = get_order(order_code)

        if not order:
            await callback.answer("سفارش پیدا نشد.", show_alert=True)
            return

        # Atomic DB protection; this also protects multiple Railway instances.
        changed = set_order_status_if_current(
            order_code,
            "approved",
            "payment_review",
        )

        if not changed:
            await callback.answer(
                "این سفارش قبلاً پردازش شده است.",
                show_alert=True,
            )
            return

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

        await callback.message.answer(
            f"✅ پرداخت سفارش <code>{esc(order_code)}</code> تأیید شد.\n\n"
            "📡 حالا دستور زیر را بزن و سپس کانفیگ را در پیام بعدی بفرست:\n"
            f"<code>/config {esc(order_code)}</code>"
        )

        await callback.answer("پرداخت تأیید شد.")

    finally:
        approval_locks.discard(order_code)


@dp.callback_query(F.data.startswith("reject:"))
async def reject_payment(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return

    order_code = callback.data.split(":", 1)[1]
    order = get_order(order_code)

    if not order:
        await callback.answer("سفارش پیدا نشد.", show_alert=True)
        return

    changed = set_order_status_if_current(
        order_code,
        "rejected",
        "payment_review",
    )

    if not changed:
        await callback.answer(
            "این سفارش قبلاً پردازش شده است.",
            show_alert=True,
        )
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass

    try:
        await bot.send_message(
            order["user_id"],
            f"❌ پرداخت سفارش <code>{esc(order_code)}</code> رد شد.\n\n"
            "اگر فکر می‌کنی اشتباهی رخ داده، با پشتیبانی تماس بگیر.",
        )
    except TelegramForbiddenError:
        logger.info("User %s blocked the bot.", order["user_id"])
    except Exception:
        logger.exception("Could not notify rejected order user.")

    await callback.message.answer(
        f"❌ پرداخت {esc(order_code)} رد شد."
    )
    await callback.answer("پرداخت رد شد.")


# =========================================================
# CONFIG DELIVERY
# =========================================================

@dp.message(Command("config"))
async def config_command(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "فرمت صحیح:\n"
            "<code>/config ORD-XXXXXXXX</code>\n\n"
            "بعد از آن کانفیگ را در پیام بعدی بفرست."
        )
        return

    order_code = parts[1].strip().split()[0]
    order = get_order(order_code)

    if not order:
        await message.answer("❌ سفارش پیدا نشد.")
        return

    if order["status"] not in ("approved", "delivered"):
        await message.answer(
            "❌ این سفارش در وضعیت قابل تحویل نیست."
        )
        return

    await state.update_data(order_code=order_code)
    await state.set_state(AdminConfigState.waiting_for_config)

    if order["status"] == "delivered" and order["config"]:
        await message.answer(
            f"ℹ️ این سفارش قبلاً تحویل شده است.\n"
            f"اگر می‌خواهی کانفیگ ذخیره‌شده دوباره برای کاربر ارسال شود، "
            f"همان کانفیگ را مجدداً بفرست."
        )
    else:
        await message.answer(
            f"📡 کانفیگ سفارش <code>{esc(order_code)}</code> را ارسال کن."
        )


@dp.message(AdminConfigState.waiting_for_config)
async def config_received_handler(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    order_code = data.get("order_code")

    if not order_code:
        await state.clear()
        return

    order = get_order(order_code)
    if not order:
        await state.clear()
        await message.answer("❌ سفارش پیدا نشد.")
        return

    config = message.text
    if not config:
        await message.answer("❌ کانفیگ باید به صورت متن ارسال شود.")
        return

    config = config.strip()
    if not config:
        await message.answer("❌ کانفیگ خالی است.")
        return

    # If delivery was already committed, this command acts as a safe resend.
    if order["status"] == "delivered":
        service_start_text = order["service_start_date"] or order["purchase_date"]
        expiry_text = order["expiry_date"]

        try:
            await bot.send_message(
                order["user_id"],
                f"""📡 کانفیگ سرویس شما:

📦 {esc(order['service_name'])}
💾 حجم: {esc(order['volume'])}
📅 شروع سرویس: {esc(service_start_text)}
⏳ تاریخ انقضا: {esc(expiry_text)}

🔐 کانفیگ:
<code>{esc(config)}</code>"""
            )
            await message.answer(
                f"✅ کانفیگ سفارش {esc(order_code)} دوباره ارسال شد."
            )
            await state.clear()
        except Exception:
            logger.exception("Resending config failed for %s", order_code)
            await message.answer("❌ ارسال کانفیگ ناموفق بود.")
        return

    if order["status"] != "approved":
        await message.answer(
            "❌ این سفارش دیگر در وضعیت تأییدشده نیست."
        )
        return

    now = datetime.now()
    service_start = now

    if order["renewal_for_order_code"]:
        source_order = get_order(order["renewal_for_order_code"])
        if source_order:
            source_expiry = parse_datetime(source_order["expiry_date"])
            if source_expiry and source_expiry > now:
                service_start = source_expiry

    duration_days = get_service_duration(order["service_key"])
    expiry = service_start + timedelta(days=duration_days)

    purchase_date = now.strftime(DATETIME_FORMAT)
    service_start_text = service_start.strftime(DATETIME_FORMAT)
    expiry_text = expiry.strftime(DATETIME_FORMAT)

    # Commit DB first. If Telegram fails afterwards, /config can safely resend.
    finalized = finalize_order_delivery(
        order_code=order_code,
        config=config,
        purchase_date=purchase_date,
        expiry_date=expiry_text,
        service_start_date=service_start_text,
    )

    if not finalized:
        refreshed = get_order(order_code)

        if refreshed and refreshed["status"] == "delivered":
            await message.answer(
                "ℹ️ این سفارش قبلاً تحویل شده است. "
                "می‌توانی دوباره /config را اجرا کنی تا کانفیگ ارسال شود."
            )
        else:
            await message.answer(
                "❌ سفارش دیگر در وضعیت قابل تحویل نیست."
            )

        await state.clear()
        return

    try:
        await bot.send_message(
            order["user_id"],
            f"""✅ پرداخت شما تأیید شد.

📡 سرویس شما آماده است:

📦 {esc(order['service_name'])}
💾 حجم: {esc(order['volume'])}
📅 شروع سرویس: {esc(service_start_text)}
⏳ تاریخ انقضا: {esc(expiry_text)}

🔐 کانفیگ:

<code>{esc(config)}</code>

❤️ ممنون از خرید شما"""
        )

        await message.answer(
            f"✅ کانفیگ سفارش {esc(order_code)} با موفقیت ارسال شد."
        )
        await state.clear()

    except TelegramForbiddenError:
        logger.warning(
            "User %s blocked the bot after order %s was delivered.",
            order["user_id"],
            order_code,
        )
        await message.answer(
            "⚠️ سفارش در دیتابیس تحویل‌شده ثبت شد، "
            "اما کاربر امکان دریافت پیام از ربات را ندارد."
        )
        await state.clear()

    except Exception as exc:
        logger.exception(
            "Config delivery failed after DB finalization: %s",
            exc,
        )
        await message.answer(
            "⚠️ سفارش در دیتابیس تکمیل شده، اما ارسال پیام ناموفق بود.\n"
            "دوباره /config را اجرا کن تا کانفیگ مجدداً ارسال شود."
        )
        await state.clear()


# =========================================================
# USER SERVICES / ORDERS
# =========================================================

@dp.message(F.text == "📡 سرویس‌های من")
async def my_services_handler(message: Message):
    services = get_user_services(message.from_user.id)

    if not services:
        await message.answer("📡 هنوز سرویسی برای شما ثبت نشده است.")
        return

    for service in services:
        status = "🟢 فعال" if service["status"] == "delivered" else "🔴 منقضی"

        text = f"""📡 سرویس شما

{status}

📦 {esc(service['service_name'])}
💾 حجم: {esc(service['volume'])}

📅 شروع سرویس:
{esc(service['service_start_date'] or service['purchase_date'])}

⏳ انقضا:
{esc(service['expiry_date'])}

🔖 سفارش:
<code>{esc(service['order_code'])}</code>"""

        await message.answer(
            text,
            reply_markup=renew_keyboard(service["order_code"]),
        )


@dp.message(F.text == "📋 سفارش‌های من")
async def my_orders_handler(message: Message):
    orders = get_user_orders(message.from_user.id)

    if not orders:
        await message.answer("📋 هنوز سفارشی ثبت نکرده‌ای.")
        return

    status_map = {
        "waiting_payment": "⏳ منتظر پرداخت",
        "payment_review": "🔍 در حال بررسی پرداخت",
        "approved": "✅ پرداخت تأیید شده",
        "delivered": "🟢 تحویل شده",
        "expired": "🔴 منقضی شده",
        "rejected": "❌ رد شده",
        "cancelled": "🚫 لغو شده",
    }

    lines = ["📋 سفارش‌های شما:\n"]

    for order in orders[:20]:
        status = status_map.get(order["status"], order["status"])

        lines.append(
            f"🔖 <code>{esc(order['order_code'])}</code>\n"
            f"📦 {esc(order['service_name'])}\n"
            f"💰 {format_money(get_order_final_price(order))} تومان\n"
            f"📌 {esc(status)}\n"
        )

    await message.answer("\n".join(lines))


# =========================================================
# SUPPORT
# =========================================================

def remember_support_user(admin_id, user_id):
    set_setting(f"support_last_user_{admin_id}", user_id)


@dp.message(F.text == "🎧 پشتیبانی")
async def support_handler(message: Message, state: FSMContext):
    await state.set_state(SupportState.waiting_for_message)
    await message.answer(
        "🎧 پیام خودت رو برای پشتیبانی بفرست.\n\n"
        "پیامت به یکی از پشتیبان‌ها ارسال میشه."
    )


@dp.message(SupportState.waiting_for_message)
async def support_message_handler(
    message: Message,
    state: FSMContext,
):
    selected_admin = get_next_support_admin(ADMIN_IDS)

    if not selected_admin:
        await message.answer("❌ در حال حاضر پشتیبانی در دسترس نیست.")
        return

    user = message.from_user
    user_name = esc(user.first_name or "")
    admin_text = f"""🎧 پیام جدید پشتیبانی

👤 کاربر:
{user_name}

🆔 آیدی:
<code>{user.id}</code>

💬 پیام:"""

    delivered_to = None

    async def send_to_admin(admin_id):
        if message.photo:
            caption = admin_text
            if message.caption:
                caption += f"\n\n{esc(message.caption)}"
            await bot.send_photo(
                admin_id,
                message.photo[-1].file_id,
                caption=caption,
            )
        elif message.text:
            await bot.send_message(
                admin_id,
                admin_text + f"\n\n{esc(message.text)}",
            )
        else:
            await bot.send_message(admin_id, admin_text)

    try:
        await send_to_admin(selected_admin)
        delivered_to = selected_admin
    except Exception as exc:
        logger.warning(
            "Primary support admin %s failed: %s",
            selected_admin,
            exc,
        )

    if delivered_to is None:
        for fallback_admin in ADMIN_IDS:
            if fallback_admin == selected_admin:
                continue

            try:
                await send_to_admin(fallback_admin)
                delivered_to = fallback_admin
                break
            except Exception:
                continue

    if delivered_to is None:
        await message.answer("❌ ارسال پیام به پشتیبانی ناموفق بود.")
        return

    remember_support_user(delivered_to, user.id)

    await message.answer("✅ پیام شما برای پشتیبانی ارسال شد.")
    await state.clear()


@dp.message(Command("reply"))
async def admin_reply_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "فرمت صحیح:\n"
            "<code>/reply متن پاسخ</code>"
        )
        return

    admin_id = message.from_user.id
    user_id_text = get_setting(f"support_last_user_{admin_id}")

    if not user_id_text:
        await message.answer(
            "❌ هنوز کاربری برای پاسخ مستقیم ثبت نشده."
        )
        return

    try:
        user_id = int(user_id_text)
    except ValueError:
        await message.answer("❌ اطلاعات کاربر نامعتبر است.")
        return

    reply_text = parts[1]

    try:
        await bot.send_message(
            user_id,
            f"🎧 پاسخ پشتیبانی:\n\n{esc(reply_text)}",
        )
        await message.answer("✅ پاسخ برای کاربر ارسال شد.")
    except TelegramForbiddenError:
        await message.answer(
            "❌ امکان ارسال پیام به این کاربر وجود ندارد."
        )
    except Exception as exc:
        logger.error("Support reply error: %s", exc)
        await message.answer("❌ ارسال پاسخ ناموفق بود.")


# =========================================================
# ADMIN - ORDERS / PAYMENTS / USERS / SERVICES / STATS
# =========================================================

@dp.message(F.text == "📦 سفارش‌های جدید")
async def admin_new_orders(message: Message):
    if not is_admin(message.from_user.id):
        return

    orders = [
        order
        for order in get_all_orders()
        if order["status"] in ("payment_review", "approved")
    ]

    if not orders:
        await message.answer("📦 سفارش جدیدی وجود ندارد.")
        return

    status_map = {
        "payment_review": "🔍 در انتظار بررسی پرداخت",
        "approved": "✅ پرداخت تأیید شده / منتظر کانفیگ",
    }

    for order in orders[:20]:
        await message.answer(
            f"""📦 سفارش

🔖 <code>{esc(order['order_code'])}</code>
👤 <code>{order['user_id']}</code>

📦 {esc(order['service_name'])}
💰 {format_money(get_order_final_price(order))} تومان

📌 وضعیت: {status_map.get(order['status'], order['status'])}"""
        )


@dp.message(F.text == "💳 پرداخت‌های در انتظار")
async def admin_pending_payments(message: Message):
    if not is_admin(message.from_user.id):
        return

    orders = get_pending_orders()

    if not orders:
        await message.answer("💳 پرداخت در انتظاری وجود ندارد.")
        return

    for order in orders[:20]:
        text = f"""💳 پرداخت در انتظار

🔖 <code>{esc(order['order_code'])}</code>
👤 <code>{order['user_id']}</code>

📦 {esc(order['service_name'])}
💰 {format_money(get_order_final_price(order))} تومان"""

        if order["receipt_file_id"]:
            try:
                await bot.send_photo(
                    message.from_user.id,
                    order["receipt_file_id"],
                    caption=text,
                    reply_markup=admin_payment_keyboard(order["order_code"]),
                )
            except Exception:
                await message.answer(
                    text,
                    reply_markup=admin_payment_keyboard(order["order_code"]),
                )
        else:
            await message.answer(
                text,
                reply_markup=admin_payment_keyboard(order["order_code"]),
            )


@dp.message(F.text == "👥 کاربران")
async def admin_users(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(f"👥 تعداد کاربران: {get_user_count()}")


@dp.message(F.text == "📡 مدیریت سرویس‌ها")
async def admin_services(message: Message):
    if not is_admin(message.from_user.id):
        return

    services = get_services()
    if not services:
        await message.answer("📡 هیچ سرویسی ثبت نشده است.")
        return

    lines = ["📡 سرویس‌ها:\n"]

    for service in services:
        status = "🟢 فعال" if service["active"] else "🔴 غیرفعال"
        lines.append(
            f"{status}\n"
            f"🔑 {esc(service['service_key'])}\n"
            f"📦 {esc(service['name'])}\n"
            f"💰 {format_money(service['price'])} تومان\n"
            f"⏳ {service['duration_days']} روز\n"
        )

    await message.answer("\n".join(lines))


@dp.message(F.text == "📊 آمار فروش")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return

    total_orders = get_order_count()
    users = get_user_count()
    delivered = get_order_count_by_status("delivered")
    expired = get_order_count_by_status("expired")
    pending = get_order_count_by_status("payment_review")
    sales = get_total_sales()

    await message.answer(
        f"""📊 آمار فروش

👥 کاربران: {users}
📦 کل سفارش‌ها: {total_orders}
🟢 تحویل‌شده: {delivered}
🔴 منقضی‌شده: {expired}
💳 در انتظار بررسی پرداخت: {pending}

💰 مجموع فروش:
{format_money(sales)} تومان"""
    )


# =========================================================
# ADMIN SEARCH
# =========================================================

@dp.message(F.text == "🔍 جستجوی سفارش")
async def admin_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(AdminSearchState.waiting_for_query)
    await message.answer("🔍 کد سفارش، آیدی کاربر یا نام سرویس را بفرست.")


@dp.message(AdminSearchState.waiting_for_query)
async def admin_search_result(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    if not message.text:
        await message.answer("❌ عبارت جستجو باید متنی باشد.")
        return

    rows = search_orders(message.text.strip())

    if not rows:
        await message.answer("❌ نتیجه‌ای پیدا نشد.")
        await state.clear()
        return

    for order in rows[:20]:
        await message.answer(
            f"🔖 <code>{esc(order['order_code'])}</code>\n"
            f"👤 <code>{order['user_id']}</code>\n"
            f"📦 {esc(order['service_name'])}\n"
            f"💰 {format_money(get_order_final_price(order))} تومان\n"
            f"📌 {esc(order['status'])}"
        )

    await state.clear()


# =========================================================
# DISCOUNTS
# =========================================================

@dp.message(F.text == "🎟 کدهای تخفیف")
async def admin_discounts(message: Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        """🎟 مدیریت کد تخفیف

برای ساخت کد:

<code>/discount CODE percent VALUE MAX_USE EXPIRY</code>

مثال:
<code>/discount OFF20 percent 20 100 0</code>

مبلغ ثابت:
<code>/discount OFF50000 fixed 50000 10 2026-12-31 23:59:59</code>

MAX_USE = 0 یعنی بدون محدودیت.
EXPIRY = 0 یعنی بدون تاریخ انقضا.

مشاهده:
<code>/discounts</code>

فعال/غیرفعال:
<code>/discount_toggle CODE</code>"""
    )


@dp.message(Command("discount"))
async def create_discount_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) < 6:
        await message.answer(
            "فرمت:\n"
            "<code>/discount CODE percent VALUE MAX_USE EXPIRY</code>"
        )
        return

    code = parts[1].strip().upper()
    discount_type = parts[2].lower()

    try:
        value = int(parts[3])
        max_uses = int(parts[4])
    except ValueError:
        await message.answer(
            "❌ مقدار تخفیف و تعداد استفاده باید عدد باشند."
        )
        return

    if value < 0 or max_uses < 0:
        await message.answer("❌ مقدارها نمی‌توانند منفی باشند.")
        return

    if discount_type == "percent" and value > 100:
        await message.answer("❌ درصد تخفیف نمی‌تواند بیشتر از ۱۰۰ باشد.")
        return

    expiry_raw = " ".join(parts[5:])

    if expiry_raw == "0":
        expiry = None
    else:
        expiry_dt = parse_datetime(expiry_raw)
        if expiry_dt is None:
            await message.answer(
                "❌ فرمت تاریخ صحیح نیست.\n"
                "<code>2026-12-31 23:59:59</code>"
            )
            return
        expiry = expiry_raw

    if discount_type not in ("percent", "fixed"):
        await message.answer(
            "❌ نوع تخفیف باید percent یا fixed باشد."
        )
        return

    try:
        create_discount_code(
            code,
            discount_type,
            value,
            max_uses,
            expiry,
        )
    except sqlite3.IntegrityError:
        await message.answer("❌ این کد تخفیف قبلاً وجود دارد.")
        return
    except Exception:
        logger.exception("Creating discount failed.")
        await message.answer("❌ ساخت کد تخفیف ناموفق بود.")
        return

    await message.answer(
        f"✅ کد تخفیف <code>{esc(code)}</code> ساخته شد."
    )


@dp.message(Command("discounts"))
async def list_discounts_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    rows = get_discount_codes()
    if not rows:
        await message.answer("🎟 کد تخفیفی وجود ندارد.")
        return

    lines = ["🎟 کدهای تخفیف:\n"]

    for row in rows:
        status = "🟢 فعال" if row["active"] else "🔴 غیرفعال"
        lines.append(
            f"{status}\n"
            f"کد: <code>{esc(row['code'])}</code>\n"
            f"نوع: {esc(row['discount_type'])}\n"
            f"مقدار: {row['discount_value']}\n"
            f"استفاده: {row['used_count']}/{row['max_uses'] or '∞'}\n"
            f"انقضا: {esc(row['expires_at'] or 'ندارد')}\n"
        )

    await message.answer("\n".join(lines))


@dp.message(Command("discount_toggle"))
async def toggle_discount_handler(message: Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer(
            "فرمت:\n"
            "<code>/discount_toggle CODE</code>"
        )
        return

    if not toggle_discount(parts[1]):
        await message.answer("❌ کد تخفیف پیدا نشد.")
        return

    await message.answer("✅ وضعیت کد تخفیف تغییر کرد.")


# =========================================================
# BROADCAST
# =========================================================

@dp.message(F.text == "📢 پیام همگانی")
async def broadcast_start(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(BroadcastState.waiting_for_message)
    await message.answer("📢 متن پیام همگانی را ارسال کن.")


async def send_with_retry(user_id, text, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            await bot.send_message(user_id, text)
            return True

        except TelegramRetryAfter as exc:
            delay = max(1, int(exc.retry_after))
            logger.warning(
                "Telegram rate limit: sleeping %s seconds.",
                delay,
            )
            await asyncio.sleep(delay)

        except TelegramForbiddenError:
            return False

        except TelegramBadRequest:
            return False

        except Exception as exc:
            if attempt == max_attempts - 1:
                logger.warning(
                    "Broadcast send failed for %s: %s",
                    user_id,
                    exc,
                )
                return False
            await asyncio.sleep(2 ** attempt)

    return False


@dp.message(BroadcastState.waiting_for_message)
async def broadcast_send(
    message: Message,
    state: FSMContext,
):
    if not is_admin(message.from_user.id):
        return

    if not message.text:
        await message.answer(
            "❌ فعلاً پیام همگانی فقط به صورت متن پشتیبانی می‌شود."
        )
        return

    users = get_all_users()
    success = 0
    failed = 0
    text = f"📢 پیام مدیریت:\n\n{esc(message.text)}"

    # Safer default than 20 msg/sec. Telegram may still return RetryAfter,
    # which is handled by send_with_retry().
    for user in users:
        if await send_with_retry(user["user_id"], text):
            success += 1
        else:
            failed += 1

        await asyncio.sleep(0.08)

    await message.answer(
        f"📢 ارسال پیام تمام شد.\n\n"
        f"✅ موفق: {success}\n"
        f"❌ ناموفق: {failed}"
    )
    await state.clear()


# =========================================================
# EXPIRY / REMINDERS
# =========================================================

async def expiry_loop():
    while True:
        try:
            now = datetime.now()
            now_text = now.strftime(DATETIME_FORMAT)
            services = get_services_for_expiry_check()

            for order in services:
                expiry = parse_datetime(order["expiry_date"])
                if expiry is None:
                    continue

                # A delivered renewal should suppress reminders/expiry notice
                # for the old service.
                has_renewal = has_scheduled_renewal(
                    order["order_code"],
                    order["expiry_date"],
                    now_text,
                )

                if expiry <= now:
                    if has_renewal:
                        mark_expired(order["order_code"])
                        continue

                    if not order["expired_notified"]:
                        try:
                            await bot.send_message(
                                order["user_id"],
                                f"""🔴 سرویس شما منقضی شد.

📦 {esc(order['service_name'])}
⏳ تاریخ انقضا:
{esc(order['expiry_date'])}

برای فعال‌سازی مجدد می‌توانید سرویس را تمدید کنید."""
                            )

                            mark_expired(order["order_code"])
                            mark_expired_notified(order["order_code"])

                        except TelegramForbiddenError:
                            # The user blocked the bot. Mark as expired
                            # anyway so the same notification is not retried forever.
                            mark_expired(order["order_code"])
                            mark_expired_notified(order["order_code"])

                        except Exception as exc:
                            logger.warning(
                                "Expiry notification failed for %s: %s",
                                order["order_code"],
                                exc,
                            )

                    continue

                remaining = expiry - now

                if (
                    not has_renewal
                    and remaining <= timedelta(days=3)
                    and remaining > timedelta(days=1)
                    and not order["reminder_3_sent"]
                ):
                    try:
                        await bot.send_message(
                            order["user_id"],
                            f"""⚠️ یادآوری سرویس

📦 {esc(order['service_name'])}

حدود ۳ روز تا پایان اعتبار سرویس شما باقی مانده.

⏳ تاریخ انقضا:
{esc(order['expiry_date'])}"""
                        )
                        mark_reminder_sent(order["order_code"], 3)

                    except TelegramForbiddenError:
                        mark_reminder_sent(order["order_code"], 3)

                    except Exception as exc:
                        logger.warning(
                            "3-day reminder failed for %s: %s",
                            order["order_code"],
                            exc,
                        )

                elif (
                    not has_renewal
                    and remaining <= timedelta(days=1)
                    and remaining > timedelta(0)
                    and not order["reminder_1_sent"]
                ):
                    try:
                        await bot.send_message(
                            order["user_id"],
                            f"""🚨 هشدار پایان سرویس

📦 {esc(order['service_name'])}

کمتر از ۲۴ ساعت تا پایان اعتبار سرویس شما باقی مانده.

⏳ تاریخ انقضا:
{esc(order['expiry_date'])}"""
                        )
                        mark_reminder_sent(order["order_code"], 1)

                    except TelegramForbiddenError:
                        mark_reminder_sent(order["order_code"], 1)

                    except Exception as exc:
                        logger.warning(
                            "24-hour reminder failed for %s: %s",
                            order["order_code"],
                            exc,
                        )

        except asyncio.CancelledError:
            logger.info("Expiry loop cancelled.")
            raise

        except Exception as exc:
            logger.exception("Expiry loop error: %s", exc)

        await asyncio.sleep(15 * 60)


# =========================================================
# CANCEL COMMAND
# =========================================================

@dp.message(Command("cancel"))
async def cancel_command(
    message: Message,
    state: FSMContext,
):
    data = await state.get_data()
    order_code = data.get("order_code")

    if order_code:
        order = get_order(order_code)

        if (
            order
            and order["user_id"] == message.from_user.id
            and order["status"] == "waiting_payment"
        ):
            set_order_status_if_current(
                order_code,
                "cancelled",
                "waiting_payment",
            )

    await state.clear()

    await message.answer(
        "❌ عملیات لغو شد.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# ERROR HANDLER
# =========================================================

@dp.errors()
async def error_handler(event):
    logger.exception(
        "Unhandled bot error: %s",
        event.exception,
    )


# =========================================================
# MAIN
# =========================================================

async def main():
    logger.info("Starting bot...")

    init_db()
    seed_services(DEFAULT_SERVICES)

    expiry_task = asyncio.create_task(expiry_loop())

    try:
        logger.info("Bot started successfully.")
        await dp.start_polling(bot)

    finally:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by KeyboardInterrupt.")
