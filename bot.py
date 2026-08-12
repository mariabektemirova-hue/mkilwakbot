import os
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
# НАСТРОЙКИ
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
CALENDAR_TZ = ZoneInfo("Europe/Moscow")


# =========================================================
# ИНСТРУКЦИЯ
# =========================================================

ASSISTANT_INSTRUCTIONS = """
Ты Маша 2.0 — личный рабочий ассистент Маши.

Главная цель:
разгружать Маше голову и помогать держать под контролем встречи,
задачи, дедлайны, материалы, договоренности, переписку
и организационный хаос.

КОНТЕКСТ РАБОТЫ

- Маша работает бизнес-ассистентом руководителя МС.
- МС может внезапно попросить 30 минут с кем-то из директоров,
  поэтому важно сохранять свободные окна под срочное.
- ТМС не двигаем.
- Advisory Board не двигаем.
- Тренировки и психоаналитика Маши не двигаем.
- Для 1-2-1 повестку начинаем собирать минимум за неделю.
- Материалы желательно получать минимум за 2 суток, лучше раньше.
- Если нет необходимых материалов или повестки,
  нужно отдельно обратить внимание.

ПРЕДПОЧТЕНИЯ КАЛЕНДАРЯ

- Понедельник желательно оставлять под фокус.
- Пятница предпочтительна для внешних встреч.
- Желательный рабочий диапазон примерно 10:30–19:00.
- Между встречами желательно 15–30 минут воздуха.
- Обед не совмещать с внутренними встречами.
- Сохранять окна для срочного и собеседований.

ЧЕК-ЛИСТ ВСТРЕЧ

- Для Zoom должна быть ссылка.
- Участники должны подтвердить присутствие.
- В описании должна быть повестка.
- Для внешней встречи нужен адрес.
- В календаре должен оставаться обед.
- Для собеседования резюме должно быть вложено и распечатано.
- Для PRM заранее собрать материалы руководителей
  в онлайн-папку и проверить доступ.

ПОЧТА

У тебя есть read-only доступ к реальным письмам,
которые программа передает тебе.

Ты можешь:
- анализировать найденные письма;
- связывать несколько писем в одну историю;
- выделять договоренности;
- находить вопросы без ответа;
- определять следующий шаг;
- связывать письмо с сохраненной задачей;
- помогать подготовить ответ.

Ты НЕ можешь:
- утверждать, что письмо отправлено;
- удалять письма;
- переносить письма;
- архивировать письма;
- менять флаги;
- придумывать содержимое писем.

Если передано несколько связанных писем:
- учитывай хронологию;
- собери суть цепочки;
- не пересказывай каждое письмо без необходимости;
- скажи, какое письмо вероятнее всего искала Маша.

КАК РАБОТАТЬ С МАШЕЙ

- Отвечай по-русски.
- Говори естественно и без канцелярита.
- На простой вопрос отвечай коротко.
- Хаотичное сообщение сама структурируй.
- Учитывай предыдущий диалог.
- Учитывай ПОСТОЯННУЮ ПАМЯТЬ.
- Учитывай ОТКРЫТЫЕ ЗАДАЧИ.
- Учитывай реальные данные КАЛЕНДАРЯ и ПОЧТЫ.
- Не заставляй Машу повторять известное.
- Не утверждай, что выполнила действие, если оно не выполнено.
- Календарь пока только читаешь.
- Почту пока только читаешь.

ДОСТОВЕРНОСТЬ

Если в календаре нет описания, говори:
"в данных календаря не вижу повестки".

Если в событии нет location:
"в данных события не вижу адреса".

Если поиск почты ничего не нашел:
так и скажи.

Не придумывай адресатов, вложения, решения
или содержание писем.

ФОРМАТ

Telegram получает обычный текст.
Не используй Markdown-разметку.
Пиши компактно и практично.
"""


# =========================================================
# БАЗА
# =========================================================

def ensure_data_directory():
    os.makedirs(
        os.path.dirname(DB_PATH),
        exist_ok=True
    )


def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )
    conn.row_factory = sqlite3.Row
    return conn


def ensure_mail_contacts_schema(conn):
    columns = conn.execute(
        "PRAGMA table_info(mail_contacts)"
    ).fetchall()

    names = {row[1] for row in columns}

    if "verified" not in names:
        conn.execute("""
            ALTER TABLE mail_contacts
            ADD COLUMN verified INTEGER NOT NULL DEFAULT 0
        """)

    if "source" not in names:
        conn.execute("""
            ALTER TABLE mail_contacts
            ADD COLUMN source TEXT DEFAULT 'legacy'
        """)


def cleanup_bad_mail_contacts(conn):
    """
    Удаляем технические адреса, которые старые версии
    могли ошибочно принять за человека.
    """

    conn.execute("""
        DELETE FROM mail_contacts
        WHERE
            lower(email) LIKE '%@calendar.yandex.ru'
            OR lower(email) LIKE '%@mailer.yandex.ru'
            OR lower(email) LIKE 'noreply@%'
            OR lower(email) LIKE 'no-reply@%'
            OR lower(email) LIKE 'mailer-daemon@%'
            OR lower(email) LIKE 'postmaster@%'
    """)


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

    ensure_mail_contacts_schema(conn)
    cleanup_bad_mail_contacts(conn)

    conn.commit()
    conn.close()


# =========================================================
# ЗАДАЧИ
# =========================================================

