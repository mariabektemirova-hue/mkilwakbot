import os
import json
import base64
import re
import time
import html
import email
import sqlite3
import requests
import caldav

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, getaddresses
from icalendar import Calendar
from imapclient import IMAPClient

# =========================================================
# CONFIG
# =========================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

YANDEX_CALENDAR_LOGIN = os.environ["YANDEX_CALENDAR_LOGIN"]
YANDEX_CALENDAR_PASSWORD = os.environ["YANDEX_CALENDAR_PASSWORD"]
YANDEX_MAIL_LOGIN = os.environ["YANDEX_MAIL_LOGIN"]
YANDEX_MAIL_PASSWORD = os.environ["YANDEX_MAIL_PASSWORD"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"
YANDEX_CALDAV_URL = "https://caldav.yandex.ru/"
YANDEX_IMAP_HOST = "imap.yandex.com"
YANDEX_IMAP_PORT = 993

DB_PATH = "/data/masha.db"
TZ = ZoneInfo("Europe/Moscow")
MODEL = "gpt-5.6"
BOT_VERSION = "v7.8-consistent-evening"

ASSISTANT_INSTRUCTIONS = """
Ты Маша 2.0 — личный рабочий ассистент Маши.

Твоя задача — разгружать Маше голову: держать под контролем календарь,
почту, задачи, процессы, дедлайны, документы, подписи, оплаты,
зависшие хвосты и договоренности.

ВАЖНО: рабочие задачи Маши не обязаны напрямую касаться МС.
Учитывай административные, бухгалтерские, закупочные, кадровые,
документальные и организационные процессы Маши как полноценную работу.

КОНТЕКСТ КАЛЕНДАРЯ
- Маша работает бизнес-ассистентом руководителя МС.
- ТМС и Advisory Board не двигаем.
- Тренировки и психоаналитика Маши тоже не двигаем.
- МС может внезапно попросить 30 минут с директором — сохраняй окна.
- Для 1-2-1 повестку желательно собирать за неделю.
- Материалы к встречам желательно получать минимум за 2 суток.
- Понедельник желательно оставлять под фокус.
- Пятница предпочтительна для внешних встреч.
- Между встречами желательно 15–30 минут воздуха.
- Обед не совмещать с внутренними встречами.

ЧЕК-ЛИСТ ВСТРЕЧ
- Zoom: должна быть ссылка.
- Внешняя встреча: должен быть адрес.
- В описании должна быть повестка.
- Участники должны подтвердить присутствие.
- Собеседование: резюме вложено и распечатано.
- PRM: материалы собраны заранее, доступ к папке проверен.

ПРОЦЕССЫ
Процесс — это не одна задача, а цепочка этапов с зависимостями.
Если тебе передан блок ПРОЦЕССЫ, анализируй:
- какой следующий реально доступный шаг;
- что заблокировано предыдущими этапами;
- где ждём подпись, документ, счёт, оплату или человека;
- что пора проконтролировать;
- какие хвосты давно висят.
Не советуй выполнять этап, если его зависимости ещё не завершены.

ПОЧТА
У тебя read-only доступ к письмам, которые программа передает тебе.
Можно анализировать цепочки, выделять договорённости и следующий шаг.
Нельзя утверждать, что письмо отправлено/удалено/архивировано, если этого не было.

ДОСТОВЕРНОСТЬ
Не придумывай отсутствующие сведения.
Если в календаре нет повестки/адреса — так и говори.
Если почта ничего не нашла — так и говори.
Если система не умеет сама проверить 1С — скажи, что статус нужно проверить Маше.

СТИЛЬ
Отвечай по-русски, естественно, коротко и практично.
Telegram получает обычный текст — без Markdown-разметки.
"""

# =========================================================
# DB
# =========================================================

def ensure_data_directory():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    ensure_data_directory()
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            previous_response_id TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS mail_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            email TEXT NOT NULL,
            display_name TEXT,
            created_at TEXT NOT NULL,
            verified INTEGER NOT NULL DEFAULT 0,
            source TEXT DEFAULT 'auto',
            UNIQUE(chat_id, alias, email)
        )
    """)

    # Migration from older mail_contacts schema
    names = column_names(conn, "mail_contacts")
    if "verified" not in names:
        conn.execute("ALTER TABLE mail_contacts ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
    if "source" not in names:
        conn.execute("ALTER TABLE mail_contacts ADD COLUMN source TEXT DEFAULT 'legacy'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS processes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS process_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_id INTEGER NOT NULL,
            step_no INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            depends_on TEXT DEFAULT '',
            waiting_for TEXT DEFAULT '',
            due_at TEXT,
            remind_every_days INTEGER DEFAULT 0,
            last_reminded_at TEXT,
            completed_at TEXT,
            UNIQUE(process_id, step_no),
            FOREIGN KEY(process_id) REFERENCES processes(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS process_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            UNIQUE(process_id, key),
            FOREIGN KEY(process_id) REFERENCES processes(id)
        )
    """)



    # Extra task metadata for forwarded messages and due dates.
    task_cols = column_names(conn, "tasks")
    if "source" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN source TEXT DEFAULT 'manual'")
    if "source_ref" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_ref TEXT")
    if "due_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN due_at TEXT")
    if "notes" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN notes TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ms_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            due_at TEXT,
            source TEXT DEFAULT 'manual',
            source_ref TEXT,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            answered_at TEXT
        )
    """)

    # v7.7: store MS answers/decisions and any follow-up task created from them.
    msq_cols = column_names(conn, "ms_questions")
    if "answer_text" not in msq_cols:
        conn.execute("ALTER TABLE ms_questions ADD COLUMN answer_text TEXT DEFAULT ''")
    if "resulting_task_id" not in msq_cols:
        conn.execute("ALTER TABLE ms_questions ADD COLUMN resulting_task_id INTEGER")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarded_inbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            source_name TEXT DEFAULT '',
            raw_text TEXT DEFAULT '',
            has_image INTEGER NOT NULL DEFAULT 0,
            parsed_json TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(chat_id, telegram_message_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS control_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'background',
            priority TEXT NOT NULL DEFAULT 'normal',
            status TEXT NOT NULL DEFAULT 'open',
            next_action TEXT DEFAULT '',
            waiting_for TEXT DEFAULT '',
            deadline TEXT,
            next_check TEXT,
            cadence_days INTEGER DEFAULT 0,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, title)
        )
    """)

    # Clean bad system contacts from previous versions
    conn.execute("""
        DELETE FROM mail_contacts
        WHERE lower(email) LIKE '%@calendar.yandex.ru'
           OR lower(email) LIKE '%@mailer.yandex.ru'
           OR lower(email) LIKE 'noreply@%'
           OR lower(email) LIKE 'no-reply@%'
           OR lower(email) LIKE 'mailer-daemon@%'
           OR lower(email) LIKE 'postmaster@%'
    """)

    conn.commit()
    conn.close()

# =========================================================
# TASKS / MEMORY
# =========================================================

