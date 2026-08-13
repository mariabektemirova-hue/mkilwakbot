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
BOT_VERSION = "v7.13.1-router-hardening"

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
Никогда не говори «записала», «сохранила», «зафиксировала», «обновила» или «закрыла»,
если в текущем программном пути не было подтвержденного чтением из SQLite изменения.
Обычный ответ OpenAI сам по себе НЕ является записью в рабочую память.

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
    # Short SQLite operations are allowed to wait for one another instead of failing
    # with "database is locked". WAL itself is enabled once in init_db().
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db():
    ensure_data_directory()
    conn = get_db()
    # Enable WAL once at startup. Do not change journal_mode on every connection:
    # doing so can itself contend for a lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

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
        CREATE TABLE IF NOT EXISTS conversation_state (
            chat_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, key)
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
    if "updated_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")
    if "priority" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'normal'")
    if "request_count" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN request_count INTEGER DEFAULT 1")
    if "last_request_at" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_request_at TEXT")
    if "last_source_name" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN last_source_name TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            raw_text TEXT DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'mutation',
            before_json TEXT NOT NULL DEFAULT '{}',
            after_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'started',
            created_at TEXT NOT NULL,
            undone_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            raw_text TEXT DEFAULT '',
            source_name TEXT DEFAULT '',
            details_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)

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
        CREATE TABLE IF NOT EXISTS closeout_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            parsed_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
    """)

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

    control_cols = column_names(conn, "control_items")
    if "request_count" not in control_cols:
        conn.execute("ALTER TABLE control_items ADD COLUMN request_count INTEGER DEFAULT 1")
    if "last_request_at" not in control_cols:
        conn.execute("ALTER TABLE control_items ADD COLUMN last_request_at TEXT")
    if "last_source_name" not in control_cols:
        conn.execute("ALTER TABLE control_items ADD COLUMN last_source_name TEXT DEFAULT ''")

    # v7.11: one authoritative READ surface over every open work object.
    # It does not duplicate data: writes still go to their native tables.
    # All smart routing/brief/search reads this view first.
    conn.execute("DROP VIEW IF EXISTS work_state")
    conn.execute("""
        CREATE VIEW work_state AS
        SELECT
            chat_id, 'task' AS entity_type, id AS entity_id,
            text AS title, COALESCE(notes,'') AS detail,
            due_at AS due_at, COALESCE(priority,'normal') AS priority,
            COALESCE(request_count,1) AS request_count, last_request_at,
            COALESCE(last_source_name,'') AS last_source_name, status
        FROM tasks
        UNION ALL
        SELECT
            chat_id, 'control' AS entity_type, id AS entity_id,
            title AS title,
            TRIM(COALESCE(next_action,'') || ' | ' || COALESCE(waiting_for,'') || ' | ' || COALESCE(notes,'')) AS detail,
            COALESCE(next_check, deadline) AS due_at, COALESCE(priority,'normal') AS priority,
            COALESCE(request_count,1) AS request_count, last_request_at,
            COALESCE(last_source_name,'') AS last_source_name, status
        FROM control_items
        UNION ALL
        SELECT
            p.chat_id, 'process_step' AS entity_type, ps.id AS entity_id,
            p.title || ': ' || ps.text AS title,
            CASE WHEN COALESCE(ps.waiting_for,'')='' THEN COALESCE(p.notes,'')
                 ELSE 'ждём: ' || ps.waiting_for || ' | ' || COALESCE(p.notes,'') END AS detail,
            ps.due_at AS due_at, 'normal' AS priority, 1 AS request_count,
            NULL AS last_request_at, '' AS last_source_name,
            CASE WHEN p.status='open' THEN ps.status ELSE p.status END AS status
        FROM process_steps ps JOIN processes p ON p.id=ps.process_id
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


def add_task(chat_id, text, source="manual", source_ref=None, due_at=None, notes="",
             priority="normal", source_name=""):
    # Exact dedupe remains a cheap first guard; semantic create-vs-update is handled
    # by the unified resolver before this function is called.
    duplicate = find_exact_open_task(chat_id, text)
    if duplicate:
        # Exact duplicate can still carry a new deadline/context/source; merge it instead of ignoring it.
        if due_at is not None or notes or source_name:
            row = update_task_verified(
                chat_id, duplicate["id"], due_at=due_at, notes=notes or None,
                source_name=source_name, repeated_request=is_ms_source_name(source_name), raw_text=text
            )
            if not row:
                raise RuntimeError(f"Verified duplicate merge failed for id={duplicate['id']}")
        else:
            set_last_work_ref(chat_id, "task", duplicate["id"])
        return duplicate["id"]

    now = datetime.now(TZ).isoformat(timespec="seconds")
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO tasks(
            chat_id,text,status,created_at,source,source_ref,due_at,notes,
            updated_at,priority,request_count,last_request_at,last_source_name
        ) VALUES (?,?,'open',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chat_id, text.strip(), now, source, source_ref, due_at, notes or "",
            now, priority or "normal", 1, now, source_name or ""
        ),
    )
    task_id = cur.lastrowid
    conn.commit()

    # Reliable Write: never claim success until SQLite can read the row back.
    row = conn.execute(
        "SELECT id,text,status FROM tasks WHERE chat_id=? AND id=?",
        (chat_id, task_id),
    ).fetchone()
    conn.close()
    if not row or row["status"] != "open" or not row["text"]:
        raise RuntimeError(f"Verified task write failed for id={task_id}")

    log_work_event(chat_id, "task", task_id, "created", text, source_name, {
        "source": source, "source_ref": source_ref, "due_at": due_at, "priority": priority
    })
    set_last_work_ref(chat_id, "task", task_id)
    return task_id


