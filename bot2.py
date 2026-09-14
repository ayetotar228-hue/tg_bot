import asyncio
import logging
import os
import aiosqlite

from aiogram import Bot, Dispatcher
from aiogram.types import ChatJoinRequest, Message
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MANAGER_ID = os.getenv("MANAGER_ID")
DB_PATH = "bot.db"


if not BOT_TOKEN or not CHANNEL_ID or not MANAGER_ID:
    raise RuntimeError("Проверь .env файл! Нужны BOT_TOKEN, CHANNEL_ID и MANAGER_ID")

CHANNEL_ID = int(CHANNEL_ID)
MANAGER_ID = int(MANAGER_ID)

logging.basicConfig(level=logging.INFO)

storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

class Form(StatesGroup):
    name = State()
    referrer = State()
    age = State()

message_template = (
    "Привет! Увидели твою заявку в GPC\n"
    "Подскажи как тебя зовут?\n"
)

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

async def get_user_status(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None

async def add_user(user_id: int, tg_username: str, tg_full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, tg_username, tg_full_name) 
            VALUES (?, ?, ?)
        """, (user_id, tg_username, tg_full_name))
        await db.commit()

async def save_form_data(user_id: int, name: str, referrer: str, age: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users 
            SET form_name = ?, 
                form_referrer = ?, 
                form_age = ?, 
                status = 'filled',
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        """, (name, referrer, age, user_id))
        await db.commit()

async def update_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", 
            (status, user_id)
        )
        await db.commit()
        
        
@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    if request.chat.id != CHANNEL_ID:
        return

    user_id = request.from_user.id
    user_status = await get_user_status(user_id)

    if user_status == "filled":
        username = f"@{request.from_user.username}" if request.from_user.username else "нет"
        repeat_msg = (
            f"🔄 <b>Повторная заявка!</b>\n\n"
            f"Пользователь уже заполнял анкету ранее.\n"
            f"👤 <b>Имя:</b> {request.from_user.full_name}\n"
            f"📱 Username: {username}\n\n"
            f"Заявка оставлена на ручное рассмотрение."
        )
        await bot.send_message(MANAGER_ID, repeat_msg, parse_mode="HTML")
        logging.info("Повторная заявка от пользователя %s", user_id)
        return

    tg_username = request.from_user.username or "нет"
    tg_full_name = request.from_user.full_name or "не указано"
    await add_user(user_id, tg_username, tg_full_name)

    try:
        await bot.send_message(chat_id=user_id, text=message_template)
        
        state = FSMContext(
            storage=dp.storage,
            key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
        )
        await state.set_state(Form.name)
        await state.update_data(channel_id=request.chat.id)
        
        logging.info("Анкета отправлена пользователю %s", user_id)

    except TelegramForbiddenError:
        username = f"@{request.from_user.username}" if request.from_user.username else "нет"
        manager_msg = (
            f"⚠️ Новая заявка, но пользователь не начал диалог с ботом!\n"
            f"ID: <code>{user_id}</code>\n"
            f"Username: {username}\n"
            f"Имя: {request.from_user.full_name}\n\n"
            f"Напишите ему сами или одобрите вручную."
        )
        await bot.send_message(MANAGER_ID, manager_msg, parse_mode="HTML")
        logging.warning("Пользователь %s не начал диалог с ботом", user_id)

@dp.message(Form.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Кто тебе скинул ссылку на этот тгк? (имя фамилия или ник в тг)")
    await state.set_state(Form.referrer)

@dp.message(Form.referrer)
async def process_referrer(message: Message, state: FSMContext):
    await state.update_data(referrer=message.text)
    await message.answer("Сколько тебе лет?")
    await state.set_state(Form.age)

@dp.message(Form.age)
async def process_age(message: Message, state: FSMContext):
    await state.update_data(age=message.text)
    data = await state.get_data()
    
    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    user_link = f"tg://user?id={message.from_user.id}"
    
    summary = (
        f"✅ <b>Новая анкета!</b>\n\n"
        f"👤 <b>ФИО:</b> {data['name']}\n"
        f"🔗 <b>От кого:</b> {data['referrer']}\n"
        f"🎂 <b>Возраст:</b> {data['age']}\n\n"
        f"📱 <b>Username:</b> {username}\n"
    )
    
    try:
        await bot.send_message(MANAGER_ID, summary, parse_mode="HTML")
    except TelegramForbiddenError:
        logging.error("Ошибка отправки менеджеру: бот не имеет доступа к чату %s", MANAGER_ID)
    except Exception as e:
        logging.error(f"Ошибка при отправке менеджеру: {e}")
    
    await save_form_data(
        user_id=message.from_user.id,
        name=data['name'],
        referrer=data['referrer'],
        age=data['age']
    )
    
    await message.answer(
        "Спасибо! Твоя анкета отправлена. "
        "Ожидай, скоро мы рассмотрим твою заявку и одобрим вступление."
    )
    await state.clear()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для заявок в закрытый канал. "
        "Просто нажми кнопку вступления в канале, и я задам тебе пару вопросов."
    )

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())