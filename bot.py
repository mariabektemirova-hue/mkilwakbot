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
from email.utils import parsedate_to_datetime
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
# ИНСТРУКЦИЯ МАШИ 2.0
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
- находить вопросы, которые остались без ответа;
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
- учитывай их хронологию;
- не пересказывай каждое письмо отдельно без необходимости;
- собери суть цепочки;
- отдельно скажи, какое письмо наиболее вероятно искала Маша.

Если найдена переписка с человеком, а запрос Маши содержит
русский вариант имени, допускай, что, например,
"Эльдар" и "Eldar" — это один человек,
если адрес/контекст это подтверждают.

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
"в данных календаря не вижу повестки",
а не "повестки нет".

Если в событии нет location:
"в данных события не вижу адреса".

Если поиск почты ничего не нашел:
так и скажи.

Если письмо найдено только по косвенному совпадению:
объясни, почему считаешь его релевантным.

Не придумывай адресатов, вложения, договоренности,
решения или содержание писем.

ФОРМАТ

Telegram получает обычный текст.

Не используй Markdown:
не используй **, *, ###.

Пиши компактно и практично.
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
        for i in range(
            0,
            len(text),
            max_length
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
            "partstat": str(
                participation
            )
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
            "attendees": (
                extract_attendees(
                    component
                )
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

        for field in [
            "location",
            "description",
            "url"
        ]:
            if (
                not existing[field]
                and event[field]
            ):
                existing[field] = (
                    event[field]
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
        time_text = (
            start.strftime("%H:%M")
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

    events = get_calendar_events(
        start_dt,
        start_dt + timedelta(days=days)
    )

    lines = [
        "КАЛЕНДАРЬ:"
    ]

    if not events:
        lines.append(
            "Событий нет."
        )

        return "\n".join(
            lines
        )

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
            when = (
                start.strftime(
                    "%d.%m.%Y %H:%M"
                )
            )

        lines.extend([
            "",
            f"Событие {index}",
            (
                "Название: "
                + event["summary"]
            ),
            (
                "Время: "
                + when
            ),
            (
                "Место/адрес: "
                + (
                    event["location"]
                    or "не указано"
                )
            )
        ])

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

    return "\n".join(
        lines
    )


# =========================================================
# ЯНДЕКС.ПОЧТА — READ ONLY
# =========================================================

def decode_mime_header(value):
    if not value:
        return ""

    try:
        return str(
            make_header(
                decode_header(
                    value
                )
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

    value = html.unescape(
        value
    )

    value = re.sub(
        r"[ \t]+",
        " ",
        value
    )

    value = re.sub(
        r"\n\s*\n\s*\n+",
        "\n\n",
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
                    plain_parts.append(
                        value
                    )

            elif content_type == "text/html":
                value = decode_part_payload(
                    part
                )

                if value:
                    html_parts.append(
                        clean_html_text(
                            value
                        )
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
                clean_html_text(
                    value
                )
            )

        else:
            plain_parts.append(
                value
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


# =========================================================
# НОРМАЛИЗАЦИЯ ПОИСКА ПОЧТЫ
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


def transliterate_ru(value):
    result = []

    for char in value.lower():
        result.append(
            RUS_TO_LAT.get(
                char,
                char
            )
        )

    return "".join(
        result
    )


def normalize_search_text(value):
    value = value.lower()

    value = value.replace(
        "ё",
        "е"
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def russian_word_stems(word):
    """
    Очень простая эвристика, чтобы:
    Эльдара -> Эльдар
    Эльдару -> Эльдар
    Эльдаром -> Эльдар
    """

    variants = {
        word
    }

    endings = [
        "ами",
        "ями",
        "ого",
        "ему",
        "ому",
        "ами",
        "ями",
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

    if len(word) >= 5:
        for ending in endings:
            if (
                word.endswith(
                    ending
                )
                and len(word) - len(
                    ending
                ) >= 4
            ):
                variants.add(
                    word[
                        :-len(ending)
                    ]
                )

    return variants


def build_search_terms(query):
    query = normalize_search_text(
        query
    )

    raw_words = re.findall(
        r"[a-zа-яё0-9@._+-]+",
        query,
        flags=re.IGNORECASE
    )

    terms = set()

    for word in raw_words:
        word = normalize_search_text(
            word
        )

        if (
            not word
            or word in MAIL_STOP_WORDS
            or len(word) < 3
        ):
            continue

        variants = {
            word
        }

        if re.search(
            r"[а-яё]",
            word,
            flags=re.IGNORECASE
        ):
            variants.update(
                russian_word_stems(
                    word
                )
            )

        expanded = set()

        for variant in variants:
            expanded.add(
                variant
            )

            translit = (
                transliterate_ru(
                    variant
                )
            )

            if translit != variant:
                expanded.add(
                    translit
                )

        terms.update(
            expanded
        )

    # Частые полезные соответствия
    manual_aliases = {
        "эльдар": [
            "eldar"
        ],
        "эльдара": [
            "eldar"
        ],
        "мшр": [
            "мшр"
        ],
        "пиар": [
            "pr"
        ]
    }

    for word in raw_words:
        low = normalize_search_text(
            word
        )

        for alias in manual_aliases.get(
            low,
            []
        ):
            terms.add(
                alias
            )

    return sorted(
        terms,
        key=len,
        reverse=True
    )


def calculate_mail_match_score(
    subject,
    sender,
    recipient,
    terms
):
    subject_n = normalize_search_text(
        subject
    )

    sender_n = normalize_search_text(
        sender
    )

    recipient_n = normalize_search_text(
        recipient
    )

    score = 0
    matched = []

    for term in terms:
        term_n = normalize_search_text(
            term
        )

        found = False

        if term_n in sender_n:
            score += 8
            found = True

        if term_n in subject_n:
            score += 6
            found = True

        if term_n in recipient_n:
            score += 3
            found = True

        if found:
            matched.append(
                term
            )

    return score, matched


# =========================================================
# РАЗБОР ПИСЕМ
# =========================================================

def parse_header_bytes(raw_header):
    message = email.message_from_bytes(
        raw_header
    )

    return {
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
        "message_id": str(
            message.get(
                "Message-ID",
                ""
            )
        ).strip(),
        "in_reply_to": str(
            message.get(
                "In-Reply-To",
                ""
            )
        ).strip()
    }


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
        "in_reply_to": str(
            message.get(
                "In-Reply-To",
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


def get_mail_folders(client):
    folders = []

    for flags, delimiter, folder_name in (
        client.list_folders()
    ):
        flags_set = {
            str(flag).lower()
            for flag in flags
        }

        # Не трогаем папки, которые нельзя выбрать.
        if "\\noselect" in flags_set:
            continue

        folders.append(
            folder_name
        )

    return folders


def fetch_headers_in_batches(
    client,
    uids,
    batch_size=250
):
    results = {}

    header_query = (
        "BODY.PEEK[HEADER.FIELDS "
        "(SUBJECT FROM TO DATE MESSAGE-ID IN-REPLY-TO)]"
    )

    for i in range(
        0,
        len(uids),
        batch_size
    ):
        batch = uids[
            i:i + batch_size
        ]

        if not batch:
            continue

        fetched = client.fetch(
            batch,
            [
                header_query
            ]
        )

        for uid, values in fetched.items():
            raw_header = None

            for key, value in values.items():
                key_text = (
                    key.decode(
                        errors="ignore"
                    )
                    if isinstance(
                        key,
                        bytes
                    )
                    else str(key)
                )

                if (
                    "HEADER.FIELDS"
                    in key_text.upper()
                ):
                    raw_header = value
                    break

            if raw_header:
                results[
                    uid
                ] = parse_header_bytes(
                    raw_header
                )

    return results


def search_mail_all_folders(
    query,
    result_limit=20
):
    """
    Ищем во ВСЕХ папках.

    Этап 1:
    быстро скачиваем только заголовки.

    Этап 2:
    для лучших совпадений скачиваем полные письма.

    Таким образом нам не нужно каждый раз
    скачивать весь текст всей почты.
    """

    terms = build_search_terms(
        query
    )

    print(
        "Mail search terms:",
        terms
    )

    if not terms:
        return []

    client = get_mail_connection()

    candidates = []

    try:
        folders = get_mail_folders(
            client
        )

        print(
            "Mail folders:",
            folders
        )

        for folder_name in folders:
            try:
                client.select_folder(
                    folder_name,
                    readonly=True
                )

                uids = client.search(
                    ["ALL"]
                )

                if not uids:
                    continue

                headers = (
                    fetch_headers_in_batches(
                        client,
                        uids
                    )
                )

                for uid, header in headers.items():
                    score, matched = (
                        calculate_mail_match_score(
                            header["subject"],
                            header["from"],
                            header["to"],
                            terms
                        )
                    )

                    if score <= 0:
                        continue

                    candidates.append({
                        "folder": folder_name,
                        "uid": uid,
                        "score": score,
                        "matched": matched,
                        "header": header
                    })

            except Exception as e:
                print(
                    "Folder scan error:",
                    repr(folder_name),
                    repr(e)
                )

                continue

        # Сначала наиболее сильные совпадения,
        # затем более новые письма.
        def candidate_sort_key(
            item
        ):
            dt = item[
                "header"
            ].get(
                "date"
            )

            timestamp = (
                dt.timestamp()
                if dt
                else 0
            )

            return (
                item["score"],
                timestamp
            )

        candidates.sort(
            key=candidate_sort_key,
            reverse=True
        )

        # Не скачиваем полные тела сотен писем.
        # Сначала берём лучшие кандидаты.
        candidates = candidates[
            :max(
                result_limit * 3,
                30
            )
        ]

        full_results = []

        for candidate in candidates:
            try:
                folder_name = (
                    candidate["folder"]
                )

                uid = candidate[
                    "uid"
                ]

                client.select_folder(
                    folder_name,
                    readonly=True
                )

                fetched = client.fetch(
                    [uid],
                    ["RFC822"]
                )

                values = fetched.get(
                    uid
                )

                if not values:
                    continue

                raw_email = (
                    values.get(
                        b"RFC822"
                    )
                    or values.get(
                        "RFC822"
                    )
                )

                if not raw_email:
                    continue

                item = parse_full_email(
                    raw_email,
                    folder_name,
                    uid
                )

                body_normalized = (
                    normalize_search_text(
                        item["body"]
                    )
                )

                body_bonus = 0
                body_matches = []

                for term in terms:
                    if (
                        normalize_search_text(
                            term
                        )
                        in body_normalized
                    ):
                        body_bonus += 2
                        body_matches.append(
                            term
                        )

                item["score"] = (
                    candidate["score"]
                    + body_bonus
                )

                item["matched_terms"] = sorted(
                    set(
                        candidate["matched"]
                        + body_matches
                    )
                )

                full_results.append(
                    item
                )

            except Exception as e:
                print(
                    "Full mail fetch error:",
                    repr(e)
                )

        # Удаляем дубли по Message-ID.
        # Например письмо может встречаться
        # в нескольких IMAP-папках.
        unique = {}

        for item in full_results:
            key = (
                item["message_id"]
                or (
                    item["folder"],
                    item["uid"]
                )
            )

            previous = unique.get(
                key
            )

            if (
                previous is None
                or item["score"]
                > previous["score"]
            ):
                unique[key] = item

        results = list(
            unique.values()
        )

        results.sort(
            key=lambda item: (
                item["score"],
                (
                    item["date"].timestamp()
                    if item["date"]
                    else 0
                )
            ),
            reverse=True
        )

        return results[
            :result_limit
        ]

    finally:
        try:
            client.logout()
        except Exception:
            pass


def get_recent_mail(
    limit=10
):
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

        selected = uids[
            -limit:
        ][::-1]

        fetched = client.fetch(
            selected,
            ["RFC822"]
        )

        result = []

        for uid in selected:
            values = fetched.get(
                uid
            )

            if not values:
                continue

            raw_email = (
                values.get(
                    b"RFC822"
                )
                or values.get(
                    "RFC822"
                )
            )

            if not raw_email:
                continue

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
        lines.extend([
            "",
            (
                f"{index}. "
                f"{format_mail_date(item['date'])}"
            ),
            (
                f"Папка: "
                f"{item.get('folder', 'неизвестно')}"
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

        if item.get(
            "attachments"
        ):
            lines.append(
                "Вложения: "
                + ", ".join(
                    item["attachments"]
                )
            )

    return "\n".join(
        lines
    )


def mail_context(messages):
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

    # Для анализа цепочки удобнее
    # показать письма в хронологическом порядке.
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
            (
                "Папка: "
                + str(
                    item.get(
                        "folder",
                        ""
                    )
                )
            ),
            (
                "От: "
                + item["from"]
            ),
            (
                "Кому: "
                + item["to"]
            ),
            (
                "Тема: "
                + item["subject"]
            )
        ])

        matched_terms = item.get(
            "matched_terms",
            []
        )

        if matched_terms:
            lines.append(
                "Совпало при поиске: "
                + ", ".join(
                    matched_terms
                )
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
            "Текст письма:\n"
            + (
                item["body"]
                or "[текст пуст]"
            )
        )

    return "\n".join(
        lines
    )


# =========================================================
# ПАМЯТЬ + ЗАДАЧИ ДЛЯ OPENAI
# =========================================================

def build_saved_context(chat_id):
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

def extract_openai_text(data):
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

    saved_context = (
        build_saved_context(
            chat_id
        )
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
# НОРМАЛИЗАЦИЯ ТЕКСТА
# =========================================================

def strip_punctuation(text):
    return re.sub(
        r"[?!.,]+$",
        "",
        text.strip().lower()
    ).strip()


# =========================================================
# ПОЧТОВЫЕ КОМАНДЫ
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

    # -----------------------------------------------------
    # CONNECTION TEST
    # -----------------------------------------------------

    if clean == "/mail":
        try:
            client = get_mail_connection()

            folders = (
                get_mail_folders(
                    client
                )
            )

            client.logout()

            send_message(
                chat_id,
                "Связь с Яндекс.Почтой есть ✅\n\n"
                f"Вижу папок: {len(folders)}."
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

    # -----------------------------------------------------
    # RECENT
    # -----------------------------------------------------

    if clean in {
        "/mailrecent",
        "последние письма",
        "покажи последние письма",
        "что нового в почте",
        "покажи последние 10 писем"
    }:
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
                "Recent mail error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать почту. "
                "Посмотри лог в Railway."
            )

        return True

    # -----------------------------------------------------
    # SEARCH
    # -----------------------------------------------------

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

        if not query:
            return False

        try:
            send_message(
                chat_id,
                f"Ищу по всей почте: {query}..."
            )

            messages = (
                search_mail_all_folders(
                    query,
                    result_limit=15
                )
            )

            if not messages:
                send_message(
                    chat_id,
                    (
                        f"По запросу «{query}» "
                        "во всех доступных папках "
                        "ничего не нашла."
                    )
                )

                return True

            context = mail_context(
                messages
            )

            analysis_prompt = (
                f"Я попросила найти в почте: {query}.\n\n"
                "Проанализируй реальные найденные письма.\n"
                "Если они относятся к одной переписке, "
                "собери их в хронологическую историю.\n"
                "Скажи:\n"
                "1. что именно нашлось;\n"
                "2. какое письмо наиболее похоже на нужное;\n"
                "3. в чем суть переписки;\n"
                "4. какая последняя договоренность;\n"
                "5. что мне нужно сделать дальше;\n"
                "6. связано ли это с моими сохраненными задачами.\n\n"
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
                "по всей почте. "
                "Посмотри лог в Railway."
            )

        return True

    return False


# =========================================================
# КАЛЕНДАРНЫЕ КОМАНДЫ
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
        "что сегодня",
        "встречи сегодня",
        "календарь сегодня",
        "покажи календарь на сегодня",
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

            send_message(
                chat_id,
                "Не смогла прочитать календарь."
            )

        return True

    if clean in {
        "/tomorrow",
        "что у меня завтра",
        "что завтра",
        "встречи завтра",
        "календарь завтра",
        "покажи календарь на завтра",
        "какие у меня завтра встречи"
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
        "что у меня на этой неделе",
        "покажи встречи на неделю"
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
        "проверь мой сегодня",
        "проверь календарь на сегодня",
        "проверь мой календарь на сегодня"
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
        "проверь мой завтра",
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

    if clean in {
        "/checkweek",
        "проверь неделю",
        "проверь мою неделю",
        "проверь календарь на неделю"
    }:
        try:
            send_message(
                chat_id,
                "Проверяю неделю..."
            )

            answer = review_calendar(
                chat_id,
                today,
                7,
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
                "Не смогла проверить неделю."
            )

        return True

    return False


# =========================================================
# ЗАДАЧИ / ПАМЯТЬ / СЛУЖЕБНЫЕ
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
            "У меня есть ИИ, постоянная память, "
            "задачи, Яндекс.Календарь "
            "и read-only Яндекс.Почта."
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

    if clean == "/calendar":
        try:
            calendars = (
                get_yandex_calendars()
            )

            names = [
                (
                    getattr(
                        calendar,
                        "name",
                        None
                    )
                    or "Календарь"
                )
                for calendar in calendars
            ]

            send_message(
                chat_id,
                (
                    "Связь с Яндекс.Календарём есть ✅\n\n"
                    + "\n".join(
                        f"• {name}"
                        for name in names
                    )
                )
            )

        except Exception as e:
            print(
                "Calendar connection error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не получилось подключиться "
                "к Яндекс.Календарю."
            )

        return True

    if clean in {
        "/tasks",
        "задачи",
        "мои задачи",
        "покажи задачи",
        "покажи мои задачи",
        "что у меня в задачах",
        "какие у меня задачи",
        "что у меня по задачам"
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
        "покажи свою память",
        "что у тебя в памяти",
        "что ты обо мне помнишь",
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
                f"Не нашла запись "
                f"#{memory_id}."
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
                f"#{memory_id}: "
                f"{memory_text}"
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
        "SQLite + OpenAI + "
        "Calendar + all-folder Mail search."
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

                # Сначала почта.
                if try_handle_mail(
                    chat_id,
                    text
                ):
                    continue

                # Потом календарь.
                if try_handle_calendar(
                    chat_id,
                    text
                ):
                    continue

                # Потом локальные задачи/память.
                if try_handle_command(
                    chat_id,
                    text
                ):
                    continue

                # Обычный разговор с OpenAI.
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