def get_open_tasks(chat_id):
    conn = get_db()
    rows = conn.execute(
        """SELECT id,text,created_at,updated_at,due_at,notes,priority,request_count,
                  last_request_at,last_source_name,source,source_ref
           FROM tasks WHERE chat_id=? AND status='open' ORDER BY id""",
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
    conn.commit()
    row=conn.execute("SELECT id,text FROM memories WHERE chat_id=? AND id=?",(chat_id,memory_id)).fetchone()
    conn.close()
    if not row or not row["text"]:
        raise RuntimeError(f"Verified memory write failed for id={memory_id}")
    return memory_id


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
# RELIABLE MEMORY / UNIFIED WORK RESOLVER
# =========================================================


def _row_to_dict(row):
    return {k: row[k] for k in row.keys()}


def snapshot_mutable_state(chat_id):
    """Snapshot every user-facing mutable work surface needed for one-message undo."""
    conn = get_db()
    snap = {}
    for table in ("tasks", "control_items", "ms_questions"):
        snap[table] = [
            _row_to_dict(r)
            for r in conn.execute(f"SELECT * FROM {table} WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
        ]
    snap["processes"] = [
        _row_to_dict(r)
        for r in conn.execute("SELECT * FROM processes WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    ]
    pids = [r["id"] for r in snap["processes"]]
    if pids:
        marks = ",".join("?" for _ in pids)
        snap["process_steps"] = [
            _row_to_dict(r)
            for r in conn.execute(f"SELECT * FROM process_steps WHERE process_id IN ({marks}) ORDER BY id", pids).fetchall()
        ]
    else:
        snap["process_steps"] = []
    conn.close()
    return snap


def _restore_rows_for_chat(conn, table, chat_id, rows):
    """Restore a chat-scoped table exactly to snapshot rows, preserving explicit IDs."""
    current_ids = {
        r["id"] for r in conn.execute(f"SELECT id FROM {table} WHERE chat_id=?", (chat_id,)).fetchall()
    }
    wanted_ids = {int(r["id"]) for r in rows}
    extra = current_ids - wanted_ids
    if extra:
        marks = ",".join("?" for _ in extra)
        conn.execute(f"DELETE FROM {table} WHERE chat_id=? AND id IN ({marks})", [chat_id, *sorted(extra)])
    for row in rows:
        cols = list(row.keys())
        marks = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        conn.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )


def restore_mutable_state(chat_id, snap):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _restore_rows_for_chat(conn, "tasks", chat_id, snap.get("tasks", []))
        _restore_rows_for_chat(conn, "control_items", chat_id, snap.get("control_items", []))
        _restore_rows_for_chat(conn, "ms_questions", chat_id, snap.get("ms_questions", []))

        # Processes are not created by the natural multi-router, but their status/steps may change.
        for row in snap.get("processes", []):
            cols = list(row.keys())
            marks = ",".join("?" for _ in cols)
            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
            conn.execute(
                f"INSERT INTO processes ({','.join(cols)}) VALUES ({marks}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                [row[c] for c in cols],
            )
        for row in snap.get("process_steps", []):
            cols = list(row.keys())
            marks = ",".join("?" for _ in cols)
            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
            conn.execute(
                f"INSERT INTO process_steps ({','.join(cols)}) VALUES ({marks}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                [row[c] for c in cols],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def begin_operation(chat_id, raw_text, kind="mutation"):
    snap = snapshot_mutable_state(chat_id)
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO operation_log(chat_id,raw_text,kind,before_json,status,created_at)
           VALUES (?,?,?,?, 'started', ?)""",
        (chat_id, raw_text or "", kind, json.dumps(snap, ensure_ascii=False),
         datetime.now(TZ).isoformat(timespec="seconds")),
    )
    op_id = cur.lastrowid
    conn.commit(); conn.close()
    return op_id


def finish_operation(chat_id, op_id, status="committed"):
    after = snapshot_mutable_state(chat_id)
    conn = get_db()
    conn.execute(
        "UPDATE operation_log SET after_json=?,status=? WHERE chat_id=? AND id=?",
        (json.dumps(after, ensure_ascii=False), status, chat_id, op_id),
    )
    conn.commit(); conn.close()


def undo_last_operation(chat_id):
    conn = get_db()
    row = conn.execute(
        """SELECT * FROM operation_log
           WHERE chat_id=? AND status='committed'
           ORDER BY id DESC LIMIT 1""",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    snap = json.loads(row["before_json"] or "{}")
    restore_mutable_state(chat_id, snap)
    conn = get_db()
    conn.execute(
        "UPDATE operation_log SET status='undone',undone_at=? WHERE id=?",
        (datetime.now(TZ).isoformat(timespec="seconds"), row["id"]),
    )
    conn.commit(); conn.close()
    return {"id": row["id"], "raw_text": row["raw_text"], "kind": row["kind"]}


def set_conversation_value(chat_id, key, value):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO conversation_state(chat_id,key,value,updated_at) VALUES (?,?,?,?)
        ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (chat_id, key, str(value), datetime.now(TZ).isoformat(timespec="seconds")),
    )
    conn.commit(); conn.close()


def get_conversation_value(chat_id, key, default=""):
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM conversation_state WHERE chat_id=? AND key=?",
        (chat_id, key),
    ).fetchone()
    conn.close()
    return row["value"] if row else default


def set_last_work_ref(chat_id, entity_type, entity_id):
    set_conversation_value(chat_id, "last_work_ref", f"{entity_type}:{int(entity_id)}")


def get_last_work_ref(chat_id):
    value = get_conversation_value(chat_id, "last_work_ref", "")
    m = re.match(r"^(task|control|process_step):(\d+)$", value or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def log_work_event(chat_id, entity_type, entity_id, event_type, raw_text="", source_name="", details=None):
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO work_events(
                chat_id,entity_type,entity_id,event_type,raw_text,source_name,details_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (chat_id, entity_type, int(entity_id), event_type, raw_text or "", source_name or "",
             json.dumps(details or {}, ensure_ascii=False), datetime.now(TZ).isoformat(timespec="seconds")),
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("work event log error:", repr(e))


def is_ms_source_name(source_name):
    n = normalize_text(source_name or "")
    return bool(n and ("ситков" in n or "sitkov" in n or n in {"мс", "мария"}))


def _stem_token(token):
    """Very small Russian-oriented stemmer for task matching, not linguistics.
    It mainly normalizes case endings and common action forms so
    «Анжелика/Анжелики», «внести/внесла», «повестка/повестки» match.
    """
    t=(token or "").lower().replace("ё","е")
    irregular={
        "внести":"внес","внесла":"внес","внесли":"внес","внес":"внес",
        "прислать":"присл","прислала":"присл","прислал":"присл","прислали":"присл",
        "получить":"получ","получила":"получ","получил":"получ","получили":"получ",
        "поставить":"постав","поставила":"постав","поставил":"постав","поставили":"постав",
    }
    if t in irregular:
        return irregular[t]
    # conservative noun/adjective endings; don't over-stem short words
    if len(t) >= 6:
        for suf in ("иями","ями","ами","ого","ему","ому","ыми","ими","ей","ой","ий","ый","ая","яя","ое","ее","ую","юю","ов","ев","ам","ям","ах","ях","ом","ем","ы","и","а","я","у","ю","е"):
            if t.endswith(suf) and len(t)-len(suf) >= 4:
                return t[:-len(suf)]
    return t


def _meaningful_tokens(value):
    stop = {
        "надо","нужно","сделать","сделай","проверить","проверь","получить","дать","добавить",
        "поставить","встречу","встреча","сегодня","завтра","мс","маша","маш","попросила","попросил",
        "все","всё","одну","одной","для","про","по","на","в","и","или","от","к","с","со","у",
        "это","эту","этой","там","уже","еще","ещё","мне","ей","им","их","как","чтобы"
    }
    out=set()
    for t in task_dedupe_key(value).split():
        if len(t) < 3 or t in stop:
            continue
        out.add(_stem_token(t))
    return out


def _token_similarity(a, b):
    aa, bb = _meaningful_tokens(a), _meaningful_tokens(b)
    if not aa or not bb:
        return 0.0
    inter = aa & bb
    if not inter:
        return 0.0
    # Favor multiple distinctive shared words; robust for reordered wording.
    return len(inter) / max(1, min(len(aa), len(bb)))


def get_unified_work_candidates(chat_id, limit=80):
    """Single authoritative read surface for active work.
    work_state is a SQL VIEW, so there is no copied/duplicated state.
    """
    conn = get_db()
    base = conn.execute(
        """SELECT chat_id,entity_type,entity_id,title,detail,due_at,priority,
                  request_count,last_request_at,last_source_name
           FROM work_state
           WHERE chat_id=? AND status='open'
           ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, entity_id DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    rows=[]
    for r in base:
        item={k:r[k] for k in r.keys()}
        item["request_count"]=int(item.get("request_count") or 1)
        if item["entity_type"]=="process_step":
            pr=conn.execute("SELECT process_id FROM process_steps WHERE id=?",(item["entity_id"],)).fetchone()
            if pr:
                item["process_id"]=pr["process_id"]
        rows.append(item)
    conn.close()
    return rows


def find_unified_work_candidate(chat_id, text, threshold=0.55):
    best, best_score = None, 0.0
    for item in get_unified_work_candidates(chat_id):
        score = max(_token_similarity(text, item["title"]), _token_similarity(text, item["detail"]))
        # Title+detail often carries the old wording plus the current next action.
        score = max(score, _token_similarity(text, item["title"] + " " + item["detail"]))
        if score > best_score:
            best, best_score = item, score
    return (best, best_score) if best and best_score >= threshold else (None, best_score)


def _append_note(existing, note):
    note = (note or "").strip()
    existing = (existing or "").strip()
    if not note:
        return existing
    if task_dedupe_key(note) and task_dedupe_key(note) in task_dedupe_key(existing):
        return existing
    stamp = datetime.now(TZ).strftime("%d.%m %H:%M")
    return (existing + "\n" + f"[{stamp}] {note}").strip()


def update_task_verified(chat_id, task_id, *, text=None, due_at=None, notes=None, priority=None,
                         source_name="", repeated_request=False, raw_text=""):
    conn = get_db()
    before = conn.execute("SELECT * FROM tasks WHERE chat_id=? AND id=? AND status='open'", (chat_id, task_id)).fetchone()
    if not before:
        conn.close(); return None
    now = datetime.now(TZ).isoformat(timespec="seconds")
    new_text = text.strip() if text else before["text"]
    new_due = due_at if due_at is not None else before["due_at"]
    new_notes = _append_note(before["notes"], notes) if notes else (before["notes"] or "")
    count = int(before["request_count"] or 1) + (1 if repeated_request else 0)
    new_priority = priority or before["priority"] or "normal"
    if repeated_request and count >= 2 and new_priority == "normal":
        new_priority = "high"
    conn.execute(
        """UPDATE tasks SET text=?,due_at=?,notes=?,updated_at=?,priority=?,request_count=?,
           last_request_at=?,last_source_name=? WHERE chat_id=? AND id=? AND status='open'""",
        (new_text,new_due,new_notes,now,new_priority,count,now,source_name or before["last_source_name"] or "",chat_id,task_id),
    )
    conn.commit()
    after = conn.execute("SELECT * FROM tasks WHERE chat_id=? AND id=? AND status='open'", (chat_id,task_id)).fetchone()
    conn.close()
    if not after:
        raise RuntimeError(f"Verified task update failed for id={task_id}")
    log_work_event(chat_id,"task",task_id,"repeated_request" if repeated_request else "updated",raw_text,source_name,{
        "due_at":new_due,"priority":new_priority,"request_count":count,"notes":notes or ""
    })
    set_last_work_ref(chat_id,"task",task_id)
    return after


def update_control_verified(chat_id, control_id, *, next_action=None, due_at=None, notes=None, priority=None,
                            source_name="", repeated_request=False, raw_text=""):
    conn = get_db()
    before = conn.execute("SELECT * FROM control_items WHERE chat_id=? AND id=? AND status='open'", (chat_id,control_id)).fetchone()
    if not before:
        conn.close(); return None
    now = datetime.now(TZ).isoformat(timespec="seconds")
    count = int(before["request_count"] or 1) + (1 if repeated_request else 0)
    new_priority = priority or before["priority"] or "normal"
    if repeated_request and count >= 2 and new_priority == "normal":
        new_priority = "high"
    new_notes = _append_note(before["notes"], notes) if notes else (before["notes"] or "")
    conn.execute(
        """UPDATE control_items SET next_action=?,next_check=?,notes=?,priority=?,updated_at=?,request_count=?,
           last_request_at=?,last_source_name=? WHERE chat_id=? AND id=? AND status='open'""",
        (next_action or before["next_action"], due_at if due_at is not None else before["next_check"],
         new_notes,new_priority,now,count,now,source_name or before["last_source_name"] or "",chat_id,control_id),
    )
    conn.commit()
    after = conn.execute("SELECT * FROM control_items WHERE chat_id=? AND id=? AND status='open'", (chat_id,control_id)).fetchone()
    conn.close()
    if not after:
        raise RuntimeError(f"Verified control update failed for id={control_id}")
    log_work_event(chat_id,"control",control_id,"repeated_request" if repeated_request else "updated",raw_text,source_name,{
        "next_action":next_action,"due_at":due_at,"priority":new_priority,"request_count":count
    })
    set_last_work_ref(chat_id,"control",control_id)
    return after


def update_process_step_verified(chat_id, step_id, *, due_at=None, notes=None, source_name="", raw_text=""):
    conn = get_db()
    row = conn.execute(
        """SELECT ps.*,p.title AS process_title FROM process_steps ps JOIN processes p ON p.id=ps.process_id
           WHERE p.chat_id=? AND ps.id=? AND p.status='open' AND ps.status='open'""",
        (chat_id,step_id),
    ).fetchone()
    if not row:
        conn.close(); return None
    if due_at is not None:
        conn.execute("UPDATE process_steps SET due_at=? WHERE id=?", (due_at,step_id))
    if notes:
        p = conn.execute("SELECT notes FROM processes WHERE id=?", (row["process_id"],)).fetchone()
        conn.execute("UPDATE processes SET notes=? WHERE id=?", (_append_note(p["notes"] if p else "", notes),row["process_id"]))
    conn.commit()
    check = conn.execute("SELECT id,due_at FROM process_steps WHERE id=? AND status='open'", (step_id,)).fetchone()
    conn.close()
    if not check:
        raise RuntimeError(f"Verified process step update failed for id={step_id}")
    log_work_event(chat_id,"process_step",step_id,"updated",raw_text,source_name,{"due_at":due_at,"notes":notes or ""})
    set_last_work_ref(chat_id,"process_step",step_id)
    return check


def complete_unified_work(chat_id, entity_type, entity_id, raw_text="", source_name=""):
    if entity_type == "task":
        ok = complete_task(chat_id, entity_id)
    elif entity_type == "control":
        conn = get_db(); cur = conn.execute(
            "UPDATE control_items SET status='done',updated_at=? WHERE chat_id=? AND id=? AND status='open'",
            (datetime.now(TZ).isoformat(timespec="seconds"),chat_id,entity_id)
        ); conn.commit(); ok = cur.rowcount > 0
        verify = conn.execute("SELECT status FROM control_items WHERE chat_id=? AND id=?", (chat_id,entity_id)).fetchone(); conn.close()
        ok = ok and verify and verify["status"] == "done"
    elif entity_type == "ms_question":
        ok = complete_ms_question(chat_id, entity_id, answer_text=raw_text or "Закрыто Машей")
    elif entity_type == "process_step":
        conn = get_db(); row = conn.execute(
            """SELECT ps.step_no,ps.process_id FROM process_steps ps JOIN processes p ON p.id=ps.process_id
               WHERE p.chat_id=? AND ps.id=? AND ps.status='open'""", (chat_id,entity_id)
        ).fetchone(); conn.close()
        ok = bool(row and complete_process_step(chat_id,row["process_id"],row["step_no"])[0])
    else:
        ok = False
    if ok:
        log_work_event(chat_id,entity_type,entity_id,"completed",raw_text,source_name,{})
    return bool(ok)


def resolve_due_from_text(text):
    n = normalize_text(text)
    today = datetime.now(TZ).date()
    if "послезавтра" in n:
        return (today + timedelta(days=2)).isoformat()
    if "завтра" in n:
        return (today + timedelta(days=1)).isoformat()
    if "сегодня" in n:
        return today.isoformat()
    weekdays = {"понедельник":0,"вторник":1,"сред":2,"четверг":3,"пятниц":4,"суббот":5,"воскрес":6}
    for stem, wd in weekdays.items():
        if stem in n:
            delta = (wd - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return (today + timedelta(days=delta)).isoformat()
    return None


def unified_work_context(chat_id, limit=60):
    rows = get_unified_work_candidates(chat_id, limit=limit)
    lines = ["ЕДИНОЕ АКТУАЛЬНОЕ РАБОЧЕЕ СОСТОЯНИЕ (источник истины = SQLite):"]
    if not rows:
        return lines[0] + "\nНет открытых дел."
    for r in rows:
        label = {"task":"TASK","control":"CONTROL","process_step":"STEP"}.get(r["entity_type"],r["entity_type"])
        line = f"- {label}:{r['entity_id']} | {r['title']}"
        if r.get("detail"):
            line += f" | {r['detail'][:500]}"
        if r.get("due_at"):
            line += f" | срок/контроль: {r['due_at']}"
        if r.get("priority") and r["priority"] != "normal":
            line += f" | priority={r['priority']}"
        if int(r.get("request_count") or 1) > 1:
            line += f" | запросов={r['request_count']}"
        lines.append(line)
    return "\n".join(lines)


def resolve_work_action_with_ai(chat_id, raw_text, source_name=""):
    candidates = get_unified_work_candidates(chat_id, limit=50)
    last_type,last_id = get_last_work_ref(chat_id)
    lines=[]
    for c in candidates:
        lines.append(f"{c['entity_type']}:{c['entity_id']} | {c['title']} | {c['detail'][:350]} | due={c.get('due_at')}")
    conn=get_db()
    ev=conn.execute(
        """SELECT entity_type,entity_id,event_type,raw_text,source_name,created_at
           FROM work_events WHERE chat_id=? ORDER BY id DESC LIMIT 12""",
        (chat_id,),
    ).fetchall()
    conn.close()
    history="\n".join(
        f"{r['created_at']} | {r['entity_type']}:{r['entity_id']} | {r['event_type']} | {r['source_name']} | {r['raw_text'][:250]}"
        for r in reversed(ev)
    ) or "нет"
    prompt = f"""
Ты маршрутизатор рабочей памяти. Новое сообщение Маши должно либо обновить существующее дело,
закрыть его, создать новое дело, либо быть просто вопросом/справкой. Не создавай дубль, если это
продолжение, уточнение, ремайндер, новый срок, новый участник или факт выполнения существующего дела.
Короткие фразы типа «сделаю завтра», «они прислали, я внесла», «дожму сегодня» обычно относятся к
последнему активному делу или очевидному кандидату.

Сообщение: {raw_text}
Источник: {source_name or 'сама Маша'}
Последнее активное дело: {last_type}:{last_id}

Недавняя история изменений:
{history}

Кандидаты:
{chr(10).join(lines) if lines else 'нет'}

Если сообщение короткое/контекстное и похоже на продолжение, предпочитай update существующего дела.
Если уверенность в create ниже 0.65, не создавай новую задачу только из-за неоднозначности: верни ignore.

Верни только JSON:
{{
  "action":"create|update|complete|query|ignore",
  "entity_type":"task|control|process_step|none",
  "entity_id":123,
  "task_text":"самодостаточный текст новой задачи, только если create",
  "next_action":"обновлённый следующий шаг, если update и он ясен",
  "due_at":"YYYY-MM-DD или null",
  "notes":"что нового добавилось к контексту",
  "reason":"коротко",
  "confidence":0.0
}}
"""
    payload={"model":MODEL,"instructions":"Отвечай только валидным JSON. Не выдумывай факты.","input":prompt,"store":False}
    r=requests.post(OPENAI_URL,headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},json=payload,timeout=120)
    r.raise_for_status()
    out=parse_json_from_model(extract_openai_text(r.json()))
    if not isinstance(out,dict):
        raise ValueError("work resolver returned non-object")
    return out


def apply_reconciled_work_input(chat_id, raw_text, *, source="natural", source_ref=None, source_name="",
                                allow_create=True, force_candidate=None, due_override=None, notes_hint=""):
    """Central create-vs-update path used by natural messages and forwarded tasks."""
    raw_text=(raw_text or "").strip()
    if not raw_text:
        return {"action":"ignore"}
    due=due_override or resolve_due_from_text(raw_text)
    repeated=is_ms_source_name(source_name)

    # 0) Explicit entity reference is authoritative and must never fall through to AI.
    # Examples: "по TASK 20 ещё добавь...", "обнови задачу #20...", "к задаче 20..."
    explicit_candidate = None
    m_explicit = re.search(
        r"\b(?:task|задач(?:а|е|у|и)?|таск)\s*[:#№]?\s*(\d+)\b",
        normalize_text(raw_text),
        flags=re.I,
    )
    if m_explicit:
        explicit_id = int(m_explicit.group(1))
        explicit_candidate = next(
            (
                c for c in get_unified_work_candidates(chat_id, limit=120)
                if c["entity_type"] == "task" and c["entity_id"] == explicit_id
            ),
            None,
        )
        # If an explicit TASK id was named but it is not open/found, fail closed:
        # do not create a new unrelated task from the same sentence.
        if explicit_candidate is None:
            return {
                "action": "ignore",
                "reason": f"TASK:{explicit_id} not found or not open",
                "explicit_task_id": explicit_id,
            }

    # 1) Strong lexical match: fast and deterministic.
    if explicit_candidate is not None:
        candidate, score = explicit_candidate, 1.0
    else:
        candidate,score=find_unified_work_candidate(chat_id,raw_text,threshold=0.45)
    if force_candidate:
        candidate=force_candidate; score=1.0

    # 2) Pronoun/continuation without useful nouns -> last active work item.
    n=normalize_text(raw_text)
    continuation=bool(re.match(r"^(?:сделаю|сделать|можно\s+сделать|дожму|проверю|напишу|поставлю|внесу|перенесу|тогда|давай\s+завтра)",n))
    if not candidate and continuation:
        lt,li=get_last_work_ref(chat_id)
        if lt and li:
            candidate=next((c for c in get_unified_work_candidates(chat_id) if c["entity_type"]==lt and c["entity_id"]==li),None)

    complete_signal=bool(re.search(r"\b(?:готово|сделано|закрыла|закрыл|выполнено|внесла|внес|поставила|поставил|отправила|отправил|получила|получил)\b", n))
    completion_words=["готово","сделано","закрыла","закрыл","выполнено","внесла","внес ","поставила","поставил","отправила","отправил"]
    # «получила/получил» считается завершением только если само дело было про получение.
    if "получила" in n or "получил" in n:
        if candidate and any(x in normalize_text(candidate.get("title", "")) for x in ["получить","жду","дождаться"]):
            completion_words += ["получила","получил"]
    if candidate and complete_signal and any(x in n for x in completion_words):
        if complete_unified_work(chat_id,candidate["entity_type"],candidate["entity_id"],raw_text,source_name):
            return {"action":"complete","candidate":candidate}

    if candidate:
        # Repeated request from MS without a stated date means "needs attention now".
        # Do not invent a hard deadline, but move the operational check/action to today.
        if repeated and not due:
            due = datetime.now(TZ).date().isoformat()
        note=" | ".join(x for x in [notes_hint.strip(), raw_text] if x)
        next_action=None
        # A direct new instruction from MS is usually the newest next action.
        if repeated and re.search(r"\b(?:надо|нужно|сделай|сделать|дайте|дай|проверь|поставь|соберите|собрать|сложите)\b",n):
            next_action=raw_text
        if candidate["entity_type"]=="task":
            row=update_task_verified(chat_id,candidate["entity_id"],due_at=due,notes=note,source_name=source_name,repeated_request=repeated,raw_text=raw_text)
        elif candidate["entity_type"]=="control":
            row=update_control_verified(chat_id,candidate["entity_id"],next_action=next_action,due_at=due,notes=note,source_name=source_name,repeated_request=repeated,raw_text=raw_text)
        else:
            row=update_process_step_verified(chat_id,candidate["entity_id"],due_at=due,notes=note,source_name=source_name,raw_text=raw_text)
        return {"action":"update","candidate":candidate,"row":row,"score":score}

    # 3) AI resolves ambiguous update/complete/create against the whole current state.
    try:
        decision=resolve_work_action_with_ai(chat_id,raw_text,source_name)
    except Exception as e:
        print("work resolver AI error:",repr(e))
        # Fail closed: reliability is more important than inventing a new DB row.
        decision={"action":"ignore","reason":"resolver unavailable"}

    action=str(decision.get("action") or "").lower()
    et=str(decision.get("entity_type") or "none")
    try: eid=int(decision.get("entity_id") or 0)
    except Exception: eid=0
    candidate=next((c for c in get_unified_work_candidates(chat_id) if c["entity_type"]==et and c["entity_id"]==eid),None)
    conf=float(decision.get("confidence") or 0.0)
    due=decision.get("due_at") or due

    if action in {"update","complete"} and candidate and conf >= 0.55:
        if action=="complete":
            ok=complete_unified_work(chat_id,et,eid,raw_text,source_name)
            return {"action":"complete" if ok else "ignore","candidate":candidate}
        # Lossless update: never replace concrete user information with an AI summary.
        # The raw message is always preserved; AI notes are only supplementary.
        ai_note = str(decision.get("notes") or "").strip()
        note_parts = [x.strip() for x in [notes_hint, raw_text, ai_note]
                      if x and x.strip()]
        note = " | ".join(dict.fromkeys(note_parts))
        next_action=str(decision.get("next_action") or "").strip() or None
        if et=="task":
            row=update_task_verified(chat_id,eid,due_at=due,notes=note,source_name=source_name,repeated_request=repeated,raw_text=raw_text)
        elif et=="control":
            row=update_control_verified(chat_id,eid,next_action=next_action,due_at=due,notes=note,source_name=source_name,repeated_request=repeated,raw_text=raw_text)
        else:
            row=update_process_step_verified(chat_id,eid,due_at=due,notes=note,source_name=source_name,raw_text=raw_text)
        return {"action":"update","candidate":candidate,"row":row,"score":conf}

    if action=="create" and allow_create and conf >= 0.65:
        body=str(decision.get("task_text") or raw_text).strip()
        tid=add_task(chat_id,body,source=source,source_ref=source_ref,due_at=due,notes=str(decision.get("notes") or notes_hint or ""),source_name=source_name)
        return {"action":"create","task_id":tid,"task_text":body}
    # Clear explicit standalone instructions should still be reliably saved even if AI routing is unavailable.
    explicit_new = bool(re.search(r"\b(?:надо|нужно|не забыть|мс попросила|попросила|попросил|во вторник|завтра надо|сегодня надо)\b", n))
    if allow_create and explicit_new and not candidate:
        tid=add_task(chat_id,raw_text,source=source,source_ref=source_ref,due_at=due,notes="",source_name=source_name)
        return {"action":"create","task_id":tid,"task_text":raw_text}
    return {"action":action or "ignore","decision":decision}



def resolve_multi_work_actions_with_ai(chat_id, raw_text, source_name=""):
    """Resolve one natural-language message into independent atomic work mutations."""
    candidates = get_multi_mutation_candidates(chat_id, limit=100)
    last_type, last_id = get_last_work_ref(chat_id)
    lines = []
    for c in candidates:
        lines.append(
            f"{c['entity_type']}:{c['entity_id']} | {c['title']} | {c['detail'][:450]} | due={c.get('due_at')}"
        )
    prompt = f"""
Ты надёжный маршрутизатор SQLite рабочего ассистента. Одно сообщение Маши может содержать НЕСКОЛЬКО
независимых изменений разных дел. Разложи сообщение на атомарные действия и ничего не теряй.

СООБЩЕНИЕ:
{raw_text}

ИСТОЧНИК: {source_name or 'сама Маша'}
ПОСЛЕДНЕЕ ДЕЛО: {last_type}:{last_id}

ОТКРЫТЫЕ ДЕЛА:
{chr(10).join(lines) if lines else 'нет'}

Правила:
1. Каждый отдельный факт выполнения, новый срок, новая деталь, перенос, создание или закрытие = отдельный action.
2. Сначала ищи существующее дело. Не создавай дубль, если подходящий объект уже есть.
3. complete означает, что конкретное существующее дело действительно выполнено целиком.
4. update означает новую деталь/срок/следующий шаг существующего дела.
5. create — только самостоятельное новое рабочее дело, которого нет среди кандидатов.
6. Для update/complete укажи реальный entity_type и entity_id из списка. Не выдумывай ID.
6a. Вопрос/решение, которое надо вынести МС, = ask_ms. Не превращай его в обычный TASK.
6b. Если Маша говорит, что вопрос к МС уже решён / больше не нужен, найди ms_question и complete его.
6c. Фраза «добавить в вечерний апдейт для МС» относится ТОЛЬКО к тому атомарному пункту, где она написана.
    Она никогда не должна превращать остальные пункты сообщения в один большой вопрос МС.
7. notes сохраняй максимально близко к факту пользователя; не теряй имена, даты и условия.
8. Если часть сообщения не является рабочим изменением, не создавай action для неё.
9. Верни ВСЕ действия в порядке сообщения.
10. В начале строки Маша иногда пишет номер пункта из только что показанного списка, например
    «23. Монитор подвинули». Это НЕ обязательно TASK:23. Голый номер без префикса TASK/CONTROL/STEP
    никогда не считай ID базы. Сопоставляй дело по смыслу текста после номера.
11. Формулировки «сделали», «подвинули», «получили», «отправили» могут означать complete,
    если существующее дело этим полностью выполнено. Если после факта остаётся действие
    («поменяла время на 10:30, проверить в календаре») — это update, а не complete.

Верни только JSON:
{{"actions":[
  {{"action":"create|update|complete|ask_ms","entity_type":"task|control|process_step|ms_question|none","entity_id":0,
    "task_text":"только для create","next_action":"если явно изменился следующий шаг",
    "due_at":"YYYY-MM-DD или null","notes":"новый факт без домыслов","confidence":0.0}}
]}}
"""
    payload={"model":MODEL,"instructions":"Отвечай только валидным JSON. Не выдумывай факты и ID.","input":prompt,"store":False}
    r=requests.post(OPENAI_URL,headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},json=payload,timeout=120)
    r.raise_for_status()
    out=parse_json_from_model(extract_openai_text(r.json()))
    actions=out.get("actions") if isinstance(out,dict) else None
    if not isinstance(actions,list):
        raise ValueError("multi resolver returned invalid actions")
    return actions


def apply_multi_work_input(chat_id, raw_text, *, source="natural", source_ref=None, source_name=""):
    """Apply every independently resolved mutation, verifying each native SQLite write."""
    actions=resolve_multi_work_actions_with_ai(chat_id,raw_text,source_name)
    results=[]
    failures=[]
    repeated=is_ms_source_name(source_name)

    for idx,a in enumerate(actions,1):
        try:
            action=str(a.get("action") or "").lower()
            et=str(a.get("entity_type") or "none")
            try: eid=int(a.get("entity_id") or 0)
            except Exception: eid=0
            conf=float(a.get("confidence") or 0.0)
            due=a.get("due_at") or None
            notes=str(a.get("notes") or "").strip()
            next_action=str(a.get("next_action") or "").strip() or None
            candidates=get_multi_mutation_candidates(chat_id,limit=140)
            candidate=next((c for c in candidates if c["entity_type"]==et and c["entity_id"]==eid),None)

            if action in {"update","complete"}:
                if not candidate or conf < 0.55:
                    raise RuntimeError(f"target not safely resolved: {et}:{eid}, confidence={conf:.2f}")
                if action=="complete":
                    if not complete_unified_work(chat_id,et,eid,raw_text,source_name):
                        raise RuntimeError(f"completion verification failed: {et}:{eid}")
                    results.append({"action":"complete","candidate":candidate,"atomic":a})
                    continue
                # Preserve the exact atomic fact in notes; do not append the whole multi-message to every object.
                if et=="task":
                    row=update_task_verified(chat_id,eid,due_at=due,notes=notes or next_action,
                        source_name=source_name,repeated_request=repeated,raw_text=notes or raw_text)
                elif et=="control":
                    row=update_control_verified(chat_id,eid,next_action=next_action,due_at=due,notes=notes,
                        source_name=source_name,repeated_request=repeated,raw_text=notes or raw_text)
                elif et=="ms_question":
                    row=update_ms_question_verified(
                        chat_id,eid,text=next_action or None,due_at=due,notes=notes,raw_text=notes or raw_text
                    )
                else:
                    row=update_process_step_verified(chat_id,eid,due_at=due,notes=notes or next_action,
                        source_name=source_name,raw_text=notes or raw_text)
                if row is None:
                    raise RuntimeError(f"update verification failed: {et}:{eid}")
                results.append({"action":"update","candidate":candidate,"row":row,"atomic":a})
                continue

            if action=="ask_ms":
                if conf < 0.55:
                    raise RuntimeError(f"ask_ms confidence too low: {conf:.2f}")
                body=str(a.get("task_text") or notes or next_action or "").strip()
                if not body:
                    raise RuntimeError("empty ask_ms body")
                qid, created = add_ms_question_dedup(chat_id, body, due_at=due, notes=notes)
                results.append({
                    "action":"ask_ms","question_id":qid,"question_text":body,
                    "created":created,"atomic":a
                })
                continue

            if action=="create":
                if conf < 0.65:
                    raise RuntimeError(f"create confidence too low: {conf:.2f}")
                body=str(a.get("task_text") or notes or "").strip()
                if not body:
                    raise RuntimeError("empty create body")
                tid=add_task(chat_id,body,source=source,source_ref=source_ref,due_at=due,
                    notes=notes,source_name=source_name)
                results.append({"action":"create","task_id":tid,"task_text":body,"atomic":a})
                continue
        except Exception as e:
            failures.append({"index":idx,"action":a,"error":str(e)})
            print("Multi-action write failure:",idx,repr(a),repr(e))

    return {"action":"multi","results":results,"failures":failures,"resolved_count":len(actions)}


def format_multi_reconciled_result(result, source_name=""):
    done=result.get("results") or []
    failures=result.get("failures") or []
    lines=[]
    if done:
        lines.append(f"Обновила {len(done)} дел(а) в базе ✅")
        for r in done:
            a=r.get("atomic") or {}
            action=r.get("action")
            if action=="create":
                lines.append(f"— Создала TASK:{r['task_id']} — {r['task_text']}")
            elif action=="ask_ms":
                prefix="Добавила" if r.get("created") else "Уже было"
                lines.append(f"— {prefix} Q{r['question_id']} в вопросы МС — {r['question_text']}")
            else:
                c=r.get("candidate") or {}
                label={"task":"TASK","control":"CONTROL","process_step":"STEP","ms_question":"Q"}.get(c.get("entity_type"),"ДЕЛО")
                verb="Закрыла" if action=="complete" else "Обновила"
                detail=str(a.get("notes") or a.get("next_action") or "").strip()
                line=f"— {verb} {label}:{c.get('entity_id')} — {c.get('title','')}"
                if detail: line += f" | {detail}"
                lines.append(line)
    if failures:
        lines.append(f"⚠️ Не удалось надёжно сохранить {len(failures)} действие(я):")
        for f in failures:
            a=f.get("action") or {}
            desc=str(a.get("notes") or a.get("task_text") or a.get("next_action") or f"действие #{f['index']}").strip()
            lines.append(f"— {desc}: {f['error']}")
    if not done and not failures:
        lines.append("Ничего в базе не меняла.")
    return "\n".join(lines)

def format_reconciled_result(result, source_name=""):
    action=result.get("action")
    if action=="create":
        return f"Записала в базу ✅ #{result['task_id']} — {result['task_text']}"
    if action=="update":
        c=result.get("candidate") or {}
        phrase={
            "task":"существующую задачу",
            "control":"существующее контрольное дело",
            "process_step":"существующий этап процесса",
        }.get(c.get("entity_type"),"существующее дело")
        extra=""
        row=result.get("row")
        try:
            count=int(row["request_count"] or 1) if row is not None and "request_count" in row.keys() else int(c.get("request_count") or 1)
            if is_ms_source_name(source_name) and count >= 2:
                extra=f"\nЭто уже повторный запрос МС (№{count}) — приоритет повышен."
        except Exception:
            pass
        saved_detail = ""
        try:
            if row is not None and "notes" in row.keys() and row["notes"]:
                last_note = str(row["notes"]).splitlines()[-1]
                saved_detail = f"\nСохранила детали: {last_note}"
        except Exception:
            pass
        return f"Обновила {phrase} ✅\n{c.get('title','')}" + saved_detail + extra
    if action=="complete":
        c=result.get("candidate") or {}
        return f"Закрыла существующее дело ✅\n{c.get('title','')}"
    return "Ничего в базе не меняла."


def repair_known_reliable_memory_artifacts(chat_id):
    """Safe one-time cleanup of two duplicates observed before v7.10.
    Uses content, not hard-coded IDs, so it is idempotent.
    """
    try:
        # Retro reminder task -> canonical control item.
        retro = find_control_fuzzy(chat_id, "Ретро и аналитические записки директоров")
        if retro:
            conn=get_db()
            rows=conn.execute("SELECT * FROM tasks WHERE chat_id=? AND status='open' ORDER BY id",(chat_id,)).fetchall()
            for t in rows:
                n=normalize_text(t["text"])
                if "ретро" in n and "аналитичес" in n and ("папк" in n or "справк" in n or "записк" in n):
                    update_control_verified(chat_id,retro["id"],next_action=t["text"],notes="Слито из старой дублирующей задачи до v7.10.",raw_text=t["text"])
                    conn.execute("UPDATE tasks SET status='merged',completed_at=NULL WHERE id=?",(t["id"],))
            conn.commit(); conn.close()

        # v7.12.2 repair: remove accidental "compose an update" pseudo-question.
        conn=get_db()
        bad_qs=conn.execute(
            "SELECT id,text FROM ms_questions WHERE chat_id=? AND status='open'",
            (chat_id,)
        ).fetchall()
        for q in bad_qs:
            qn=normalize_text(q["text"])
            if "апдейт" in qn and "мс" in qn and any(v in qn for v in ("напиши","составь","собери","подготовь","дай")):
                conn.execute(
                    "UPDATE ms_questions SET status='cancelled',answered_at=? WHERE id=?",
                    (datetime.now(TZ).isoformat(timespec="seconds"),q["id"])
                )
        conn.commit(); conn.close()

        # v7.12.2 repair: Eldar "put questions to MS / remind her to review"
        # was incorrectly split into ordinary TASKs in v7.12.0.
        conn=get_db()
        open_tasks=conn.execute(
            "SELECT id,text FROM tasks WHERE chat_id=? AND status='open' ORDER BY id",
            (chat_id,)
        ).fetchall()
        eldar_bug=[
            t for t in open_tasks
            if "эльдар" in normalize_text(t["text"])
            and (
                ("вопрос" in normalize_text(t["text"]) and "мс" in normalize_text(t["text"]))
                or ("напомнить" in normalize_text(t["text"]) and "посмотр" in normalize_text(t["text"]))
            )
        ]
        conn.close()
        if eldar_bug:
            existing_qs=get_open_ms_questions(chat_id)
            already=any("эльдар" in normalize_text(q["text"]) for q in existing_qs)
            if not already:
                add_ms_question(
                    chat_id,
                    "По Эльдару: внести вопросы МС и напомнить ей посмотреть их.",
                    source="repair_v7.12.2"
                )
            conn=get_db()
            for t in eldar_bug:
                conn.execute(
                    "UPDATE tasks SET status='merged',completed_at=NULL,notes=? WHERE id=? AND status='open'",
                    ("Перенесено в вопросы МС в v7.12.2.", t["id"])
                )
            conn.commit(); conn.close()

        # v7.13.1 repair: read-only commands accidentally stored as MS questions (e.g. Q9 "дай вопросы МС").
        conn=get_db()
        pseudo_qs=conn.execute(
            "SELECT id,text FROM ms_questions WHERE chat_id=? AND status='open' ORDER BY id",
            (chat_id,)
        ).fetchall()
        now=datetime.now(TZ).isoformat(timespec="seconds")
        for q in pseudo_qs:
            if classify_read_only_intent(q["text"]):
                conn.execute(
                    "UPDATE ms_questions SET status='cancelled',answered_at=?,answer_text=? WHERE id=?",
                    (now, "Автоочистка v7.13.1: read-only команда ошибочно попала в вопросы МС.", q["id"])
                )
        conn.commit(); conn.close()

        # v7.13 repair: a whole numbered work dump was accidentally stored as one MS question.
        conn=get_db()
        qs=conn.execute(
            "SELECT id,text FROM ms_questions WHERE chat_id=? AND status='open' ORDER BY id",
            (chat_id,)
        ).fetchall()
        for q in qs:
            qn=normalize_text(q["text"])
            refs=len(re.findall(r"\b(?:task|control|step)\s*[:#№]?\s*\d+\b", q["text"], flags=re.I))
            if refs >= 2 or (len(q["text"]) > 600 and "закрой" in qn):
                conn.execute(
                    "UPDATE ms_questions SET status='cancelled',answered_at=?,answer_text=? WHERE id=?",
                    (datetime.now(TZ).isoformat(timespec="seconds"),
                     "Автоочистка v7.13: рабочий brain dump ошибочно попал в вопросы МС.", q["id"])
                )
        conn.commit(); conn.close()

        # Keep only one open Eldar question with the same intent.
        conn=get_db()
        eldar_qs=conn.execute(
            "SELECT id,text FROM ms_questions WHERE chat_id=? AND status='open' ORDER BY id DESC",
            (chat_id,)
        ).fetchall()
        seen_eldar=False
        for q in eldar_qs:
            qn=normalize_text(q["text"])
            if "эльдар" in qn and ("вопрос" in qn or "посмотр" in qn):
                if not seen_eldar:
                    seen_eldar=True
                else:
                    conn.execute(
                        "UPDATE ms_questions SET status='cancelled',answered_at=?,answer_text=? WHERE id=?",
                        (datetime.now(TZ).isoformat(timespec="seconds"),
                         "Автоочистка v7.13: дубль вопроса по Эльдару.", q["id"])
                    )
        conn.commit(); conn.close()

        # Meeting with Nikita + orphan follow-up "choose time after 24 Aug".
        conn=get_db()
        tasks=conn.execute("SELECT * FROM tasks WHERE chat_id=? AND status='open' ORDER BY id",(chat_id,)).fetchall()
        nikita=next((t for t in tasks if "никит" in normalize_text(t["text"]) and "встреч" in normalize_text(t["text"])),None)
        orphan=next((t for t in tasks if "выбрать время" in normalize_text(t["text"]) and "24 августа" in normalize_text(t["text"])),None)
        conn.close()
        if nikita and orphan and nikita["id"] != orphan["id"]:
            update_task_verified(chat_id,nikita["id"],due_at=orphan["due_at"],notes=orphan["text"] + " (слито из старой дублирующей задачи)",raw_text=orphan["text"])
            conn=get_db(); conn.execute("UPDATE tasks SET status='merged',completed_at=NULL WHERE id=?",(orphan["id"],)); conn.commit(); conn.close()
    except Exception as e:
        print("Reliable-memory artifact repair error:",repr(e))


def answer_unified_work_query(chat_id, text):
    n=normalize_text(text)
    if not any(p in n for p in ["что у меня горит","что горит","что я обещала мс","что обещала мс","что у меня по задачам","что сейчас важно"]):
        return False
    rows=get_unified_work_candidates(chat_id,limit=80)
    today=datetime.now(TZ).date()
    if "обещала мс" in n:
        filtered=[r for r in rows if "мс" in normalize_text(r["detail"]+" "+r["title"]) or "sitkov" in normalize_text(r.get("last_source_name","") or "") or "ситков" in normalize_text(r.get("last_source_name","") or "")]
        title="Что сейчас связано с обещаниями/запросами МС:"
    else:
        def urgency(r):
            score=0
            if r.get("priority")=="high": score+=4
            if int(r.get("request_count") or 1)>1: score+=3
            if r.get("due_at"):
                try:
                    d=date.fromisoformat(str(r["due_at"])[:10]);
                    if d<=today: score+=5
                    elif d<=today+timedelta(days=1): score+=3
                except Exception: pass
            return score
        filtered=sorted(rows,key=urgency,reverse=True)[:10]
        title="Что сейчас горит / важно:"
    lines=[title]
    for r in filtered[:12]:
        line=f"— {r['title']}"
        if r.get("due_at"): line+=f" (срок/контроль: {r['due_at']})"
        if int(r.get("request_count") or 1)>1: line+=f" [запросов МС/повторов: {r['request_count']}]"
        lines.append(line)
    if len(lines)==1: lines.append("Ничего подходящего в актуальной базе не нашла.")
    send_message(chat_id,"\n".join(lines)); return True

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
    conn.commit()
    row=conn.execute("SELECT id,title,status FROM processes WHERE chat_id=? AND id=?",(chat_id,pid)).fetchone()
    conn.close()
    if not row or row["status"]!="open" or not row["title"]:
        raise RuntimeError(f"Verified process write failed for id={pid}")
    return pid


def add_process_step(process_id, step_no, text, depends_on="", waiting_for="", due_at=None, remind_every_days=0):
    conn = get_db()
    cur=conn.execute("""
        INSERT INTO process_steps(
            process_id,step_no,text,status,depends_on,waiting_for,due_at,remind_every_days
        ) VALUES (?,?,?,'open',?,?,?,?)
    """, (process_id, step_no, text.strip(), depends_on, waiting_for, due_at, remind_every_days))
    step_id=cur.lastrowid
    conn.commit()
    row=conn.execute("SELECT id,text,status FROM process_steps WHERE id=?",(step_id,)).fetchone()
    conn.close()
    if not row or row["status"]!="open" or not row["text"]:
        raise RuntimeError(f"Verified process-step write failed for id={step_id}")
    return step_id


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
    row=conn.execute("SELECT id,text,status FROM ms_questions WHERE chat_id=? AND id=?",(chat_id,qid)).fetchone()
    conn.close()
    if not row or row["status"] != "open" or not row["text"]:
        raise RuntimeError(f"Verified MS question write failed for id={qid}")
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



def update_ms_question_verified(chat_id, question_id, *, text=None, due_at=None, notes=None, raw_text=""):
    conn = get_db()
    before = conn.execute(
        "SELECT * FROM ms_questions WHERE chat_id=? AND id=? AND status='open'",
        (chat_id, question_id),
    ).fetchone()
    if not before:
        conn.close(); return None
    new_text = (text or before["text"]).strip()
    new_due = due_at if due_at is not None else before["due_at"]
    new_notes = _append_note(before["notes"], notes) if notes else (before["notes"] or "")
    conn.execute(
        "UPDATE ms_questions SET text=?,due_at=?,notes=? WHERE chat_id=? AND id=? AND status='open'",
        (new_text, new_due, new_notes, chat_id, question_id),
    )
    conn.commit()
    after = conn.execute(
        "SELECT * FROM ms_questions WHERE chat_id=? AND id=? AND status='open'",
        (chat_id, question_id),
    ).fetchone()
    conn.close()
    if not after:
        raise RuntimeError(f"Verified MS-question update failed for Q{question_id}")
    return after


def get_multi_mutation_candidates(chat_id, limit=140):
    rows = list(get_unified_work_candidates(chat_id, limit=limit))
    for q in get_open_ms_questions(chat_id):
        rows.append({
            "chat_id": chat_id,
            "entity_type": "ms_question",
            "entity_id": q["id"],
            "title": q["text"],
            "detail": q["notes"] or "",
            "due_at": q["due_at"],
            "priority": "normal",
            "request_count": 1,
            "last_request_at": None,
            "last_source_name": "",
        })
    return rows


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
            result = apply_reconciled_work_input(
                chat_id, task_text, source="ms_decision", source_ref=f"ms_question:{q['id']}",
                source_name="МС", allow_create=True
            )
            if result.get("action") == "create":
                resulting_task_id = result.get("task_id")
            elif result.get("action") == "update":
                c=result.get("candidate") or {}
                if c.get("entity_type") == "task":
                    resulting_task_id=c.get("entity_id")
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
    """Legacy compatibility only.
    Never recreate or reopen the Alena question automatically. User-resolved state wins.
    If historical duplicates exist, keep at most one currently-open row and cancel extra open duplicates.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM ms_questions WHERE chat_id=? ORDER BY id",
        (chat_id,),
    ).fetchall()
    matches = []
    for row in rows:
        n = normalize_text(row["text"])
        if "алена" in n and "operations" in n and ("зиновец" in n or "марин" in n):
            matches.append(row)
    open_matches = [r for r in matches if r["status"] == "open"]
    if len(open_matches) > 1:
        keep = open_matches[0]
        now = datetime.now(TZ).isoformat(timespec="seconds")
        for extra in open_matches[1:]:
            conn.execute(
                "UPDATE ms_questions SET status='cancelled',answered_at=?,answer_text=? WHERE id=?",
                (now, "Автоочистка: дубль вопроса по Алёне.", extra["id"]),
            )
    conn.commit(); conn.close()
    return open_matches[0]["id"] if open_matches else None



def repair_old_duplicate_artifacts(chat_id):
    """
    v7.8 briefly marked exact duplicates as ordinary completed tasks.
    Reclassify those historical auto-closed duplicates so they do not appear
    under "Готово сегодня".
    """
    conn = get_db()
    conn.execute(
        """
        UPDATE tasks
        SET status='duplicate', completed_at=NULL
        WHERE chat_id=?
          AND status='done'
          AND notes LIKE '%Закрыто автоматически как дубль задачи%'
        """,
        (chat_id,),
    )
    conn.commit()
    conn.close()



def migrate_gift_artist_names(chat_id):
    """
    Rename generic artist #1/#2 steps in the gift process to the picture names:
    Тишина and Прыжок.
    Existing step numbers stay the same:
      5/6 -> Тишина
      7/8 -> Прыжок
    """
    target = None
    for p in get_open_processes(chat_id):
        if "подарки двум директорам" in normalize_text(p["title"]):
            target = p
            break
    if not target:
        return

    replacements = {
        5: "Тишина: получить подписанный акт",
        6: "Тишина: получить счёт на оплату",
        7: "Прыжок: получить подписанный акт",
        8: "Прыжок: получить счёт на оплату",
        9: "Создать заявку в 1С по картине «Тишина» и прикрепить счёт + подписанный акт",
        10: "Создать заявку в 1С по картине «Прыжок» и прикрепить счёт + подписанный акт",
        11: "Проверять статус заявки по «Тишине» в 1С до оплаты",
        12: "Проверять статус заявки по «Прыжку» в 1С до оплаты",
    }

    conn = get_db()
    for step_no, new_text in replacements.items():
        conn.execute(
            "UPDATE process_steps SET text=? WHERE process_id=? AND step_no=?",
            (new_text, target["id"], step_no),
        )

    # waiting_for should also use real labels.
    conn.execute(
        "UPDATE process_steps SET waiting_for='Тишина' WHERE process_id=? AND step_no IN (5,6)",
        (target["id"],),
    )
    conn.execute(
        "UPDATE process_steps SET waiting_for='Прыжок' WHERE process_id=? AND step_no IN (7,8)",
        (target["id"],),
    )
    conn.commit()
    conn.close()


def repair_current_gift_artist_state(chat_id):
    """
    Authoritative state confirmed by Masha:
    - both invoices are received;
    - signed act for Прыжок is received;
    - signed act for Тишина is still awaited.
    """
    target = None
    for p in get_open_processes(chat_id):
        if "подарки двум директорам" in normalize_text(p["title"]):
            target = p
            break
    if not target:
        return

    migrate_gift_artist_names(chat_id)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    conn = get_db()

    # Known done: both invoices, act for Прыжок.
    for step_no in (6, 7, 8):
        conn.execute(
            """
            UPDATE process_steps
            SET status='done', completed_at=COALESCE(completed_at, ?)
            WHERE process_id=? AND step_no=?
            """,
            (now, target["id"], step_no),
        )

    # Тишина act must remain open until actually received.
    conn.execute(
        """
        UPDATE process_steps
        SET status='open', completed_at=NULL
        WHERE process_id=? AND step_no=5
        """,
        (target["id"],),
    )

    conn.commit()
    conn.close()


def ensure_known_gift_state(chat_id):
    migrate_gift_artist_names(chat_id)
    """
    One-time factual repair from the real work state already confirmed by Masha:
    - приказ подготовлен;
    - Алёна подписала приказ;
    - счёт художника №1 получен.
    This is idempotent and only touches those exact steps in the gift process.
    """
    target = None
    for p in get_open_processes(chat_id):
        if "подарки двум директорам" in normalize_text(p["title"]):
            target = p
            break

    if not target:
        return

    steps = {s["step_no"]: s for s in get_process_steps(target["id"])}
    now = datetime.now(TZ).isoformat(timespec="seconds")

    # Known completed steps: 1 = order prepared, 4 = Alena signed, 6 = artist #1 invoice received.
    known_done = [1, 4, 6]

    conn = get_db()
    for step_no in known_done:
        step = steps.get(step_no)
        if step and step["status"] != "done":
            conn.execute(
                """
                UPDATE process_steps
                SET status='done', completed_at=COALESCE(completed_at, ?)
                WHERE process_id=? AND step_no=?
                """,
                (now, target["id"], step_no),
            )
    conn.commit()
    conn.close()
    repair_current_gift_artist_state(chat_id)


def apply_known_gift_update(chat_id, text):
    """
    Natural-language updates for the current gift process.
    Returns True when a known step was recognized and persisted.
    """
    n = normalize_text(text)

    target = None
    for p in get_open_processes(chat_id):
        if "подарки двум директорам" in normalize_text(p["title"]):
            target = p
            break
    if not target:
        return False

    step_no = None
    if "анжелик" in n and ("подпис" in n or "соглас" in n) and "приказ" in n:
        step_no = 2
    elif "финанс" in n and ("подпис" in n or "соглас" in n) and "приказ" in n:
        step_no = 3
    elif ("алена" in n or "алёна" in text.lower()) and "подпис" in n and "приказ" in n:
        step_no = 4
    elif ("тишин" in n or "перв" in n or "№1" in text) and "акт" in n and (
        "получ" in n or "подпис" in n or "готов" in n
    ):
        step_no = 5
    elif ("тишин" in n or "перв" in n or "№1" in text) and ("счет" in n or "счёт" in text.lower()) and (
        "получ" in n or "есть" in n or "готов" in n
    ):
        step_no = 6
    elif ("прыж" in n or "втор" in n or "№2" in text) and "акт" in n and (
        "получ" in n or "подпис" in n or "готов" in n
    ):
        step_no = 7
    elif ("прыж" in n or "втор" in n or "№2" in text) and ("счет" in n or "счёт" in text.lower()) and (
        "получ" in n or "есть" in n or "готов" in n
    ):
        step_no = 8

    if not step_no:
        return False

    ok, msg = complete_process_step(chat_id, target["id"], step_no)
    send_message(chat_id, msg)
    if ok:
        send_message(chat_id, format_process(target["id"], chat_id))
    return True


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
            SET status='duplicate',
                completed_at=NULL,
                notes=CASE
                    WHEN notes IS NULL OR notes='' THEN ?
                    ELSE notes || '\n' || ?
                END
            WHERE id=? AND status='open'
            """,
            (
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


def classify_read_only_intent(text):
    """Deterministic guard: read/compose requests must never mutate SQLite."""
    clean = strip_punctuation(re.sub(r"\s+", " ", (text or "").strip()).strip())
    n = normalize_text(clean)

    # Questions/approvals for MS: display only.
    if re.search(r"\b(?:дай|покажи|покажи мне|пришли|выведи|какие|список)\b.*\bвопрос", n) and "мс" in n:
        return "show_ms_questions"
    if n in {"вопросы мс", "вопросы к мс", "что спросить у мс", "что нужно спросить у мс"}:
        return "show_ms_questions"

    # Compose a copy-ready message to MS: display only.
    if "мс" in n and "апдейт" in n and any(v in n for v in ("напиши","составь","собери","подготовь","дай","покажи","пришли")):
        return "compose_ms_update"

    # Read all work / status. This is a query, never a new task.
    all_work_phrases = (
        "все дела", "всем делам", "по всем делам", "актуальный список дел",
        "апдейт всех дел", "апдейт по всем делам", "что у меня висит", "что у меня сейчас",
    )
    if any(p in n for p in all_work_phrases) and any(v in n for v in ("дай","покажи","пришли","апдейт","что")):
        return "show_all_work"

    return None


def handle_read_only_intent(chat_id, text):
    intent = classify_read_only_intent(text)
    if not intent:
        return False
    if intent == "show_ms_questions":
        show_ms_questions(chat_id)
        return True
    if intent == "compose_ms_update":
        send_ms_end_of_day_message(chat_id)
        return True
    if intent == "show_all_work":
        # Use the same rich contextual answer as normal chat, but bypass every write router.
        send_message(chat_id, "Думаю…")
        try:
            prompt = (
                "Дай актуальный апдейт по всем моим рабочим делам. "
                "Сначала срочное/просроченное, затем по датам, затем без срока, процессы и вопросы МС. "
                "Отдельно отметь расхождения и вероятные дубли. Ничего не придумывай."
            )
            send_message(chat_id, ask_openai(chat_id, prompt))
        except Exception as e:
            print("All-work read error:", repr(e))
            send_message(chat_id, "Не смогла собрать полный апдейт. Посмотри лог Railway.")
        return True
    return False


def looks_like_ms_question(text):
    if classify_read_only_intent(text):
        return False
    n = normalize_text(text)
    triggers = [
        "вечернем апдейте",
        "спросить мс",
        "уточнить у мс",
        "выяснить у мс",
        "согласовать с мс",
        "узнать у мс",
        "спросить у мс",
        "вопрос мс",
        "вопрос к мс",
        "вопросы мс",
        "вопросы к мс",
        "в мс вопросы",
        "вопросы внести в мс",
        "внести вопросы в мс",
        "напомнить мс",
    ]
    # "вечерний апдейт" alone is NOT a question: it may be a compose command,
    # which is handled earlier by try_handle_command.
    if any(t in n for t in triggers):
        return True
    # Natural shorthand such as "По Эльдару надо в МС вопросы внести..."
    return ("мс" in n and "вопрос" in n and any(v in n for v in ("внести","добавить","спросить","уточнить","напомнить")))


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

{unified_work_context(chat_id, limit=50)}

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

    # 0) A forwarded reminder/update from MS is itself new work context even when
    # the underlying action already exists. Reconcile the RAW message first.
    # This prevents the forward classifier from swallowing a repeated request as
    # "already accounted for / no new actions".
    if raw_text and is_ms_source_name(source_name):
        try:
            ms_candidate, ms_score = find_unified_work_candidate(chat_id, raw_text, threshold=0.35)
            if ms_candidate is not None:
                result = apply_reconciled_work_input(
                    chat_id,
                    raw_text,
                    source="forwarded",
                    source_ref=source_ref,
                    source_name=source_name,
                    allow_create=False,
                    force_candidate=ms_candidate,
                    notes_hint="Повторная/уточняющая пересылка от МС."
                )
                # Store the original forward independently as an audit trail.
                save_forwarded_inbox(
                    chat_id, message, source_name, raw_text,
                    {
                        "summary": "Пересылка МС сверена с существующим делом до классификатора.",
                        "items": [],
                        "reconciled": {
                            "action": result.get("action"),
                            "entity_type": ms_candidate.get("entity_type"),
                            "entity_id": ms_candidate.get("entity_id"),
                            "score": ms_score,
                        },
                    },
                )
                if result.get("action") in {"update", "complete"}:
                    send_message(
                        chat_id,
                        "Пересылку МС сверила с базой ✅\n" +
                        format_reconciled_result(result, source_name)
                    )
                    return True
        except Exception as e:
            # Do not lose the message because the deterministic preflight failed.
            # Fall through to the normal forward pipeline, but log the exact error.
            print("MS forward preflight error:", repr(e))

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

                result = apply_reconciled_work_input(
                    chat_id, item_text, source="forwarded", source_ref=source_ref,
                    source_name=source_name, allow_create=True,
                    due_override=item.get("due_at") or None, notes_hint=" | ".join(x for x in [raw_text.strip(), str(item.get("notes") or "").strip()] if x)
                )
                if result.get("action") == "create":
                    created_tasks.append((result["task_id"], result["task_text"]))
                else:
                    created_tasks.append((None, format_reconciled_result(result, source_name)))

            save_forwarded_inbox(
                chat_id,
                message,
                source_name,
                raw_text,
                parsed,
            )

            if created_tasks:
                lines = ["Пересылку обработала и сверила с базой ✅"]
                for tid, item_text in created_tasks:
                    lines.append(f"#{tid} — {item_text}" if tid else item_text)
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
                result = apply_reconciled_work_input(
                    chat_id, item_text, source="forwarded", source_ref=source_ref,
                    source_name=source_name, allow_create=True,
                    due_override=item.get("due_at") or None, notes_hint=" | ".join(x for x in [raw_text.strip(), str(item.get("notes") or "").strip()] if x)
                )
                if result.get("action") == "create":
                    created_tasks.append((result["task_id"], result["task_text"]))
                else:
                    created_tasks.append((None, format_reconciled_result(result, source_name)))

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
            lines.append(f"#{tid} — {item_text}" if tid else item_text)

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



def find_control_fuzzy(chat_id, hint):
    h = task_dedupe_key(hint or "")
    rows = get_control_items(chat_id)
    best, score_best = None, 0
    for r in rows:
        t = task_dedupe_key(r["title"])
        score = len(set(h.split()) & set(t.split()))
        if h and (h in t or t in h):
            return r
        if score > score_best:
            best, score_best = r, score
    return best if score_best >= 1 else None


def update_control_item(chat_id, row, next_action=None, waiting_for=None, next_check=None, notes=None):
    if not row:
        return False
    conn = get_db()
    fields, vals = [], []
    if next_action:
        fields.append("next_action=?"); vals.append(next_action)
    if waiting_for is not None:
        fields.append("waiting_for=?"); vals.append(waiting_for)
    if next_check:
        fields.append("next_check=?"); vals.append(next_check)
    if notes:
        fields.append("notes=?"); vals.append(((row["notes"] or "") + "\n" + notes).strip())
    fields.append("updated_at=?"); vals.append(datetime.now(TZ).isoformat(timespec="seconds"))
    vals += [chat_id, row["id"]]
    conn.execute(f"UPDATE control_items SET {', '.join(fields)} WHERE chat_id=? AND id=?", vals)
    conn.commit(); conn.close()
    return True


def closeout_calendar_context():
    now = datetime.now(TZ)
    try:
        events = fetch_calendar_events(
            now.replace(hour=0, minute=0, second=0, microsecond=0),
            now + timedelta(days=30),
        )
    except Exception as e:
        print("Closeout calendar error:", repr(e))
        return "Календарь недоступен."
    out = []
    for ev in events[:120]:
        start = ev.get("start")
        stamp = start.strftime("%d.%m %H:%M") if isinstance(start, datetime) else str(start)
        out.append(f"- {stamp} — {ev.get('title') or '(без названия)'}")
    return "\n".join(out) or "Событий не найдено."


def parse_closeout(chat_id, raw_text):
    prompt = f"""
Разбери вечерний brain dump бизнес-ассистента в изменения рабочей системы.

Правила:
- не дублируй существующие задачи, процессы, control items и вопросы МС;
- task = новое действие Маши;
- ask_ms = только то, что требует ответа/подтверждения/действия МС;
- если Маше сначала надо самой отправить МС ссылку/документ, это task, а не ask_ms;
- control_update = обновление уже существующего долгого контрольного дела;
- done = реально выполненное;
- waiting = уже запросили и теперь ждём другого человека;
- clarification = нельзя безопасно понять важную деталь;
- завтра = {(datetime.now(TZ).date()+timedelta(days=1)).isoformat()};
- если дата забылась, попробуй взять её из календаря;
- не угадывай, от какого художника акт, если сказано только "один акт";
- по подаркам: если сказано "два счёта", это факт, что оба счёта получены;
- если приказ подписан Алёной и Анжеликой, это факт выполнения этих подписей;
- по Яндекс Диску, ретро и школе Антона обновляй существующие control items;
- если запрос Анжелике уже отправлен, сохраняй ожидание/контроль, а не задачу "написать Анжелике";
- если в тексте упомянуты таунхол 3 сентября + Кадровый комитет + CEO говорит, сохрани один ask_ms со всеми этими событиями, не теряй два последних;
- если указаны названия картин «Тишина» и «Прыжок», используй их вместо художник №1/№2;
- подтверждённый акт по «Прыжку» = готово; акт по «Тишине» = ждём.

ПРОЦЕССЫ:
{brief_processes_context(chat_id)}

CONTROL:
{control_context(chat_id, due_only=False)}

ЗАДАЧИ:
{brief_tasks_context(chat_id)}

ВОПРОСЫ МС:
{brief_ms_questions_context(chat_id)}

КАЛЕНДАРЬ:
{closeout_calendar_context()}

ТЕКСТ:
{raw_text}
"""

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "task",
                                "ask_ms",
                                "control_update",
                                "done",
                                "waiting",
                                "clarification",
                                "info",
                            ],
                        },
                        "text": {"type": "string"},
                        "due_at": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "waiting_for": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "control_hint": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "notes": {"type": "string"},
                    },
                    "required": [
                        "kind",
                        "text",
                        "due_at",
                        "waiting_for",
                        "control_hint",
                        "notes",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    payload = {
        "model": MODEL,
        "instructions": (
            "Ты диспетчер рабочей системы. "
            "Строго классифицируй пункты вечернего brain dump по переданной JSON-схеме."
        ),
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "closeout_items",
                "strict": True,
                "schema": schema,
            }
        },
        "store": False,
    }

    r = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )

    if not r.ok:
        print(
            "Closeout OpenAI HTTP error:",
            r.status_code,
            r.text[:5000],
        )
    r.raise_for_status()

    raw = extract_openai_text(r.json())
    print("Closeout structured raw:", raw[:5000])

    obj = json.loads(raw)
    if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
        raise ValueError("invalid closeout structured result")

    return obj
