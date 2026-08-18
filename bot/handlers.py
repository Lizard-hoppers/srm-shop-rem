from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from bot.config import MINIAPP_URL
from core import auth as core_auth
from core import clients as core_clients
from core import qr as core_qr
from core.storage import get_conn

router = Router()

STAFF_WELCOME = (
    "Здравствуйте! Нажмите кнопку ниже, чтобы открыть CRM сервис-центра.\n\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n"
    "Если доступ ещё не открыт — пришлите этот ID владельцу."
)

CLIENT_ASK_PHONE = (
    "Здравствуйте! Это бот сервис-центра.\n\n"
    "Поделитесь номером телефона, чтобы получить карту скидок — при следующем визите "
    "мы найдём вас по QR-коду за секунду."
)

CLIENT_WELCOME_BACK = "С возвращением! Вот ваша карта скидок — покажите её в сервисе."
CLIENT_REGISTERED = "Готово! Вот ваша карта скидок — покажите её в сервисе."


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message) -> None:
    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
        client = None if staff else core_clients.get_by_telegram_id(conn, message.from_user.id)

    if staff:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=MINIAPP_URL))]]
        )
        await message.answer(STAFF_WELCOME.format(user_id=message.from_user.id), reply_markup=keyboard)
        return

    if client:
        await _send_card(message, client["id"], CLIENT_WELCOME_BACK)
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(CLIENT_ASK_PHONE, reply_markup=keyboard)


@router.message(Command("chatid"))
async def chat_id_cmd(message: Message) -> None:
    """Onboarding helper: add the bot to the staff group as admin, send this
    there, and put the returned id into CRM_STAFF_GROUP_CHAT_ID in .env —
    that's the chat new-repair cards get posted to. Commands reach the bot
    in groups regardless of privacy mode, so no extra bot setup is needed."""
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(F.contact, F.chat.type == "private")
async def got_contact(message: Message) -> None:
    """Private-chat only — the bot is also a member of staff groups
    ("Работа", "Мастера 007"), and without this filter, staff sharing a
    *client's* contact card into a group topic (to note it for a repair,
    say) made the bot reply there with the client-registration flow's
    "share your own number" message, which makes no sense outside the
    bot's own DM onboarding."""
    if not message.contact or message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, поделитесь своим собственным номером — кнопкой ниже.")
        return

    name = message.from_user.full_name or "Клиент"
    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, name, message.contact.phone_number, source="online")
        core_clients.link_telegram(conn, client_id, message.from_user.id)

    await _send_card(message, client_id, CLIENT_REGISTERED, remove_keyboard=True)


async def _send_card(message: Message, client_id: int, caption: str, remove_keyboard: bool = False) -> None:
    png = core_qr.generate_png(core_qr.client_code(client_id))
    photo = BufferedInputFile(png, filename="card.png")
    kwargs = {"reply_markup": ReplyKeyboardRemove()} if remove_keyboard else {}
    await message.answer_photo(photo, caption=f"{caption}\n\nНомер карты: №{client_id}", **kwargs)