def add_task(chat_id, text):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO tasks (
            chat_id,
            text,
            status,
            created_at
        )
        VALUES (?, ?, 'open', ?)
        """,
        (
            chat_id,
            text.strip(),
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    task_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return task_id


def get_open_tasks(chat_id):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, text, created_at
        FROM tasks
        WHERE chat_id = ?
        AND status = 'open'
        ORDER BY id
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    return rows


def complete_task(chat_id, task_id):
    conn = get_db()

    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'done',
            completed_at = ?
        WHERE chat_id = ?
        AND id = ?
        AND status = 'open'
        """,
        (
            datetime.now().isoformat(
                timespec="seconds"
            ),
            chat_id,
            task_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# ПАМЯТЬ
# =========================================================

def add_memory(chat_id, text):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO memories (
            chat_id,
            text,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            chat_id,
            text.strip(),
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    memory_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return memory_id


def get_memories(chat_id):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id, text, created_at
        FROM memories
        WHERE chat_id = ?
        ORDER BY id
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    return rows


def delete_memory(chat_id, memory_id):
    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM memories
        WHERE chat_id = ?
        AND id = ?
        """,
        (
            chat_id,
            memory_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# OPENAI DIALOG STATE
# =========================================================

def get_previous_response_id(chat_id):
    conn = get_db()

    row = conn.execute(
        """
        SELECT previous_response_id
        FROM chat_state
        WHERE chat_id = ?
        """,
        (chat_id,)
    ).fetchone()

    conn.close()

    if row:
        return row["previous_response_id"]

    return None


def save_previous_response_id(
    chat_id,
    response_id
):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO chat_state (
            chat_id,
            previous_response_id
        )
        VALUES (?, ?)

        ON CONFLICT(chat_id)
        DO UPDATE SET
            previous_response_id =
            excluded.previous_response_id
        """,
        (
            chat_id,
            response_id
        )
    )

    conn.commit()
    conn.close()


def clear_previous_response_id(chat_id):
    conn = get_db()

    conn.execute(
        """
        DELETE FROM chat_state
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    conn.commit()
    conn.close()


# =========================================================
# TELEGRAM
# =========================================================

def send_message(chat_id, text):
    text = str(text or "")

    parts = [
        text[i:i + 4000]
        for i in range(
            0,
            len(text),
            4000
        )
    ]

    if not parts:
        parts = [""]

    for part in parts:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": part
            },
            timeout=30
        )

        response.raise_for_status()


# =========================================================
# ЯНДЕКС КАЛЕНДАРЬ
# =========================================================

def get_yandex_client():
    return caldav.DAVClient(
        url=YANDEX_CALDAV_URL,
        username=YANDEX_CALENDAR_LOGIN,
        password=YANDEX_CALENDAR_PASSWORD
    )


def get_yandex_calendars():
    client = get_yandex_client()
    principal = client.principal()

    try:
        return principal.get_calendars()
    except AttributeError:
        return principal.calendars()


def normalize_calendar_datetime(value):
    if isinstance(value, datetime):
        dt = value

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=CALENDAR_TZ
            )
        else:
            dt = dt.astimezone(
                CALENDAR_TZ
            )

        return dt, False

    if isinstance(value, date):
        return datetime(
            value.year,
            value.month,
            value.day,
            tzinfo=CALENDAR_TZ
        ), True

    return None, False


def extract_attendees(component):
    raw = component.get("ATTENDEE")

    if not raw:
        return []

    if not isinstance(raw, list):
        raw = [raw]

    result = []

    for attendee in raw:
        try:
            name = attendee.params.get(
                "CN",
                ""
            )
        except Exception:
            name = ""

        try:
            partstat = attendee.params.get(
                "PARTSTAT",
                ""
            )
        except Exception:
            partstat = ""

        result.append({
            "name": str(name),
            "value": str(attendee),
            "partstat": str(partstat)
        })

    return result


def extract_event_from_ical(
    ical_data,
    calendar_name=""
):
    if not ical_data:
        return []

    if isinstance(ical_data, str):
        ical_data = ical_data.encode(
            "utf-8"
        )

    parsed = Calendar.from_ical(
        ical_data
    )

    result = []

    for component in parsed.walk():

        if component.name != "VEVENT":
            continue

        dtstart_property = component.get(
            "DTSTART"
        )

        if not dtstart_property:
            continue

        start, all_day = (
            normalize_calendar_datetime(
                dtstart_property.dt
            )
        )

        if start is None:
            continue

        end = None

        dtend_property = component.get(
            "DTEND"
        )

        if dtend_property:
            end, _ = (
                normalize_calendar_datetime(
                    dtend_property.dt
                )
            )

        result.append({
            "uid": str(
                component.get("UID", "")
            ),
            "summary": str(
                component.get(
                    "SUMMARY",
                    "Без названия"
                )
            ),
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": str(
                component.get(
                    "LOCATION",
                    ""
                )
            ),
            "description": str(
                component.get(
                    "DESCRIPTION",
                    ""
                )
            ),
            "url": str(
                component.get(
                    "URL",
                    ""
                )
            ),
            "calendar": calendar_name,
            "attendees": extract_attendees(
                component
            )
        })

    return result


def get_calendar_events(
    start_dt,
    end_dt
):
    calendars = get_yandex_calendars()

    collected = []

    for calendar in calendars:

        calendar_name = (
            getattr(
                calendar,
                "name",
                None
            )
            or "Календарь"
        )

        try:
            events = calendar.date_search(
                start=start_dt,
                end=end_dt,
                expand=True
            )
        except TypeError:
            events = calendar.date_search(
                start_dt,
                end_dt,
                expand=True
            )

        for event in events:
            try:
                collected.extend(
                    extract_event_from_ical(
                        event.data,
                        calendar_name
                    )
                )
            except Exception as e:
                print(
                    "Calendar parse error:",
                    repr(e)
                )

    unique = {}

    for event in collected:

        key = (
            event["summary"]
            .strip()
            .lower(),
            event["start"].isoformat()
            if event["start"]
            else "",
            event["end"].isoformat()
            if event["end"]
            else ""
        )

        if key not in unique:
            unique[key] = event
            continue

        existing = unique[key]

        for field in [
            "location",
            "description",
            "url"
        ]:
            if (
                not existing[field]
                and event[field]
            ):
                existing[field] = event[field]

        if (
            not existing["attendees"]
            and event["attendees"]
        ):
            existing["attendees"] = (
                event["attendees"]
            )

    result = list(unique.values())

    result.sort(
        key=lambda item: item["start"]
    )

    return result


def format_event(event):
    start = event["start"]
    end = event["end"]

    if event["all_day"]:
        time_text = "весь день"

    elif end:
        time_text = (
            f"{start.strftime('%H:%M')}"
            f"–{end.strftime('%H:%M')}"
        )

    else:
        time_text = start.strftime(
            "%H:%M"
        )

    text = (
        f"{time_text} — "
        f"{event['summary']}"
    )

    if event["location"]:
        text += (
            f"\n📍 {event['location']}"
        )

    return text


def format_calendar_day(
    target_date,
    title
):
    start_dt = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        tzinfo=CALENDAR_TZ
    )

    events = get_calendar_events(
        start_dt,
        start_dt + timedelta(days=1)
    )

    if not events:
        return (
            f"{title}\n\n"
            "В календаре событий нет."
        )

    return (
        title
        + "\n\n"
        + "\n\n".join(
            format_event(event)
            for event in events
        )
    )


def format_calendar_period(
    start_date,
    days
):
    start_dt = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=CALENDAR_TZ
    )

    events = get_calendar_events(
        start_dt,
        start_dt + timedelta(days=days)
    )

    if not events:
        return (
            "На этот период "
            "в календаре событий нет."
        )

    grouped = {}

    for event in events:
        event_date = (
            event["start"]
            .astimezone(CALENDAR_TZ)
            .date()
        )

        grouped.setdefault(
            event_date,
            []
        ).append(event)

    weekdays = {
        0: "Пн",
        1: "Вт",
        2: "Ср",
        3: "Чт",
        4: "Пт",
        5: "Сб",
        6: "Вс"
    }

    lines = []

    for event_date in sorted(
        grouped.keys()
    ):
        lines.append(
            f"{weekdays[event_date.weekday()]}, "
            f"{event_date.strftime('%d.%m')}"
        )

        for event in grouped[event_date]:
            lines.append(
                format_event(event)
            )

        lines.append("")

    return "\n".join(lines).strip()


def calendar_context(
    start_date,
    days=1
):
    start_dt = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=CALENDAR_TZ
    )

    events = get_calendar_events(
        start_dt,
        start_dt + timedelta(days=days)
    )

    lines = ["КАЛЕНДАРЬ:"]

    if not events:
        lines.append("Событий нет.")
        return "\n".join(lines)

    for index, event in enumerate(
        events,
        start=1
    ):
        start = event["start"]
        end = event["end"]

        if event["all_day"]:
            when = (
                start.strftime("%d.%m.%Y")
                + " весь день"
            )

        elif end:
            when = (
                start.strftime(
                    "%d.%m.%Y %H:%M"
                )
                + "–"
                + end.strftime("%H:%M")
            )

        else:
            when = start.strftime(
                "%d.%m.%Y %H:%M"
            )

        lines.extend([
            "",
            f"Событие {index}",
            "Название: " + event["summary"],
            "Время: " + when,
            (
                "Место/адрес: "
                + (
                    event["location"]
                    or "не указано"
                )
            )
        ])

        description = (
            event["description"].strip()
        )

        if len(description) > 2500:
            description = (
                description[:2500]
                + "..."
            )

        lines.append(
            "Описание: "
            + (
                description
                or "не указано"
            )
        )

        attendees = event.get(
            "attendees",
            []
        )

        if attendees:
            lines.append(
                "Участники: "
                + "; ".join(
                    (
                        f"{a['name'] or a['value']} "
                        f"(статус: "
                        f"{a['partstat'] or 'не указан'})"
                    )
                    for a in attendees
                )
            )

    return "\n".join(lines)


# =========================================================
# ЯНДЕКС ПОЧТА
# =========================================================

def decode_mime_header(value):
    if not value:
        return ""

    try:
        return str(
            make_header(
                decode_header(value)
            )
        )
    except Exception:
        return str(value)


def get_mail_connection():
    client = IMAPClient(
        YANDEX_IMAP_HOST,
        port=YANDEX_IMAP_PORT,
        ssl=True
    )

    client.login(
        YANDEX_MAIL_LOGIN,
        YANDEX_MAIL_PASSWORD
    )

    return client


def clean_html_text(value):
    if not value:
        return ""

    value = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>",
        " ",
        value
    )

    value = re.sub(
        r"(?s)<[^>]+>",
        " ",
        value
    )

    value = html.unescape(value)

    value = re.sub(
        r"[ \t]+",
        " ",
        value
    )

    return value.strip()


def decode_part_payload(part):
    try:
        payload = part.get_payload(
            decode=True
        )

        if payload is None:
            return ""

        charset = (
            part.get_content_charset()
            or "utf-8"
        )

        try:
            return payload.decode(
                charset,
                errors="replace"
            )
        except LookupError:
            return payload.decode(
                "utf-8",
                errors="replace"
            )

    except Exception:
        return ""


def extract_mail_body(message):
    plain_parts = []
    html_parts = []

    if message.is_multipart():

        for part in message.walk():

            content_type = (
                part.get_content_type()
            )

            disposition = str(
                part.get(
                    "Content-Disposition",
                    ""
                )
            ).lower()

            if "attachment" in disposition:
                continue

            if content_type == "text/plain":

                value = decode_part_payload(
                    part
                )

                if value:
                    plain_parts.append(value)

            elif content_type == "text/html":

                value = decode_part_payload(
                    part
                )

                if value:
                    html_parts.append(
                        clean_html_text(value)
                    )

    else:
        value = decode_part_payload(
            message
        )

        if (
            message.get_content_type()
            == "text/html"
        ):
            html_parts.append(
                clean_html_text(value)
            )

        else:
            plain_parts.append(value)

    if plain_parts:
        body = "\n".join(plain_parts)

    elif html_parts:
        body = "\n".join(html_parts)

    else:
        body = ""

    body = body.strip()

    if len(body) > 10000:
        body = (
            body[:10000]
            + "\n...[письмо сокращено]"
        )

    return body


def get_attachment_names(message):
    names = []

    for part in message.walk():

        filename = part.get_filename()

        if filename:
            names.append(
                decode_mime_header(
                    filename
                )
            )

    return names


def parse_email_date(value):
    if not value:
        return None

    try:
        dt = parsedate_to_datetime(
            value
        )

        if not dt:
            return None

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=CALENDAR_TZ
            )
        else:
            dt = dt.astimezone(
                CALENDAR_TZ
            )

        return dt

    except Exception:
        return None


def parse_full_email(
    raw_email,
    folder_name,
    uid
):
    message = email.message_from_bytes(
        raw_email
    )

    return {
        "uid": uid,
        "folder": str(folder_name),
        "message_id": str(
            message.get(
                "Message-ID",
                ""
            )
        ).strip(),
        "subject": decode_mime_header(
            message.get(
                "Subject",
                ""
            )
        ),
        "from": decode_mime_header(
            message.get(
                "From",
                ""
            )
        ),
        "to": decode_mime_header(
            message.get(
                "To",
                ""
            )
        ),
        "date": parse_email_date(
            message.get(
                "Date",
                ""
            )
        ),
        "body": extract_mail_body(
            message
        ),
        "attachments": (
            get_attachment_names(
                message
            )
        )
    }


# =========================================================
# ПОИСКОВАЯ НОРМАЛИЗАЦИЯ
# =========================================================

RUS_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya"
}


MAIL_STOP_WORDS = {
    "найди",
    "найти",
    "письмо",
    "письма",
    "переписку",
    "переписка",
    "почта",
    "почте",
    "поищи",
    "про",
    "от",
    "по",
    "с",
    "мне",
    "мои",
    "мою",
    "все",
    "всё",
    "что",
    "было",
    "есть",
    "посмотри",
    "покажи"
}


SKIP_MAIL_FOLDERS = {
    "drafts",
    "drafts|template",
    "outbox",
    "spam",
    "trash",
    "черновики",
    "спам",
    "удаленные",
    "удалённые"
}


BLOCKED_MAIL_DOMAINS = {
    "calendar.yandex.ru",
    "mailer.yandex.ru"
}


BLOCKED_MAIL_LOCALPARTS = {
    "noreply",
    "no-reply",
    "mailer-daemon",
    "postmaster",
    "notifications",
    "notification"
}


def normalize_search_text(value):
    return re.sub(
        r"\s+",
        " ",
        str(value)
        .lower()
        .replace("ё", "е")
    ).strip()


def transliterate_ru(value):
    return "".join(
        RUS_TO_LAT.get(
            char,
            char
        )
        for char in normalize_search_text(
            value
        )
    )


def russian_stem(word):
    word = normalize_search_text(
        word
    )

    candidates = {word}

    endings = [
        "ами",
        "ями",
        "ого",
        "ему",
        "ому",
        "ом",
        "ем",
        "ах",
        "ях",
        "ов",
        "ев",
        "а",
        "я",
        "у",
        "ю",
        "е",
        "ы",
        "и"
    ]

    for ending in endings:

        if (
            word.endswith(ending)
            and len(word) - len(ending) >= 4
        ):
            candidates.add(
                word[:-len(ending)]
            )

    return min(
        candidates,
        key=len
    )


def extract_query_alias(query):
    words = re.findall(
        r"[а-яёa-z]+",
        normalize_search_text(
            query
        ),
        flags=re.IGNORECASE
    )

    useful = [
        word
        for word in words
        if (
            word not in MAIL_STOP_WORDS
            and len(word) >= 4
        )
    ]

    if not useful:
        return ""

    return russian_stem(
        useful[0]
    )


def extract_email_addresses(value):
    if not value:
        return []

    return [
        addr.lower()
        for name, addr in getaddresses(
            [value]
        )
        if addr and "@" in addr
    ]


def is_system_email(address):
    address = (
        str(address)
        .strip()
        .lower()
    )

    if "@" not in address:
        return True

    local_part, domain = (
        address.rsplit("@", 1)
    )

    if domain in BLOCKED_MAIL_DOMAINS:
        return True

    if local_part in BLOCKED_MAIL_LOCALPARTS:
        return True

    if local_part.startswith(
        "noreply"
    ):
        return True

    if local_part.startswith(
        "no-reply"
    ):
        return True

    if local_part.startswith(
        "mailer-daemon"
    ):
        return True

    return False


# =========================================================
# ПОЧТОВЫЕ КОНТАКТЫ
# =========================================================

def save_verified_mail_contact(
    chat_id,
    alias,
    email_address,
    display_name
):
    alias = normalize_search_text(alias)
    email_address = (
        email_address.strip().lower()
    )

    if (
        not alias
        or not email_address
        or is_system_email(
            email_address
        )
    ):
        return False

    conn = get_db()

    conn.execute(
        """
        DELETE FROM mail_contacts
        WHERE chat_id = ?
        AND alias = ?
        AND email != ?
        """,
        (
            chat_id,
            alias,
            email_address
        )
    )

    conn.execute(
        """
        INSERT INTO mail_contacts (
            chat_id,
            alias,
            email,
            display_name,
            created_at,
            verified,
            source
        )
        VALUES (?, ?, ?, ?, ?, 1, 'matched-name')

        ON CONFLICT(chat_id, alias, email)
        DO UPDATE SET
            display_name = excluded.display_name,
            verified = 1,
            source = 'matched-name'
        """,
        (
            chat_id,
            alias,
            email_address,
            display_name,
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    conn.commit()
    conn.close()

    print(
        "Verified mail contact saved:",
        repr(alias),
        "->",
        repr(email_address)
    )

    return True


def get_verified_mail_contacts(
    chat_id,
    query
):
    alias = extract_query_alias(
        query
    )

    if not alias:
        return []

    alias_lat = transliterate_ru(
        alias
    )

    conn = get_db()

    rows = conn.execute(
        """
        SELECT alias, email, display_name
        FROM mail_contacts
        WHERE chat_id = ?
        AND verified = 1
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    result = []

    for row in rows:

        if is_system_email(
            row["email"]
        ):
            continue

        saved_alias = normalize_search_text(
            row["alias"]
        )

        if (
            saved_alias == alias
            or transliterate_ru(
                saved_alias
            ) == alias_lat
        ):
            result.append(row)

    return result


def list_verified_mail_contacts(
    chat_id
):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT alias, email, display_name
        FROM mail_contacts
        WHERE chat_id = ?
        AND verified = 1
        ORDER BY alias
        """,
        (chat_id,)
    ).fetchall()

    conn.close()

    return [
        row
        for row in rows
        if not is_system_email(
            row["email"]
        )
    ]


def forget_mail_contact(
    chat_id,
    query
):
    alias = extract_query_alias(
        query
    )

    if not alias:
        alias = normalize_search_text(
            query
        )

    conn = get_db()

    cursor = conn.execute(
        """
        DELETE FROM mail_contacts
        WHERE chat_id = ?
        AND alias = ?
        """,
        (
            chat_id,
            alias
        )
    )

    count = cursor.rowcount

    conn.commit()
    conn.close()

    return count


def contact_match_score(
    alias,
    display_name,
    email_address
):
    """
    Оцениваем, насколько адрес действительно
    похож на человека из запроса.

    Эльдар:
    Eldar Gincharadze <eldar.gincharadze@ddb.ru>
    -> высокий score

    Яндекс Календарь <info@calendar.yandex.ru>
    -> сразу запрещён.
    """

    if is_system_email(
        email_address
    ):
        return -100

    alias = normalize_search_text(
        alias
    )

    alias_lat = transliterate_ru(
        alias
    )

    display = normalize_search_text(
        display_name
    )

    display_lat = transliterate_ru(
        display
    )

    local_part = (
        email_address
        .split("@", 1)[0]
        .lower()
    )

    score = 0

    if alias and alias in display:
        score += 20

    if (
        alias_lat
        and alias_lat in display_lat
    ):
        score += 15

    if (
        alias_lat
        and alias_lat in local_part
    ):
        score += 12

    if display:
        score += 1

    return score


def remember_best_contact_from_messages(
    chat_id,
    query,
    messages
):
    """
    За один запрос сохраняем только ОДИН
    наиболее вероятный адрес человека.
    """

    alias = extract_query_alias(
        query
    )

    if not alias:
        return

    candidates = {}

    for item in messages:

        for header_name in [
            "from",
            "to"
        ]:

            raw_value = item.get(
                header_name,
                ""
            )

            for (
                display_name,
                email_address
            ) in getaddresses(
                [raw_value]
            ):

                email_address = (
                    email_address
                    .strip()
                    .lower()
                )

                if not email_address:
                    continue

                if (
                    email_address
                    == YANDEX_MAIL_LOGIN.lower()
                ):
                    continue

                if is_system_email(
                    email_address
                ):
                    continue

                decoded_name = (
                    decode_mime_header(
                        display_name
                    )
                )

                score = contact_match_score(
                    alias,
                    decoded_name,
                    email_address
                )

                existing = candidates.get(
                    email_address
                )

                if (
                    existing is None
                    or score
                    > existing["score"]
                ):
                    candidates[
                        email_address
                    ] = {
                        "score": score,
                        "display_name": (
                            decoded_name
                            or raw_value
                        )
                    }

    if not candidates:
        return

    best_email, best_data = max(
        candidates.items(),
        key=lambda item: (
            item[1]["score"]
        )
    )

    print(
        "Best contact candidate:",
        repr(alias),
        "->",
        repr(best_email),
        "score:",
        best_data["score"]
    )

    # Не сохраняем сомнительное совпадение.
    if best_data["score"] < 10:
        print(
            "Contact candidate rejected:"
            " score too low."
        )
        return

    save_verified_mail_contact(
        chat_id,
        alias,
        best_email,
        best_data["display_name"]
    )


# =========================================================
# ПАПКИ
# =========================================================

def get_useful_mail_folders(client):
    result = []

    for (
        flags,
        delimiter,
        folder_name
    ) in client.list_folders():

        flags_text = {
            (
                flag.decode(
                    errors="ignore"
                )
                if isinstance(flag, bytes)
                else str(flag)
            ).lower()
            for flag in flags
        }

        if "\\noselect" in flags_text:
            continue

        folder_text = (
            str(folder_name)
            .strip()
        )

        if (
            folder_text.lower()
            in SKIP_MAIL_FOLDERS
        ):
            continue

        result.append(folder_name)

    def priority(folder):
        value = str(folder).lower()

        if value == "inbox":
            return 0

        if value == "sent":
            return 1

        if value == "archive":
            return 2

        return 3

    result.sort(
        key=priority
    )

    return result


# =========================================================
# ПОИСК
# =========================================================

def build_fast_search_terms(
    chat_id,
    query
):
    exact_emails = (
        extract_email_addresses(
            query
        )
    )

    if exact_emails:
        return [
            exact_emails[0]
        ]

    known = get_verified_mail_contacts(
        chat_id,
        query
    )

    if known:
        email_address = known[0][
            "email"
        ]

        print(
            "Using VERIFIED cached contact:",
            email_address
        )

        return [
            email_address
        ]

    words = re.findall(
        r"[a-zа-яё0-9._+-]+",
        normalize_search_text(
            query
        ),
        flags=re.IGNORECASE
    )

    useful = [
        word
        for word in words
        if (
            word not in MAIL_STOP_WORDS
            and len(word) >= 3
        )
    ]

    if not useful:
        return []

    main_word = max(
        useful,
        key=len
    )

    stem = russian_stem(
        main_word
    )

    result = [stem]

    translit = transliterate_ru(
        stem
    )

    if (
        translit
        and translit != stem
    ):
        result.append(translit)

    return result[:2]


def search_headers_only(
    client,
    terms
):
    found = set()

    for term in terms:

        # Если уже знаем точный email,
        # SUBJECT проверять нет смысла.
        if "@" in term:
            fields = [
                "FROM",
                "TO"
            ]
        else:
            fields = [
                "FROM",
                "TO",
                "SUBJECT"
            ]

        for field in fields:

            try:
                uids = client.search(
                    [
                        field,
                        term
                    ],
                    charset="UTF-8"
                )

                found.update(uids)

            except Exception as e:
                print(
                    "IMAP search failed:",
                    field,
                    repr(term),
                    repr(e)
                )

    return list(found)


def search_text_fallback(
    client,
    terms
):
    found = set()

    for term in terms:

        try:
            uids = client.search(
                [
                    "TEXT",
                    term
                ],
                charset="UTF-8"
            )

            found.update(uids)

        except Exception as e:
            print(
                "IMAP TEXT failed:",
                repr(term),
                repr(e)
            )

    return list(found)


def fetch_messages(
    client,
    folder_name,
    uids,
    max_count=25
):
    if not uids:
        return []

    selected = sorted(
        uids,
        reverse=True
    )[:max_count]

    fetched = client.fetch(
        selected,
        ["RFC822"]
    )

    result = []

    for uid in selected:

        values = fetched.get(uid)

        if not values:
            continue

        raw_email = (
            values.get(b"RFC822")
            or values.get("RFC822")
        )

        if not raw_email:
            continue

        result.append(
            parse_full_email(
                raw_email,
                folder_name,
                uid
            )
        )

    return result


def relevance_score(
    item,
    terms
):
    subject = normalize_search_text(
        item["subject"]
    )

    sender = normalize_search_text(
        item["from"]
    )

    recipient = normalize_search_text(
        item["to"]
    )

    body = normalize_search_text(
        item["body"]
    )

    score = 0
    matched = []

    for term in terms:

        term_n = normalize_search_text(
            term
        )

        part_score = 0

        if term_n in sender:
            part_score += 10

        if term_n in recipient:
            part_score += 7

        if term_n in subject:
            part_score += 8

        if term_n in body:
            part_score += 2

        if part_score:
            score += part_score
            matched.append(term)

    item["matched_terms"] = sorted(
        set(matched)
    )

    return score


def search_mail_fast(
    chat_id,
    query,
    result_limit=15
):
    terms = build_fast_search_terms(
        chat_id,
        query
    )

    print(
        "Fast mail search terms:",
        terms
    )

    if not terms:
        return []

    client = get_mail_connection()

    all_results = []

    try:
        folders = (
            get_useful_mail_folders(
                client
            )
        )

        print(
            "Useful mail folders:",
            folders
        )

        # -----------------------------------------
        # ПРОХОД 1 — ЗАГОЛОВКИ
        # -----------------------------------------

        for folder_name in folders:

            started = time.time()

            try:
                client.select_folder(
                    folder_name,
                    readonly=True
                )

                uids = search_headers_only(
                    client,
                    terms
                )

                elapsed = (
                    time.time()
                    - started
                )

                print(
                    "Header search folder:",
                    repr(folder_name),
                    "uids:",
                    len(uids),
                    "time:",
                    round(elapsed, 2),
                    "sec"
                )

                if not uids:
                    continue

                messages = fetch_messages(
                    client,
                    folder_name,
                    uids
                )

                for item in messages:

                    item["score"] = (
                        relevance_score(
                            item,
                            terms
                        )
                    )

                    if item["score"] > 0:
                        all_results.append(
                            item
                        )

            except Exception as e:
                print(
                    "Folder search error:",
                    repr(folder_name),
                    repr(e)
                )

        # -----------------------------------------
        # ПРОХОД 2 — TEXT ТОЛЬКО ЕСЛИ 0
        # -----------------------------------------

        if not all_results:

            print(
                "No header matches. "
                "Starting TEXT fallback."
            )

            for folder_name in folders:

                started = time.time()

                try:
                    client.select_folder(
                        folder_name,
                        readonly=True
                    )

                    uids = (
                        search_text_fallback(
                            client,
                            terms
                        )
                    )

                    elapsed = (
                        time.time()
                        - started
                    )

                    print(
                        "TEXT search folder:",
                        repr(folder_name),
                        "uids:",
                        len(uids),
                        "time:",
                        round(elapsed, 2),
                        "sec"
                    )

                    if not uids:
                        continue

                    messages = (
                        fetch_messages(
                            client,
                            folder_name,
                            uids
                        )
                    )

                    for item in messages:

                        item["score"] = (
                            relevance_score(
                                item,
                                terms
                            )
                        )

                        if item["score"] > 0:
                            all_results.append(
                                item
                            )

                except Exception as e:
                    print(
                        "TEXT folder error:",
                        repr(folder_name),
                        repr(e)
                    )

        # -----------------------------------------
        # ДУБЛИ
        # -----------------------------------------

        unique = {}

        for item in all_results:

            key = (
                item["message_id"]
                or (
                    item["folder"],
                    item["uid"]
                )
            )

            current = unique.get(key)

            if (
                current is None
                or item["score"]
                > current["score"]
            ):
                unique[key] = item

        results = list(
            unique.values()
        )

        results.sort(
            key=lambda item: (
                item["score"],
                item["date"].timestamp()
                if item["date"]
                else 0
            ),
            reverse=True
        )

        results = results[
            :result_limit
        ]

        # Теперь один запрос =
        # максимум один новый контакт.
        if results:
            remember_best_contact_from_messages(
                chat_id,
                query,
                results
            )

        return results

    finally:
        try:
            client.logout()
        except Exception:
            pass


# =========================================================
# ПОСЛЕДНИЕ ПИСЬМА
# =========================================================

def get_recent_mail(limit=10):
    client = get_mail_connection()

    try:
        client.select_folder(
            "INBOX",
            readonly=True
        )

        uids = client.search(
            ["ALL"]
        )

        if not uids:
            return []

        selected = (
            uids[-limit:]
        )[::-1]

        fetched = client.fetch(
            selected,
            ["RFC822"]
        )

        result = []

        for uid in selected:

            values = fetched.get(uid)

            if not values:
                continue

            raw_email = (
                values.get(b"RFC822")
                or values.get("RFC822")
            )

            if raw_email:
                result.append(
                    parse_full_email(
                        raw_email,
                        "INBOX",
                        uid
                    )
                )

        return result

    finally:
        try:
            client.logout()
        except Exception:
            pass


# =========================================================
# ФОРМАТ ПОЧТЫ
# =========================================================

def format_mail_date(dt):
    if not dt:
        return "дата неизвестна"

    return dt.strftime(
        "%d.%m.%Y %H:%M"
    )


def format_mail_list(
    messages,
    title
):
    if not messages:
        return (
            f"{title}\n\n"
            "Ничего не нашла."
        )

    lines = [title]

    for index, item in enumerate(
        messages,
        start=1
    ):

        lines.extend([
            "",
            (
                f"{index}. "
                f"{format_mail_date(item['date'])}"
            ),
            (
                f"От: "
                f"{item['from']}"
            ),
            (
                f"Тема: "
                f"{item['subject']}"
            )
        ])

    return "\n".join(lines)


def mail_context(messages):
    lines = ["ПОЧТА:"]

    if not messages:
        return (
            "ПОЧТА:\n"
            "Подходящих писем не найдено."
        )

    ordered = sorted(
        messages,
        key=lambda item: (
            item["date"]
            or datetime.min.replace(
                tzinfo=CALENDAR_TZ
            )
        )
    )

    for index, item in enumerate(
        ordered,
        start=1
    ):

        lines.extend([
            "",
            f"Письмо {index}",
            (
                "Дата: "
                + format_mail_date(
                    item["date"]
                )
            ),
            "Папка: " + item["folder"],
            "От: " + item["from"],
            "Кому: " + item["to"],
            "Тема: " + item["subject"]
        ])

        if item["attachments"]:
            lines.append(
                "Вложения: "
                + ", ".join(
                    item["attachments"]
                )
            )
        else:
            lines.append(
                "Вложения: нет"
            )

        lines.append(
            "Текст письма:\n"
            + (
                item["body"]
                or "[текст пуст]"
            )
        )

    return "\n".join(lines)


# =========================================================
# OPENAI
# =========================================================

def build_saved_context(chat_id):
    memories = get_memories(chat_id)
    tasks = get_open_tasks(chat_id)

    sections = []

    if memories:
        sections.append(
            "ПОСТОЯННАЯ ПАМЯТЬ:\n"
            + "\n".join(
                f"- {row['text']}"
                for row in memories
            )
        )

    if tasks:
        sections.append(
            "ОТКРЫТЫЕ ЗАДАЧИ:\n"
            + "\n".join(
                (
                    f"- Задача #{row['id']}: "
                    f"{row['text']}"
                )
                for row in tasks
            )
        )

    return "\n\n".join(sections)


def extract_openai_text(data):
    result = []

    for item in data.get(
        "output",
        []
    ):

        if item.get("type") != "message":
            continue

        for content in item.get(
            "content",
            []
        ):

            if (
                content.get("type")
                == "output_text"
            ):

                text = content.get(
                    "text",
                    ""
                )

                if text:
                    result.append(text)

    if result:
        return "\n".join(result)

    return (
        "Я получила ответ, "
        "но не смогла разобрать его текст."
    )


def ask_openai(
    chat_id,
    user_text,
    extra_context=""
):
    sections = []

    saved = build_saved_context(
        chat_id
    )

    if saved:
        sections.append(saved)

    if extra_context:
        sections.append(extra_context)

    sections.append(
        "ТЕКУЩЕЕ СООБЩЕНИЕ МАШИ:\n"
        + user_text
    )

    payload = {
        "model": "gpt-5.6",
        "instructions": (
            ASSISTANT_INSTRUCTIONS
        ),
        "input": "\n\n".join(
            sections
        ),
        "store": True
    }

    previous_id = (
        get_previous_response_id(
            chat_id
        )
    )

    if previous_id:
        payload[
            "previous_response_id"
        ] = previous_id

    response = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": (
                f"Bearer "
                f"{OPENAI_API_KEY}"
            ),
            "Content-Type": (
                "application/json"
            )
        },
        json=payload,
        timeout=120
    )

    if (
        response.status_code == 400
        and previous_id
    ):

        clear_previous_response_id(
            chat_id
        )

        payload.pop(
            "previous_response_id",
            None
        )

        response = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": (
                    f"Bearer "
                    f"{OPENAI_API_KEY}"
                ),
                "Content-Type": (
                    "application/json"
                )
            },
            json=payload,
            timeout=120
        )

    response.raise_for_status()

    data = response.json()

    if data.get("id"):
        save_previous_response_id(
            chat_id,
            data["id"]
        )

    return extract_openai_text(
        data
    )


# =========================================================
# КАЛЕНДАРЬ + ИИ
# =========================================================

def review_calendar(
    chat_id,
    target_date,
    days,
    user_request
):
    context = calendar_context(
        target_date,
        days
    )

    prompt = """
Проверь календарь как внимательный бизнес-ассистент.

Проверь:
- пересечения;
- встречи без 15–30 минут воздуха;
- наличие обеда;
- повестки;
- адреса;
- Zoom-ссылки;
- подтверждения участников;
- связь с открытыми задачами.

Не выдумывай отсутствующие сведения.
Сначала важные риски.
Потом коротко скажи, что выглядит хорошо.
"""

    return ask_openai(
        chat_id,
        user_request,
        extra_context=(
            context
            + "\n\n"
            + prompt
        )
    )


# =========================================================
# ПОКАЗ ПАМЯТИ / ЗАДАЧ / КОНТАКТОВ
# =========================================================

def show_tasks(chat_id):
    tasks = get_open_tasks(chat_id)

    if not tasks:
        send_message(
            chat_id,
            "Сейчас открытых задач нет."
        )
        return

    send_message(
        chat_id,
        "Твои открытые задачи:\n\n"
        + "\n\n".join(
            (
                f"#{row['id']} — "
                f"{row['text']}"
            )
            for row in tasks
        )
    )


def show_memory(chat_id):
    memories = get_memories(chat_id)

    if not memories:
        send_message(
            chat_id,
            "В постоянной памяти "
            "пока ничего нет."
        )
        return

    send_message(
        chat_id,
        "Вот что я помню:\n\n"
        + "\n\n".join(
            (
                f"#{row['id']} — "
                f"{row['text']}"
            )
            for row in memories
        )
    )


def show_mail_contacts(chat_id):
    rows = list_verified_mail_contacts(
        chat_id
    )

    if not rows:
        send_message(
            chat_id,
            "Проверенных почтовых "
            "контактов пока нет."
        )
        return

    lines = [
        "Проверенные почтовые контакты:"
    ]

    for row in rows:

        lines.append(
            "\n"
            f"{row['alias']} → "
            f"{row['email']}"
        )

        if row["display_name"]:
            lines.append(
                str(row["display_name"])
            )

    send_message(
        chat_id,
        "\n".join(lines)
    )


# =========================================================
# КОМАНДЫ
# =========================================================

def strip_punctuation(text):
    return re.sub(
        r"[?!.,]+$",
        "",
        text.strip().lower()
    ).strip()


def try_handle_mail(
    chat_id,
    text
):
    normalized = re.sub(
        r"\s+",
        " ",
        text.strip()
    ).strip()

    clean = strip_punctuation(
        normalized
    )

    if clean == "/mail":

        try:
            client = get_mail_connection()

            folders = (
                get_useful_mail_folders(
                    client
                )
            )

            client.logout()

            send_message(
                chat_id,
                "Связь с Яндекс.Почтой есть ✅\n\n"
                f"Рабочих папок вижу: "
                f"{len(folders)}."
            )

        except Exception as e:
            print(
                "Mail connection error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не получилось подключиться "
                "к Яндекс.Почте."
            )

        return True

    if clean in {
        "/mailcontacts",
        "почтовые контакты",
        "покажи почтовые контакты"
    }:

        show_mail_contacts(
            chat_id
        )

        return True

    forget_match = re.match(
        r"^(?:"
        r"/mailforget|"
        r"забудь почтовый контакт"
        r")\s+(.+)$",
        normalized,
        flags=re.IGNORECASE
    )

    if forget_match:

        query = (
            forget_match
            .group(1)
            .strip()
        )

        deleted = forget_mail_contact(
            chat_id,
            query
        )

        if deleted:
            send_message(
                chat_id,
                f"Удалила почтовый контакт "
                f"«{query}» ✅"
            )
        else:
            send_message(
                chat_id,
                f"Контакт «{query}» "
                "не нашла."
            )

        return True

    if clean in {
        "/mailrecent",
        "последние письма",
        "покажи последние письма",
        "что нового в почте"
    }:

        try:
            send_message(
                chat_id,
                "Смотрю почту..."
            )

            messages = get_recent_mail(
                10
            )

            send_message(
                chat_id,
                format_mail_list(
                    messages,
                    "Последние письма:"
                )
            )

        except Exception as e:
            print(
                "Recent mail error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать почту."
            )

        return True

    search_patterns = [
        r"^найди\s+письмо\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+письма\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+переписку\s+(?:про|с|по)?\s*(.+)$",
        r"^найди\s+все\s+письма\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+всё\s+от\s+(.+)$",
        r"^поищи\s+письмо\s+(?:про|от|по)?\s*(.+)$",
        r"^поищи\s+в\s+почте\s+(.+)$",
        r"^поиск\s+почты\s*[,:-]?\s*(.+)$",
        r"^почта\s*[,:-]\s*(.+)$"
    ]

    for pattern in search_patterns:

        match = re.match(
            pattern,
            normalized,
            flags=re.IGNORECASE
        )

        if not match:
            continue

        query = (
            match.group(1)
            .strip()
        )

        try:
            started = time.time()

            send_message(
                chat_id,
                f"Ищу в почте: {query}..."
            )

            messages = search_mail_fast(
                chat_id,
                query
            )

            print(
                "Mail search finished in",
                round(
                    time.time()
                    - started,
                    2
                ),
                "seconds"
            )

            if not messages:
                send_message(
                    chat_id,
                    (
                        f"По запросу «{query}» "
                        "ничего не нашла."
                    )
                )
                return True

            context = mail_context(
                messages
            )

            analysis_prompt = (
                f"Я попросила найти в почте: {query}.\n\n"
                "Проанализируй только реальные найденные письма.\n"
                "Если это одна переписка, собери её по хронологии.\n"
                "Коротко скажи:\n"
                "1. что нашлось;\n"
                "2. какое письмо наиболее похоже на нужное;\n"
                "3. в чем суть;\n"
                "4. последнюю договоренность;\n"
                "5. что мне нужно сделать дальше;\n"
                "6. связано ли это с моими сохраненными задачами.\n"
                "Не придумывай отсутствующие сведения."
            )

            answer = ask_openai(
                chat_id,
                analysis_prompt,
                extra_context=context
            )

            send_message(
                chat_id,
                answer
            )

        except Exception as e:
            print(
                "Mail search error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла выполнить поиск "
                "по почте. Посмотри лог Railway."
            )

        return True

    return False


def try_handle_calendar(
    chat_id,
    text
):
    clean = strip_punctuation(
        text
    )

    today = datetime.now(
        CALENDAR_TZ
    ).date()

    if clean in {
        "/today",
        "что у меня сегодня",
        "что сегодня",
        "встречи сегодня",
        "календарь сегодня",
        "какие у меня сегодня встречи"
    }:

        try:
            send_message(
                chat_id,
                format_calendar_day(
                    today,
                    "Сегодня:"
                )
            )
        except Exception as e:
            print(
                "Calendar error:",
                repr(e)
            )

        return True

    if clean in {
        "/tomorrow",
        "что у меня завтра",
        "что завтра",
        "встречи завтра",
        "календарь завтра"
    }:

        tomorrow = (
            today
            + timedelta(days=1)
        )

        send_message(
            chat_id,
            format_calendar_day(
                tomorrow,
                "Завтра:"
            )
        )

        return True

    if clean in {
        "/week",
        "календарь на неделю",
        "встречи на неделю",
        "что у меня на этой неделе"
    }:

        send_message(
            chat_id,
            format_calendar_period(
                today,
                7
            )
        )

        return True

    if clean in {
        "/checktoday",
        "проверь сегодня",
        "проверь календарь на сегодня",
        "проверь мой календарь на сегодня"
    }:

        send_message(
            chat_id,
            "Проверяю календарь..."
        )

        send_message(
            chat_id,
            review_calendar(
                chat_id,
                today,
                1,
                text
            )
        )

        return True

    if clean in {
        "/checktomorrow",
        "проверь завтра",
        "проверь календарь на завтра"
    }:

        tomorrow = (
            today
            + timedelta(days=1)
        )

        send_message(
            chat_id,
            "Проверяю календарь..."
        )

        send_message(
            chat_id,
            review_calendar(
                chat_id,
                tomorrow,
                1,
                text
            )
        )

        return True

    if clean in {
        "/checkweek",
        "проверь неделю",
        "проверь мою неделю"
    }:

        send_message(
            chat_id,
            "Проверяю неделю..."
        )

        send_message(
            chat_id,
            review_calendar(
                chat_id,
                today,
                7,
                text
            )
        )

        return True

    return False


def try_handle_command(
    chat_id,
    text
):
    normalized = re.sub(
        r"\s+",
        " ",
        text.strip()
    ).strip()

    clean = strip_punctuation(
        normalized
    )

    if clean == "/start":

        send_message(
            chat_id,
            "Привет 👋 Я Маша 2.0.\n\n"
            "У меня есть ИИ, память, задачи, "
            "Яндекс.Календарь и Яндекс.Почта."
        )

        return True

    if clean == "/health":

        send_message(
            chat_id,
            "Я работаю ✅"
        )

        return True

    if clean == "/new":

        clear_previous_response_id(
            chat_id
        )

        send_message(
            chat_id,
            "Начинаем новый разговор. "
            "Память и задачи я не забыла."
        )

        return True

    if clean in {
        "/tasks",
        "задачи",
        "мои задачи",
        "покажи задачи",
        "покажи мои задачи"
    }:

        show_tasks(chat_id)
        return True

    done_match = re.match(
        r"^(?:"
        r"закрой задачу|"
        r"закрыть задачу|"
        r"/done"
        r")"
        r"\s*#?\s*(\d+)",
        normalized,
        flags=re.IGNORECASE
    )

    if done_match:

        task_id = int(
            done_match.group(1)
        )

        if complete_task(
            chat_id,
            task_id
        ):

            send_message(
                chat_id,
                f"Задачу #{task_id} закрыла ✅"
            )

        return True

    task_patterns = [
        r"^задача\s*[,:-]?\s+(.+)$",
        r"^запиши\s+задачу\s*[,:-]?\s+(.+)$",
        r"^добавь\s+задачу\s*[,:-]?\s+(.+)$",
        r"^добавь\s+в\s+задачи\s*[,:-]?\s+(.+)$",
        r"^создай\s+задачу\s*[,:-]?\s+(.+)$",
        r"^поставь\s+задачу\s*[,:-]?\s+(.+)$",
        r"^запомни\s+задачу\s*[,:-]?\s+(.+)$"
    ]

    for pattern in task_patterns:

        match = re.match(
            pattern,
            normalized,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            )
        )

        if match:

            task_text = (
                match.group(1)
                .strip()
            )

            task_id = add_task(
                chat_id,
                task_text
            )

            send_message(
                chat_id,
                "Записала задачу ✅\n\n"
                f"#{task_id}: {task_text}"
            )

            return True

    if clean in {
        "/memory",
        "что ты помнишь",
        "покажи память",
        "что у тебя в памяти",
        "что ты запомнила"
    }:

        show_memory(chat_id)
        return True

    memory_patterns = [
        r"^запомни\s*,?\s*что\s+(.+)$",
        r"^запомни\s*[,:-]?\s+(.+)$",
        r"^сохрани\s+в\s+память\s*[,:-]?\s+(.+)$"
    ]

    for pattern in memory_patterns:

        match = re.match(
            pattern,
            normalized,
            flags=(
                re.IGNORECASE
                | re.DOTALL
            )
        )

        if match:

            memory_text = (
                match.group(1)
                .strip()
            )

            memory_id = add_memory(
                chat_id,
                memory_text
            )

            send_message(
                chat_id,
                "Запомнила 🧠\n\n"
                f"#{memory_id}: "
                f"{memory_text}"
            )

            return True

    return False


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    print(
        "Masha 2.0 запущена: "
        "safe mail contacts v5."
    )

    offset = None

    while True:

        params = {
            "timeout": 30
        }

        if offset is not None:
            params["offset"] = offset

        try:

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params=params,
                timeout=40
            )

            response.raise_for_status()

            data = response.json()

            for update in data.get(
                "result",
                []
            ):

                offset = (
                    update["update_id"]
                    + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat_id = (
                    message["chat"]["id"]
                )

                text = message.get(
                    "text",
                    ""
                )

                if not text:
                    continue

                print(
                    "Incoming:",
                    chat_id,
                    repr(text)
                )

                if try_handle_mail(
                    chat_id,
                    text
                ):
                    continue

                if try_handle_calendar(
                    chat_id,
                    text
                ):
                    continue

                if try_handle_command(
                    chat_id,
                    text
                ):
                    continue

                send_message(
                    chat_id,
                    "Думаю..."
                )

                try:

                    answer = ask_openai(
                        chat_id,
                        text
                    )

                    send_message(
                        chat_id,
                        answer
                    )

                except requests.HTTPError as e:

                    print(
                        "OpenAI HTTP error:",
                        (
                            e.response.text
                            if e.response
                            else repr(e)
                        )
                    )

                    send_message(
                        chat_id,
                        "Я дошла до OpenAI, "
                        "но получила ошибку API."
                    )

                except Exception as e:

                    print(
                        "OpenAI error:",
                        repr(e)
                    )

                    send_message(
                        chat_id,
                        "У меня возникла техническая "
                        "ошибка при обращении к ИИ."
                    )

        except requests.RequestException as e:

            print(
                "Telegram network error:",
                repr(e)
            )

            time.sleep(3)

        except Exception as e:

            print(
                "Telegram error:",
                repr(e)
            )

            time.sleep(3)


if __name__ == "__main__":
    main()
