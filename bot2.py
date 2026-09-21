import asyncio
import logging
import os
import re
import aiosqlite
import random
import string

from aiogram import Bot, Dispatcher
from aiogram.types import ChatJoinRequest, Message
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.filters import Command, CommandObject
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MANAGER_ID = os.getenv("MANAGER_ID")
LOOK_ID = os.getenv("LOOK_ID")
DB_PATH = "bot.db"

if not BOT_TOKEN or not CHANNEL_ID or not MANAGER_ID:
    raise RuntimeError("Проверь .env файл!")

CHANNEL_ID = int(CHANNEL_ID)
MANAGER_ID = int(MANAGER_ID)

logging.basicConfig(level=logging.INFO)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)


class FormBasic(StatesGroup):
    name = State()
    referrer = State()
    age = State()


class FormEvent(StatesGroup):
    gender = State()
    referral_code = State()


# ==================== РАБОТА С БД ====================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'pending',
                tg_username TEXT,
                tg_full_name TEXT,
                form_name TEXT,
                form_referrer TEXT,
                form_age TEXT,
                gender TEXT,
                base_price INTEGER DEFAULT 500,
                final_price INTEGER DEFAULT 500,
                used_referral_code TEXT,
                admin_message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS referral_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                code TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                code TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            )
        """)

        migrations = [
            "ALTER TABLE users ADD COLUMN final_price INTEGER DEFAULT 500",
            "ALTER TABLE users ADD COLUMN used_referral_code TEXT",
            "ALTER TABLE users ADD COLUMN admin_message_id INTEGER",
        ]
        for m in migrations:
            try:
                await db.execute(m)
            except aiosqlite.OperationalError:
                pass

        await db.commit()

async def get_all_referral_codes():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT
                rc.code,
                rc.user_id,
                u.tg_username,
                u.tg_full_name,
                u.base_price,
                u.final_price,
                COUNT(r.id) AS referral_count
            FROM referral_codes rc
            LEFT JOIN users u ON u.user_id = rc.user_id
            LEFT JOIN referrals r ON r.referrer_id = rc.user_id
            GROUP BY rc.id
            ORDER BY rc.created_at DESC
        """)
        return await cursor.fetchall()

