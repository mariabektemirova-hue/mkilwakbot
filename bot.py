import os
import re
import time
import sqlite3
import imaplib
import email
import html
import requests
import caldav

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from icalendar import Calendar


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
# ИНСТРУКЦИЯ МАШИ 2.0
# =========================================================

ASSISTANT_INSTRUCTIONS = """
Ты Маша 2.0 — личный рабочий ассистент Маши.

Твоя главная цель:
разгружать Маше голову и помогать держать под контролем встречи,
задачи, дедлайны, материалы, договоренности и организационный хаос.

КОНТЕКСТ РАБОТЫ

- Маша работает бизнес-ассистентом руководителя МС.
- МС может внезапно попросить поставить 30 минут с кем-то из директоров,
  поэтому в календаре важно сохранять привычные слоты и свободные окна
  под срочные встречи.
- ТМС не двигаем.
- Advisory Board (AB) не двигаем.
- Тренировки Маши не двигаем.
- Психоаналитика Маши не двигаем.
- Для 1-2-1 повестку нужно начинать собирать минимум за неделю.
- Если к встрече нет повестки или необходимых материалов,
  нужно обратить на это внимание.
- По рабочему правилу встречу без необходимых материалов можно отменять.
- Материалы к важным встречам желательно получать минимум за 2 суток,
  лучше заранее.

ПРЕДПОЧТЕНИЯ ПО КАЛЕНДАРЮ

- Понедельник желательно оставлять под фокусную работу.
- Пятница предпочтительна для внешних встреч.
- Желательный рабочий диапазон примерно 10:30–19:00.
- Между встречами желательно оставлять 15–30 минут воздуха.
- Обед желательно не совмещать с внутренними встречами.
- Нужно сохранять окна для срочных встреч и собеседований.

ЧЕК-ЛИСТ ВСТРЕЧ

- Для Zoom должна быть ссылка.
- Все участники должны подтвердить присутствие.
- В описании события должна быть повестка.
- Для внешней встречи обязательно должен быть адрес.
- В календаре должно быть время на обед.
- Для собеседования резюме должно быть вложено в событие.
- Перед собеседованием резюме нужно распечатать.
- Для PRM нужно заранее собрать материалы руководителей
  в онлайн-папку и проверить доступ.

ПОЧТА

- У тебя есть read-only доступ к Яндекс.Почте через данные,
  которые программа передает тебе.
- Ты можешь анализировать найденные письма.
- Не говори, что отправила письмо.
- Не говори, что удалила, архивировала или переместила письмо.
- Не придумывай содержимое писем.
- Если поиск ничего не нашел, так и скажи.
- Если из письма виден следующий шаг, помоги Маше его сформулировать.
- Если письмо связано с уже сохраненной задачей, укажи на связь.

КАК РАБОТАТЬ С МАШЕЙ

- Отвечай по-русски.
- Говори естественно, тепло и без канцелярита.
- На простой вопрос отвечай коротко.
- Если сообщение хаотичное, сама структурируй.
- Учитывай предыдущий диалог.
- Учитывай ПОСТОЯННУЮ ПАМЯТЬ.
- Учитывай ОТКРЫТЫЕ ЗАДАЧИ.
- Учитывай реальные данные КАЛЕНДАРЯ и ПОЧТЫ, если они переданы.
- Не заставляй Машу повторять уже известное.
- Не утверждай, что реально выполнила действие, если оно не выполнено.
- Календарь пока только читаешь.
- Почту пока только читаешь.

ВАЖНО О ДОСТОВЕРНОСТИ

Если в данных календаря нет описания, говори:
"в данных календаря не вижу повестки",
а не "повестки нет".

Если нет location:
"в данных события не вижу адреса".

Если в разделе ПОЧТА есть письмо,
опирайся только на реально переданный текст.

Не выдумывай адресатов, решения, вложения и договоренности.

ВАЖНО О ФОРМАТЕ

Telegram получает обычный текст.
Не используй Markdown-разметку.
Не используй **, *, ### для оформления.
Пиши компактно.
"""