def task_dedupe_key(value):
    value = normalize_text(value)
    value = re.sub(r"[^a-zа-яё0-9]+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()


def find_exact_open_task(chat_id, text):
    key = task_dedupe_key(text)
    if not key:
        return None
    conn = get_db()
    rows = conn.execute(
        "SELECT id,text FROM tasks WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 100",
        (chat_id,),
    ).fetchall()
    conn.close()
    for row in rows:
        if task_dedupe_key(row["text"]) == key:
            return row
    return None


def add_task(chat_id, text, source="manual", source_ref=None, due_at=None, notes=""):
    # v7.7: do not create the same open task repeatedly from repeated forwards.
    duplicate = find_exact_open_task(chat_id, text)
    if duplicate:
        print("Task dedupe: reusing", duplicate["id"], repr(text))
        return duplicate["id"]

    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO tasks(chat_id,text,status,created_at,source,source_ref,due_at,notes)
        VALUES (?,?,'open',?,?,?,?,?)
        """,
        (
            chat_id,
            text.strip(),
            datetime.now(TZ).isoformat(timespec="seconds"),
            source,
            source_ref,
            due_at,
            notes or "",
        ),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


def get_open_tasks(chat_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,text,created_at FROM tasks WHERE chat_id=? AND status='open' ORDER BY id",
        (chat_id,),
    ).fetchall()
    conn.close(); return rows


def complete_task(chat_id, task_id):
    conn = get_db()
    cur = conn.execute(
        "UPDATE tasks SET status='done',completed_at=? WHERE chat_id=? AND id=? AND status='open'",
        (datetime.now(TZ).isoformat(timespec="seconds"), chat_id, task_id),
    )
    ok = cur.rowcount > 0
    conn.commit(); conn.close(); return ok


def add_memory(chat_id, text):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO memories(chat_id,text,created_at) VALUES (?,?,?)",
        (chat_id, text.strip(), datetime.now(TZ).isoformat(timespec="seconds")),
    )
    memory_id = cur.lastrowid
    conn.commit(); conn.close(); return memory_id


def get_memories(chat_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT id,text,created_at FROM memories WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    conn.close(); return rows


def delete_memory(chat_id, memory_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM memories WHERE chat_id=? AND id=?", (chat_id, memory_id))
    ok = cur.rowcount > 0
    conn.commit(); conn.close(); return ok

# =========================================================
# PROCESSES
# =========================================================

def create_process(chat_id, title, notes=""):
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO processes(chat_id,title,status,notes,created_at) VALUES (?,?,'open',?,?)",
        (chat_id, title.strip(), notes.strip(), datetime.now(TZ).isoformat(timespec="seconds")),
    )
    pid = cur.lastrowid
    conn.commit(); conn.close(); return pid


def add_process_step(process_id, step_no, text, depends_on="", waiting_for="", due_at=None, remind_every_days=0):
    conn = get_db()
    conn.execute("""
        INSERT INTO process_steps(
            process_id,step_no,text,status,depends_on,waiting_for,due_at,remind_every_days
        ) VALUES (?,?,?,'open',?,?,?,?)
    """, (process_id, step_no, text.strip(), depends_on, waiting_for, due_at, remind_every_days))
    conn.commit(); conn.close()


def set_process_meta(process_id, key, value):
    conn = get_db()
    conn.execute("""
        INSERT INTO process_meta(process_id,key,value) VALUES (?,?,?)
        ON CONFLICT(process_id,key) DO UPDATE SET value=excluded.value
    """, (process_id, key, str(value)))
    conn.commit(); conn.close()


def get_process_meta(process_id):
    conn = get_db()
    rows = conn.execute("SELECT key,value FROM process_meta WHERE process_id=?", (process_id,)).fetchall()
    conn.close(); return {r["key"]: r["value"] for r in rows}


def get_open_processes(chat_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM processes WHERE chat_id=? AND status='open' ORDER BY id", (chat_id,)
    ).fetchall()
    conn.close(); return rows


def get_process(process_id, chat_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM processes WHERE id=? AND chat_id=?", (process_id, chat_id)).fetchone()
    conn.close(); return row


def get_process_steps(process_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM process_steps WHERE process_id=? ORDER BY step_no", (process_id,)
    ).fetchall()
    conn.close(); return rows


def parse_depends(depends_on):
    if not depends_on:
        return []
    result = []
    for part in str(depends_on).split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def is_step_available(step, steps_by_no):
    if step["status"] != "open":
        return False
    for dep_no in parse_depends(step["depends_on"]):
        dep = steps_by_no.get(dep_no)
        if not dep or dep["status"] != "done":
            return False
    return True


def complete_process_step(chat_id, process_id, step_no):
    process = get_process(process_id, chat_id)
    if not process:
        return False, "Не нашла такой процесс."

    conn = get_db()
    cur = conn.execute("""
        UPDATE process_steps
        SET status='done', completed_at=?
        WHERE process_id=? AND step_no=? AND status='open'
    """, (datetime.now(TZ).isoformat(timespec="seconds"), process_id, step_no))
    changed = cur.rowcount > 0
    conn.commit(); conn.close()

    if not changed:
        return False, f"Этап {step_no} не найден или уже закрыт."

    # Close whole process if every step is done
    steps = get_process_steps(process_id)
    if steps and all(s["status"] == "done" for s in steps):
        conn = get_db()
        conn.execute(
            "UPDATE processes SET status='done',completed_at=? WHERE id=?",
            (datetime.now(TZ).isoformat(timespec="seconds"), process_id),
        )
        conn.commit(); conn.close()
        return True, f"Этап {step_no} закрыт ✅ И весь процесс завершён."

    return True, f"Этап {step_no} закрыт ✅"


def format_process(process_id, chat_id):
    process = get_process(process_id, chat_id)
    if not process:
        return "Не нашла такой процесс."

    steps = get_process_steps(process_id)
    steps_by_no = {s["step_no"]: s for s in steps}
    lines = [f"Процесс #{process['id']}: {process['title']}"]
    if process["notes"]:
        lines.append(process["notes"])
    lines.append("")

    for s in steps:
        if s["status"] == "done":
            icon = "✅"
        elif is_step_available(s, steps_by_no):
            icon = "➡️"
        else:
            icon = "⏳"

        line = f"{icon} {s['step_no']}. {s['text']}"
        if s["waiting_for"]:
            line += f" [ждём: {s['waiting_for']}]"
        if s["remind_every_days"]:
            line += f" [контроль каждые {s['remind_every_days']} дн.]"
        lines.append(line)

    return "\n".join(lines)


def processes_context(chat_id):
    processes = get_open_processes(chat_id)
    if not processes:
        return "ПРОЦЕССЫ:\nОткрытых процессов нет."

    lines = ["ПРОЦЕССЫ:"]
    for p in processes:
        steps = get_process_steps(p["id"])
        by_no = {s["step_no"]: s for s in steps}
        lines.append(f"\nПроцесс #{p['id']}: {p['title']}")
        if p["notes"]:
            lines.append(f"Примечание: {p['notes']}")
        for s in steps:
            state = "готово" if s["status"] == "done" else ("доступен" if is_step_available(s, by_no) else "заблокирован")
            line = f"- {s['step_no']}. [{state}] {s['text']}"
            if s["waiting_for"]:
                line += f"; ждём: {s['waiting_for']}"
            if s["remind_every_days"]:
                line += f"; контроль каждые {s['remind_every_days']} дн."
            lines.append(line)
    return "\n".join(lines)


def seed_gift_process(chat_id):
    # Avoid duplicate test process
    for p in get_open_processes(chat_id):
        if normalize_text(p["title"]) == normalize_text("Подарки двум директорам — картины и букеты"):
            return p["id"], False

    pid = create_process(
        chat_id,
        "Подарки двум директорам — картины и букеты",
        "Картины и букеты уже получены. Остался бухгалтерский и документальный хвост.",
    )

    steps = [
        (1, "Подготовить приказ по подаркам по существующему шаблону", "", "", 0),
        (2, "Получить подпись Анжелики, гендиректора, на приказе", "1", "Анжелика", 0),
        (3, "Получить согласование/подпись финансов на приказе", "1", "финансы", 0),
        (4, "Получить подпись Алёны, HR-директора, на приказе", "1", "Алёна", 0),
        (5, "По художнику №1: подписать акт", "", "художник №1", 0),
        (6, "По художнику №1: получить счёт на оплату", "", "художник №1", 0),
        (7, "По художнику №2: подписать акт", "", "художник №2", 0),
        (8, "По художнику №2: получить счёт на оплату", "", "художник №2", 0),
        (9, "Создать заявку №1 в 1С и прикрепить счёт + подписанный акт", "2,3,4,5,6", "", 0),
        (10, "Создать заявку №2 в 1С и прикрепить счёт + подписанный акт", "2,3,4,7,8", "", 0),
        (11, "Проверять статус заявки №1 в 1С до оплаты", "9", "1С", 2),
        (12, "Проверять статус заявки №2 в 1С до оплаты", "10", "1С", 2),
        (13, "Передать оригиналы документов по подаркам в бухгалтерию", "9,10", "бухгалтерия", 0),
    ]
    for no, text, deps, waiting, repeat in steps:
        add_process_step(pid, no, text, deps, waiting, None, repeat)

    set_process_meta(pid, "template_order", "есть шаблон приказа — позже привязать файл на Яндекс.Диске")
    set_process_meta(pid, "template_act", "есть шаблон акта — позже привязать файл на Яндекс.Диске")
    set_process_meta(pid, "physical_gifts_received", "yes")

    # Separate backlog process for originals, because it is broader than gifts.
    existing = [p for p in get_open_processes(chat_id) if normalize_text(p["title"]) == normalize_text("Оригиналы документов в бухгалтерию")]
    if not existing:
        opid = create_process(
            chat_id,
            "Оригиналы документов в бухгалтерию",
            "Накопительный административный хвост; оригиналы не передавались с января.",
        )
        add_process_step(opid, 1, "Разобрать накопленные оригиналы документов с января", "", "", None, 0)
        add_process_step(opid, 2, "Сформировать пачку/реестр для передачи в бухгалтерию", "1", "", None, 0)
        add_process_step(opid, 3, "Передать оригиналы в бухгалтерию", "2", "бухгалтерия", None, 0)

    return pid, True


# =========================================================
# CONTROL LAYER — BACKGROUND / WEEKLY WORK
# =========================================================

def upsert_control_item(chat_id, title, category="background", priority="normal",
                        next_action="", waiting_for="", deadline=None,
                        next_check=None, cadence_days=0, notes=""):
    now = datetime.now(TZ).isoformat(timespec="seconds")
    conn = get_db()
    conn.execute("""
        INSERT INTO control_items(
            chat_id,title,category,priority,status,next_action,waiting_for,
            deadline,next_check,cadence_days,notes,created_at,updated_at
        ) VALUES (?,?,?,?, 'open', ?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id,title) DO UPDATE SET updated_at=excluded.updated_at
    """, (chat_id,title,category,priority,next_action,waiting_for,deadline,next_check,
          cadence_days,notes,now,now))
    conn.commit()
    conn.close()


def seed_control_items(chat_id):
    today = datetime.now(TZ).date()
    items = [
        ("Описание текущих процессов БА","longterm","low","Описать один небольшой процесс/блок работы БА","",None,(today+timedelta(days=7)).isoformat(),7,"Долгосрочная база знаний для порядка и передачи следующему человеку."),
        ("Перенос документов Google → Яндекс Диск","waiting","normal","Проверить статус заявки Support UU №58396","Support UU",None,(today+timedelta(days=2)).isoformat(),2,"После помощи IT перенести документы и навести порядок."),
        ("Антон — прикрепление к школе","deadline","high","Напомнить МС: Антону нужно сходить с МС или Гулей к врачу за справкой для школы","МС / Гуля","2026-08-31",(today+timedelta(days=3)).isoformat(),3,"Остальные документы есть. Завершить в августе."),
        ("Горничная в квартиру МС","background","low","Узнать у МС, нравится ли текущий человек; затем спокойно искать замену при необходимости","МС",None,(today+timedelta(days=7)).isoformat(),7,"Несрочно."),
        ("Ретро и аналитические записки директоров","collection","normal","Собрать со всех директоров ретро и аналитические записки, сохранить в одной папке и дать МС ссылку","директора",None,(today+timedelta(days=3)).isoformat(),3,""),
        ("НГ-подарки партнёрам","project","high","Подготовить сообщения в чаты C1 и C2 с просьбой дать списки поздравляемых: рядовые сотрудники и руководители","C1 / C2",None,(today+timedelta(days=2)).isoformat(),2,"После сбора свести списки и передать Даше Поповой для запуска книг и мерча."),
        ("Химчистка пледов в кабинете МС","waiting","normal","Узнать у Олега Козлова дату готовности пледов","Олег Козлов",None,(today+timedelta(days=2)).isoformat(),2,"Олег сдал пледы в клининг."),
        ("Встречи по БДР","planning","normal","Поставить встречи по БДР на остальные месяцы после августа","",None,(today+timedelta(days=3)).isoformat(),0,"Август уже поставлен. Основание — письмо Анжелики."),
        ("Индонезия — поездка МС с КЧЗ","waiting","normal","Поставить даты конференции в календарь и пинговать Шорену по вариантам перелёта КЧЗ","Шорена",None,(today+timedelta(days=2)).isoformat(),2,"После вариантов перелёта организовать поездку МС."),
        ("Фестиваль Маяк — октябрь","project","normal","Проверить даты фестиваля в календаре и контролировать результаты регистрации","МС / организаторы",None,(today+timedelta(days=5)).isoformat(),5,"После подтверждения организовать перелёт и проживание."),
    ]
    for item in items:
        upsert_control_item(chat_id, *item)


def get_control_items(chat_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM control_items WHERE chat_id=? AND status='open'
        ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id
    """, (chat_id,)).fetchall()
    conn.close()
    return rows


def control_context(chat_id, due_only=False):
    seed_control_items(chat_id)
    rows = get_control_items(chat_id)
    today = datetime.now(TZ).date()
    lines = ["ФОНОВЫЕ ДЕЛА НА КОНТРОЛЕ:"]
    selected = []
    for r in rows:
        due = False
        if r["deadline"]:
            try:
                due = date.fromisoformat(r["deadline"]) <= today + timedelta(days=7)
            except Exception:
                pass
        if r["next_check"]:
            try:
                due = due or date.fromisoformat(r["next_check"]) <= today
            except Exception:
                pass
        if not due_only or due:
            selected.append(r)
    if not selected:
        return "ФОНОВЫЕ ДЕЛА НА КОНТРОЛЕ:\nСегодня контрольных точек нет."
    for r in selected:
        line = f"- {r['title']}: {r['next_action'] or 'следующий шаг не задан'}"
        if r["waiting_for"]: line += f"; ждём: {r['waiting_for']}"
        if r["deadline"]: line += f"; дедлайн: {r['deadline']}"
        if r["next_check"]: line += f"; следующая проверка: {r['next_check']}"
        lines.append(line)
    return "\n".join(lines)


def show_control(chat_id):
    seed_control_items(chat_id)
    rows = get_control_items(chat_id)
    lines = ["Контрольные дела:"]
    for i, r in enumerate(rows, 1):
        lines += ["", f"{i}. {r['title']}", f"Следующий шаг: {r['next_action'] or 'не задан'}"]
        if r["waiting_for"]: lines.append(f"Ждём: {r['waiting_for']}")
        if r["deadline"]: lines.append(f"Дедлайн: {r['deadline']}")
        if r["next_check"]: lines.append(f"Проверить: {r['next_check']}")
    send_message(chat_id, "\n".join(lines))


def show_week_control(chat_id):
    seed_control_items(chat_id)
    prompt = """
Составь рабочий контроль на ближайшие 7 дней. Это НЕ календарь встреч.
Раздели на: 1) Сделать самой. 2) Кого пингануть / чего ждём.
3) Дедлайны и контрольные точки. 4) Можно пока не трогать.
Не возвращай закрытые этапы. Давний хвост оригиналов не делай ежедневным приоритетом.
"""
    extra = "\n\n".join([
        "ОБЫЧНЫЕ ЗАДАЧИ:\n" + ("\n".join(f"- #{r['id']}: {r['text']}" for r in get_open_tasks(chat_id)) or "нет"),
        processes_context(chat_id),
        control_context(chat_id, due_only=False),
        prompt,
    ])
    send_message(chat_id, ask_openai(chat_id, "Составь мой рабочий контроль на неделю.", extra_context=extra, use_history=False))


# =========================================================
# REMINDERS FOR PROCESS MONITORING
# =========================================================

