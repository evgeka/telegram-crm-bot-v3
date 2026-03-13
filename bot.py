import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Dict, Any, Optional, List

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "./crm.sqlite")

if not BOT_TOKEN:
    raise RuntimeError("Не задано BOT_TOKEN у змінних середовища Railway")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# ---------------------------
# DB
# ---------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_tg_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workspace_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                added_at TEXT NOT NULL,
                UNIQUE(workspace_id, telegram_id)
            );

            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                last_name TEXT,
                first_name TEXT,
                middle_name TEXT,
                phone TEXT,
                social TEXT,
                reliability INTEGER NOT NULL DEFAULT 3,
                bonus_points INTEGER NOT NULL DEFAULT 0,
                average_check REAL NOT NULL DEFAULT 0,
                address TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_clients_workspace ON clients(workspace_id);
            CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone);
            CREATE INDEX IF NOT EXISTS idx_clients_social ON clients(social);
            CREATE INDEX IF NOT EXISTS idx_clients_rating ON clients(reliability);
            """
        )
        conn.commit()


# ---------------------------
# Helpers
# ---------------------------

def now_str() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def clean_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


def get_user_workspaces(tg_id: int) -> List[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT w.id, w.name, wu.role
            FROM workspace_users wu
            JOIN workspaces w ON w.id = wu.workspace_id
            WHERE wu.telegram_id = ?
            ORDER BY w.id ASC
            """,
            (tg_id,),
        )
        return cur.fetchall()


def get_active_workspace(user_id: int) -> Optional[sqlite3.Row]:
    state = USER_STATE.get(user_id, {})
    workspace_id = state.get("active_workspace_id")
    if workspace_id:
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, name FROM workspaces WHERE id = ?", (workspace_id,))
            row = cur.fetchone()
            if row:
                return row

    workspaces = get_user_workspaces(user_id)
    if len(workspaces) == 1:
        USER_STATE.setdefault(user_id, {})["active_workspace_id"] = workspaces[0]["id"]
        return workspaces[0]
    return None


def user_role_in_workspace(user_id: int, workspace_id: int) -> Optional[str]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role FROM workspace_users WHERE telegram_id = ? AND workspace_id = ?",
            (user_id, workspace_id),
        )
        row = cur.fetchone()
        return row["role"] if row else None


def ensure_workspace_selected(user_id: int) -> Optional[str]:
    ws = get_active_workspace(user_id)
    if ws:
        return None
    workspaces = get_user_workspaces(user_id)
    if not workspaces:
        return "У вас ще немає кабінету. Натисніть <b>🏪 Створити кабінет</b>."
    text = ["У вас кілька кабінетів. Надішліть номер потрібного кабінету:"]
    for i, w in enumerate(workspaces, start=1):
        text.append(f"{i}. {w['name']} ({w['role']})")
    USER_STATE.setdefault(user_id, {})["mode"] = "choose_workspace"
    USER_STATE[user_id]["workspace_choices"] = [dict(w) for w in workspaces]
    return "\n".join(text)


def get_workspace_clients(workspace_id: int, search: Optional[str] = None) -> List[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        if search:
            s = f"%{search}%"
            cur.execute(
                """
                SELECT * FROM clients
                WHERE workspace_id = ?
                  AND (
                    COALESCE(phone, '') LIKE ? OR
                    COALESCE(social, '') LIKE ? OR
                    COALESCE(last_name, '') LIKE ? OR
                    COALESCE(first_name, '') LIKE ? OR
                    COALESCE(middle_name, '') LIKE ?
                  )
                ORDER BY updated_at DESC
                LIMIT 20
                """,
                (workspace_id, s, s, s, s, s),
            )
        else:
            cur.execute(
                "SELECT * FROM clients WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 20",
                (workspace_id,),
            )
        return cur.fetchall()


def get_client_by_id(client_id: int, workspace_id: int) -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM clients WHERE id = ? AND workspace_id = ?",
            (client_id, workspace_id),
        )
        return cur.fetchone()


def client_short_name(row: sqlite3.Row) -> str:
    parts = [row["last_name"], row["first_name"], row["middle_name"]]
    full = " ".join(p for p in parts if p)
    return full.strip() or "Без ПІБ"