def add_ms_question_dedup(chat_id, body, due_at=None, notes=""):
    key = task_dedupe_key(body)
    words = set(key.split())
    for q in get_open_ms_questions(chat_id):
        qkey = task_dedupe_key(q["text"])
        overlap = len(words & set(qkey.split()))
        if key == qkey or overlap >= 6:
            return q["id"], False
    return add_ms_question(chat_id, body, due_at=due_at, source="closeout", notes=notes), True


def handle_closeout(chat_id, raw_text):
    send_message(chat_id, "Разбираю итоги дня и раскладываю по задачам, контролю и вопросам МС…")
    try:
        parsed = parse_closeout(chat_id, raw_text)
    except Exception as e:
        print("Closeout parse error:", type(e).__name__, repr(e))
        send_message(
            chat_id,
            "Не смогла разобрать итоги. Ничего не меняла. "
            "В Railway теперь будет точная строка Closeout parse error с типом ошибки."
        )
        return True

    tasks, questions, controls, waits, clarifications = [], [], [], [], []

    # First persist AI-classified items.
    for item in parsed.get("items", [])[:30]:
        kind = str(item.get("kind") or "").lower().strip()
        body = str(item.get("text") or "").strip()
        if not body:
            continue
        due = item.get("due_at") or None

        if kind == "task":
            result=apply_reconciled_work_input(chat_id,body,source="closeout",allow_create=True,due_override=due,notes_hint=str(item.get("notes") or ""))
            if result.get("action")=="create":
                tasks.append((result["task_id"], result["task_text"]))
            elif result.get("action")=="update":
                c=result.get("candidate") or {}; controls.append((c.get("entity_id"), c.get("title")))

        elif kind == "ask_ms":
            qid, _ = add_ms_question_dedup(chat_id, body, due_at=due, notes=str(item.get("notes") or ""))
            questions.append((qid, body))

        elif kind == "control_update":
            row = find_control_fuzzy(chat_id, item.get("control_hint") or body)
            if row:
                update_control_item(
                    chat_id, row, next_action=body,
                    waiting_for=item.get("waiting_for"),
                    next_check=due, notes=str(item.get("notes") or "")
                )
                controls.append((row["id"], row["title"]))
            else:
                result=apply_reconciled_work_input(chat_id,body,source="closeout",allow_create=True,due_override=due,notes_hint=str(item.get("notes") or ""))
                if result.get("action")=="create": tasks.append((result["task_id"],result["task_text"]))

        elif kind == "waiting":
            waits.append((item.get("waiting_for"), body))
            # If this belongs to a control item, persist it there.
            row = find_control_fuzzy(chat_id, item.get("control_hint") or body)
            if row:
                update_control_item(
                    chat_id, row, next_action=body,
                    waiting_for=item.get("waiting_for"),
                    next_check=due
                )
                controls.append((row["id"], row["title"]))
            else:
                # Keep a standalone waiting action searchable, but reconcile first.
                result=apply_reconciled_work_input(chat_id,body,source="closeout_waiting",allow_create=True,due_override=due,notes_hint=f"Ждём: {item.get('waiting_for') or 'другого человека'}")
                if result.get("action")=="create": tasks.append((result["task_id"],result["task_text"]))

        elif kind == "done":
            # Close the matching authoritative work item; do not manufacture a temporary completed task.
            result=apply_reconciled_work_input(chat_id,"Готово: "+body,source="closeout_done",allow_create=False)
            if result.get("action") not in {"complete","update"}:
                clarifications.append("Не смогла однозначно сопоставить выполненное дело: " + body)

        elif kind == "clarification":
            clarifications.append(body)

    # Deterministic safety net for critical brain-dump items that must not be lost.
    n_full = normalize_text(raw_text)
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()

    if "яндекс" in n_full and ("айти" in n_full or "it" in n_full) and "завтра" in n_full:
        tid = add_task(
            chat_id,
            "Подёргать IT по переносу документов Google → Яндекс Диск",
            source="closeout",
            due_at=tomorrow,
        )
        tasks.append((tid, "Подёргать IT по переносу документов Google → Яндекс Диск"))

    if "finance" in n_full and "аналитик" in n_full and "повест" in n_full and "анжелик" in n_full:
        tid = add_task(
            chat_id,
            "Дожать Анжелику по повесткам на встречи Finance и Аналитика",
            source="closeout",
            due_at=tomorrow,
            notes="Запрос уже отправлен; ждём ответ Анжелики.",
        )
        tasks.append((tid, "Дожать Анжелику по повесткам на встречи Finance и Аналитика"))

    if "егор" in n_full and "водител" in n_full and ("фио" in n_full or "фото" in n_full):
        tid = add_task(
            chat_id,
            "Получить от Егора фото и ФИО водителей для анкеты новой школы Антона",
            source="closeout",
            due_at=tomorrow,
            notes="Сегодня информация от Егора не получена.",
        )
        tasks.append((tid, "Получить от Егора фото и ФИО водителей для анкеты новой школы Антона"))

    # Deterministic factual repair for the gift process from phrases we can trust.
    n = normalize_text(raw_text)
    if "приказ" in n and "подписан" in n and ("ален" in n or "алён" in n):
        apply_known_gift_update(chat_id, "Алёна подписала приказ")
    if "приказ" in n and "подписан" in n and "анжелик" in n:
        apply_known_gift_update(chat_id, "Анжелика подписала приказ")

    if ("два счета" in n or "два счёта" in raw_text.lower()) and "подар" in n:
        p = next((p for p in get_open_processes(chat_id) if "подарки двум директорам" in normalize_text(p["title"])), None)
        if p:
            for no in (6, 8):
                steps = {s["step_no"]: s for s in get_process_steps(p["id"])}
                if no in steps and steps[no]["status"] != "done":
                    complete_process_step(chat_id, p["id"], no)

    if "прыж" in n and "акт" in n and ("есть" in n or "получ" in n or "подпис" in n):
        apply_known_gift_update(chat_id, "Прыжок: подписанный акт получен")
    if "тишин" in n and "акт" in n and ("жду" in n or "ожида" in n):
        repair_current_gift_artist_state(chat_id)

    # The school dates were explicitly reported as entered in calendar.
    if "внес" in n and "даты" in n and "школ" in n and ("пикник" in n or "свечк" in n):
        row = find_control_fuzzy(chat_id, "Антон прикрепление к школе")
        if row:
            update_control_item(chat_id, row, notes="Даты школьных событий внесены в календарь: родительское собрание, пикник и свечка.")

    conn = get_db()
    conn.execute(
        "INSERT INTO closeout_log(chat_id,raw_text,parsed_json,created_at) VALUES (?,?,?,?)",
        (chat_id, raw_text, json.dumps(parsed, ensure_ascii=False), datetime.now(TZ).isoformat(timespec="seconds"))
    )
    conn.commit(); conn.close()

    lines = ["Итоги сохранила ✅"]
    if tasks: lines.append(f"Задачи: {len(tasks)}.")
    if controls: lines.append(f"Обновления контрольных дел: {len(set(x[0] for x in controls))}.")
    if questions: lines.append(f"Вопросы / согласования с МС: {len(set(x[0] for x in questions))}.")
    if waits: lines.append(f"Ожидания от других: {len(waits)}.")

    if questions:
        lines += ["", "Вопросы / нужно от МС:"]
        seen = set()
        for qid, body in questions:
            if qid not in seen:
                seen.add(qid); lines.append(f"— Q{qid}: {body}")

    if clarifications:
        lines += ["", "Нужно уточнить:"]
        for c in clarifications:
            lines.append(f"— {c}")

    # Show current authoritative gift state when the closeout mentioned gifts.
    if "подар" in normalize_text(raw_text):
        try:
            gift = next(
                (
                    p for p in get_open_processes(chat_id)
                    if "подарки двум директорам" in normalize_text(p["title"])
                ),
                None,
            )
            if gift:
                lines += ["", "По подаркам сейчас зафиксировано:"]
                for s in get_process_steps(gift["id"]):
                    icon = "✅" if s["status"] == "done" else "➡️"
                    if s["status"] == "done" or s["step_no"] in {2,3,4,5,6,7,8,9,10}:
                        lines.append(f"{icon} {s['step_no']}. {s['text']}")
        except Exception as e:
            print("Closeout gift summary error:", repr(e))

    lines += ["", "После уточнений можно вызвать /evening — он соберёт внутренний итог уже из обновлённого состояния."]
    send_message(chat_id, "\n".join(lines))

    ms_msg = build_ms_end_of_day_message(chat_id)
    if ms_msg:
        send_message(
            chat_id,
            "Готовый текст для МС на конец дня:\n\n" + ms_msg
        )
    else:
        send_message(
            chat_id,
            "На конец дня от МС дополнительных ответов/согласований не требуется."
        )
    return True