# =========================================================
# БАЗА ДАННЫХ
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
            datetime.now().isoformat(timespec="seconds")
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
            datetime.now().isoformat(timespec="seconds"),
            chat_id,
            task_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# =========================================================
# ПОСТОЯННАЯ ПАМЯТЬ
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
            datetime.now().isoformat(timespec="seconds")
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
# ПАМЯТЬ ДИАЛОГА OPENAI
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


def save_previous_response_id(chat_id, response_id):
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
            previous_response_id = excluded.previous_response_id
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

    max_length = 4000

    parts = [
        text[i:i + max_length]
        for i in range(0, len(text), max_length)
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
# ЯНДЕКС.КАЛЕНДАРЬ
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
    attendees = []

    raw_attendees = component.get(
        "ATTENDEE"
    )

    if not raw_attendees:
        return attendees

    if not isinstance(
        raw_attendees,
        list
    ):
        raw_attendees = [
            raw_attendees
        ]

    for attendee in raw_attendees:
        name = ""
        participation = ""

        try:
            name = attendee.params.get(
                "CN",
                ""
            )
        except Exception:
            pass

        try:
            participation = attendee.params.get(
                "PARTSTAT",
                ""
            )
        except Exception:
            pass

        attendees.append({
            "name": str(name),
            "value": str(attendee),
            "partstat": str(participation)
        })

    return attendees


def extract_event_from_ical(
    ical_data,
    calendar_name=""
):
    if not ical_data:
        return []

    if isinstance(
        ical_data,
        str
    ):
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

        start, all_day = normalize_calendar_datetime(
            dtstart_property.dt
        )

        if start is None:
            continue

        end = None

        dtend_property = component.get(
            "DTEND"
        )

        if dtend_property:
            end, _ = normalize_calendar_datetime(
                dtend_property.dt
            )

        result.append({
            "uid": str(
                component.get(
                    "UID",
                    ""
                )
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

    # Убираем дубли между календарями Маши и МС.
    unique = {}

    for event in collected:
        start_key = (
            event["start"].isoformat()
            if event["start"]
            else ""
        )

        end_key = (
            event["end"].isoformat()
            if event["end"]
            else ""
        )

        key = (
            event["summary"]
            .strip()
            .lower(),
            start_key,
            end_key
        )

        if key not in unique:
            unique[key] = event
            continue

        existing = unique[key]

        if (
            not existing["location"]
            and event["location"]
        ):
            existing["location"] = (
                event["location"]
            )

        if (
            not existing["description"]
            and event["description"]
        ):
            existing["description"] = (
                event["description"]
            )

        if (
            not existing["url"]
            and event["url"]
        ):
            existing["url"] = (
                event["url"]
            )

        if (
            not existing["attendees"]
            and event["attendees"]
        ):
            existing["attendees"] = (
                event["attendees"]
            )

    result = list(
        unique.values()
    )

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

    end_dt = (
        start_dt
        + timedelta(days=1)
    )

    events = get_calendar_events(
        start_dt,
        end_dt
    )

    if not events:
        return (
            f"{title}\n\n"
            "В календаре событий нет."
        )

    lines = [title]

    for event in events:
        lines.append(
            format_event(event)
        )

    return "\n\n".join(lines)


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

    end_dt = (
        start_dt
        + timedelta(days=days)
    )

    events = get_calendar_events(
        start_dt,
        end_dt
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
            .astimezone(
                CALENDAR_TZ
            )
            .date()
        )

        grouped.setdefault(
            event_date,
            []
        ).append(event)

    weekday_names = {
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
            f"{weekday_names[event_date.weekday()]}, "
            f"{event_date.strftime('%d.%m')}"
        )

        for event in grouped[
            event_date
        ]:
            lines.append(
                format_event(event)
            )

        lines.append("")

    return "\n".join(
        lines
    ).strip()


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

    end_dt = (
        start_dt
        + timedelta(days=days)
    )

    events = get_calendar_events(
        start_dt,
        end_dt
    )

    lines = [
        "КАЛЕНДАРЬ:"
    ]

    if not events:
        lines.append(
            "Событий нет."
        )
        return "\n".join(lines)

    for index, event in enumerate(
        events,
        start=1
    ):
        start = event["start"]
        end = event["end"]

        if event["all_day"]:
            when = (
                start.strftime(
                    "%d.%m.%Y"
                )
                + " весь день"
            )

        elif end:
            when = (
                start.strftime(
                    "%d.%m.%Y %H:%M"
                )
                + "–"
                + end.strftime(
                    "%H:%M"
                )
            )

        else:
            when = start.strftime(
                "%d.%m.%Y %H:%M"
            )

        lines.append("")
        lines.append(
            f"Событие {index}"
        )

        lines.append(
            f"Название: "
            f"{event['summary']}"
        )

        lines.append(
            f"Время: {when}"
        )

        lines.append(
            "Место/адрес: "
            + (
                event["location"]
                or "не указано"
            )
        )

        description = (
            event["description"]
            .strip()
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
            attendee_lines = []

            for attendee in attendees:
                name = (
                    attendee["name"]
                    or attendee["value"]
                )

                attendee_lines.append(
                    f"{name} "
                    f"(статус: "
                    f"{attendee['partstat'] or 'не указан'})"
                )

            lines.append(
                "Участники: "
                + "; ".join(
                    attendee_lines
                )
            )

    return "\n".join(lines)


# =========================================================
# ЯНДЕКС.ПОЧТА — READ ONLY
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
    mail = imaplib.IMAP4_SSL(
        YANDEX_IMAP_HOST,
        YANDEX_IMAP_PORT
    )

    mail.login(
        YANDEX_MAIL_LOGIN,
        YANDEX_MAIL_PASSWORD
    )

    return mail


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

    value = html.unescape(
        value
    )

    value = re.sub(
        r"\s+",
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

        charset = part.get_content_charset()

        if not charset:
            charset = "utf-8"

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

            # Вложения в текст письма не подмешиваем.
            if "attachment" in disposition:
                continue

            if content_type == "text/plain":
                text = decode_part_payload(
                    part
                )

                if text:
                    plain_parts.append(
                        text
                    )

            elif content_type == "text/html":
                text = decode_part_payload(
                    part
                )

                if text:
                    html_parts.append(
                        clean_html_text(text)
                    )

    else:
        content_type = (
            message.get_content_type()
        )

        text = decode_part_payload(
            message
        )

        if content_type == "text/html":
            html_parts.append(
                clean_html_text(text)
            )
        else:
            plain_parts.append(
                text
            )

    if plain_parts:
        body = "\n".join(
            plain_parts
        )

    elif html_parts:
        body = "\n".join(
            html_parts
        )

    else:
        body = ""

    # Для OpenAI не нужно пересылать бесконечные цепочки.
    body = body.strip()

    if len(body) > 8000:
        body = (
            body[:8000]
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

        if dt is None:
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


def fetch_mail_message(
    mail,
    message_id
):
    status, data = mail.fetch(
        message_id,
        "(RFC822)"
    )

    if status != "OK":
        return None

    raw_email = None

    for item in data:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
        ):
            raw_email = item[1]
            break

    if not raw_email:
        return None

    message = email.message_from_bytes(
        raw_email
    )

    return {
        "id": (
            message_id.decode()
            if isinstance(
                message_id,
                bytes
            )
            else str(message_id)
        ),
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


def get_recent_mail(
    limit=10
):
    mail = get_mail_connection()

    try:
        status, _ = mail.select(
            "INBOX",
            readonly=True
        )

        if status != "OK":
            raise RuntimeError(
                "Не удалось открыть INBOX"
            )

        status, data = mail.search(
            None,
            "ALL"
        )

        if status != "OK":
            return []

        ids = data[0].split()

        ids = ids[
            -limit:
        ]

        ids.reverse()

        messages = []

        for message_id in ids:
            item = fetch_mail_message(
                mail,
                message_id
            )

            if item:
                messages.append(
                    item
                )

        return messages

    finally:
        try:
            mail.logout()
        except Exception:
            pass


def search_recent_mail(
    query,
    scan_limit=100,
    result_limit=10
):
    """
    Для надежной работы с русским текстом
    не полагаемся на серверный IMAP SEARCH UTF-8.

    Берем последние письма и ищем локально
    в теме, отправителе, получателе и тексте.
    """

    query = query.strip().lower()

    if not query:
        return []

    mail = get_mail_connection()

    try:
        status, _ = mail.select(
            "INBOX",
            readonly=True
        )

        if status != "OK":
            raise RuntimeError(
                "Не удалось открыть INBOX"
            )

        status, data = mail.search(
            None,
            "ALL"
        )

        if status != "OK":
            return []

        ids = data[0].split()

        ids = ids[
            -scan_limit:
        ]

        ids.reverse()

        results = []

        for message_id in ids:
            item = fetch_mail_message(
                mail,
                message_id
            )

            if not item:
                continue

            haystack = (
                item["subject"]
                + "\n"
                + item["from"]
                + "\n"
                + item["to"]
                + "\n"
                + item["body"]
            ).lower()

            if query in haystack:
                results.append(
                    item
                )

            if (
                len(results)
                >= result_limit
            ):
                break

        return results

    finally:
        try:
            mail.logout()
        except Exception:
            pass


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

    lines = [
        title
    ]

    for index, item in enumerate(
        messages,
        start=1
    ):
        lines.append(
            "\n"
            f"{index}. "
            f"{format_mail_date(item['date'])}\n"
            f"От: {item['from']}\n"
            f"Тема: {item['subject']}"
        )

        if item["attachments"]:
            lines.append(
                "Вложения: "
                + ", ".join(
                    item["attachments"]
                )
            )

    return "\n".join(
        lines
    )


def mail_context(
    messages
):
    lines = [
        "ПОЧТА:"
    ]

    if not messages:
        lines.append(
            "Подходящих писем не найдено."
        )

        return "\n".join(
            lines
        )

    for index, item in enumerate(
        messages,
        start=1
    ):
        lines.append("")
        lines.append(
            f"Письмо {index}"
        )

        lines.append(
            "Дата: "
            + format_mail_date(
                item["date"]
            )
        )

        lines.append(
            "От: "
            + item["from"]
        )

        lines.append(
            "Кому: "
            + item["to"]
        )

        lines.append(
            "Тема: "
            + item["subject"]
        )

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
            "Текст:\n"
            + (
                item["body"]
                or "[текст письма пуст]"
            )
        )

    return "\n".join(
        lines
    )


# =========================================================
# ПАМЯТЬ И ЗАДАЧИ ДЛЯ OPENAI
# =========================================================

def build_saved_context(
    chat_id
):
    memories = get_memories(
        chat_id
    )

    tasks = get_open_tasks(
        chat_id
    )

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

    return "\n\n".join(
        sections
    )


# =========================================================
# OPENAI
# =========================================================

def extract_openai_text(
    data
):
    texts = []

    for item in data.get(
        "output",
        []
    ):
        if (
            item.get("type")
            != "message"
        ):
            continue

        for content in item.get(
            "content",
            []
        ):
            if (
                content.get("type")
                == "output_text"
            ):
                value = content.get(
                    "text",
                    ""
                )

                if value:
                    texts.append(
                        value
                    )

    if texts:
        return "\n".join(
            texts
        )

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

    saved_context = build_saved_context(
        chat_id
    )

    if saved_context:
        sections.append(
            saved_context
        )

    if extra_context:
        sections.append(
            extra_context
        )

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
        timeout=90
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
            timeout=90
        )

    response.raise_for_status()

    data = response.json()

    response_id = data.get(
        "id"
    )

    if response_id:
        save_previous_response_id(
            chat_id,
            response_id
        )

    return extract_openai_text(
        data
    )


# =========================================================
# УМНАЯ ПРОВЕРКА КАЛЕНДАРЯ
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

    instruction = """
Проверь календарь как внимательный бизнес-ассистент.

Проверь:
- пересечения;
- встречи без 15–30 минут воздуха;
- наличие окна на обед;
- повестки/описания;
- адреса;
- Zoom-ссылки, если встреча явно онлайн;
- подтверждения участников;
- связь с открытыми задачами.

Не выдумывай отсутствующие сведения.
Сначала важные риски.
Отдельно коротко скажи, что выглядит хорошо.
"""

    return ask_openai(
        chat_id,
        user_request,
        extra_context=(
            context
            + "\n\n"
            + instruction
        )
    )


# =========================================================
# ПРОСТЫЕ ПОКАЗЫ
# =========================================================

def show_tasks(chat_id):
    tasks = get_open_tasks(
        chat_id
    )

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
    memories = get_memories(
        chat_id
    )

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


# =========================================================
# НОРМАЛИЗАЦИЯ
# =========================================================

def strip_punctuation(text):
    return re.sub(
        r"[?!.,]+$",
        "",
        text.strip().lower()
    ).strip()


# =========================================================
# КОМАНДЫ ПОЧТЫ
# =========================================================

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

    # Проверка соединения
    if clean == "/mail":
        try:
            mail = get_mail_connection()

            status, _ = mail.select(
                "INBOX",
                readonly=True
            )

            mail.logout()

            if status == "OK":
                send_message(
                    chat_id,
                    "Связь с Яндекс.Почтой есть ✅"
                )
            else:
                send_message(
                    chat_id,
                    "К почте подключилась, "
                    "но INBOX открыть не удалось."
                )

        except Exception as e:
            print(
                "Mail connection error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не получилось подключиться "
                "к Яндекс.Почте. "
                "Посмотри лог в Railway."
            )

        return True

    # Последние письма
    recent_phrases = {
        "/mailrecent",
        "последние письма",
        "покажи последние письма",
        "что нового в почте",
        "покажи последние 10 писем"
    }

    if clean in recent_phrases:
        try:
            send_message(
                chat_id,
                "Смотрю почту..."
            )

            messages = get_recent_mail(
                limit=10
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
                "Mail read error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать почту. "
                "Посмотри лог в Railway."
            )

        return True

    # "Найди письмо Эльдар"
    search_patterns = [
        r"^найди\s+письмо\s+(?:про|от|по)?\s*(.+)$",
        r"^найди\s+письма\s+(?:про|от|по)?\s*(.+)$",
        r"^поищи\s+письмо\s+(?:про|от|по)?\s*(.+)$",
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

        query = match.group(1).strip()

        if not query:
            return False

        try:
            send_message(
                chat_id,
                f"Ищу в почте: {query}..."
            )

            messages = search_recent_mail(
                query=query,
                scan_limit=100,
                result_limit=10
            )

            if not messages:
                send_message(
                    chat_id,
                    f"По запросу «{query}» "
                    "в последних 100 письмах ничего не нашла."
                )

                return True

            # Не просто список — даем найденные письма ИИ.
            context = mail_context(
                messages
            )

            answer = ask_openai(
                chat_id,
                (
                    f"Я попросила найти в почте "
                    f"письма по запросу: {query}. "
                    "Коротко скажи, что нашлось, "
                    "что в письмах важно и есть ли "
                    "следующий шаг для меня."
                ),
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
                "по почте. Посмотри лог в Railway."
            )

        return True

    return False


# =========================================================
# КОМАНДЫ КАЛЕНДАРЯ
# =========================================================

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
        "встречи сегодня",
        "календарь сегодня"
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

            send_message(
                chat_id,
                "Не смогла прочитать календарь."
            )

        return True

    if clean in {
        "/tomorrow",
        "что у меня завтра",
        "встречи завтра",
        "календарь завтра"
    }:
        try:
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

        except Exception as e:
            print(
                "Calendar error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать календарь."
            )

        return True

    if clean in {
        "/week",
        "календарь на неделю",
        "встречи на неделю",
        "что у меня на этой неделе"
    }:
        try:
            send_message(
                chat_id,
                format_calendar_period(
                    today,
                    7
                )
            )

        except Exception as e:
            print(
                "Calendar error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать календарь."
            )

        return True

    if clean in {
        "/checktoday",
        "проверь сегодня",
        "проверь календарь на сегодня"
    }:
        try:
            send_message(
                chat_id,
                "Проверяю календарь..."
            )

            answer = review_calendar(
                chat_id,
                today,
                1,
                text
            )

            send_message(
                chat_id,
                answer
            )

        except Exception as e:
            print(
                "Calendar review error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла проверить календарь."
            )

        return True

    if clean in {
        "/checktomorrow",
        "проверь завтра",
        "проверь календарь на завтра",
        "проверь мой календарь на завтра"
    }:
        try:
            tomorrow = (
                today
                + timedelta(days=1)
            )

            send_message(
                chat_id,
                "Проверяю календарь..."
            )

            answer = review_calendar(
                chat_id,
                tomorrow,
                1,
                text
            )

            send_message(
                chat_id,
                answer
            )

        except Exception as e:
            print(
                "Calendar review error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла проверить календарь."
            )

        return True

    return False


# =========================================================
# ЗАДАЧИ / ПАМЯТЬ / СЛУЖЕБНЫЕ КОМАНДЫ
# =========================================================

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
            "Яндекс.Календарь и read-only Яндекс.Почта."
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
        "покажи мои задачи",
        "что у меня в задачах"
    }:
        show_tasks(
            chat_id
        )
        return True

    done_match = re.match(
        r"^(?:"
        r"закрой задачу|"
        r"закрыть задачу|"
        r"задача выполнена|"
        r"готово по задаче|"
        r"/done"
        r")"
        r"\s*#?\s*(\d+)"
        r"[.!]?$",
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
        else:
            send_message(
                chat_id,
                f"Не нашла открытую "
                f"задачу #{task_id}."
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
        show_memory(
            chat_id
        )
        return True

    forget_match = re.match(
        r"^(?:"
        r"забудь|"
        r"удали из памяти|"
        r"удали запись"
        r")"
        r"\s*#?\s*(\d+)"
        r"[.!]?$",
        normalized,
        flags=re.IGNORECASE
    )

    if forget_match:
        memory_id = int(
            forget_match.group(1)
        )

        if delete_memory(
            chat_id,
            memory_id
        ):
            send_message(
                chat_id,
                f"Удалила запись "
                f"#{memory_id} из памяти."
            )
        else:
            send_message(
                chat_id,
                f"Не нашла запись #{memory_id}."
            )

        return True

    memory_patterns = [
        r"^запомни\s*,?\s*что\s+(.+)$",
        r"^запомни\s*[,:-]?\s+(.+)$",
        r"^сохрани\s+в\s+память\s*[,:-]?\s+(.+)$",
        r"^добавь\s+в\s+память\s*[,:-]?\s+(.+)$"
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
                f"#{memory_id}: {memory_text}"
            )

            return True

    return False


# =========================================================
# ОСНОВНОЙ ЦИКЛ
# =========================================================

def main():
    init_db()

    print(
        "Masha 2.0 запущена: "
        "SQLite + OpenAI + Calendar + Mail read-only"
    )

    offset = None

    while True:
        params = {
            "timeout": 30
        }

        if offset is not None:
            params[
                "offset"
            ] = offset

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

                # Сначала почта
                if try_handle_mail(
                    chat_id,
                    text
                ):
                    continue

                # Потом календарь
                if try_handle_calendar(
                    chat_id,
                    text
                ):
                    continue

                # Потом задачи и память
                if try_handle_command(
                    chat_id,
                    text
                ):
                    continue

                # Обычный разговор
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
                    status = None
                    error_text = ""

                    if e.response is not None:
                        status = (
                            e.response.status_code
                        )

                        error_text = (
                            e.response.text
                        )

                    print(
                        "OpenAI HTTP error:",
                        status,
                        error_text
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