def client_basic_line(row: sqlite3.Row) -> str:
    contact = row["phone"] or row["social"] or "без контакту"
    return f"#{row['id']} | {client_short_name(row)} | {contact} | рейтинг {row['reliability']}/5"


def client_full_text(row: sqlite3.Row) -> str:
    contact = row["phone"] or "-"
    social = row["social"] or "-"
    address = row["address"] or "-"
    note = row["note"] or "-"
    avg = int(row["average_check"]) if float(row["average_check"]).is_integer() else row["average_check"]
    return (
        f"<b>Клієнт #{row['id']}</b>\n"
        f"<b>ПІБ:</b> {client_short_name(row)}\n"
        f"<b>Телефон:</b> {contact}\n"
        f"<b>Соцмережа/нік:</b> {social}\n"
        f"<b>Адреса:</b> {address}\n"
        f"<b>Середній чек:</b> {avg}\n"
        f"<b>Рейтинг:</b> {row['reliability']}/5\n"
        f"<b>ББ 🎁:</b> {row['bonus_points']}\n"
        f"<b>Примітка:</b> {note}"
    )


def main_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("🏪 Створити кабінет"), KeyboardButton("📂 Мій кабінет"))
    kb.row(KeyboardButton("👤 Додати клієнта"), KeyboardButton("🔎 Знайти клієнта"))
    kb.row(KeyboardButton("✏️ Оновити рейтинг/ББ"), KeyboardButton("👥 Додати працівника"))
    kb.row(KeyboardButton("⚠️ Низький рейтинг"), KeyboardButton("🌐 Глобальний список 0-2"))
    kb.row(KeyboardButton("❓ Допомога"), KeyboardButton("↩️ Скасувати"))
    return kb


USER_STATE: Dict[int, Dict[str, Any]] = {}


def reset_mode(user_id: int):
    state = USER_STATE.setdefault(user_id, {})
    active_workspace_id = state.get("active_workspace_id")
    USER_STATE[user_id] = {}
    if active_workspace_id:
        USER_STATE[user_id]["active_workspace_id"] = active_workspace_id


# ---------------------------
# Handlers
# ---------------------------
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    reset_mode(message.from_user.id)
    await message.answer(
        "Вітаю. Це Telegram CRM бот.\n\n"
        "Один бот - окремі кабінети для різних власників.\n"
        "Спочатку створіть кабінет або відкрийте свій.",
        reply_markup=main_keyboard(),
    )


@dp.message_handler(lambda m: m.text == "❓ Допомога")
async def help_cmd(message: types.Message):
    await message.answer(
        "<b>Що вже працює:</b>\n"
        "- створення окремого кабінету\n"
        "- додавання клієнтів\n"
        "- пошук по телефону, ніку, ПІБ\n"
        "- список клієнтів із низьким рейтингом у вашому кабінеті\n"
        "- глобальний список 0-2 з базовими даними\n"
        "- додавання працівника в кабінет\n"
        "- оновлення рейтингу та ББ 🎁\n\n"
        "<b>Порада:</b> якщо бот чекає введення і ви передумали - натисніть <b>↩️ Скасувати</b>."
    )


@dp.message_handler(lambda m: m.text == "↩️ Скасувати")
async def cancel_cmd(message: types.Message):
    reset_mode(message.from_user.id)
    await message.answer("Поточну дію скасовано.", reply_markup=main_keyboard())


@dp.message_handler(lambda m: m.text == "🏪 Створити кабінет")
async def create_workspace_begin(message: types.Message):
    USER_STATE.setdefault(message.from_user.id, {})["mode"] = "await_workspace_name"
    await message.answer("Введіть назву вашої системи або магазину.")


@dp.message_handler(lambda m: m.text == "📂 Мій кабінет")
async def my_workspace(message: types.Message):
    user_id = message.from_user.id
    workspaces = get_user_workspaces(user_id)
    if not workspaces:
        await message.answer("У вас ще немає кабінету. Натисніть <b>🏪 Створити кабінет</b>.")
        return

    if len(workspaces) == 1:
        USER_STATE.setdefault(user_id, {})["active_workspace_id"] = workspaces[0]["id"]
        await message.answer(f"Активний кабінет: <b>{workspaces[0]['name']}</b>")
        return

    lines = ["Оберіть активний кабінет, надіславши його номер:"]
    for i, w in enumerate(workspaces, start=1):
        lines.append(f"{i}. {w['name']} ({w['role']})")
    USER_STATE.setdefault(user_id, {})["mode"] = "choose_workspace"
    USER_STATE[user_id]["workspace_choices"] = [dict(w) for w in workspaces]
    await message.answer("\n".join(lines))