def cleanup_stale_tasks(chat_id):
    """
    Close known obsolete standalone tasks when newer authoritative structures exist.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id,text FROM tasks WHERE chat_id=? AND status='open'",
        (chat_id,),
    ).fetchall()

    for row in rows:
        n = normalize_text(row["text"])

        # Eldar email has already been found; current work is Q2 / agenda.
        if "эльдар" in n and ("письмо" in n or "поднять" in n):
            conn.execute(
                "UPDATE tasks SET status='obsolete', completed_at=NULL WHERE id=?",
                (row["id"],),
            )

    conn.commit()
    conn.close()


def reconcile_closeout_state(chat_id):
    """
    Normalize the known current state so all views agree.
    Safe/idempotent migration for the current working database.
    """
    cleanup_stale_tasks(chat_id)
    migrate_gift_artist_names(chat_id)
    repair_current_gift_artist_state(chat_id)

    conn = get_db()

    # Old Eldar task is obsolete: the email is found; the open action lives in MS questions.
    rows = conn.execute(
        "SELECT id,text FROM tasks WHERE chat_id=? AND status='open'",
        (chat_id,),
    ).fetchall()
    for row in rows:
        n = normalize_text(row["text"])
        if "эльдар" in n and ("письмо" in n or "поднять" in n):
            conn.execute(
                "UPDATE tasks SET status='obsolete', completed_at=NULL WHERE id=?",
                (row["id"],),
            )

        # Monitor was reported completed during the closeout tests.
        if "подвинуть монитор" in n:
            conn.execute(
                """
                UPDATE tasks
                SET status='done',
                    completed_at=COALESCE(completed_at, ?),
                    notes=CASE
                        WHEN notes IS NULL OR notes='' THEN 'Закрыто по факту: монитор подвинули.'
                        ELSE notes
                    END
                WHERE id=?
                """,
                (datetime.now(TZ).isoformat(timespec="seconds"), row["id"]),
            )

    # Ensure the three lost closeout actions exist.
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    conn.commit()
    conn.close()

    add_task(
        chat_id,
        "Подёргать IT по переносу документов Google → Яндекс Диск",
        source="reconcile_v7_9_6",
        due_at=tomorrow,
    )
    add_task(
        chat_id,
        "Дожать Анжелику по повесткам на встречи Finance и Аналитика",
        source="reconcile_v7_9_6",
        due_at=tomorrow,
        notes="Запрос уже отправлен; ждём ответ Анжелики.",
    )
    add_task(
        chat_id,
        "Получить от Егора фото и ФИО водителей для анкеты новой школы Антона",
        source="reconcile_v7_9_6",
        due_at=tomorrow,
        notes="Сегодня информация не получена.",
    )


def build_pretty_ms_update(chat_id):
    """
    Produce a natural copy-ready message for MS from open MS questions only.
    Internal wording like 'Напомнить МС...' is converted into direct speech.
    """
    ensure_alena_operations_question(chat_id)
    rows = get_open_ms_questions(chat_id)
    today = datetime.now(TZ).date()
    active = []

    for q in rows:
        if q["due_at"]:
            try:
                if date.fromisoformat(q["due_at"]) > today:
                    continue
            except Exception:
                pass
        active.append(q)

    if not active:
        return ""

    bullets = []
    for q in active:
        t = q["text"].strip()
        n = normalize_text(t)

        if "алена" in n and "operations" in n:
            t = "Подскажи, пожалуйста, нужна ли Алёна на Operations 18 августа с Мариной Зиновец?"
        elif "мшр" in n or "московская школа рекламы" in n:
            t = "Посмотри, пожалуйста, предложение Эльдара по МШР."
        elif "мелан" in n and ("обратн" in n or "форм" in n):
            t = "По Мелании я пришлю ссылку на обратную связь — нужно будет заполнить форму."
        elif "таунхол" in n or "кадров" in n or "ceo" in n:
            t = "Поставила в календарь таунхол на 3 сентября, а также Кадровый комитет и «CEO говорит» в августе. Посмотри, пожалуйста, всё ли ок по датам."
        else:
            # strip internal action prefixes
            t = re.sub(r"(?i)^(напомнить|сообщить|отправить|попросить|уточнить)\s+мс\s+", "", t).strip()
            if t and t[-1] not in ".?!":
                t += "."

        bullets.append("— " + t)

    return "Маша, на сегодня несколько моментов:\n\n" + "\n".join(bullets)


def build_evening_update(chat_id):
    reconcile_closeout_state(chat_id)
    ensure_known_gift_state(chat_id)
    repair_old_duplicate_artifacts(chat_id)
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



def build_ms_end_of_day_message(chat_id):
    reconcile_closeout_state(chat_id)
    return build_pretty_ms_update(chat_id)


def send_ms_end_of_day_message(chat_id):
    msg = build_ms_end_of_day_message(chat_id)
    if not msg:
        send_message(chat_id, "Сегодня от МС ничего дополнительно не требуется.")
        return
    send_message(
        chat_id,
        "Готовый текст для МС:\n\n" + msg
    )


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
    sections=[]
    memories=get_memories(chat_id)
    if memories:
        sections.append("ПОСТОЯННАЯ ПАМЯТЬ:\n" + "\n".join(f"- {r['text']}" for r in memories))
    sections.append(unified_work_context(chat_id, limit=80))
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
    ensure_known_gift_state(chat_id)
    repair_old_duplicate_artifacts(chat_id)
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



def brief_ms_questions_context(chat_id):
    """
    Open MS questions for /brief and /closeout.
    Kept deterministic so the model sees the authoritative list
    and does not create duplicate questions.
    """
    ensure_alena_operations_question(chat_id)
    rows = get_open_ms_questions(chat_id)

    if not rows:
        return "ВОПРОСЫ / СОГЛАСОВАНИЯ С МС:\nОткрытых вопросов нет."

    lines = ["ВОПРОСЫ / СОГЛАСОВАНИЯ С МС:"]
    for row in rows:
        line = f"- Q{row['id']}: {row['text']}"
        if row["due_at"]:
            line += f"; срок: {row['due_at']}"
        lines.append(line)
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
    ensure_known_gift_state(chat_id)
    repair_old_duplicate_artifacts(chat_id)
    today = datetime.now(TZ).date()

    # Главный источник истины — актуальные статусы в SQLite.
    proc = brief_processes_context(chat_id)
    control = control_context(chat_id, due_only=True)
    task_context = brief_tasks_context(chat_id)
    unified_context = unified_work_context(chat_id, limit=80)
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
1. ЕДИНОЕ АКТУАЛЬНОЕ РАБОЧЕЕ СОСТОЯНИЕ SQLite.
2. Детализация процессов и зависимостей.
3. Календарь.
4. Почта.
Если старый специализированный блок противоречит единому состоянию, доверяй единому состоянию.

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
        "=== 1. ЕДИНОЕ АКТУАЛЬНОЕ РАБОЧЕЕ СОСТОЯНИЕ — ГЛАВНЫЙ ИСТОЧНИК ИСТИНЫ ===\n" + unified_context,
        "=== 2. ДЕТАЛИ ПРОЦЕССОВ И ЗАВИСИМОСТЕЙ ===\n" + proc,
        "=== 3. КОНТРОЛЬНЫЕ ДЕЛА НА СЕГОДНЯ ===\n" + control,
        "=== 4. ОБЫЧНЫЕ ЗАДАЧИ (совместимость со старой схемой) ===\n" + task_context,
        "=== 5. КАЛЕНДАРЬ НА СЕГОДНЯ ===\n" + cal,
        "=== 6. СВЕЖАЯ ПОЧТА ===\n" + mail_brief,
        prompt,
    ])

    return ask_openai_grounded_brief(chat_id, source_context)

# =========================================================
# DISPLAY HELPERS
# =========================================================

def show_task_detail(chat_id, task_id):
    conn = get_db()
    task = conn.execute("SELECT * FROM tasks WHERE chat_id=? AND id=?", (chat_id, task_id)).fetchone()
    if not task:
        conn.close(); send_message(chat_id, f"Не нашла TASK:{task_id}."); return
    events = conn.execute(
        "SELECT event_type,raw_text,source_name,created_at FROM work_events WHERE chat_id=? AND entity_type='task' AND entity_id=? ORDER BY id",
        (chat_id, task_id),
    ).fetchall()
    conn.close()
    lines=[f"TASK:{task_id}", "", "Задача:", task["text"]]
    if task["notes"]: lines += ["", "Детали:", task["notes"]]
    if task["due_at"]: lines += ["", f"Срок/контроль: {task['due_at']}"]
    lines += [f"Приоритет: {task['priority'] or 'normal'}", f"Количество запросов: {int(task['request_count'] or 1)}"]
    if events:
        lines += ["", "История:"]
        for e in events[-12:]:
            try:
                dt=datetime.fromisoformat(e["created_at"]).astimezone(TZ).strftime("%d.%m %H:%M")
            except Exception:
                dt=str(e["created_at"] or "")
            kind={"created":"создано","updated":"обновлено","repeated_request":"повторный запрос","completed":"закрыто"}.get(e["event_type"],e["event_type"])
            src=f" — {e['source_name']}" if e["source_name"] else ""
            raw=(e["raw_text"] or "").strip()
            lines.append(f"[{dt}] {kind}{src}: {raw}" if raw else f"[{dt}] {kind}{src}")
    send_message(chat_id, "\n".join(lines))


def show_tasks(chat_id):
    rows = get_unified_work_candidates(chat_id, limit=100)
    if not rows:
        send_message(chat_id, "Сейчас открытых дел нет.")
        return
    lines=["Открытые дела из единой рабочей базы:"]
    for r in rows:
        prefix={"task":"T","control":"C","process_step":"P"}.get(r["entity_type"],"W")
        line=f"{prefix}{r['entity_id']} — {r['title']}"
        if r.get("due_at"): line+=f" [срок/контроль: {r['due_at']}]"
        if r.get("priority")=="high": line+=" [высокий приоритет]"
        lines.append(line)
    send_message(chat_id,"\n".join(lines))


def show_memory(chat_id):
    rows = get_memories(chat_id)
    send_message(chat_id, "В постоянной памяти пока ничего нет." if not rows else "Вот что я помню:\n\n" + "\n".join(f"#{r['id']} — {r['text']}" for r in rows))


def show_processes(chat_id):
    ensure_known_gift_state(chat_id)
    repair_old_duplicate_artifacts(chat_id)
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

    # Persist obvious human-language progress updates for the gift process.
    if apply_known_gift_update(chat_id, normalized):
        return True

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




def try_handle_natural_work_request(chat_id, text):
    """Natural work conversation over the authoritative SQLite state.
    Handles create, update, completion, follow-ups and work-state questions.
    """
    raw=text.strip(); n=normalize_text(raw)
    if raw.startswith("/"):
        return False
    if classify_read_only_intent(raw):
        return False
    if answer_unified_work_query(chat_id, raw):
        return True
    protected=(
        n.startswith("мс ответила") or n.startswith("ответ мс") or
        n.startswith("подбиваю день") or n.startswith("закрываю день") or n.startswith("итоги дня") or
        "вечернем апдейте надо спросить" in n
    )
    if protected:
        return False

    # Work-like signals. We deliberately include short continuations because last_work_ref
    # makes phrases like «сделаю завтра» safe and useful.
    work_signals=[
        "надо","нужно","не забыть","попросила","попросил","сделаю","можно сделать","дожму","проверю","проверить","напишу",
        "добавь","добавить","добавлю","обнови","обновить",
        "поставить встречу","организовать встречу","прислала","прислал","прислали",
        "внесла","внес","внесли","поставила","поставили","отправила","отправили",
        "готово","сделано","сделали","закрыла","закрыли","получила","получили",
        "подвинули","подвинула","поменяла","поменяли","перенесла","перенесли",
        "договорились","назначила","назначили",
        "во вторник","завтра надо","сегодня надо","перенесу","тогда завтра"
    ]
    # Numbered status lines from the just-shown work list are also work updates.
    numbered_status = bool(re.search(r"(?m)^\s*\d{1,3}[.)]\s*\S+", raw))
    if not any(s in n for s in work_signals) and not numbered_status:
        return False

    try:
        # One user message = one undoable operation, even if it mutates many rows.
        op_id = begin_operation(chat_id, raw, kind="multi_work")
        result=apply_multi_work_input(chat_id,raw,source="natural_work_request",source_name="")
        if result.get("action")=="multi" and (result.get("results") or result.get("failures")):
            finish_operation(chat_id, op_id, "committed")
            send_message(chat_id,format_multi_reconciled_result(result))
            return True
        finish_operation(chat_id, op_id, "noop")
        if result.get("explicit_task_id"):
            send_message(
                chat_id,
                f"⚠️ TASK:{result['explicit_task_id']} не найдена среди открытых задач. "
                "Ничего в базе не меняла."
            )
            return True
    except Exception as e:
        print("Reliable natural write error:",repr(e))
        send_message(chat_id,"⚠️ Я поняла рабочее сообщение, но НЕ смогла надёжно сохранить изменение в базе. Ничего не считаю записанным. Посмотри лог Railway: Reliable natural write error.")
        return True
    return False


def build_tomorrow_plan(chat_id):
    """
    Simple tomorrow list: actionable tasks + control points due tomorrow.
    """
    reconcile_closeout_state(chat_id)
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)

    lines = [f"На завтра, {tomorrow.strftime('%d.%m')}:"]

    picked = []

    for row in get_open_tasks(chat_id):
        due = row["due_at"]
        if not due:
            continue
        try:
            if date.fromisoformat(str(due)[:10]) == tomorrow:
                picked.append(row["text"])
        except Exception:
            pass

    for row in get_control_items(chat_id):
        if row["next_check"] == tomorrow.isoformat():
            picked.append(row["next_action"])

    seen = set()
    clean_items = []
    for item in picked:
        key = task_dedupe_key(item)
        if key and key not in seen:
            seen.add(key)
            clean_items.append(item)

    if clean_items:
        lines.append("")
        for item in clean_items[:12]:
            lines.append(f"— {item}")
    else:
        lines += ["", "Явных задач с датой на завтра пока нет."]

    return "\n".join(lines)


def show_help(chat_id):
    send_message(
        chat_id,
        """Masha 2.0 — главное