def due_process_reminders(chat_id):
    now = datetime.now(TZ)
    reminders = []

    for p in get_open_processes(chat_id):
        steps = get_process_steps(p["id"])
        by_no = {s["step_no"]: s for s in steps}
        for s in steps:
            if not is_step_available(s, by_no):
                continue

            due = False
            if s["due_at"]:
                try:
                    due_dt = datetime.fromisoformat(s["due_at"])
                    if due_dt.tzinfo is None:
                        due_dt = due_dt.replace(tzinfo=TZ)
                    due = due_dt <= now
                except Exception:
                    pass

            repeat_days = int(s["remind_every_days"] or 0)
            if repeat_days > 0:
                if not s["last_reminded_at"]:
                    # For recurring monitoring, don't nag immediately after dependency opens.
                    # Use completed time of latest dependency as starting point if possible.
                    deps = parse_depends(s["depends_on"])
                    opened_at = None
                    for dep_no in deps:
                        dep = by_no.get(dep_no)
                        if dep and dep["completed_at"]:
                            dt = datetime.fromisoformat(dep["completed_at"])
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=TZ)
                            if opened_at is None or dt > opened_at:
                                opened_at = dt
                    if opened_at and now >= opened_at + timedelta(days=repeat_days):
                        due = True
                else:
                    last = datetime.fromisoformat(s["last_reminded_at"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=TZ)
                    if now >= last + timedelta(days=repeat_days):
                        due = True

            if due:
                reminders.append((p, s))

    return reminders


def mark_step_reminded(step_id):
    conn = get_db()
    conn.execute(
        "UPDATE process_steps SET last_reminded_at=? WHERE id=?",
        (datetime.now(TZ).isoformat(timespec="seconds"), step_id),
    )
    conn.commit(); conn.close()

# =========================================================
# OPENAI STATE
# =========================================================

def get_previous_response_id(chat_id):
    conn = get_db()
    row = conn.execute("SELECT previous_response_id FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close(); return row["previous_response_id"] if row else None


def save_previous_response_id(chat_id, response_id):
    conn = get_db()
    conn.execute("""
        INSERT INTO chat_state(chat_id,previous_response_id) VALUES (?,?)
        ON CONFLICT(chat_id) DO UPDATE SET previous_response_id=excluded.previous_response_id
    """, (chat_id, response_id))
    conn.commit(); conn.close()


def clear_previous_response_id(chat_id):
    conn = get_db(); conn.execute("DELETE FROM chat_state WHERE chat_id=?", (chat_id,)); conn.commit(); conn.close()


# =========================================================
# FORWARDED INBOX + QUESTIONS FOR MS / EVENING UPDATE
# =========================================================

def add_ms_question(chat_id, text, due_at=None, source="manual", source_ref=None, notes=""):
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO ms_questions(chat_id,text,status,due_at,source,source_ref,notes,created_at)
        VALUES (?,?,'open',?,?,?,?,?)
        """,
        (
            chat_id,
            text.strip(),
            due_at,
            source,
            source_ref,
            notes or "",
            datetime.now(TZ).isoformat(timespec="seconds"),
        ),
    )
    qid = cur.lastrowid
    conn.commit()
    conn.close()
    return qid


def get_open_ms_questions(chat_id):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM ms_questions
        WHERE chat_id=? AND status='open'
        ORDER BY
          CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,
          due_at,
          id
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_ms_question(chat_id, question_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ms_questions WHERE chat_id=? AND id=?",
        (chat_id, question_id),
    ).fetchone()
    conn.close()
    return row


def complete_ms_question(chat_id, question_id, answer_text="", resulting_task_id=None):
    conn = get_db()
    cur = conn.execute(
        """
        UPDATE ms_questions
        SET status='answered', answered_at=?, answer_text=?, resulting_task_id=?
        WHERE chat_id=? AND id=? AND status='open'
        """,
        (
            datetime.now(TZ).isoformat(timespec="seconds"),
            answer_text or "",
            resulting_task_id,
            chat_id,
            question_id,
        ),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def choose_question_for_ms_answer(chat_id, explicit_qid=None):
    if explicit_qid:
        q = get_ms_question(chat_id, explicit_qid)
        return q if q and q["status"] == "open" else None
    rows = get_open_ms_questions(chat_id)
    if not rows:
        return None
    # Natural replies most often answer the last question added to the evening update.
    return max(rows, key=lambda r: r["id"])


def classify_ms_answer(question_text, answer_text):
    prompt = f"""
Ты превращаешь ответ руководителя МС на ранее сохранённый вопрос в рабочее решение.

ВОПРОС:
{question_text}

ОТВЕТ МС:
{answer_text}

Нужно решить, возникает ли после ответа конкретное действие Маши.
Пример: вопрос "нужна ли Алёна на встрече?", ответ "да, нужна" -> создать задачу "Добавить/проверить Алёну участником этой встречи".
Ответ "нет, не нужна" -> новой задачи нет.
Не выдумывай действие, если его не следует из ответа.

Верни ТОЛЬКО JSON:
{{
  "decision": "коротко зафиксированное решение",
  "create_task": true,
  "task_text": "самодостаточная задача или пустая строка"
}}
"""
    payload = {
        "model": MODEL,
        "instructions": "Ты рабочий классификатор решений. Отвечай только валидным JSON без Markdown.",
        "input": prompt,
        "store": False,
    }
    r = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    parsed = parse_json_from_model(extract_openai_text(r.json()))
    if not isinstance(parsed, dict):
        raise ValueError("MS answer classifier returned non-object")
    return parsed


def handle_natural_ms_answer(chat_id, text):
    m = re.match(
        r"^(?:мс\s+ответила|ответ\s+мс|мс\s+сказала)\s*(?:на\s*q?(\d+))?\s*[:,-]?\s*(.+)$",
        text.strip(), flags=re.I | re.S,
    )
    if not m:
        return False

    explicit_qid = int(m.group(1)) if m.group(1) else None
    answer = m.group(2).strip()
    q = choose_question_for_ms_answer(chat_id, explicit_qid)
    if not q:
        send_message(chat_id, "Не нашла открытый вопрос МС, к которому относится этот ответ. Напиши, например: МС ответила на Q1: ...")
        return True

    resulting_task_id = None
    decision = answer
    try:
        parsed = classify_ms_answer(q["text"], answer)
        decision = str(parsed.get("decision") or answer).strip()
        task_text = str(parsed.get("task_text") or "").strip()
        if parsed.get("create_task") is True and task_text:
            resulting_task_id = add_task(
                chat_id, task_text, source="ms_decision",
                source_ref=f"ms_question:{q['id']}",
                notes=f"Создано из ответа МС на Q{q['id']}. Решение: {decision}",
            )
    except Exception as e:
        print("MS answer classification error:", repr(e))

    complete_ms_question(chat_id, q["id"], answer_text=decision, resulting_task_id=resulting_task_id)
    # Mark that this was a real answer, so the v7.8 test-state repair will not reopen it.
    conn = get_db()
    conn.execute(
        """
        UPDATE ms_questions
        SET notes=CASE
            WHEN notes IS NULL OR notes='' THEN 'real_ms_answer'
            ELSE notes || '\nreal_ms_answer'
        END
        WHERE id=?
        """,
        (q["id"],),
    )
    conn.commit()
    conn.close()
    lines = [f"Q{q['id']} закрыла ✅", f"Решение: {decision}"]
    if resulting_task_id:
        task = next((r for r in get_open_tasks(chat_id) if r["id"] == resulting_task_id), None)
        lines.append(f"Следующий шаг записала задачей #{resulting_task_id}: {task['text'] if task else 'см. /tasks'}")
    else:
        lines.append("Нового действия из ответа не требуется.")
    send_message(chat_id, "\n".join(lines))
    return True



def ensure_alena_operations_question(chat_id):
    """
    One-time repair for the test state:
    the question about Alena on Operations 18.08 was closed during testing,
    but in reality Masha has not asked MS yet.
    Keep exactly one open question until a real answer is recorded later.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM ms_questions
        WHERE chat_id=?
        ORDER BY id
        """,
        (chat_id,),
    ).fetchall()

    matches = []
    for row in rows:
        n = normalize_text(row["text"])
        if (
            "алена" in n
            and "operations" in n
            and "18 августа" in n
            and ("зиновец" in n or "марин" in n)
        ):
            matches.append(row)

    if matches:
        # Keep the oldest matching question as the canonical one.
        canonical = matches[0]

        # If a real answer was recorded later, respect it and do not reopen.
        if "real_ms_answer" not in (canonical["notes"] or ""):
            conn.execute(
                """
                UPDATE ms_questions
                SET status='open',
                    due_at=COALESCE(due_at, ?),
                    answer_text='',
                    resulting_task_id=NULL,
                    answered_at=NULL
                WHERE id=?
                """,
                (datetime.now(TZ).date().isoformat(), canonical["id"]),
            )
        for extra in matches[1:]:
            conn.execute(
                """
                UPDATE ms_questions
                SET status='answered',
                    answer_text='duplicate test entry',
                    answered_at=?
                WHERE id=?
                """,
                (datetime.now(TZ).isoformat(timespec="seconds"), extra["id"]),
            )
        qid = canonical["id"]
    else:
        qid = add_ms_question(
            chat_id,
            "Сегодня в вечернем апдейте надо спросить у МС, нужна ли Алёна на встрече Operations 18 августа с Мариной Зиновец.",
            due_at=datetime.now(TZ).date().isoformat(),
            source="repair_v7_8",
            notes="Восстановлено после тестового закрытия; фактического ответа МС ещё не было.",
        )

    conn.commit()
    conn.close()
    return qid


def collapse_exact_task_duplicates(chat_id):
    """
    Close exact duplicate open tasks, leaving the oldest one open.
    This cleans the monitor test duplicates and protects task lists from clutter.
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id,text FROM tasks
        WHERE chat_id=? AND status='open'
        ORDER BY id
        """,
        (chat_id,),
    ).fetchall()

    seen = {}
    closed = []
    for row in rows:
        key = task_dedupe_key(row["text"])
        if not key:
            continue
        if key not in seen:
            seen[key] = row["id"]
            continue

        conn.execute(
            """
            UPDATE tasks
            SET status='done',
                completed_at=?,
                notes=CASE
                    WHEN notes IS NULL OR notes='' THEN ?
                    ELSE notes || '\n' || ?
                END
            WHERE id=? AND status='open'
            """,
            (
                datetime.now(TZ).isoformat(timespec="seconds"),
                f"Закрыто автоматически как дубль задачи #{seen[key]}.",
                f"Закрыто автоматически как дубль задачи #{seen[key]}.",
                row["id"],
            ),
        )
        closed.append(row["id"])

    conn.commit()
    conn.close()
    return closed


def show_ms_questions(chat_id):
    ensure_alena_operations_question(chat_id)
    collapse_exact_task_duplicates(chat_id)
    rows = get_open_ms_questions(chat_id)
    if not rows:
        send_message(chat_id, "Вопросов к МС сейчас нет.")
        return

    lines = ["Вопросы / согласования с МС:"]
    for row in rows:
        line = f"\nQ{row['id']} — {row['text']}"
        if row["due_at"]:
            line += f"\nСрок: {row['due_at']}"
        lines.append(line)
    send_message(chat_id, "\n".join(lines))


def looks_like_ms_question(text):
    n = normalize_text(text)
    triggers = [
        "вечернем апдейте",
        "вечерний апдейт",
        "спросить мс",
        "уточнить у мс",
        "выяснить у мс",
        "согласовать с мс",
        "узнать у мс",
        "спросить у мс",
        "вопрос мс",
        "вопрос к мс",
    ]
    return any(t in n for t in triggers)


def detect_due_from_text(text):
    """
    Очень консервативно: сегодня/завтра. Остальные даты сохраняем в самом тексте.
    """
    n = normalize_text(text)
    today = datetime.now(TZ).date()
    if "сегодня" in n:
        return today.isoformat()
    if "завтра" in n:
        return (today + timedelta(days=1)).isoformat()
    return None


def get_forward_source_name(message):
    origin = message.get("forward_origin") or {}

    if origin:
        typ = origin.get("type", "")
        if typ == "user":
            user = origin.get("sender_user") or {}
            return " ".join(
                x for x in [user.get("first_name", ""), user.get("last_name", "")]
                if x
            ).strip() or user.get("username", "") or "пользователь"
        if typ == "hidden_user":
            return origin.get("sender_user_name", "") or "скрытый отправитель"
        if typ in {"chat", "channel"}:
            chat = origin.get("sender_chat") or origin.get("chat") or {}
            return chat.get("title", "") or chat.get("username", "") or typ

    # Legacy fields for older Telegram payloads.
    if message.get("forward_sender_name"):
        return message["forward_sender_name"]
    if message.get("forward_from"):
        user = message["forward_from"]
        return " ".join(
            x for x in [user.get("first_name", ""), user.get("last_name", "")]
            if x
        ).strip() or user.get("username", "") or "пользователь"
    if message.get("forward_from_chat"):
        chat = message["forward_from_chat"]
        return chat.get("title", "") or chat.get("username", "") or "чат"

    return ""


def is_forwarded_message(message):
    return bool(
        message.get("forward_origin")
        or message.get("forward_from")
        or message.get("forward_from_chat")
        or message.get("forward_sender_name")
        or message.get("forward_date")
    )


def message_text_or_caption(message):
    return (message.get("text") or message.get("caption") or "").strip()


def telegram_photo_data_url(message):
    photos = message.get("photo") or []
    if not photos:
        return None

    # Last PhotoSize is usually the largest.
    file_id = photos[-1].get("file_id")
    if not file_id:
        return None

    meta = requests.get(
        f"{TELEGRAM_API}/getFile",
        params={"file_id": file_id},
        timeout=30,
    )
    meta.raise_for_status()
    file_path = meta.json().get("result", {}).get("file_path")
    if not file_path:
        return None

    blob = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}",
        timeout=60,
    )
    blob.raise_for_status()

    ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else "jpg"
    mime = {
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
    }.get(ext, "image/jpeg")

    encoded = base64.b64encode(blob.content).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def recent_forward_context(chat_id, limit=12, minutes=20):
    """
    Context window for short forwarded chains from MS.
    Only recent messages are included so old unrelated forwards do not pollute context.
    """
    cutoff = datetime.now(TZ) - timedelta(minutes=minutes)

    conn = get_db()
    rows = conn.execute(
        """
        SELECT source_name,raw_text,has_image,created_at
        FROM forwarded_inbox
        WHERE chat_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()
    conn.close()

    selected = []
    for row in rows:
        try:
            dt = datetime.fromisoformat(row["created_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            if dt >= cutoff:
                selected.append(row)
        except Exception:
            selected.append(row)

    if not selected:
        return "Недавних связанных пересылок нет."

    lines = [
        f"НЕДАВНЯЯ ЦЕПОЧКА ПЕРЕСЫЛОК (последние {minutes} минут; используй только для контекста):"
    ]
    for row in reversed(selected):
        body = (row["raw_text"] or "").strip()
        if row["has_image"]:
            body = ("[скриншот/изображение] " + body).strip()
        if not body:
            body = "[пересылка без текста]"
        lines.append(
            f"- {row['source_name'] or 'источник не указан'}: {body[:900]}"
        )
    return "\n".join(lines)


def parse_json_from_model(text_value):
    value = (text_value or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        return json.loads(value)
    except Exception:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise



def fast_parse_simple_forward(message):
    """
    Быстрый локальный разбор очевидных текстовых поручений.
    Никакого OpenAI — ответ почти мгновенный.
    Если фраза неоднозначная, возвращаем None и идём в ИИ-разбор.
    """
    raw = message_text_or_caption(message).strip()
    if not raw:
        return None

    n = normalize_text(raw)

    # Не берём вопросы и короткие продолжения — им нужен контекст.
    if "?" in raw:
        return None
    if len(n) < 8:
        return None

    # Явное поручение "надо / нужно / необходимо / сделай / проверить..."
    patterns = [
        r"^(?:маш[а-я]*[,\s]*)?(?:надо|нужно|необходимо)\s+(.+)$",
        r"^(?:маш[а-я]*[,\s]*)?(?:сделай|сделать)\s+(.+)$",
        r"^(?:маш[а-я]*[,\s]*)?(?:проверь|проверить)\s+(.+)$",
        r"^(?:маш[а-я]*[,\s]*)?(?:узнай|узнать)\s+(.+)$",
        r"^(?:маш[а-я]*[,\s]*)?(?:поставь|поставить)\s+(.+)$",
        r"^(?:маш[а-я]*[,\s]*)?(?:добавь|добавить)\s+(.+)$",
    ]

    for pattern in patterns:
        m = re.match(pattern, n, flags=re.I)
        if not m:
            continue

        action = m.group(1).strip(" .")
        if not action:
            return None

        # Делаем формулировку самостоятельной.
        task_text = action[0].upper() + action[1:]
        return {
            "summary": "Нашла очевидное поручение.",
            "items": [{
                "kind": "task",
                "text": task_text,
                "due_at": None,
                "notes": "Создано напрямую из пересланного сообщения без ИИ-разбора.",
            }]
        }

    return None


def analyze_forwarded_message(chat_id, message):
    raw_text = message_text_or_caption(message)
    source_name = get_forward_source_name(message)
    image_url = None

    if message.get("photo"):
        try:
            image_url = telegram_photo_data_url(message)
        except Exception as e:
            print("Forward photo download error:", repr(e))

    prompt = f"""
Ты разбираешь входящее рабочее сообщение, которое Маша переслала своему ассистенту-боту.
Источник пересылки: {source_name or "не определён"}.
Текст/подпись:
{raw_text or "[текста нет — смотри изображение]"}

Задача: ничего не потерять, но и не создавать мусор.

Разложи ТОЛЬКО новые действия из текущей пересылки.
Недавний контекст дан ниже, чтобы понимать местоимения и короткие продолжения.
Если текущее сообщение продолжает предыдущую задачу, сформулируй действие самодостаточно.
Если это короткое продолжение вроде «И адженду с Дашей», обязательно используй недавнюю цепочку пересылок.
Если перед вопросом был скриншот календаря, считай вопрос вроде «Почему там Бутко?» относящимся к этому скриншоту, если нет более правдоподобного контекста.

Типы:
- task: конкретное действие Маши.
- ask_ms: вопрос/согласование, которое нужно вынести МС, например в вечерний апдейт.
- info: важная справочная информация, из которой сейчас не следует действие.

Особые правила:
- Если в сообщении есть важные даты/события, которые логично внести в календарь, но бот календарь пока только читает,
  создай task вида "Внести в календарь: ...".
- Если руководитель спрашивает "почему там X?", это обычно task: проверить и исправить проблему.
- Если руководитель пишет "надо ...", это task.
- Если речь про адженду/структуру/пункт встречи — сформулируй полноценную задачу по повестке.
- Не утверждай, что действие выполнено.
- Не создавай ask_ms просто потому, что сообщение пришло от МС: ask_ms нужен, когда Маше надо задать МС вопрос или получить решение.
- Максимум 6 элементов.

Верни ТОЛЬКО JSON:
{{
  "summary": "коротко, что поняла",
  "items": [
    {{
      "kind": "task|ask_ms|info",
      "text": "самодостаточная формулировка",
      "due_at": "YYYY-MM-DD или null",
      "notes": "краткий контекст или пустая строка"
    }}
  ]
}}

{recent_forward_context(chat_id)}
"""

    content = [{"type": "input_text", "text": prompt}]
    if image_url:
        content.append({
            "type": "input_image",
            "image_url": image_url,
            "detail": "original",
        })

    payload = {
        "model": MODEL,
        "instructions": (
            "Ты рабочий классификатор входящих поручений. "
            "Отвечай только валидным JSON без Markdown."
        ),
        "input": [{"role": "user", "content": content}],
        "store": False,
    }

    r = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    parsed = parse_json_from_model(extract_openai_text(r.json()))
    if not isinstance(parsed, dict):
        raise ValueError("Forward classifier returned non-object")
    parsed.setdefault("summary", "")
    parsed.setdefault("items", [])
    return parsed


def save_forwarded_inbox(chat_id, message, source_name, raw_text, parsed):
    conn = get_db()
    conn.execute(
        """
        INSERT OR REPLACE INTO forwarded_inbox(
            chat_id,telegram_message_id,source_name,raw_text,has_image,parsed_json,created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            chat_id,
            message.get("message_id", 0),
            source_name or "",
            raw_text or "",
            1 if message.get("photo") else 0,
            json.dumps(parsed, ensure_ascii=False),
            datetime.now(TZ).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()


def handle_forwarded_message(chat_id, message):
    source_name = get_forward_source_name(message)
    raw_text = message_text_or_caption(message)
    source_ref = f"telegram:{message.get('message_id', '')}"

    # 1) Сначала мгновенный локальный разбор.
    # Для очевидного "Маш, надо..." вообще не показываем "Разбираю..."
    # и не обращаемся в OpenAI.
    parsed = fast_parse_simple_forward(message)

    if parsed is not None:
        try:
            created_tasks = []

            for item in parsed.get("items", [])[:6]:
                if str(item.get("kind", "")).strip().lower() != "task":
                    continue

                item_text = str(item.get("text", "")).strip()
                if not item_text:
                    continue

                tid = add_task(
                    chat_id,
                    item_text,
                    source="forwarded",
                    source_ref=source_ref,
                    due_at=item.get("due_at") or None,
                    notes=str(item.get("notes", "") or ""),
                )
                created_tasks.append((tid, item_text))

            save_forwarded_inbox(
                chat_id,
                message,
                source_name,
                raw_text,
                parsed,
            )

            if created_tasks:
                lines = ["Пересылку сохранила ✅", "Задача:"]
                for tid, item_text in created_tasks:
                    lines.append(f"#{tid} — {item_text}")
                send_message(chat_id, "\n".join(lines))
            else:
                send_message(chat_id, "Пересылку сохранила ✅")

            return True

        except Exception as e:
            print("Fast forward save error:", repr(e))
            send_message(
                chat_id,
                "Пересылку поняла, но не смогла записать задачу в базу. "
                "Посмотри лог Railway — там будет строка Fast forward save error."
            )
            return True

    # 2) Только сложные/неоднозначные пересылки отправляем в OpenAI.
    send_message(
        chat_id,
        f"Разбираю сложную пересылку{f' от {source_name}' if source_name else ''}..."
    )

    try:
        parsed = analyze_forwarded_message(chat_id, message)
    except Exception as e:
        print("Forward analysis error:", repr(e))
        parsed = {
            "summary": "Не смогла автоматически разобрать пересылку.",
            "items": [{
                "kind": "info",
                "text": raw_text or "Переслано изображение без текста.",
                "due_at": None,
                "notes": "Нужен ручной разбор.",
            }],
        }

    created_tasks = []
    created_questions = []
    info_items = []

    try:
        for item in parsed.get("items", [])[:6]:
            kind = str(item.get("kind", "")).strip().lower()
            item_text = str(item.get("text", "")).strip()
            if not item_text:
                continue

            due_at = item.get("due_at") or None
            notes = str(item.get("notes", "") or "")

            if kind == "task":
                tid = add_task(
                    chat_id,
                    item_text,
                    source="forwarded",
                    source_ref=source_ref,
                    due_at=due_at,
                    notes=notes,
                )
                created_tasks.append((tid, item_text))

            elif kind == "ask_ms":
                qid = add_ms_question(
                    chat_id,
                    item_text,
                    due_at=due_at,
                    source="forwarded",
                    source_ref=source_ref,
                    notes=notes,
                )
                created_questions.append((qid, item_text))

            else:
                info_items.append(item_text)

        save_forwarded_inbox(
            chat_id,
            message,
            source_name,
            raw_text,
            parsed,
        )

    except Exception as e:
        print("Forward save error:", repr(e))
        send_message(
            chat_id,
            "Пересылку разобрала, но не смогла сохранить результат в базу. "
            "Посмотри лог Railway — там будет Forward save error."
        )
        return True

    lines = ["Пересылку сохранила ✅"]

    if parsed.get("summary"):
        lines.append(parsed["summary"])

    if created_tasks:
        lines.append("\nЗадачи:")
        for tid, item_text in created_tasks:
            lines.append(f"#{tid} — {item_text}")

    if created_questions:
        lines.append("\nВопросы МС / в вечерний апдейт:")
        for qid, item_text in created_questions:
            lines.append(f"Q{qid} — {item_text}")

    if info_items:
        lines.append("\nСправочно:")
        for item_text in info_items[:3]:
            lines.append(f"— {item_text}")

    if not created_tasks and not created_questions and not info_items:
        lines.append("\nНовых действий не нашла.")

    send_message(chat_id, "\n".join(lines))
    return True



def evening_process_status(chat_id):
    """
    Build factual evening blocks directly from SQLite.
    No AI decides what is done / waiting / blocked.
    """
    waiting = []
    next_steps = []

    for p in get_open_processes(chat_id):
        title_norm = normalize_text(p["title"])

        # Old broad originals backlog should not dominate every evening.
        if "оригинал" in title_norm and "бухгалтер" in title_norm:
            continue

        steps = get_process_steps(p["id"])
        by_no = {s["step_no"]: s for s in steps}

        for s in steps:
            if s["status"] != "open":
                continue
            if not is_step_available(s, by_no):
                continue

            line = s["text"]
            if s["waiting_for"]:
                waiting.append((s["waiting_for"], line, p["title"]))
            else:
                next_steps.append((line, p["title"]))

    # Deduplicate exact text.
    def unique_rows(rows):
        result, seen = [], set()
        for row in rows:
            key = task_dedupe_key(" | ".join(str(x) for x in row))
            if key in seen:
                continue
            seen.add(key)
            result.append(row)
        return result

    return unique_rows(waiting), unique_rows(next_steps)


def evening_open_tasks(chat_id, limit=8):
    """
    Open standalone tasks, after exact duplicate cleanup.
    """
    collapse_exact_task_duplicates(chat_id)
    rows = get_open_tasks(chat_id)

    # Keep a manageable list; process tasks are handled separately.
    result = []
    seen = set()
    for row in rows:
        key = task_dedupe_key(row["text"])
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result[-limit:]


def get_completed_today_context(chat_id):
    today = datetime.now(TZ).date()
    lines = []
    conn = get_db()
    task_rows = conn.execute(
        "SELECT text,completed_at FROM tasks WHERE chat_id=? AND status='done' AND completed_at IS NOT NULL ORDER BY completed_at DESC",
        (chat_id,),
    ).fetchall()
    process_rows = conn.execute(
        """
        SELECT ps.text, ps.completed_at, p.title
        FROM process_steps ps JOIN processes p ON p.id=ps.process_id
        WHERE p.chat_id=? AND ps.status='done' AND ps.completed_at IS NOT NULL
        ORDER BY ps.completed_at DESC
        """,
        (chat_id,),
    ).fetchall()
    conn.close()
    for row in list(task_rows) + list(process_rows):
        try:
            dt = datetime.fromisoformat(row["completed_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            if dt.astimezone(TZ).date() == today:
                lines.append(row["text"])
        except Exception:
            pass
    # stable unique
    result = []
    seen = set()
    for item in lines:
        key = task_dedupe_key(item)
        if key and key not in seen:
            seen.add(key); result.append(item)
    return result[:10]


def build_evening_update(chat_id):
    # Repair the accidentally closed test question before building the update.
    ensure_alena_operations_question(chat_id)
    collapse_exact_task_duplicates(chat_id)

    questions = get_open_ms_questions(chat_id)
    completed = get_completed_today_context(chat_id)
    waiting, process_next = evening_process_status(chat_id)
    standalone = evening_open_tasks(chat_id, limit=8)

    lines = ["Итоги дня"]

    if completed:
        lines += ["", "Готово сегодня:"]
        lines += [f"— {x}" for x in completed]

    lines += ["", "Вопросы / нужно решение МС:"]
    if questions:
        for q in questions:
            suffix = f" (срок: {q['due_at']})" if q["due_at"] else ""
            lines.append(f"— Q{q['id']}: {q['text']}{suffix}")
    else:
        lines.append("— Сейчас открытых вопросов к МС нет.")

    if waiting:
        lines += ["", "Жду от других:"]
        for who, action, process_title in waiting[:8]:
            lines.append(f"— {who}: {action}")

    # Build deterministic next steps from available process steps + standalone tasks.
    next_lines = []
    for action, process_title in process_next:
        next_lines.append(action)

    for row in standalone:
        t = row["text"]
        n = normalize_text(t)

        # Do not resurrect the old "find Eldar email" formulation.
        if "эльдар" in n and ("найти" in n or "письмо" in n):
            t = "Включить статус МШР в нужную повестку и проверить, состоялось ли обсуждение презентации."

        # Don't surface the broad originals backlog as a default daily priority.
        if "оригинал" in n and "бухгалтер" in n:
            continue

        next_lines.append(t)

    # Exact dedupe and max 8.
    deduped = []
    seen = set()
    for item in next_lines:
        key = task_dedupe_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    if deduped:
        lines += ["", "Следующие шаги:"]
        lines += [f"— {x}" for x in deduped[:8]]

    # Due control items only — no generic long-term noise.
    due_control = []
    today = datetime.now(TZ).date()
    for row in get_control_items(chat_id):
        due = False
        if row["next_check"]:
            try:
                due = date.fromisoformat(row["next_check"]) <= today
            except Exception:
                pass
        if row["deadline"]:
            try:
                due = due or date.fromisoformat(row["deadline"]) <= today + timedelta(days=3)
            except Exception:
                pass
        if due:
            due_control.append(row)

    if due_control:
        lines += ["", "Контрольные точки:"]
        for row in due_control[:6]:
            lines.append(f"— {row['title']}: {row['next_action']}")

    return "\n".join(lines)


def show_forwarded_inbox(chat_id, limit=10):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT * FROM forwarded_inbox
        WHERE chat_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()
    conn.close()

    if not rows:
        send_message(chat_id, "Пересланных входящих пока нет.")
        return

    lines = ["Последние пересланные входящие:"]
    for row in rows:
        raw = (row["raw_text"] or "[изображение]").replace("\n", " ")
        lines.append(
            f"\n#{row['id']} — {row['source_name'] or 'источник не указан'}\n"
            f"{raw[:280]}"
        )
    send_message(chat_id, "\n".join(lines))


# =========================================================
# TELEGRAM
# =========================================================

def send_message(chat_id, text):
    text = str(text or "")
    parts = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    for part in parts:
        r = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": part},
            timeout=30,
        )
        r.raise_for_status()

# =========================================================
# CALENDAR
# =========================================================

def get_yandex_client():
    return caldav.DAVClient(
        url=YANDEX_CALDAV_URL,
        username=YANDEX_CALENDAR_LOGIN,
        password=YANDEX_CALENDAR_PASSWORD,
    )


def get_yandex_calendars():
    principal = get_yandex_client().principal()
    try:
        return principal.get_calendars()
    except AttributeError:
        return principal.calendars()


def normalize_calendar_datetime(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ), False
        return value.astimezone(TZ), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=TZ), True
    return None, False


def extract_attendees(component):
    raw = component.get("ATTENDEE")
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    out = []
    for a in raw:
        try: name = a.params.get("CN", "")
        except Exception: name = ""
        try: partstat = a.params.get("PARTSTAT", "")
        except Exception: partstat = ""
        out.append({"name": str(name), "value": str(a), "partstat": str(partstat)})
    return out


def extract_event_from_ical(ical_data, calendar_name=""):
    if not ical_data:
        return []
    if isinstance(ical_data, str):
        ical_data = ical_data.encode("utf-8")
    parsed = Calendar.from_ical(ical_data)
    result = []
    for component in parsed.walk():
        if component.name != "VEVENT":
            continue
        start_prop = component.get("DTSTART")
        if not start_prop:
            continue
        start, all_day = normalize_calendar_datetime(start_prop.dt)
        if start is None:
            continue
        end = None
        end_prop = component.get("DTEND")
        if end_prop:
            end, _ = normalize_calendar_datetime(end_prop.dt)
        result.append({
            "uid": str(component.get("UID", "")),
            "summary": str(component.get("SUMMARY", "Без названия")),
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": str(component.get("LOCATION", "")),
            "description": str(component.get("DESCRIPTION", "")),
            "url": str(component.get("URL", "")),
            "calendar": calendar_name,
            "attendees": extract_attendees(component),
        })
    return result


def get_calendar_events(start_dt, end_dt):
    collected = []
    for cal in get_yandex_calendars():
        name = getattr(cal, "name", None) or "Календарь"
        try:
            events = cal.date_search(start=start_dt, end=end_dt, expand=True)
        except TypeError:
            events = cal.date_search(start_dt, end_dt, expand=True)
        for ev in events:
            try:
                collected.extend(extract_event_from_ical(ev.data, name))
            except Exception as e:
                print("Calendar parse error:", repr(e))

    unique = {}
    for ev in collected:
        key = (
            ev["summary"].strip().lower(),
            ev["start"].isoformat() if ev["start"] else "",
            ev["end"].isoformat() if ev["end"] else "",
        )
        if key not in unique:
            unique[key] = ev
        else:
            ex = unique[key]
            for field in ("location", "description", "url"):
                if not ex[field] and ev[field]:
                    ex[field] = ev[field]
            if not ex["attendees"] and ev["attendees"]:
                ex["attendees"] = ev["attendees"]
    result = list(unique.values())
    result.sort(key=lambda x: x["start"])
    return result


def format_event(ev):
    if ev["all_day"]:
        when = "весь день"
    elif ev["end"]:
        when = f"{ev['start'].strftime('%H:%M')}–{ev['end'].strftime('%H:%M')}"
    else:
        when = ev["start"].strftime("%H:%M")
    text = f"{when} — {ev['summary']}"
    if ev["location"]:
        text += f"\n📍 {ev['location']}"
    return text


def format_calendar_day(target_date, title):
    start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=TZ)
    events = get_calendar_events(start, start + timedelta(days=1))
    if not events:
        return f"{title}\n\nВ календаре событий нет."
    return title + "\n\n" + "\n\n".join(format_event(e) for e in events)


def format_calendar_period(start_date, days):
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=TZ)
    events = get_calendar_events(start, start + timedelta(days=days))
    if not events:
        return "На этот период в календаре событий нет."
    grouped = {}
    for e in events:
        d = e["start"].astimezone(TZ).date()
        grouped.setdefault(d, []).append(e)
    wd = {0:"Пн",1:"Вт",2:"Ср",3:"Чт",4:"Пт",5:"Сб",6:"Вс"}
    lines = []
    for d in sorted(grouped):
        lines.append(f"{wd[d.weekday()]}, {d.strftime('%d.%m')}")
        lines.extend(format_event(e) for e in grouped[d])
        lines.append("")
    return "\n".join(lines).strip()


def calendar_context(start_date, days=1):
    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=TZ)
    events = get_calendar_events(start, start + timedelta(days=days))
    lines = ["КАЛЕНДАРЬ:"]
    if not events:
        return "КАЛЕНДАРЬ:\nСобытий нет."
    for i, e in enumerate(events, 1):
        if e["all_day"]:
            when = e["start"].strftime("%d.%m.%Y") + " весь день"
        elif e["end"]:
            when = e["start"].strftime("%d.%m.%Y %H:%M") + "–" + e["end"].strftime("%H:%M")
        else:
            when = e["start"].strftime("%d.%m.%Y %H:%M")
        desc = e["description"].strip()
        if len(desc) > 1800:
            desc = desc[:1800] + "..."
        lines.extend([
            "",
            f"Событие {i}",
            f"Название: {e['summary']}",
            f"Время: {when}",
            f"Место/адрес: {e['location'] or 'не указано'}",
            f"Описание: {desc or 'не указано'}",
        ])
        if e["attendees"]:
            lines.append("Участники: " + "; ".join(
                f"{a['name'] or a['value']} (статус: {a['partstat'] or 'не указан'})" for a in e["attendees"]
            ))
    return "\n".join(lines)

# =========================================================
# MAIL
# =========================================================

def decode_mime_header(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def get_mail_connection():
    client = IMAPClient(YANDEX_IMAP_HOST, port=YANDEX_IMAP_PORT, ssl=True)
    client.login(YANDEX_MAIL_LOGIN, YANDEX_MAIL_PASSWORD)
    return client


def clean_html_text(value):
    if not value:
        return ""
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def decode_part_payload(part):
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_mail_body(msg):
    plain, rich = [], []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in disposition:
                continue
            ctype = part.get_content_type()
            value = decode_part_payload(part)
            if ctype == "text/plain" and value:
                plain.append(value)
            elif ctype == "text/html" and value:
                rich.append(clean_html_text(value))
    else:
        value = decode_part_payload(msg)
        if msg.get_content_type() == "text/html":
            rich.append(clean_html_text(value))
        else:
            plain.append(value)
    body = "\n".join(plain or rich).strip()
    return body[:10000] + ("\n...[письмо сокращено]" if len(body) > 10000 else "")


def get_attachment_names(msg):
    result = []
    for part in msg.walk():
        filename = part.get_filename()
        if filename:
            result.append(decode_mime_header(filename))
    return result


def parse_email_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None


def parse_full_email(raw_email, folder_name, uid):
    msg = email.message_from_bytes(raw_email)
    return {
        "uid": uid,
        "folder": str(folder_name),
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "subject": decode_mime_header(msg.get("Subject", "")),
        "from": decode_mime_header(msg.get("From", "")),
        "to": decode_mime_header(msg.get("To", "")),
        "date": parse_email_date(msg.get("Date", "")),
        "body": extract_mail_body(msg),
        "attachments": get_attachment_names(msg),
    }

RUS_TO_LAT = {
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
    "и":"i","й":"y","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
    "с":"s","т":"t","у":"u","ф":"f","х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"sch",
    "ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya"
}
MAIL_STOP_WORDS = {"найди","найти","письмо","письма","переписку","переписка","почта","почте","поищи","про","от","по","с","мне","мои","мою","все","всё","что","было","есть","посмотри","покажи"}
SKIP_MAIL_FOLDERS = {"drafts","drafts|template","outbox","spam","trash","черновики","спам","удаленные","удалённые"}
BLOCKED_MAIL_DOMAINS = {"calendar.yandex.ru", "mailer.yandex.ru"}
BLOCKED_MAIL_LOCALPARTS = {"noreply","no-reply","mailer-daemon","postmaster","notifications","notification"}


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value).lower().replace("ё", "е")).strip()


def transliterate_ru(value):
    return "".join(RUS_TO_LAT.get(c, c) for c in normalize_text(value))


def russian_stem(word):
    word = normalize_text(word)
    candidates = {word}
    for ending in ["ами","ями","ого","ему","ому","ом","ем","ах","ях","ов","ев","а","я","у","ю","е","ы","и"]:
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            candidates.add(word[:-len(ending)])
    return min(candidates, key=len)


def extract_query_alias(query):
    words = re.findall(r"[а-яёa-z]+", normalize_text(query), flags=re.I)
    useful = [w for w in words if w not in MAIL_STOP_WORDS and len(w) >= 4]
    return russian_stem(useful[0]) if useful else ""


def extract_email_addresses(value):
    return [addr.lower() for _, addr in getaddresses([value]) if addr and "@" in addr]


def is_system_email(address):
    address = str(address).strip().lower()
    if "@" not in address:
        return True
    local, domain = address.rsplit("@", 1)
    return (
        domain in BLOCKED_MAIL_DOMAINS
        or local in BLOCKED_MAIL_LOCALPARTS
        or local.startswith("noreply")
        or local.startswith("no-reply")
        or local.startswith("mailer-daemon")
    )


def save_verified_mail_contact(chat_id, alias, email_address, display_name):
    alias = normalize_text(alias)
    email_address = email_address.strip().lower()
    if not alias or not email_address or is_system_email(email_address):
        return False
    conn = get_db()
    conn.execute("DELETE FROM mail_contacts WHERE chat_id=? AND alias=? AND email!=?", (chat_id, alias, email_address))
    conn.execute("""
        INSERT INTO mail_contacts(chat_id,alias,email,display_name,created_at,verified,source)
        VALUES (?,?,?,?,?,1,'matched-name')
        ON CONFLICT(chat_id,alias,email) DO UPDATE SET display_name=excluded.display_name,verified=1,source='matched-name'
    """, (chat_id, alias, email_address, display_name, datetime.now(TZ).isoformat(timespec="seconds")))
    conn.commit(); conn.close()
    print("Verified mail contact saved:", repr(alias), "->", repr(email_address))
    return True


def get_verified_mail_contacts(chat_id, query):
    alias = extract_query_alias(query)
    if not alias:
        return []
    alias_lat = transliterate_ru(alias)
    conn = get_db()
    rows = conn.execute("SELECT alias,email,display_name FROM mail_contacts WHERE chat_id=? AND verified=1", (chat_id,)).fetchall()
    conn.close()
    return [r for r in rows if not is_system_email(r["email"]) and (normalize_text(r["alias"]) == alias or transliterate_ru(r["alias"]) == alias_lat)]


def list_verified_mail_contacts(chat_id):
    conn = get_db()
    rows = conn.execute("SELECT alias,email,display_name FROM mail_contacts WHERE chat_id=? AND verified=1 ORDER BY alias", (chat_id,)).fetchall()
    conn.close()
    return [r for r in rows if not is_system_email(r["email"])]


def forget_mail_contact(chat_id, query):
    alias = extract_query_alias(query) or normalize_text(query)
    conn = get_db()
    cur = conn.execute("DELETE FROM mail_contacts WHERE chat_id=? AND alias=?", (chat_id, alias))
    count = cur.rowcount
    conn.commit(); conn.close(); return count


def contact_match_score(alias, display_name, email_address):
    if is_system_email(email_address):
        return -100
    alias = normalize_text(alias)
    alias_lat = transliterate_ru(alias)
    display = normalize_text(display_name)
    display_lat = transliterate_ru(display)
    local = email_address.split("@", 1)[0].lower()
    score = 0
    if alias and alias in display: score += 20
    if alias_lat and alias_lat in display_lat: score += 15
    if alias_lat and alias_lat in local: score += 12
    if display: score += 1
    return score


def remember_best_contact_from_messages(chat_id, query, messages):
    alias = extract_query_alias(query)
    if not alias:
        return
    candidates = {}
    for item in messages:
        for header in ("from", "to"):
            raw = item.get(header, "")
            for display_name, addr in getaddresses([raw]):
                addr = addr.strip().lower()
                if not addr or addr == YANDEX_MAIL_LOGIN.lower() or is_system_email(addr):
                    continue
                name = decode_mime_header(display_name)
                score = contact_match_score(alias, name, addr)
                if addr not in candidates or score > candidates[addr]["score"]:
                    candidates[addr] = {"score": score, "display_name": name or raw}
    if not candidates:
        return
    best_email, best = max(candidates.items(), key=lambda kv: kv[1]["score"])
    print("Best contact candidate:", repr(alias), "->", repr(best_email), "score:", best["score"])
    if best["score"] >= 10:
        save_verified_mail_contact(chat_id, alias, best_email, best["display_name"])


def get_useful_mail_folders(client):
    result = []
    for flags, delimiter, folder_name in client.list_folders():
        flags_text = {(f.decode(errors="ignore") if isinstance(f, bytes) else str(f)).lower() for f in flags}
        if "\\noselect" in flags_text:
            continue
        if str(folder_name).strip().lower() in SKIP_MAIL_FOLDERS:
            continue
        result.append(folder_name)
    def priority(folder):
        value = str(folder).lower()
        return 0 if value == "inbox" else 1 if value == "sent" else 2 if value == "archive" else 3
    result.sort(key=priority)
    return result


def build_fast_search_terms(chat_id, query):
    exact = extract_email_addresses(query)
    if exact:
        return [exact[0]]
    known = get_verified_mail_contacts(chat_id, query)
    if known:
        print("Using VERIFIED cached contact:", known[0]["email"])
        return [known[0]["email"]]
    words = re.findall(r"[a-zа-яё0-9._+-]+", normalize_text(query), flags=re.I)
    useful = [w for w in words if w not in MAIL_STOP_WORDS and len(w) >= 3]
    if not useful:
        return []
    stem = russian_stem(max(useful, key=len))
    result = [stem]
    lat = transliterate_ru(stem)
    if lat and lat != stem:
        result.append(lat)
    return result[:2]


def search_headers_only(client, terms):
    found = set()
    for term in terms:
        fields = ["FROM", "TO"] if "@" in term else ["FROM", "TO", "SUBJECT"]
        for field in fields:
            try:
                found.update(client.search([field, term], charset="UTF-8"))
            except Exception as e:
                print("IMAP search failed:", field, repr(term), repr(e))
    return list(found)


def search_text_fallback(client, terms):
    found = set()
    for term in terms:
        try:
            found.update(client.search(["TEXT", term], charset="UTF-8"))
        except Exception as e:
            print("IMAP TEXT failed:", repr(term), repr(e))
    return list(found)


def fetch_messages(client, folder_name, uids, max_count=25):
    if not uids:
        return []
    selected = sorted(uids, reverse=True)[:max_count]
    fetched = client.fetch(selected, ["RFC822"])
    result = []
    for uid in selected:
        values = fetched.get(uid)
        if not values:
            continue
        raw = values.get(b"RFC822") or values.get("RFC822")
        if raw:
            result.append(parse_full_email(raw, folder_name, uid))
    return result


def relevance_score(item, terms):
    subject, sender, recipient, body = map(normalize_text, [item["subject"], item["from"], item["to"], item["body"]])
    score, matched = 0, []
    for term in terms:
        t = normalize_text(term)
        part = (10 if t in sender else 0) + (7 if t in recipient else 0) + (8 if t in subject else 0) + (2 if t in body else 0)
        if part:
            score += part; matched.append(term)
    item["matched_terms"] = sorted(set(matched))
    return score


def search_mail_fast(chat_id, query, result_limit=15):
    terms = build_fast_search_terms(chat_id, query)
    print("Fast mail search terms:", terms)
    if not terms:
        return []
    client = get_mail_connection()
    all_results = []
    try:
        folders = get_useful_mail_folders(client)
        print("Useful mail folders:", folders)
        for folder in folders:
            started = time.time()
            try:
                client.select_folder(folder, readonly=True)
                uids = search_headers_only(client, terms)
                print("Header search folder:", repr(folder), "uids:", len(uids), "time:", round(time.time()-started,2), "sec")
                for item in fetch_messages(client, folder, uids):
                    item["score"] = relevance_score(item, terms)
                    if item["score"] > 0:
                        all_results.append(item)
            except Exception as e:
                print("Folder search error:", repr(folder), repr(e))

        if not all_results:
            print("No header matches. Starting TEXT fallback.")
            for folder in folders:
                started = time.time()
                try:
                    client.select_folder(folder, readonly=True)
                    uids = search_text_fallback(client, terms)
                    print("TEXT search folder:", repr(folder), "uids:", len(uids), "time:", round(time.time()-started,2), "sec")
                    for item in fetch_messages(client, folder, uids):
                        item["score"] = relevance_score(item, terms)
                        if item["score"] > 0:
                            all_results.append(item)
                except Exception as e:
                    print("TEXT folder error:", repr(folder), repr(e))

        unique = {}
        for item in all_results:
            key = item["message_id"] or (item["folder"], item["uid"])
            if key not in unique or item["score"] > unique[key]["score"]:
                unique[key] = item
        results = list(unique.values())
        results.sort(key=lambda x: (x["score"], x["date"].timestamp() if x["date"] else 0), reverse=True)
        results = results[:result_limit]
        if results:
            remember_best_contact_from_messages(chat_id, query, results)
        return results
    finally:
        try: client.logout()
        except Exception: pass


def get_recent_mail(limit=10):
    client = get_mail_connection()
    try:
        client.select_folder("INBOX", readonly=True)
        uids = client.search(["ALL"])
        if not uids:
            return []
        selected = uids[-limit:][::-1]
        fetched = client.fetch(selected, ["RFC822"])
        result = []
        for uid in selected:
            values = fetched.get(uid)
            raw = values.get(b"RFC822") or values.get("RFC822") if values else None
            if raw:
                result.append(parse_full_email(raw, "INBOX", uid))
        return result
    finally:
        try: client.logout()
        except Exception: pass


def format_mail_date(dt):
    return dt.strftime("%d.%m.%Y %H:%M") if dt else "дата неизвестна"


def format_mail_list(messages, title):
    if not messages:
        return title + "\n\nНичего не нашла."
    lines = [title]
    for i, item in enumerate(messages, 1):
        lines.extend(["", f"{i}. {format_mail_date(item['date'])}", f"От: {item['from']}", f"Тема: {item['subject']}"])
    return "\n".join(lines)


def mail_context(messages, body_limit=7000):
    if not messages:
        return "ПОЧТА:\nПодходящих писем не найдено."
    lines = ["ПОЧТА:"]
    ordered = sorted(messages, key=lambda x: x["date"] or datetime.min.replace(tzinfo=TZ))
    for i, item in enumerate(ordered, 1):
        body = item["body"][:body_limit]
        lines.extend([
            "", f"Письмо {i}", f"Дата: {format_mail_date(item['date'])}", f"Папка: {item['folder']}",
            f"От: {item['from']}", f"Кому: {item['to']}", f"Тема: {item['subject']}",
            "Вложения: " + (", ".join(item["attachments"]) if item["attachments"] else "нет"),
            "Текст письма:\n" + (body or "[текст пуст]"),
        ])
    return "\n".join(lines)

# =========================================================
# OPENAI
# =========================================================

def build_saved_context(chat_id):
    sections = []
    memories = get_memories(chat_id)
    tasks = get_open_tasks(chat_id)
    if memories:
        sections.append("ПОСТОЯННАЯ ПАМЯТЬ:\n" + "\n".join(f"- {r['text']}" for r in memories))
    if tasks:
        sections.append("ОТКРЫТЫЕ ЗАДАЧИ:\n" + "\n".join(f"- Задача #{r['id']}: {r['text']}" for r in tasks))
    sections.append(processes_context(chat_id))
    return "\n\n".join(sections)


def extract_openai_text(data):
    texts = []
    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts) if texts else "Я получила ответ, но не смогла разобрать его текст."


def ask_openai(chat_id, user_text, extra_context="", use_history=True):
    sections = [build_saved_context(chat_id)]
    if extra_context:
        sections.append(extra_context)
    sections.append("ТЕКУЩЕЕ СООБЩЕНИЕ МАШИ:\n" + user_text)
    payload = {
        "model": MODEL,
        "instructions": ASSISTANT_INSTRUCTIONS,
        "input": "\n\n".join(sections),
        "store": True,
    }
    previous_id = get_previous_response_id(chat_id) if use_history else None
    if previous_id:
        payload["previous_response_id"] = previous_id
    r = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if r.status_code == 400 and previous_id:
        clear_previous_response_id(chat_id)
        payload.pop("previous_response_id", None)
        r = requests.post(
            OPENAI_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
    r.raise_for_status()
    data = r.json()
    if data.get("id") and use_history:
        save_previous_response_id(chat_id, data["id"])
    return extract_openai_text(data)



def brief_processes_context(chat_id):
    """
    Контекст процессов специально для /brief.
    Не передаём противоречивые подсказки вроде
    '[готово] ... ; ждём: Алёна'.
    """
    processes = get_open_processes(chat_id)
    if not processes:
        return "АКТУАЛЬНЫЕ ПРОЦЕССЫ:\nОткрытых процессов нет."

    today = datetime.now(TZ).date()
    lines = ["АКТУАЛЬНЫЕ ПРОЦЕССЫ:"]

    for p in processes:
        title_norm = normalize_text(p["title"])

        # Накопительный хвост оригиналов не тащим в ежедневный бриф.
        # По пятницам его можно показывать.
        if "оригинал" in title_norm and "бухгалтер" in title_norm and today.weekday() != 4:
            continue

        steps = get_process_steps(p["id"])
        by_no = {s["step_no"]: s for s in steps}

        done_steps = [s for s in steps if s["status"] == "done"]
        open_steps = [s for s in steps if s["status"] != "done"]

        lines.append(f"\nПроцесс #{p['id']}: {p['title']}")

        if done_steps:
            lines.append("Уже готово:")
            for s in done_steps:
                lines.append(f"- {s['text']}")

        available = [s for s in open_steps if is_step_available(s, by_no)]
        blocked = [s for s in open_steps if not is_step_available(s, by_no)]

        if available:
            lines.append("Можно делать сейчас:")
            for s in available:
                line = f"- {s['text']}"
                if s["waiting_for"]:
                    line += f"; ждём: {s['waiting_for']}"
                if s["remind_every_days"]:
                    line += f"; контроль каждые {s['remind_every_days']} дн."
                lines.append(line)

        if blocked:
            lines.append("Пока заблокировано:")
            for s in blocked:
                lines.append(f"- {s['text']}")

    return "\n".join(lines)


def brief_tasks_context(chat_id):
    """
    Обычные задачи — вторичный источник.
    Убираем известную устаревшую формулировку по Эльдару:
    письмо уже найдено, следующий шаг — повестка.
    """
    tasks = get_open_tasks(chat_id)
    if not tasks:
        return "ОБЫЧНЫЕ ЗАДАЧИ:\nОткрытых задач нет."

    lines = ["ОБЫЧНЫЕ ЗАДАЧИ (ВТОРИЧНЫЙ ИСТОЧНИК):"]
    eldar_replaced = False

    for r in tasks:
        t = r["text"]
        n = normalize_text(t)

        if "эльдар" in n and ("найти" in n or "поднять письмо" in n or "письмо" in n):
            if not eldar_replaced:
                lines.append(
                    "- Эльдар / МШР: письмо уже найдено. "
                    "Следующий шаг — включить статус проекта «МШР — Московская Школа Рекламы» "
                    "в нужную повестку и проверить, состоялось ли обсуждение презентации."
                )
                eldar_replaced = True
            continue

        lines.append(f"- #{r['id']}: {t}")

    return "\n".join(lines)


def ask_openai_grounded_brief(chat_id, source_context):
    """
    Отдельный вызов OpenAI для /brief.
    ВАЖНО: намеренно НЕ добавляет build_saved_context() и НЕ использует
    previous_response_id. Это не даёт старой памяти/истории переопределять
    актуальное состояние процессов.
    """
    instructions = ASSISTANT_INSTRUCTIONS + """

СПЕЦИАЛЬНЫЕ ПРАВИЛА ДЛЯ РАБОЧЕГО БРИФА:
1. Используй ТОЛЬКО факты из блока ИСТОЧНИКИ БРИФА в текущем запросе.
2. Не используй старую историю диалога для восстановления статусов задач.
3. При конфликте данных приоритет такой:
   АКТУАЛЬНЫЕ ПРОЦЕССЫ > КОНТРОЛЬНЫЕ ДЕЛА > ОБЫЧНЫЕ ЗАДАЧИ > КАЛЕНДАРЬ > СВЕЖАЯ ПОЧТА.
4. Этап процесса со статусом "готово" считается закрытым. Никогда не предлагай выполнить его снова.
5. "Заблокирован" означает: не предлагай действие сейчас, пока не выполнена зависимость.
6. Если обычная задача противоречит актуальному процессу, верь процессу.
7. Заголовок письма сам по себе не меняет статус процесса.
8. Не делай вывод, что подпись/счёт/акт/письмо отсутствует, если актуальный процесс говорит, что оно уже получено.
9. Не выдумывай выполненные действия, сроки, участников, ссылки или адреса.
"""
    payload = {
        "model": MODEL,
        "instructions": instructions,
        "input": "ИСТОЧНИКИ БРИФА:\n\n" + source_context,
        "store": False,
    }
    r = requests.post(
        OPENAI_URL,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    return extract_openai_text(r.json())


# =========================================================
# BRIEF
# =========================================================

def build_brief(chat_id):
    today = datetime.now(TZ).date()

    # Главный источник истины — актуальные статусы в SQLite.
    proc = brief_processes_context(chat_id)
    control = control_context(chat_id, due_only=True)
    task_context = brief_tasks_context(chat_id)
    cal = calendar_context(today, 1)

    try:
        recent = get_recent_mail(8)
        mail_lines = ["СВЕЖАЯ ПОЧТА:"]
        if recent:
            for m in recent:
                mail_lines.append(
                    f"- {format_mail_date(m['date'])}: {m['from']} — {m['subject']}"
                )
        else:
            mail_lines.append("Свежих писем не найдено.")
        mail_brief = "\n".join(mail_lines)
    except Exception as e:
        print("Brief mail error:", repr(e))
        mail_brief = "СВЕЖАЯ ПОЧТА:\nНе удалось получить почту."

    prompt = """
Составь текущий рабочий бриф Маши на сегодня.

ПОРЯДОК ДОВЕРИЯ К ИСТОЧНИКАМ:
1. АКТУАЛЬНЫЕ ПРОЦЕССЫ.
2. КОНТРОЛЬНЫЕ ДЕЛА.
3. ОБЫЧНЫЕ ЗАДАЧИ.
4. КАЛЕНДАРЬ.
5. ПОЧТА.

Формат:
1. Сегодня — ключевые встречи.
2. Риски календаря.
3. Что требует действия.
4. Что ждём от других.
5. Что нужно проконтролировать.
6. Почта — потенциально важное.
7. Топ-3 на сегодня.

ЖЁСТКИЕ ПРАВИЛА:
- Всё из блока "Уже готово" — ЗАКРЫТО. Никогда не проси это сделать снова.
- В "Что ждём от других" включай только открытые этапы из "Можно делать сейчас",
  у которых действительно указан "ждём".
- Этапы из "Пока заблокировано" не выдавай как действие на сейчас.
- Если процесс говорит, что подпись Алёны уже получена — не упоминай её как ожидаемую.
- Если процесс говорит, что счёт художника №1 уже получен — не запрашивай его снова.
- По Эльдару письмо уже найдено: не предлагай "найти" или "поднять письмо".
  Следующий шаг — включить МШР в нужную повестку.
- Давний накопительный процесс оригиналов не делай ежедневным приоритетом,
  если он не передан в текущих контрольных делах.
- Не требуй место/Zoom у каждой встречи. Упоминай реквизиты только если
  из календаря ясно, что встреча внешняя или онлайн.
- Не утверждай подтверждение участника без явного PARTSTAT=ACCEPTED.
- Не придумывай содержание писем по одному заголовку.

Перед отправкой ответа мысленно проверь:
"Не предложила ли я действие, которое в процессах уже находится в 'Уже готово'?"

Будь компактной и практичной.
"""

    source_context = "\n\n".join([
        "=== 1. АКТУАЛЬНЫЕ ПРОЦЕССЫ — ГЛАВНЫЙ ИСТОЧНИК ИСТИНЫ ===\n" + proc,
        "=== 2. КОНТРОЛЬНЫЕ ДЕЛА НА СЕГОДНЯ ===\n" + control,
        "=== 3. ОБЫЧНЫЕ ЗАДАЧИ ===\n" + task_context,
        "=== 4. КАЛЕНДАРЬ НА СЕГОДНЯ ===\n" + cal,
        "=== 5. СВЕЖАЯ ПОЧТА ===\n" + mail_brief,
        prompt,
    ])

    return ask_openai_grounded_brief(chat_id, source_context)

# =========================================================
# DISPLAY HELPERS
# =========================================================

def show_tasks(chat_id):
    rows = get_open_tasks(chat_id)
    send_message(chat_id, "Сейчас открытых задач нет." if not rows else "Твои открытые задачи:\n\n" + "\n".join(f"#{r['id']} — {r['text']}" for r in rows))


def show_memory(chat_id):
    rows = get_memories(chat_id)
    send_message(chat_id, "В постоянной памяти пока ничего нет." if not rows else "Вот что я помню:\n\n" + "\n".join(f"#{r['id']} — {r['text']}" for r in rows))


def show_processes(chat_id):
    rows = get_open_processes(chat_id)
    if not rows:
        send_message(chat_id, "Открытых процессов пока нет.")
        return
    lines = ["Открытые процессы:"]
    for p in rows:
        steps = get_process_steps(p["id"])
        by_no = {s["step_no"]: s for s in steps}
        available = [s for s in steps if is_step_available(s, by_no)]
        next_text = available[0]["text"] if available else "нет доступного шага"
        lines.append(f"\n#{p['id']} — {p['title']}\nСледующий шаг: {next_text}")
    send_message(chat_id, "\n".join(lines))


def show_mail_contacts(chat_id):
    rows = list_verified_mail_contacts(chat_id)
    if not rows:
        send_message(chat_id, "Проверенных почтовых контактов пока нет.")
        return
    lines = ["Проверенные почтовые контакты:"]
    for r in rows:
        lines.append(f"\n{r['alias']} → {r['email']}")
    send_message(chat_id, "\n".join(lines))


def strip_punctuation(text):
    return re.sub(r"[?!.,]+$", "", text.strip().lower()).strip()

# =========================================================
# COMMAND HANDLERS
# =========================================================

def try_handle_process(chat_id, text):
    normalized = re.sub(r"\s+", " ", text.strip()).strip()
    clean = strip_punctuation(normalized)

    if clean in {"/processes", "процессы", "покажи процессы", "мои процессы"}:
        show_processes(chat_id); return True

    if clean in {"/giftcase", "создай процесс по подаркам", "создай процесс подарки директорам"}:
        pid, created = seed_gift_process(chat_id)
        if created:
            send_message(chat_id, "Создала первый процесс ✅\n\n" + format_process(pid, chat_id))
        else:
            send_message(chat_id, "Он уже есть. Вот текущее состояние:\n\n" + format_process(pid, chat_id))
        return True

    m = re.match(r"^/(?:process|case)\s+(\d+)\s*$", normalized, flags=re.I)
    if m:
        send_message(chat_id, format_process(int(m.group(1)), chat_id)); return True

    m = re.match(r"^(?:покажи процесс|процесс)\s*#?(\d+)\s*$", normalized, flags=re.I)
    if m:
        send_message(chat_id, format_process(int(m.group(1)), chat_id)); return True

    m = re.match(r"^(?:/donep|закрой этап|этап готов)\s*#?(\d+)[\.:](\d+)\s*$", normalized, flags=re.I)
    if m:
        pid, step_no = int(m.group(1)), int(m.group(2))
        ok, msg = complete_process_step(chat_id, pid, step_no)
        send_message(chat_id, msg)
        if ok:
            send_message(chat_id, format_process(pid, chat_id))
        return True

    m = re.match(r"^(?:создай процесс|новый процесс|процесс:)\s+(.+)$", normalized, flags=re.I)
    if m:
        title = m.group(1).strip()
        pid = create_process(chat_id, title)
        send_message(chat_id, f"Создала процесс #{pid}: {title}\n\nДобавить этап: /step {pid} текст этапа")
        return True

    m = re.match(r"^/step\s+(\d+)\s+(.+)$", normalized, flags=re.I | re.S)
    if m:
        pid, step_text = int(m.group(1)), m.group(2).strip()
        if not get_process(pid, chat_id):
            send_message(chat_id, "Не нашла такой процесс."); return True
        steps = get_process_steps(pid)
        step_no = (max([s["step_no"] for s in steps]) + 1) if steps else 1
        add_process_step(pid, step_no, step_text)
        send_message(chat_id, f"Добавила этап {pid}.{step_no} ✅")
        return True

    return False


def try_handle_mail(chat_id, text):
    normalized = re.sub(r"\s+", " ", text.strip()).strip()
    clean = strip_punctuation(normalized)

    if clean == "/mail":
        try:
            c = get_mail_connection(); folders = get_useful_mail_folders(c); c.logout()
            send_message(chat_id, f"Связь с Яндекс.Почтой есть ✅\nРабочих папок вижу: {len(folders)}.")
        except Exception as e:
            print("Mail connection error:", repr(e)); send_message(chat_id, "Не получилось подключиться к Яндекс.Почте.")
        return True

    if clean in {"/mailcontacts", "почтовые контакты", "покажи почтовые контакты"}:
        show_mail_contacts(chat_id); return True

    m = re.match(r"^(?:/mailforget|забудь почтовый контакт)\s+(.+)$", normalized, flags=re.I)
    if m:
        q = m.group(1).strip(); n = forget_mail_contact(chat_id, q)
        send_message(chat_id, f"Удалила почтовый контакт «{q}» ✅" if n else f"Контакт «{q}» не нашла.")
        return True

    if clean in {"/mailrecent", "последние письма", "покажи последние письма", "что нового в почте"}:
        try:
            send_message(chat_id, "Смотрю почту...")
            send_message(chat_id, format_mail_list(get_recent_mail(10), "Последние письма:"))
        except Exception as e:
            print("Recent mail error:", repr(e)); send_message(chat_id, "Не смогла прочитать почту.")
        return True

    patterns = [
        r"^найди\s+письмо\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+письма\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+переписку\s+(?:про|с|по)?\s*(.+)$",
        r"^найди\s+все\s+письма\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+всё\s+от\s+(.+)$",
        r"^поищи\s+письмо\s+(?:про|от|по)?\s*(.+)$",
        r"^поищи\s+в\s+почте\s+(.+)$",
        r"^поиск\s+почты\s*[,:-]?\s*(.+)$",
        r"^почта\s*[,:-]\s*(.+)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, normalized, flags=re.I)
        if not m:
            continue
        query = m.group(1).strip()
        try:
            started = time.time(); send_message(chat_id, f"Ищу в почте: {query}...")
            messages = search_mail_fast(chat_id, query)
            print("Mail search finished in", round(time.time()-started,2), "seconds")
            if not messages:
                send_message(chat_id, f"По запросу «{query}» ничего не нашла."); return True
            prompt = (
                f"Я попросила найти в почте: {query}.\n"
                "Проанализируй только реальные найденные письма. Если это цепочка — собери хронологию. "
                "Коротко: что нашлось; какое письмо нужное; суть; последняя договоренность; следующий шаг; связь с задачами/процессами."
            )
            answer = ask_openai(chat_id, prompt, extra_context=mail_context(messages))
            send_message(chat_id, answer)
        except Exception as e:
            print("Mail search error:", repr(e)); send_message(chat_id, "Не смогла выполнить поиск по почте. Посмотри лог Railway.")
        return True
    return False


def try_handle_calendar(chat_id, text):
    clean = strip_punctuation(text)
    today = datetime.now(TZ).date()

    if clean in {"/today","что у меня сегодня","что сегодня","встречи сегодня","календарь сегодня","какие у меня сегодня встречи"}:
        send_message(chat_id, format_calendar_day(today, "Сегодня:")); return True
    if clean in {"/tomorrow","что у меня завтра","что завтра","встречи завтра","календарь завтра"}:
        send_message(chat_id, format_calendar_day(today + timedelta(days=1), "Завтра:")); return True
    if clean in {"/calendarweek","календарь на неделю","встречи на неделю","что у меня в календаре на этой неделе"}:
        send_message(chat_id, format_calendar_period(today, 7)); return True
    if clean in {"/checktoday","проверь сегодня","проверь календарь на сегодня","проверь мой календарь на сегодня"}:
        prompt = "Проверь календарь: пересечения, воздух, обед, повестки, адреса, ссылки, подтверждения и связь с задачами/процессами. Не выдумывай."
        send_message(chat_id, "Проверяю календарь...")
        send_message(chat_id, ask_openai(chat_id, text, calendar_context(today, 1) + "\n\n" + prompt)); return True
    if clean in {"/checkweek","проверь неделю","проверь мою неделю"}:
        prompt = "Проверь неделю: перегруз, конфликты, отсутствие воздуха/обеда, подготовку к встречам и доступные рабочие процессы."
        send_message(chat_id, "Проверяю неделю...")
        send_message(chat_id, ask_openai(chat_id, text, calendar_context(today, 7) + "\n\n" + prompt)); return True
    return False


def try_handle_command(chat_id, text):
    normalized = re.sub(r"\s+", " ", text.strip()).strip()
    clean = strip_punctuation(normalized)

    if clean == "/version":
        send_message(chat_id, f"Masha 2.0 {BOT_VERSION} ✅")
        return True
    if clean in {"/questions","вопросы мс","что спросить у мс"}:
        show_ms_questions(chat_id)
        return True
    if clean in {"/update","/evening","вечерний апдейт","собери вечерний апдейт"}:
        send_message(chat_id, "Собираю вечерний апдейт...")
        try:
            send_message(chat_id, build_evening_update(chat_id))
        except Exception as e:
            print("Evening update error:", repr(e))
            send_message(chat_id, "Не смогла собрать вечерний апдейт. Посмотри лог Railway.")
        return True
    if clean in {"/inbox","входящие","пересланные входящие"}:
        show_forwarded_inbox(chat_id)
        return True

    # Natural answer with content: "МС ответила: ..." or "МС ответила на Q1: ..."
    if handle_natural_ms_answer(chat_id, normalized):
        return True

    m = re.match(r"^(?:/answered|ответила мс|мс ответила)\s*#?q?(\d+)\s*$", normalized, flags=re.I)
    if m:
        qid = int(m.group(1))
        send_message(
            chat_id,
            f"Вопрос Q{qid} закрыла ✅"
            if complete_ms_question(chat_id, qid)
            else f"Не нашла открытый вопрос Q{qid}."
        )
        return True

    # Фраза обычным человеческим языком тоже может сразу попасть в вечерний апдейт.
    if looks_like_ms_question(normalized):
        qid = add_ms_question(
            chat_id,
            normalized,
            due_at=detect_due_from_text(normalized),
            source="natural",
        )
        send_message(
            chat_id,
            f"Добавила в вопросы МС / вечерний апдейт ✅\nQ{qid} — {normalized}"
        )
        return True
    if clean in {"/control","контроль","что на контроле"}:
        show_control(chat_id)
        return True
    if clean in {"/week","дела на неделю","контроль на неделю"}:
        send_message(chat_id, "Собираю рабочий контроль на неделю...")
        try:
            show_week_control(chat_id)
        except Exception as e:
            print("Week control error:", repr(e))
            send_message(chat_id, "Не смогла собрать недельный контроль. Посмотри лог Railway.")
        return True

    if clean == "/start":
        send_message(chat_id, "Привет 👋 Я Маша 2.0. У меня есть ИИ, память, задачи, процессы, Яндекс.Календарь и read-only Яндекс.Почта."); return True
    if clean == "/health":
        send_message(chat_id, "Я работаю ✅"); return True
    if clean == "/new":
        clear_previous_response_id(chat_id); send_message(chat_id, "Начинаем новый разговор. Постоянную память, задачи и процессы я не забыла."); return True
    if clean in {"/brief","бриф","мой бриф","утренний бриф","что важно сегодня"}:
        send_message(chat_id, "Собираю бриф: календарь, задачи, процессы и свежую почту...")
        try:
            send_message(chat_id, build_brief(chat_id))
        except Exception as e:
            print("Brief error:", repr(e)); send_message(chat_id, "Не смогла собрать весь бриф. Посмотри лог Railway.")
        return True
    if clean in {"/tasks","задачи","мои задачи","покажи задачи","покажи мои задачи"}:
        show_tasks(chat_id); return True
    if clean in {"/memory","что ты помнишь","покажи память","что у тебя в памяти","что ты запомнила"}:
        show_memory(chat_id); return True

    m = re.match(r"^(?:закрой задачу|закрыть задачу|/done)\s*#?\s*(\d+)", normalized, flags=re.I)
    if m:
        tid = int(m.group(1)); send_message(chat_id, f"Задачу #{tid} закрыла ✅" if complete_task(chat_id, tid) else f"Не нашла открытую задачу #{tid}."); return True

    for pattern in [
        r"^задача\s*[,:-]?\s+(.+)$", r"^запиши\s+задачу\s*[,:-]?\s+(.+)$",
        r"^добавь\s+задачу\s*[,:-]?\s+(.+)$", r"^добавь\s+в\s+задачи\s*[,:-]?\s+(.+)$",
        r"^создай\s+задачу\s*[,:-]?\s+(.+)$", r"^поставь\s+задачу\s*[,:-]?\s+(.+)$",
        r"^запомни\s+задачу\s*[,:-]?\s+(.+)$",
    ]:
        m = re.match(pattern, normalized, flags=re.I | re.S)
        if m:
            t = m.group(1).strip(); tid = add_task(chat_id, t); send_message(chat_id, f"Записала задачу ✅\n#{tid}: {t}"); return True

    m = re.match(r"^(?:забудь|удали из памяти|удали запись)\s*#?\s*(\d+)", normalized, flags=re.I)
    if m:
        mid = int(m.group(1)); send_message(chat_id, f"Удалила запись #{mid}." if delete_memory(chat_id, mid) else f"Не нашла запись #{mid}."); return True

    for pattern in [r"^запомни\s*,?\s*что\s+(.+)$", r"^запомни\s*[,:-]?\s+(.+)$", r"^сохрани\s+в\s+память\s*[,:-]?\s+(.+)$"]:
        m = re.match(pattern, normalized, flags=re.I | re.S)
        if m:
            t = m.group(1).strip(); mid = add_memory(chat_id, t); send_message(chat_id, f"Запомнила 🧠\n#{mid}: {t}"); return True

    return False

# =========================================================
# MAIN
# =========================================================

def main():
    init_db()
    print(f"Masha 2.0 {BOT_VERSION} запущена: consistent evening + pending MS question repair + forward chains + dedupe.")
    offset = None
    known_chat_ids = set()
    last_reminder_scan = 0

    while True:
        # Periodic reminder scan once per minute for chats seen since this process started.
        if time.time() - last_reminder_scan >= 60:
            last_reminder_scan = time.time()
            for chat_id in list(known_chat_ids):
                try:
                    for p, s in due_process_reminders(chat_id):
                        send_message(chat_id, f"Напоминание по процессу #{p['id']} «{p['title']}»:\n{s['step_no']}. {s['text']}")
                        mark_step_reminded(s["id"])
                except Exception as e:
                    print("Reminder scan error:", chat_id, repr(e))

        params = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset

        try:
            r = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=40)
            r.raise_for_status()
            data = r.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                chat_id = message["chat"]["id"]
                known_chat_ids.add(chat_id)
                text = message_text_or_caption(message)

                # Пересланные сообщения разбираем до обычных команд.
                # Это работает и для photo-only пересылок.
                if is_forwarded_message(message):
                    print(
                        "Incoming forwarded:",
                        chat_id,
                        repr(text),
                        "source=",
                        repr(get_forward_source_name(message)),
                    )
                    handle_forwarded_message(chat_id, message)
                    continue

                if not text:
                    continue

                print("Incoming:", chat_id, repr(text))

                if try_handle_process(chat_id, text):
                    continue
                if try_handle_mail(chat_id, text):
                    continue
                if try_handle_calendar(chat_id, text):
                    continue
                if try_handle_command(chat_id, text):
                    continue

                send_message(chat_id, "Думаю...")
                try:
                    send_message(chat_id, ask_openai(chat_id, text))
                except requests.HTTPError as e:
                    print("OpenAI HTTP error:", e.response.text if e.response is not None else repr(e))
                    send_message(chat_id, "Я дошла до OpenAI, но получила ошибку API.")
                except Exception as e:
                    print("OpenAI error:", repr(e))
                    send_message(chat_id, "У меня возникла техническая ошибка при обращении к ИИ.")

        except requests.RequestException as e:
            print("Telegram network error:", repr(e)); time.sleep(3)
        except Exception as e:
            print("Telegram error:", repr(e)); time.sleep(3)


if __name__ == "__main__":
    main()