@dp.message_handler(lambda m: m.text == "👤 Додати клієнта")
async def add_client_begin(message: types.Message):
    hint = ensure_workspace_selected(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    USER_STATE.setdefault(message.from_user.id, {})["mode"] = "client_last_name"
    USER_STATE[message.from_user.id]["client_form"] = {}
    ws = get_active_workspace(message.from_user.id)
    await message.answer(f"Активний кабінет: <b>{ws['name']}</b>\nВведіть прізвище клієнта.")


@dp.message_handler(lambda m: m.text == "🔎 Знайти клієнта")
async def search_client_begin(message: types.Message):
    hint = ensure_workspace_selected(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    USER_STATE.setdefault(message.from_user.id, {})["mode"] = "search_client"
    await message.answer("Введіть номер телефону, нік, прізвище або ім'я клієнта.")


@dp.message_handler(lambda m: m.text == "✏️ Оновити рейтинг/ББ")
async def update_client_begin(message: types.Message):
    hint = ensure_workspace_selected(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    USER_STATE.setdefault(message.from_user.id, {})["mode"] = "update_client_id"
    await message.answer("Введіть ID клієнта, якого потрібно оновити.")


@dp.message_handler(lambda m: m.text == "👥 Додати працівника")
async def add_worker_begin(message: types.Message):
    hint = ensure_workspace_selected(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    ws = get_active_workspace(message.from_user.id)
    role = user_role_in_workspace(message.from_user.id, ws["id"])
    if role != "owner":
        await message.answer("Додавати працівників може тільки власник кабінету.")
        return
    USER_STATE.setdefault(message.from_user.id, {})["mode"] = "add_worker_tg_id"
    await message.answer(
        "Надішліть Telegram ID працівника.\n"
        "Працівник має хоча б раз запустити вашого бота через /start."
    )


@dp.message_handler(lambda m: m.text == "⚠️ Низький рейтинг")
async def low_rating_workspace(message: types.Message):
    hint = ensure_workspace_selected(message.from_user.id)
    if hint:
        await message.answer(hint)
        return
    ws = get_active_workspace(message.from_user.id)
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, last_name, first_name, middle_name, phone, social, reliability
            FROM clients
            WHERE workspace_id = ? AND reliability BETWEEN 0 AND 2
            ORDER BY reliability ASC, updated_at DESC
            LIMIT 100
            """,
            (ws["id"],),
        )
        rows = cur.fetchall()
    if not rows:
        await message.answer(f"У кабінеті <b>{ws['name']}</b> клієнтів з рейтингом 0-2 немає.")
        return
    lines = [f"<b>⚠️ Низький рейтинг - {ws['name']}</b>"]
    for row in rows:
        contact = row["phone"] or row["social"] or "без контакту"
        name = " ".join(p for p in [row["last_name"], row["first_name"], row["middle_name"]] if p) or "Без ПІБ"
        lines.append(f"{name} | {contact} | рейтинг {row['reliability']}/5")
    await message.answer("\n".join(lines[:80]))


@dp.message_handler(lambda m: m.text == "🌐 Глобальний список 0-2")
async def global_low_rating(message: types.Message):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT c.id, c.last_name, c.first_name, c.middle_name, c.phone, c.social, c.reliability
            FROM clients c
            WHERE c.reliability BETWEEN 0 AND 2
            ORDER BY c.reliability ASC, c.updated_at DESC
            LIMIT 100
            """
        )
        rows = cur.fetchall()
    if not rows:
        await message.answer("Глобальний список 0-2 поки порожній.")
        return
    lines = ["<b>🌐 Глобальний список клієнтів з рейтингом 0-2</b>"]
    for row in rows:
        contact = row["phone"] or row["social"] or "без контакту"
        name = " ".join(p for p in [row["last_name"], row["first_name"], row["middle_name"]] if p) or "Без ПІБ"
        lines.append(f"{name} | {contact} | рейтинг {row['reliability']}/5")
    await message.answer("\n".join(lines[:80]))


@dp.message_handler(content_types=types.ContentType.TEXT)
async def universal_text_handler(message: types.Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = USER_STATE.setdefault(user_id, {})
    mode = state.get("mode")

    # Workspace create
    if mode == "await_workspace_name":
        if len(text) < 2:
            await message.answer("Назва занадто коротка. Введіть нормальну назву магазину або системи.")
            return
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO workspaces (name, owner_tg_id, created_at) VALUES (?, ?, ?)",
                (text, user_id, now_str()),
            )
            workspace_id = cur.lastrowid
            cur.execute(
                "INSERT OR IGNORE INTO workspace_users (workspace_id, telegram_id, role, added_at) VALUES (?, ?, 'owner', ?)",
                (workspace_id, user_id, now_str()),
            )
            conn.commit()
        reset_mode(user_id)
        USER_STATE.setdefault(user_id, {})["active_workspace_id"] = workspace_id
        await message.answer(
            f"Кабінет <b>{text}</b> створено.\n"
            f"Тепер можете додавати клієнтів або шукати їх.",
            reply_markup=main_keyboard(),
        )
        return

    # Choose workspace
    if mode == "choose_workspace":
        choices = state.get("workspace_choices", [])
        if not text.isdigit():
            await message.answer("Надішліть саме номер кабінету зі списку.")
            return
        idx = int(text) - 1
        if idx < 0 or idx >= len(choices):
            await message.answer("Невірний номер. Спробуйте ще раз.")
            return
        chosen = choices[idx]
        reset_mode(user_id)
        USER_STATE.setdefault(user_id, {})["active_workspace_id"] = chosen["id"]
        await message.answer(f"Активний кабінет: <b>{chosen['name']}</b>")
        return

    # Add client wizard
    if mode == "client_last_name":
        state["client_form"]["last_name"] = text
        state["mode"] = "client_first_name"
        await message.answer("Введіть ім'я клієнта.")
        return

    if mode == "client_first_name":
        state["client_form"]["first_name"] = text
        state["mode"] = "client_middle_name"
        await message.answer("Введіть по батькові або напишіть - якщо немає.")
        return

    if mode == "client_middle_name":
        state["client_form"]["middle_name"] = "" if text == "-" else text
        state["mode"] = "client_phone"
        await message.answer("Введіть номер телефону або напишіть - якщо немає.")
        return

    if mode == "client_phone":
        state["client_form"]["phone"] = "" if text == "-" else clean_phone(text)
        state["mode"] = "client_social"
        await message.answer("Введіть Instagram/Viber/Telegram нік або інший соцконтакт. Якщо немає - напишіть -")
        return

    if mode == "client_social":
        state["client_form"]["social"] = "" if text == "-" else text
        state["mode"] = "client_address"
        await message.answer("Введіть адресу доставки або напишіть -")
        return

    if mode == "client_address":
        state["client_form"]["address"] = "" if text == "-" else text
        state["mode"] = "client_average_check"
        await message.answer("Введіть середній чек у гривнях. Якщо поки невідомо - 0")
        return

    if mode == "client_average_check":
        try:
            average_check = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Потрібно ввести число. Наприклад: 450")
            return
        state["client_form"]["average_check"] = average_check
        state["mode"] = "client_reliability"
        await message.answer("Введіть рейтинг надійності від 0 до 5")
        return

    if mode == "client_reliability":
        if not text.isdigit() or int(text) not in range(0, 6):
            await message.answer("Рейтинг має бути числом від 0 до 5")
            return
        state["client_form"]["reliability"] = int(text)
        state["mode"] = "client_bonus_points"
        await message.answer("Введіть ББ 🎁 (кількість бонусних балів). Якщо немає - 0")
        return

    if mode == "client_bonus_points":
        if not text.lstrip("-").isdigit():
            await message.answer("ББ мають бути цілим числом. Наприклад: 0 або 1")
            return
        state["client_form"]["bonus_points"] = int(text)
        state["mode"] = "client_note"
        await message.answer("Введіть примітку або напишіть -")
        return

    if mode == "client_note":
        form = state["client_form"]
        form["note"] = "" if text == "-" else text
        ws = get_active_workspace(user_id)
        if not ws:
            reset_mode(user_id)
            await message.answer("Не вдалося визначити активний кабінет. Спробуйте знову.")
            return
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO clients (
                    workspace_id, last_name, first_name, middle_name, phone, social,
                    reliability, bonus_points, average_check, address, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws["id"],
                    form.get("last_name", ""),
                    form.get("first_name", ""),
                    form.get("middle_name", ""),
                    form.get("phone", ""),
                    form.get("social", ""),
                    form.get("reliability", 3),
                    form.get("bonus_points", 0),
                    form.get("average_check", 0),
                    form.get("address", ""),
                    form.get("note", ""),
                    now_str(),
                    now_str(),
                ),
            )
            client_id = cur.lastrowid
            conn.commit()
        reset_mode(user_id)
        row = get_client_by_id(client_id, ws["id"])
        await message.answer("Клієнта додано:\n\n" + client_full_text(row), reply_markup=main_keyboard())
        return

    # Search client
    if mode == "search_client":
        ws = get_active_workspace(user_id)
        if not ws:
            reset_mode(user_id)
            await message.answer("Не вдалося визначити активний кабінет.")
            return
        rows = get_workspace_clients(ws["id"], text)
        reset_mode(user_id)
        if not rows:
            await message.answer("Нічого не знайдено у вашому кабінеті.")
            return
        chunks = [f"<b>Результати пошуку у кабінеті {ws['name']}</b>"]
        for row in rows[:10]:
            chunks.append(client_full_text(row))
            chunks.append("──────────")
        await message.answer("\n".join(chunks[:-1]))
        return

    # Update client id
    if mode == "update_client_id":
        if not text.isdigit():
            await message.answer("Потрібно ввести ID клієнта числом.")
            return
        ws = get_active_workspace(user_id)
        if not ws:
            reset_mode(user_id)
            await message.answer("Не вдалося визначити активний кабінет.")
            return
        row = get_client_by_id(int(text), ws["id"])
        if not row:
            await message.answer("Клієнта з таким ID у вашому кабінеті не знайдено.")
            return
        state["edit_client_id"] = int(text)
        state["mode"] = "update_client_rating"
        await message.answer(
            "Поточні дані:\n\n" + client_full_text(row) + "\n\nВведіть новий рейтинг 0-5"
        )
        return

    if mode == "update_client_rating":
        if not text.isdigit() or int(text) not in range(0, 6):
            await message.answer("Рейтинг має бути числом від 0 до 5")
            return
        state["new_rating"] = int(text)
        state["mode"] = "update_client_bonus"
        await message.answer("Введіть нове значення ББ 🎁")
        return

    if mode == "update_client_bonus":
        if not text.lstrip("-").isdigit():
            await message.answer("ББ мають бути цілим числом")
            return
        ws = get_active_workspace(user_id)
        client_id = state.get("edit_client_id")
        new_rating = state.get("new_rating")
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE clients SET reliability = ?, bonus_points = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (new_rating, int(text), now_str(), client_id, ws["id"]),
            )
            conn.commit()
        updated = get_client_by_id(client_id, ws["id"])
        reset_mode(user_id)
        await message.answer("Дані клієнта оновлено:\n\n" + client_full_text(updated), reply_markup=main_keyboard())
        return

    # Add worker
    if mode == "add_worker_tg_id":
        if not text.isdigit():
            await message.answer("Telegram ID має бути числом.")
            return
        state["new_worker_tg_id"] = int(text)
        state["mode"] = "add_worker_role"
        await message.answer("Введіть роль працівника: manager або worker")
        return

    if mode == "add_worker_role":
        role = text.lower()
        if role not in {"manager", "worker"}:
            await message.answer("Допустимі ролі: manager або worker")
            return
        ws = get_active_workspace(user_id)
        with closing(get_conn()) as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO workspace_users (workspace_id, telegram_id, role, added_at) VALUES (?, ?, ?, ?)",
                (ws["id"], state["new_worker_tg_id"], role, now_str()),
            )
            conn.commit()
        worker_id = state["new_worker_tg_id"]
        reset_mode(user_id)
        await message.answer(
            f"Працівника з Telegram ID <b>{worker_id}</b> додано до кабінету <b>{ws['name']}</b> з роллю <b>{role}</b>."
        )
        return

    await message.answer(
        "Я не зрозумів цю дію. Скористайтеся кнопками меню або натисніть <b>❓ Допомога</b>.",
        reply_markup=main_keyboard(),
    )


if __name__ == "__main__":
    init_db()
    executor.start_polling(dp, skip_updates=True)