async def cancel_user_registration(user_id: int) -> tuple[bool, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        referrer_id = row[0] if row else None

        await db.execute(
            "DELETE FROM referral_codes WHERE user_id = ?",
            (user_id,)
        )

        await db.execute(
            "UPDATE users SET status = 'pending', updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )

        await db.commit()

        if referrer_id:
            discount, new_price = await recalculate_referrer_price(referrer_id)
            return True, f"Рефереру {referrer_id} пересчитана цена: {new_price} ₽"

        return True, "Регистрация отменена."

async def get_referral_code_info(code: str) -> dict | None:
    clean_code = code.strip().upper()
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT rc.code, rc.user_id, u.tg_username, u.tg_full_name, 
                   u.status, u.final_price, u.base_price, rc.created_at
            FROM referral_codes rc
            JOIN users u ON rc.user_id = u.user_id
            WHERE rc.code = ?
        """, (clean_code,))
        owner_row = await cursor.fetchone()
        
        if not owner_row:
            return None
        
        owner = {
            "code": owner_row[0],
            "user_id": owner_row[1],
            "username": owner_row[2],
            "full_name": owner_row[3],
            "status": owner_row[4],
            "final_price": owner_row[5],
            "base_price": owner_row[6],
            "created_at": owner_row[7]
        }

        cursor = await db.execute("""
            SELECT r.referred_id, u.tg_username, u.tg_full_name, 
                   u.status, u.final_price, r.created_at
            FROM referrals r
            JOIN users u ON r.referred_id = u.user_id
            WHERE r.code = ?
            ORDER BY r.created_at ASC
        """, (clean_code,))
        rows = await cursor.fetchall()
        
        referrals = []
        for row in rows:
            referrals.append({
                "user_id": row[0],
                "username": row[1],
                "full_name": row[2],
                "status": row[3],
                "final_price": row[4],
                "created_at": row[5]
            })
        
        return {
            "owner": owner,
            "referrals": referrals
        }

async def add_user(user_id: int, tg_username: str, tg_full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, tg_username, tg_full_name)
            VALUES (?, ?, ?)
        """, (user_id, tg_username, tg_full_name))
        await db.commit()


async def get_user_status(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_user_data(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT form_name, form_referrer, form_age, gender,
                   base_price, final_price, status, tg_username, used_referral_code
            FROM users WHERE user_id = ?
        """, (user_id,))
        row = await cursor.fetchone()
        if row:
            return {
                "form_name": row[0],
                "form_referrer": row[1],
                "form_age": row[2],
                "gender": row[3],
                "base_price": row[4],
                "final_price": row[5],
                "status": row[6],
                "tg_username": row[7],
                "used_referral_code": row[8]
            }
        return None


async def save_basic_form(user_id: int, name: str, referrer: str, age: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET form_name=?, form_referrer=?, form_age=?,
            status='filled', updated_at=CURRENT_TIMESTAMP WHERE user_id=?
        """, (name, referrer, age, user_id))
        await db.commit()


async def save_event_data(user_id: int, gender: str, base_price: int, final_price: int, used_code: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET gender=?, base_price=?, final_price=?, used_referral_code=?,
            status='waiting_payment', updated_at=CURRENT_TIMESTAMP WHERE user_id=?
        """, (gender, base_price, final_price, used_code, user_id))
        await db.commit()


async def save_admin_message_id(user_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET admin_message_id = ? WHERE user_id = ?",
            (message_id, user_id)
        )
        await db.commit()
        
async def get_referrer_username_by_code(code: str) -> str | None:
    """Находит юзернейм того, кому принадлежит реферальный код"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u.tg_username FROM referral_codes rc
            JOIN users u ON rc.user_id = u.user_id
            WHERE rc.code = ?
        """, (code,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def get_admin_message_id(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT admin_message_id FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def get_referral_code(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT code FROM referral_codes WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def build_admin_message(user_id: int) -> str:
    """Собирает актуальный текст сообщения для чата админов"""
    user_data = await get_user_data(user_id)
    if not user_data:
        return ""

    own_code = await get_referral_code(user_id)
    username = f"@{user_data['tg_username']}" if user_data['tg_username'] and user_data['tg_username'] != "нет" else "нет"
    gender_text = "Мужской" if user_data['gender'] == "male" else "Женский"

    text = (
        f"🎫 <b>Новая регистрация на мероприятие!</b>\n\n"
        f"👤 <b>ФИО:</b> {user_data['form_name']}\n"
        f"🎂 <b>Возраст:</b> {user_data['form_age']}\n"
        f"👫 <b>Пол:</b> {gender_text}\n"
        f"📱 <b>Username:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"💰 <b>К оплате:</b> {user_data['final_price']} ₽\n"
    )

    if user_data['used_referral_code']:
        referrer_username = await get_referrer_username_by_code(user_data['used_referral_code'])
        referrer_text = f"@{referrer_username}" if referrer_username else "неизвестно"
        text += f"🎁 <b>Пришёл по коду:</b> {user_data['used_referral_code']} (скидка 100₽, {referrer_text})\n"
    if own_code:
        text += f"🔑 <b>Реф. код:</b> {own_code}\n"

    text += f"\n<i>Для подтверждения оплаты: /pay {username}</i>"

    return text


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


async def generate_referral_code(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT code FROM referral_codes WHERE user_id = ?", (user_id,))
        existing = await cursor.fetchone()
        if existing:
            return existing[0]

        while True:
            suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
            code = f"GPC{suffix}"
            cursor = await db.execute("SELECT 1 FROM referral_codes WHERE code = ?", (code,))
            if not await cursor.fetchone():
                break

        await db.execute("INSERT INTO referral_codes (user_id, code) VALUES (?, ?)", (user_id, code))
        await db.commit()
        return code


async def use_referral_code(referred_id: int, code: str) -> tuple[bool, str, int | None]:
    code = code.strip().upper()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM referral_codes WHERE code = ?", (code,))
        row = await cursor.fetchone()
        if not row:
            return False, "Такого реферального кода не существует.", None

        referrer_id = row[0]
        if referrer_id == referred_id:
            return False, "Нельзя использовать собственный код.", None

        cursor = await db.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (referred_id,))
        if await cursor.fetchone():
            return False, "Ты уже использовал реферальный код.", None

        try:
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id, code) VALUES (?, ?, ?)",
                (referrer_id, referred_id, code)
            )
            await db.commit()
            return True, f"Код {code} применён!", referrer_id
        except aiosqlite.IntegrityError:
            return False, "Ошибка применения кода.", None


async def recalculate_referrer_price(referrer_id: int) -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT COUNT(*) FROM referrals r
            JOIN users u ON r.referred_id = u.user_id
            WHERE r.referrer_id = ? AND u.status = 'paid'
        """, (referrer_id,))
        row = await cursor.fetchone()
        paid_count = row[0] if row else 0

        paid_count = min(paid_count, 5)
        discount = paid_count * 50

        cursor = await db.execute("SELECT base_price FROM users WHERE user_id = ?", (referrer_id,))
        base_row = await cursor.fetchone()
        base_price = base_row[0] if base_row and base_row[0] else 500

        new_price = max(0, base_price - discount)

        await db.execute(
            "UPDATE users SET final_price = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (new_price, referrer_id)
        )
        await db.commit()

        return discount, new_price


async def confirm_payment_by_username(username: str) -> tuple[bool, str]:
    clean_username = username.lstrip('@')

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, status, form_name, final_price FROM users WHERE tg_username = ?",
            (clean_username,)
        )
        row = await cursor.fetchone()

        if not row:
            return False, f"❌ Пользователь @{clean_username} не найден в БД."

        user_id, status, full_name, final_price = row

        if status == 'paid':
            return False, f"⚠️ Бронь {full_name} уже оплачена."

        if status not in ['waiting_payment', 'filled']:
            return False, f"⚠️ Пользователь {full_name} не ожидает оплату."

        await db.execute(
            "UPDATE users SET status = 'paid', updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )

        cursor = await db.execute("SELECT referrer_id FROM referrals WHERE referred_id = ?", (user_id,))
        ref_row = await cursor.fetchone()
        referrer_id = ref_row[0] if ref_row else None

        await db.commit()

    # Уведомление пользователю
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"Ты официально зарегистрирован на мероприятие. Ждем тебя! 🎉",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить уведомление пользователю {user_id}")
    except TelegramForbiddenError:
        logging.warning(f"Пользователь {user_id} заблокировал бота")
    # Если был реферер
    if referrer_id:
        discount, new_price = await recalculate_referrer_price(referrer_id)

        # Уведомление рефереру в личку
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 <b>Отличные новости!</b>\n\n"
                f"Твой друг <b>{full_name}</b> (@{clean_username}) оплатил участие по твоему коду!\n"
                f"💸 Тебе начислена скидка <b>{discount} ₽</b>.\n\n"
                f"💰 Цена твоего билета: <b>{new_price} ₽</b>",
                parse_mode="HTML"
            )
        except Exception:
            logging.warning(f"Не удалось отправить уведомление рефереру {referrer_id}")

        # Редактируем сообщение в чате админов
        admin_msg_id = await get_admin_message_id(referrer_id)
        if admin_msg_id:
            try:
                new_text = await build_admin_message(referrer_id)
                await bot.edit_message_text(
                    chat_id=MANAGER_ID,
                    message_id=admin_msg_id,
                    text=new_text,
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.warning(f"Не удалось отредактировать сообщение в чате админов: {e}")

        return True, (
            f"✅ Оплата подтверждена для {full_name} (@{clean_username}).\n"
            f"💸 Скидка рефереру: {discount} ₽. Новая цена билета: {new_price} ₽"
        )

    return True, f"✅ Оплата подтверждена для {full_name} (@{clean_username})."

async def unconfirm_payment_by_username(username: str) -> tuple[bool, str]:
    clean_username = username.lstrip('@')
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, status, form_name, final_price FROM users WHERE tg_username = ?",
            (clean_username,)
        )
        row = await cursor.fetchone()
        
        if not row:
            return False, f"❌ Пользователь @{clean_username} не найден в БД."
        
        user_id, status, full_name, final_price = row
        
        if status != 'paid':
            return False, f"⚠️ Бронь {full_name} не оплачена (статус: {status})."

        await db.execute(
            "UPDATE users SET status = 'waiting_payment', updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,)
        )

        cursor = await db.execute(
            "SELECT referrer_id FROM referrals WHERE referred_id = ?",
            (user_id,)
        )
        ref_row = await cursor.fetchone()
        referrer_id = ref_row[0] if ref_row else None
        
        await db.commit()

    try:
        await bot.send_message(
            user_id,
            f"⚠️ <b>Подтверждение оплаты отменено.</b>\n\n"
            f"Пожалуйста, свяжись с администратором для уточнения деталей.",
            parse_mode="HTML"
        )
    except TelegramForbiddenError:
        logging.warning(f"Пользователь {user_id} заблокировал бота")
    except Exception as e:
        logging.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

    if referrer_id:
        discount, new_price = await recalculate_referrer_price(referrer_id)

        try:
            await bot.send_message(
                referrer_id,
                f"⚠️ <b>Важно!</b>\n\n"
                f"Оплата твоего друга <b>{full_name}</b> (@{clean_username}) была отменена.\n"
                f"💸 Скидка по его коду аннулирована.\n\n"
                f"💰 Новая цена твоего билета: <b>{new_price} ₽</b>",
                parse_mode="HTML"
            )
        except TelegramForbiddenError:
            logging.warning(f"Реферер {referrer_id} заблокировал бота")
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление рефереру {referrer_id}: {e}")

        admin_msg_id = await get_admin_message_id(referrer_id)
        if admin_msg_id:
            try:
                new_text = await build_admin_message(referrer_id)
                await bot.edit_message_text(
                    chat_id=MANAGER_ID,
                    message_id=admin_msg_id,
                    text=new_text,
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.warning(f"Не удалось отредактировать сообщение реферера: {e}")

    admin_msg_id = await get_admin_message_id(user_id)
    if admin_msg_id:
        try:
            new_text = await build_admin_message(user_id)
            await bot.edit_message_text(
                chat_id=MANAGER_ID,
                message_id=admin_msg_id,
                text=new_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.warning(f"Не удалось отредактировать сообщение пользователя: {e}")
    
    if referrer_id:
        return True, (
            f"✅ Оплата отменена для {full_name} (@{clean_username}).\n"
            f"💸 Скидка у реферера уменьшена. Новая цена билета реферера: {new_price} ₽"
        )
    
    return True, f"✅ Оплата отменена для {full_name} (@{clean_username})."

async def get_user_full_info(username: str) -> dict | None:
    clean_username = username.lstrip('@')
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT user_id, status, tg_username, tg_full_name, 
                   form_name, form_referrer, form_age, gender,
                   base_price, final_price, used_referral_code,
                   created_at, updated_at
            FROM users WHERE tg_username = ?
        """, (clean_username,))
        row = await cursor.fetchone()
        
        if not row:
            return None
        
        user_info = {
            "user_id": row[0],
            "status": row[1],
            "tg_username": row[2],
            "tg_full_name": row[3],
            "form_name": row[4],
            "form_referrer": row[5],
            "form_age": row[6],
            "gender": row[7],
            "base_price": row[8],
            "final_price": row[9],
            "used_referral_code": row[10],
            "created_at": row[11],
            "updated_at": row[12]
        }

        cursor = await db.execute(
            "SELECT code FROM referral_codes WHERE user_id = ?",
            (user_info["user_id"],)
        )
        code_row = await cursor.fetchone()
        user_info["own_code"] = code_row[0] if code_row else None

        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
            (user_info["user_id"],)
        )
        count_row = await cursor.fetchone()
        user_info["referral_count"] = count_row[0] if count_row else 0

        cursor = await db.execute("""
            SELECT r.code, u.user_id, u.tg_username, u.tg_full_name
            FROM referrals r
            JOIN users u ON r.referrer_id = u.user_id
            WHERE r.referred_id = ?
        """, (user_info["user_id"],))
        referrer_row = await cursor.fetchone()
        
        if referrer_row:
            user_info["used_referral_code"] = referrer_row[0]  # Обновляем код из referrals
            user_info["invited_by"] = {
                "user_id": referrer_row[1],
                "username": referrer_row[2],
                "full_name": referrer_row[3]
            }
        elif user_info["used_referral_code"]:
            cursor = await db.execute("""
                SELECT user_id, tg_username, tg_full_name 
                FROM users 
                WHERE user_id IN (
                    SELECT user_id FROM referral_codes WHERE code = ?
                )
            """, (user_info["used_referral_code"],))
            fallback_row = await cursor.fetchone()
            if fallback_row:
                user_info["invited_by"] = {
                    "user_id": fallback_row[0],
                    "username": fallback_row[1],
                    "full_name": fallback_row[2]
                }
            else:
                user_info["invited_by"] = None
        else:
            user_info["invited_by"] = None
        
        return user_info


# ==================== ОБРАБОТЧИКИ ====================

@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    if request.chat.id != CHANNEL_ID:
        return

    user_id = request.from_user.id
    user_status = await get_user_status(user_id)

    if user_status in ["filled", "waiting_payment", "paid"]:
        return

    await add_user(user_id, request.from_user.username or "нет", request.from_user.full_name or "не указано")

    try:
        await bot.send_message(
            chat_id=user_id,
            text="Привет! Увидели твою заявку в GPC 👋\n\nПодскажи, как тебя зовут? (имя фамилия)"
        )
        state = FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id))
        await state.set_state(FormBasic.name)
    except TelegramForbiddenError:
        logging.warning("Пользователь %s не начал диалог с ботом", user_id)


@dp.message(FormBasic.name)
async def process_name(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправь ответ текстовым сообщением 🙂")
        return

    if not re.match(r"^[a-zA-Zа-яА-ЯёЁ0-9\s-]+$", message.text):
        await message.answer(
            "⚠️ В имени нельзя использовать специальные символы (знаки препинания, смайлики и т.д.). "
            "Пожалуйста, введи имя заново!"
        )
        return

    await state.update_data(name=message.text.strip())
    await message.answer("От кого ты узнал про GPC? (имя фамилия или ник в тг)")
    await state.set_state(FormBasic.referrer)


@dp.message(FormBasic.referrer)
async def process_referrer(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправь ответ текстовым сообщением 🙂")
        return
    
    referrer_text = message.text.strip()

    if not re.match(r"^@?[a-zA-Zа-яА-ЯёЁ0-9\s_-]+$", referrer_text):
        await message.answer(
            "⚠️ Пожалуйста, не используй лишние спецсимволы и эмодзи. "
            "Введи имя и фамилию человека или его Telegram-никнейм (например, @username)!"
        )
        return

    await state.update_data(referrer=message.text.strip())
    await message.answer("Сколько тебе лет?")
    await state.set_state(FormBasic.age)


@dp.message(FormBasic.age)
async def process_age(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправь возраст числом 🙂")
        return

    age_text = message.text.strip()
    if not age_text.isdigit():
        await message.answer("Пожалуйста, укажи возраст числом (например: 25).")
        return

    age_num = int(age_text)
    if age_num < 14 or age_num > 99:
        await message.answer("Пожалуйста, укажи реальный возраст (от 14 до 99 лет).")
        return

    await state.update_data(age=age_text)
    data = await state.get_data()

    await save_basic_form(message.from_user.id, data['name'], data['referrer'], data['age'])

    from_event_registration = data.get("from_event_registration", False)

    if from_event_registration:
        await message.answer("✅ Анкета сохранена! Теперь уточним пару деталей для мероприятия.")
        await message.answer("Укажи свой пол:\n\nМ — мужчина\nЖ — женщина")
        await state.set_state(FormEvent.gender)
        return

    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    summary = (
        f"📝 <b>Новая заявка в канал!</b>\n\n"
        f"👤 <b>ФИО:</b> {data['name']}\n"
        f"🔗 <b>От кого:</b> {data['referrer']}\n"
        f"🎂 <b>Возраст:</b> {data['age']}\n"
        f"📱 <b>Username:</b> {username}\n"
        f"🆔 <b>ID:</b> <code>{message.from_user.id}</code>"
    )

    try:
        await bot.send_message(LOOK_ID, summary, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка при отправке менеджеру: {e}")

    await message.answer(
        "✅ Анкета отправлена! Ожидай подтверждения.\n\n"
        "После того как тебя примут в канал, переходи по ссылке в посте, "
        "чтобы завершить регистрацию на мероприятие."
    )
    await state.clear()


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user_id = message.from_user.id
    if command.args == "event_registration":
        if not await is_subscribed(user_id):
            await message.answer(
                "❌ <b>Ты не подписан на канал GPC!</b>\n\n"
                "Эта ссылка только для участников клуба. Сначала вступи в канал.",
                parse_mode="HTML"
            )
            return

        await add_user(user_id, message.from_user.username or "нет", message.from_user.full_name or "не указано")

        user_data = await get_user_data(user_id)

        if not user_data or user_data["status"] == "pending":
            await state.update_data(from_event_registration=True)
            await message.answer(
                "Привет! 👋\n\n"
                "Чтобы зарегистрироваться на мероприятие, нужно сначала заполнить небольшую анкету.\n\n"
                "Подскажи, как тебя зовут? (имя фамилия)"
            )
            await state.set_state(FormBasic.name)
            return

        elif user_data["status"] == "filled":
            await message.answer(
                f"Привет, {user_data['form_name']}! 👋\n\n"
                f"Для завершения регистрации на мероприятие нужно уточнить пару деталей."
            )
            await message.answer("Укажи свой пол:\n\nМ — мужчина\nЖ — женщина")
            await state.set_state(FormEvent.gender)
            return

        elif user_data["status"] == "waiting_payment":
            await message.answer(
                f"⏳ <b>Ты уже зарегистрирован!</b>\n\n"
                f"Ожидай подтверждения оплаты.\n"
                f"💰 Твоя цена: <b>{user_data['final_price']} ₽</b>",
                parse_mode="HTML"
            )
            return

        elif user_data["status"] == "paid":
            await message.answer("✅ Твоя оплата уже подтверждена! Добро пожаловать на мероприятие.")
            return

    await add_user(user_id, message.from_user.username or "нет", message.from_user.full_name or "не указано")

    if not await is_subscribed(user_id):
        await message.answer(
            "Привет! Я бот GPC 👋\n\n"
            "Чтобы начать, подпишись на наш канал, затем напиши /start снова."
        )
        return

    user_status = await get_user_status(user_id)

    if user_status in ["filled", "waiting_payment", "paid"]:
        await message.answer(
            "✅ <b>Ты уже заполнял анкету!</b>\n\n"
            "Если хочешь зарегистрироваться на мероприятие — "
            "перейди по специальной ссылке в закрепе канала.",
            parse_mode="HTML"
        )
        return

    await message.answer(
        "Привет! Увидели тебя в канале GPC 👋\n\n"
        "Давай заполним небольшую анкету.\n\n"
        "Подскажи, как тебя зовут? (имя фамилия)"
    )
    await state.set_state(FormBasic.name)


@dp.message(FormEvent.gender)
async def process_event_gender(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправь ответ текстовым сообщением (М или Ж).")
        return

    gender = message.text.strip().lower()

    if gender not in ("м", "мужчина", "ж", "женщина"):
        await message.answer("Напиши:\nМ — мужчина\nЖ — женщина")
        return

    if gender in ("м", "мужчина"):
        gender = "male"
        base_price = 500
    else:
        gender = "female"
        base_price = 400

    await state.update_data(gender=gender, base_price=base_price)
    await message.answer(
        "Если тебя пригласил кто-то из участников GPC — отправь его реферальный код.\n\n"
        "Если тебя никто не приглашал — напиши «нет»."
    )
    await state.set_state(FormEvent.referral_code)


@dp.message(FormEvent.referral_code)
async def process_event_referral(message: Message, state: FSMContext):
    if not message.text or not message.text.strip():
        await message.answer("Пожалуйста, отправь код текстом или напиши «нет».")
        return

    user_id = message.from_user.id
    code_input = message.text.strip()
    data = await state.get_data()

    base_price = data['base_price']
    final_price = base_price
    used_code = None

    if code_input.lower() not in ("нет", "нету", "no", "-"):

        if not re.match(r"^[a-zA-Z0-9]{4,12}$", code_input):
            await message.answer(
                "⚠️ Некорректный формат кода. Реферальный код состоит только из "
                "английских букв и цифр. Попробуй ещё раз или напиши «нет»."
            )
            return

        success, result, referrer_id = await use_referral_code(referred_id=user_id, code=code_input)
        if success:
            used_code = code_input.upper()
            final_price = max(0, base_price - 100)
        else:
            await message.answer(f"⚠️ {result}\nПопробуй ещё раз или напиши «нет».")
            return

    await save_event_data(user_id, data['gender'], base_price, final_price, used_code)

    own_code = None
    if not used_code:
        own_code = await generate_referral_code(user_id)

    # Отправляем сообщение в чат админов и сохраняем его ID
    admin_text = await build_admin_message(user_id)
    admin_msg_id = None

    try:
        sent_msg = await bot.send_message(MANAGER_ID, admin_text, parse_mode="HTML")
        admin_msg_id = sent_msg.message_id
        await save_admin_message_id(user_id, admin_msg_id)
    except Exception as e:
        logging.error(f"Ошибка при отправке менеджеру: {e}")

    # Сообщение пользователю
    if own_code:
        await message.answer(
            f"✅ <b>Регистрация завершена!</b>\n\n"
            f"💰 <b>Твоя цена билета:</b> {final_price} ₽\n"
            f"🔑 <b>Твой реферальный код:</b> <code>{own_code}</code>\n\n"
            f"Передай этот код друзьям и получи скидку 50 ₽ за каждого оплатившего!\n"
            f"<i>Максимальная скидка: 250 ₽ (5 друзей)</i>\n"
	    f"💵 <a href='https://t.tb.ru/c2c-qr-choose-bank?requisiteNumber=+79835287902&bankCode=100000000'><b>Оплати</b></a> (Т-Банк) и скинь фотографию перевода администратору @Garage_Podval_Cherdak\n\n"
            f"⏳ <i>Ожидай подтверждения оплаты администратором.</i>",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            f"✅ <b>Регистрация завершена!</b>\n\n"
            f"💰 <b>Твоя цена билета:</b> {final_price} ₽\n"
            f"🎁 Скидка по реферальному коду: <b>-100 ₽</b>\n\n"
	    f"💵 <a href='https://t.tb.ru/c2c-qr-choose-bank?requisiteNumber=+79835287902&bankCode=100000000'><b>Оплати</b></a> (Т-Банк) и скинь фотографию перевода администратору @Garage_Podval_Cherdak\n\n"
            f"⏳ <i>Ожидай подтверждения оплаты администратором.</i>",
            parse_mode="HTML"
        )

    await state.clear()


@dp.message(Command("pay"))
async def cmd_pay(message: Message):
    if message.chat.id != MANAGER_ID:
        return

    if not message.text:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Использование: `/pay @username`", parse_mode="Markdown")
        return

    username = args[1].strip()
    success, result_text = await confirm_payment_by_username(username)

    await message.answer(result_text, parse_mode="HTML")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    user_id = message.from_user.id

    await state.clear()

    user_status = await get_user_status(user_id)
    if not user_status or user_status == "pending":
        await message.answer("❌ У тебя нет активной регистрации.")
        return
    
    if user_status == "paid":
        await message.answer(
            "⚠️ <b>Оплата уже подтверждена!</b>\n\n"
            "Для отмены обратись к администратору.",
            parse_mode="HTML"
        )
        return

    success, result_text = await cancel_user_registration(user_id)
    
    await message.answer(
        "❌ <b>Регистрация отменена.</b>\n\n"
        "Твой реферальный код удален.\n"
        "Если тебя кто-то пригласил, его скидка также аннулирована.\n\n"
        "Ты можешь подать заявку заново.",
        parse_mode="HTML"
    )

@dp.message(Command("codes"))
async def cmd_codes(message: Message):
    if message.chat.id != MANAGER_ID:
        return
    
    codes = await get_all_referral_codes()
    
    if not codes:
        await message.answer("📋 Реферальных кодов пока нет.")
        return
    
    lines = ["📋 <b>Все реферальные коды</b>\n"]
    
    for (
        code,
        user_id,
        username,
        full_name,
        base_price,
        final_price,
        referral_count
    ) in codes:
        username_text = (
            f"@{username}"
            if username and username != "нет"
            else "нет"
        )
        lines.append(
            f"🔑 <b>{code}</b>\n"
            f"👤 {full_name or 'Не указано'}\n"
            f"📱 {username_text}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"👥 Использовали: <b>{referral_count}</b>\n"
            f"💰 Цена: <b>{final_price or 500} ₽</b> (база: {base_price or 500} ₽)\n"
        )
    
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML"
    )

@dp.message(Command("code"))
async def cmd_code(message: Message):
    if message.chat.id != MANAGER_ID:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ Использование: `/code GPCXXXX`",
            parse_mode="Markdown"
        )
        return
    
    code = args[1].strip().upper()

    info = await get_referral_code_info(code)
    
    if not info:
        await message.answer(f"❌ Код <b>{code}</b> не найден в базе.", parse_mode="HTML")
        return
    
    owner = info["owner"]
    referrals = info["referrals"]

    status_map = {
        "pending": "⏳ ожидает",
        "filled": "📝 анкета заполнена",
        "waiting_payment": "💳 ожидает оплату",
        "paid": "✅ оплачено"
    }
    
    owner_status_text = status_map.get(owner["status"], owner["status"])
    owner_username = f"@{owner['username']}" if owner["username"] and owner["username"] != "нет" else "нет"
    
    lines = [
        f"🔑 <b>Код:</b> <code>{owner['code']}</code>\n",
        f"👑 <b>Владелец:</b>",
        f"   👤 {owner['full_name'] or 'Не указано'}",
        f"   📱 {owner_username}",
        f"   🆔 <code>{owner['user_id']}</code>",
        f"   📊 Статус: {owner_status_text}",
        f"   💰 Цена: <b>{owner['final_price']} ₽</b> (база: {owner['base_price']} ₽)",
        f"   📅 Создан: {owner['created_at']}\n",
    ]

    if referrals:
        lines.append(f"👥 <b>Использовали код ({len(referrals)}):</b>")
        for i, ref in enumerate(referrals, 1):
            ref_status_text = status_map.get(ref["status"], ref["status"])
            ref_username = f"@{ref['username']}" if ref["username"] and ref["username"] != "нет" else "нет"
            lines.append(
                f"\n{i}. 👤 {ref['full_name'] or 'Не указано'}\n"
                f"   📱 {ref_username}\n"
                f"   🆔 <code>{ref['user_id']}</code>\n"
                f"   📊 {ref_status_text}\n"
                f"   💰 Цена: {ref['final_price']} ₽\n"
                f"   📅 {ref['created_at']}"
            )
    else:
        lines.append("👥 <b>По этому коду пока никто не регистрировался.</b>")
    
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("unpay"))
async def cmd_unpay(message: Message):
    if message.chat.id != MANAGER_ID:
        return
    
    if not message.text:
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Использование: `/unpay @username`", parse_mode="Markdown")
        return
    
    username = args[1].strip()
    success, result_text = await unconfirm_payment_by_username(username)
    await message.answer(result_text, parse_mode="HTML")

@dp.message(Command("user"))
async def cmd_user(message: Message):
    if message.chat.id != MANAGER_ID:
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ Использование: `/user @username`",
            parse_mode="Markdown"
        )
        return
    
    username = args[1].strip()

    info = await get_user_full_info(username)
    
    if not info:
        await message.answer(f"❌ Пользователь @{username.lstrip('@')} не найден в БД.")
        return

    status_map = {
        "pending": "⏳ ожидает",
        "filled": "📝 анкета заполнена",
        "waiting_payment": "💳 ожидает оплату",
        "paid": "✅ оплачено"
    }
    
    status_text = status_map.get(info["status"], info["status"])
    gender_text = "Мужской" if info["gender"] == "male" else "Женский" if info["gender"] == "female" else "не указан"
    
    username_text = f"@{info['tg_username']}" if info["tg_username"] and info["tg_username"] != "нет" else "нет"
    
    lines = [
        f"👤 <b>Полная информация о пользователе</b>\n",
        f"📛 <b>ФИО:</b> {info['form_name'] or 'Не указано'}",
        f"🎂 <b>Возраст:</b> {info['form_age'] or 'Не указан'}",
        f"👫 <b>Пол:</b> {gender_text}",
        f"📱 <b>Username:</b> {username_text}",
        f"🆔 <b>ID:</b> <code>{info['user_id']}</code>\n",
        f"📊 <b>Статус:</b> {status_text}",
        f"💰 <b>Базовая цена:</b> {info['base_price']} ₽",
        f"💳 <b>К оплате:</b> <b>{info['final_price']} ₽</b>\n",
    ]
    
    # Реферальный код
    if info["own_code"]:
        lines.append(f"🔑 <b>Его реф. код:</b> <code>{info['own_code']}</code>")
        lines.append(f"👥 <b>Пришло по коду:</b> {info['referral_count']} чел.\n")
    else:
        lines.append(f"🔑 <b>Реф. код:</b> нет\n")
    
    # Кто пригласил
    if info["invited_by"]:
        inv = info["invited_by"]
        inv_username = f"@{inv['username']}" if inv["username"] and inv["username"] != "нет" else "нет"
        lines.append(f"🎁 <b>Пришёл по коду:</b> {info['used_referral_code']}")
        lines.append(f"   👤 От: {inv['full_name'] or 'Не указано'} ({inv_username})")
        lines.append(f"   🆔 ID: <code>{inv['user_id']}</code>\n")
    else:
        lines.append(f"🎁 <b>Реферальный код:</b> не использовал\n")

    if info["form_referrer"]:
        lines.append(f"🔗 <b>От кого узнал:</b> {info['form_referrer']}")
    
    # Даты
    lines.append(f"\n📅 <b>Регистрация:</b> {info['created_at']}")
    lines.append(f"🔄 <b>Обновлено:</b> {info['updated_at']}")
    
    await message.answer("\n".join(lines), parse_mode="HTML")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())