Тебе не нужно помнить много команд. Пиши боту обычным языком.

Например:
• «МС попросила поставить встречу с Никитой»
• «Мне завтра надо написать Вере»
• «Получила акт по Прыжку»
• «Сегодня в вечернем апдейте надо спросить у МС...»
• просто пересылай сообщения МС

Основные команды:
/brief — что важно сегодня
/tomorrowwork — что делать завтра
/closeout — подбить день большим сообщением
/msupdate — готовый текст для МС
/help — эта шпаргалка

Если нужно посмотреть детали:
/tasks — открытые задачи
/questions — что нужно от МС
/processes — процессы
/control — долгие дела
/calendarweek — календарь недели

Остальные старые команды продолжают работать, но помнить их не обязательно."""
    )


def try_handle_command(chat_id, text):
    normalized = re.sub(r"\s+", " ", text.strip()).strip()
    clean = strip_punctuation(normalized)

    if clean in {
        "/undo", "отмени последнее действие", "отмени это действие",
        "отмени последнее", "отмени это", "верни последнее изменение"
    }:
        undone = undo_last_operation(chat_id)
        if undone:
            preview = re.sub(r"\s+", " ", undone.get("raw_text") or "").strip()
            if len(preview) > 180:
                preview = preview[:177] + "..."
            send_message(chat_id, f"Отменила последнее изменение ↩️\n{preview}")
        else:
            send_message(chat_id, "Нет сохранённого изменения, которое можно отменить.")
        return True

    # Absolute read-only priority: these requests may inspect/compose, but never write.
    if handle_read_only_intent(chat_id, normalized):
        return True

    if clean in {"/help", "помощь", "команды", "шпаргалка"}:
        show_help(chat_id)
        return True

    # Natural-language requests to COMPOSE the MS update must be handled as commands
    # before looks_like_ms_question(). Otherwise a phrase such as
    # «напиши вечерний апдейт для МС» is mistakenly stored as a new Q-item.
    ms_update_commands = {
        "/msupdate", "/msbrief", "сообщение для мс", "апдейт для мс",
        "вечерний апдейт для мс", "напиши вечерний апдейт для мс",
        "составь вечерний апдейт для мс", "собери вечерний апдейт для мс",
        "подготовь вечерний апдейт для мс", "дай вечерний апдейт для мс",
        "напиши апдейт для мс", "составь апдейт для мс",
        "собери апдейт для мс", "подготовь апдейт для мс",
        "дай апдейт для мс",
    }
    if clean in ms_update_commands or (
        "для мс" in clean
        and "апдейт" in clean
        and any(v in clean for v in ("напиши", "составь", "собери", "подготовь", "дай", "покажи"))
    ):
        send_ms_end_of_day_message(chat_id)
        return True

    if clean in {"/tomorrowwork", "что завтра", "что мне завтра", "план на завтра"}:
        send_message(chat_id, build_tomorrow_plan(chat_id))
        return True

    if clean == "/version":
        send_message(chat_id, f"Masha 2.0 {BOT_VERSION} ✅")
        return True
    if clean == "/closeout":
        conn = get_db()
        conn.execute(
            """
            INSERT OR REPLACE INTO conversation_state(chat_id,key,value,updated_at)
            VALUES (?,?,?,?)
            """,
            (
                chat_id,
                "awaiting_closeout",
                "1",
                datetime.now(TZ).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()
        send_message(
            chat_id,
            "Готова. Пришли следующим сообщением весь вечерний brain dump как есть — без специальных команд."
        )
        return True

    if re.match(r"^(?:подбиваю день|закрываю день|итоги дня)\s*[:：]", normalized, flags=re.I):
        body = re.sub(
            r"^(?:подбиваю день|закрываю день|итоги дня)\s*[:：]\s*",
            "",
            normalized,
            flags=re.I,
        )
        return handle_closeout(chat_id, body)
    if clean in {"/questions","вопросы мс","вопросы к мс","дай вопросы мс","покажи вопросы мс","покажи вопросы к мс","что спросить у мс"}:
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

    # A short standalone phrase may be a real MS question.
    # A long status dump containing TASK:/CONTROL: references must go to the multi-router instead;
    # one clause "добавить в вечерний апдейт" must never swallow the whole message.
    explicit_work_refs = len(re.findall(r"\b(?:task|control|step)\s*[:#№]?\s*\d+\b", normalized, flags=re.I))
    long_work_dump = explicit_work_refs >= 1 or len(normalized) > 320 or normalized.count(";") >= 2
    if looks_like_ms_question(normalized) and not long_work_dump:
        op_id = begin_operation(chat_id, normalized, kind="add_ms_question")
        try:
            qid, created = add_ms_question_dedup(
                chat_id,
                normalized,
                due_at=detect_due_from_text(normalized),
                notes=""
            )
            finish_operation(chat_id, op_id, "committed")
        except Exception:
            finish_operation(chat_id, op_id, "failed")
            raise
        send_message(
            chat_id,
            (f"Добавила в вопросы МС / вечерний апдейт ✅\nQ{qid} — {normalized}"
             if created else f"Такой вопрос МС уже есть ✅\nQ{qid} — {normalized}")
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
    m = re.match(r"^(?:/task|task|таск)\s*[:#№]?\s*(\d+)\s*$", normalized, flags=re.I)
    if m:
        show_task_detail(chat_id, int(m.group(1))); return True
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
            t = m.group(1).strip()
            try:
                result=apply_reconciled_work_input(chat_id,t,source="explicit_task",allow_create=True)
                if result.get("action") in {"create","update","complete"}:
                    send_message(chat_id,format_reconciled_result(result)); return True
                send_message(chat_id,"⚠️ Не смогла надёжно определить, создать новую задачу или обновить существующую. В базе ничего не меняла."); return True
            except Exception as e:
                print("Explicit task reliable write error:",repr(e))
                send_message(chat_id,"⚠️ Не смогла надёжно записать задачу. В базе ничего не считаю сохранённым."); return True

    m = re.match(r"^(?:забудь|удали из памяти|удали запись)\s*#?\s*(\d+)", normalized, flags=re.I)
    if m:
        mid = int(m.group(1)); send_message(chat_id, f"Удалила запись #{mid}." if delete_memory(chat_id, mid) else f"Не нашла запись #{mid}."); return True

    for pattern in [r"^запомни\s*,?\s*что\s+(.+)$", r"^запомни\s*[,:-]?\s+(.+)$", r"^сохрани\s+в\s+память\s*[,:-]?\s+(.+)$"]:
        m = re.match(pattern, normalized, flags=re.I | re.S)
        if m:
            t = m.group(1).strip(); mid = add_memory(chat_id, t); send_message(chat_id, f"Запомнила 🧠\n#{mid}: {t}"); return True

    return False


def try_handle_closeout_priority(chat_id, text):
    """
    Highest-priority router for evening brain dumps.
    This MUST run before process handlers, otherwise phrases like
    "Анжелика подписала приказ" inside a long closeout are intercepted
    as a single gift-process update and the rest of the message is lost.
    """
    normalized = re.sub(r"\s+", " ", text.strip()).strip()

    # Explicit natural prefix.
    if re.match(r"^(?:подбиваю день|закрываю день|итоги дня)\s*[:：]", normalized, flags=re.I):
        body = re.sub(
            r"^(?:подбиваю день|закрываю день|итоги дня)\s*[:：]\s*",
            "",
            normalized,
            flags=re.I,
        )
        # Clear pending flag if it exists.
        conn = get_db()
        conn.execute(
            "DELETE FROM conversation_state WHERE chat_id=? AND key='awaiting_closeout'",
            (chat_id,),
        )
        conn.commit()
        conn.close()
        return handle_closeout(chat_id, body)

    # /closeout followed by an ordinary next message.
    conn = get_db()
    state = conn.execute(
        """
        SELECT value FROM conversation_state
        WHERE chat_id=? AND key='awaiting_closeout'
        """,
        (chat_id,),
    ).fetchone()

    if state and state["value"] == "1":
        conn.execute(
            "DELETE FROM conversation_state WHERE chat_id=? AND key='awaiting_closeout'",
            (chat_id,),
        )
        conn.commit()
        conn.close()
        return handle_closeout(chat_id, text)

    conn.close()
    return False


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()
    print(f"Masha 2.0 {BOT_VERSION} запущена: read-only first + unified mutations + verified writes + universal undo.")
    offset = None
    known_chat_ids = set()
    repaired_chat_ids = set()
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
                if chat_id not in repaired_chat_ids:
                    repair_known_reliable_memory_artifacts(chat_id)
                    repaired_chat_ids.add(chat_id)
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

                # IMPORTANT: whole-day closeout has priority over every other parser.
                # A long brain dump can contain phrases that look like ordinary
                # process updates, mail commands, etc. We must consume it as one unit.
                if try_handle_closeout_priority(chat_id, text):
                    continue

                # Read/compose intents have absolute priority over every mutation parser.
                if handle_read_only_intent(chat_id, text):
                    continue

                if try_handle_process(chat_id, text):
                    continue
                if try_handle_mail(chat_id, text):
                    continue
                if try_handle_calendar(chat_id, text):
                    continue
                if try_handle_command(chat_id, text):
                    continue
                if try_handle_natural_work_request(chat_id, text):
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
