import os
import re
import time
import sqlite3
import requests
import caldav

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from icalendar import Calendar


# =========================================================
# НАСТРОЙКИ
# =========================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

YANDEX_CALENDAR_LOGIN = os.environ["YANDEX_CALENDAR_LOGIN"]
YANDEX_CALENDAR_PASSWORD = os.environ["YANDEX_CALENDAR_PASSWORD"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"

YANDEX_CALDAV_URL = "https://caldav.yandex.ru/"

DB_PATH = "/data/masha.db"

CALENDAR_TZ = ZoneInfo("Europe/Moscow")


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

ЧЕК-ЛИСТ ВСТРЕЧ

- Для Zoom должна быть ссылка.
- Все участники должны подтвердить присутствие.
- В описании события должна быть повестка.
- Для внешней встречи обязательно должен быть адрес.
- В календаре нужно сохранять время на обед.
- Между встречами желательно оставлять немного воздуха.
- Для собеседования резюме должно быть вложено в событие.
- Перед собеседованием резюме нужно распечатать.
- Для PRM нужно заранее собрать материалы руководителей в онлайн-папку
  и проверить доступ к ней.

КАК РАБОТАТЬ С МАШЕЙ

- Отвечай по-русски.
- Говори естественно, тепло и без канцелярита.
- На простой вопрос отвечай коротко.
- Если сообщение хаотичное — сама структурируй его.
- Учитывай предыдущие сообщения текущего диалога.
- Учитывай ПОСТОЯННУЮ ПАМЯТЬ, если она передана тебе.
- Учитывай ОТКРЫТЫЕ ЗАДАЧИ, если они переданы тебе.
- Не заставляй Машу повторять уже известное.
- Если видишь риск забыть дедлайн, повестку, материалы,
  подтверждение, ссылку или адрес — отдельно подсвети это.
- Помогай формулировать письма, сообщения, планы и списки действий.
- Не утверждай, что реально выполнила действие, если оно не выполнено.
- Пока у тебя нет доступа к Яндекс.Почте.
- Календарь ты пока умеешь только читать.
- Не утверждай, что создала, перенесла или удалила событие.
- Не говори, что поставила напоминание, если реальное напоминание
  технически не создано.

ВАЖНО О ФОРМАТЕ

Telegram сейчас получает обычный текст.
Не используй Markdown-разметку.
Не ставь **, *, ### для оформления.

ВАЖНО О ПАМЯТИ

Если ниже есть раздел ПОСТОЯННАЯ ПАМЯТЬ,
это реально сохраненные сведения.

Если ниже есть раздел ОТКРЫТЫЕ ЗАДАЧИ,
это реально сохраненные незакрытые задачи.

Если ниже есть раздел КАЛЕНДАРЬ,
это реальные события, полученные из Яндекс.Календаря.

Не выдумывай события, которых нет в переданном календаре.
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
# ПАМЯТЬ ТЕКУЩЕГО ДИАЛОГА
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
# ЯНДЕКС.КАЛЕНДАРЬ — ТОЛЬКО ЧТЕНИЕ
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
        calendars = principal.get_calendars()
    except AttributeError:
        calendars = principal.calendars()

    return calendars


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
        dt = datetime(
            value.year,
            value.month,
            value.day,
            tzinfo=CALENDAR_TZ
        )

        return dt, True

    return None, False


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

        start_raw = dtstart_property.dt

        start, all_day = (
            normalize_calendar_datetime(
                start_raw
            )
        )

        if start is None:
            continue

        dtend_property = component.get(
            "DTEND"
        )

        end = None

        if dtend_property:
            end, _ = (
                normalize_calendar_datetime(
                    dtend_property.dt
                )
            )

        summary = str(
            component.get(
                "SUMMARY",
                "Без названия"
            )
        )

        location = str(
            component.get(
                "LOCATION",
                ""
            )
        )

        description = str(
            component.get(
                "DESCRIPTION",
                ""
            )
        )

        url = str(
            component.get(
                "URL",
                ""
            )
        )

        uid = str(
            component.get(
                "UID",
                ""
            )
        )

        result.append({
            "uid": uid,
            "summary": summary,
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": location,
            "description": description,
            "url": url,
            "calendar": calendar_name
        })

    return result


def get_calendar_events(start_dt, end_dt):
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
                parsed_events = (
                    extract_event_from_ical(
                        event.data,
                        calendar_name
                    )
                )

                collected.extend(
                    parsed_events
                )

            except Exception as e:
                print(
                    "Calendar event parse error:",
                    repr(e)
                )

    # -----------------------------------------------------
    # УДАЛЯЕМ ДУБЛИ
    #
    # Одна и та же встреча может присутствовать
    # одновременно в календаре Маши и календаре МС.
    #
    # Название календаря специально НЕ включаем в ключ.
    # -----------------------------------------------------

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

        summary_key = (
            event["summary"]
            .strip()
            .lower()
        )

        key = (
            summary_key,
            start_key,
            end_key
        )

        if key not in unique:
            unique[key] = event
            continue

        # Если дубль из другого календаря содержит
        # больше полезной информации, дополняем первую запись.

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

    result = list(
        unique.values()
    )

    result.sort(
        key=lambda event: event["start"]
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

    lines = [
        title
    ]

    for event in events:
        lines.append(
            format_event(event)
        )

    return "\n\n".join(
        lines
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
        weekday = weekday_names[
            event_date.weekday()
        ]

        lines.append(
            f"{weekday}, "
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


# =========================================================
# КОНТЕКСТ ПАМЯТИ И ЗАДАЧ
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
        lines = [
            f"- {row['text']}"
            for row in memories
        ]

        sections.append(
            "ПОСТОЯННАЯ ПАМЯТЬ:\n"
            + "\n".join(lines)
        )

    if tasks:
        lines = [
            (
                f"- Задача #{row['id']}: "
                f"{row['text']}"
            )
            for row in tasks
        ]

        sections.append(
            "ОТКРЫТЫЕ ЗАДАЧИ:\n"
            + "\n".join(lines)
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
                    texts.append(text)

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
    saved_context = (
        build_saved_context(
            chat_id
        )
    )

    sections = []

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

    full_input = "\n\n".join(
        sections
    )

    payload = {
        "model": "gpt-5.6",
        "instructions": (
            ASSISTANT_INSTRUCTIONS
        ),
        "input": full_input,
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
        print(
            "Old OpenAI conversation "
            "unavailable. Starting new."
        )

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
# ПОКАЗ ЗАДАЧ И ПАМЯТИ
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

    lines = [
        "Твои открытые задачи:"
    ]

    for row in tasks:
        lines.append(
            f"#{row['id']} — "
            f"{row['text']}"
        )

    send_message(
        chat_id,
        "\n\n".join(lines)
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

    lines = [
        "Вот что я помню:"
    ]

    for row in memories:
        lines.append(
            f"#{row['id']} — "
            f"{row['text']}"
        )

    send_message(
        chat_id,
        "\n\n".join(lines)
    )


# =========================================================
# ТЕКСТОВАЯ НОРМАЛИЗАЦИЯ
# =========================================================

def strip_punctuation(text):
    return re.sub(
        r"[?!.,]+$",
        "",
        text.strip().lower()
    ).strip()


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

    today_phrases = {
        "/today",
        "что у меня сегодня",
        "что сегодня",
        "встречи сегодня",
        "календарь сегодня",
        "покажи календарь на сегодня",
        "покажи встречи на сегодня",
        "какие встречи сегодня",
        "какие у меня сегодня встречи",
        "что у меня сегодня по календарю"
    }

    tomorrow_phrases = {
        "/tomorrow",
        "что у меня завтра",
        "что завтра",
        "встречи завтра",
        "календарь завтра",
        "покажи календарь на завтра",
        "покажи встречи на завтра",
        "какие встречи завтра",
        "какие у меня завтра встречи",
        "что у меня завтра по календарю"
    }

    week_phrases = {
        "/week",
        "календарь на неделю",
        "покажи неделю",
        "что у меня на неделю",
        "что у меня на этой неделе",
        "встречи на неделю",
        "покажи встречи на неделю"
    }

    if clean in today_phrases:
        try:
            result = format_calendar_day(
                today,
                "Сегодня:"
            )

            send_message(
                chat_id,
                result
            )

        except Exception as e:
            print(
                "Yandex Calendar error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать "
                "Яндекс.Календарь. "
                "Посмотри мой лог в Railway."
            )

        return True

    if clean in tomorrow_phrases:
        try:
            tomorrow = (
                today
                + timedelta(days=1)
            )

            result = format_calendar_day(
                tomorrow,
                "Завтра:"
            )

            send_message(
                chat_id,
                result
            )

        except Exception as e:
            print(
                "Yandex Calendar error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать "
                "Яндекс.Календарь. "
                "Посмотри мой лог в Railway."
            )

        return True

    if clean in week_phrases:
        try:
            result = (
                format_calendar_period(
                    today,
                    7
                )
            )

            send_message(
                chat_id,
                result
            )

        except Exception as e:
            print(
                "Yandex Calendar error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не смогла прочитать "
                "Яндекс.Календарь. "
                "Посмотри мой лог в Railway."
            )

        return True

    return False


# =========================================================
# ОСТАЛЬНЫЕ КОМАНДЫ
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

    # START

    if clean == "/start":
        send_message(
            chat_id,
            "Привет 👋 Я Маша 2.0.\n\n"
            "У меня есть ИИ, "
            "постоянная память, "
            "база задач и чтение "
            "Яндекс.Календаря."
        )

        return True

    # HEALTH

    if clean == "/health":
        send_message(
            chat_id,
            "Я работаю ✅\n"
            "Память и база доступны."
        )

        return True

    # НОВЫЙ ДИАЛОГ

    if clean == "/new":
        clear_previous_response_id(
            chat_id
        )

        send_message(
            chat_id,
            "Готово. Начинаем "
            "новый разговор.\n\n"
            "Память и задачи "
            "я не забыла."
        )

        return True

    # ТЕСТ КАЛЕНДАРЯ

    if clean == "/calendar":
        try:
            calendars = (
                get_yandex_calendars()
            )

            names = []

            for calendar in calendars:
                name = (
                    getattr(
                        calendar,
                        "name",
                        None
                    )
                    or "Календарь"
                )

                names.append(
                    name
                )

            if names:
                text_result = (
                    "Связь с "
                    "Яндекс.Календарём есть ✅\n\n"
                    "Нашла календари:\n"
                    + "\n".join(
                        f"• {name}"
                        for name in names
                    )
                )

            else:
                text_result = (
                    "К Яндекс.Календарю "
                    "подключилась, "
                    "но календарей не нашла."
                )

            send_message(
                chat_id,
                text_result
            )

        except Exception as e:
            print(
                "Calendar connection error:",
                repr(e)
            )

            send_message(
                chat_id,
                "Не получилось подключиться "
                "к Яндекс.Календарю.\n\n"
                "Посмотри Deploy Logs "
                "в Railway."
            )

        return True

    # СПИСОК ЗАДАЧ

    task_list_phrases = {
        "/tasks",
        "задачи",
        "мои задачи",
        "покажи задачи",
        "покажи мои задачи",
        "что у меня в задачах",
        "какие у меня задачи",
        "что у меня по задачам"
    }

    if clean in task_list_phrases:
        show_tasks(
            chat_id
        )

        return True

    # ЗАКРЫТИЕ ЗАДАЧИ

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
                f"Задачу #{task_id} "
                "закрыла ✅"
            )

        else:
            send_message(
                chat_id,
                f"Не нашла открытую "
                f"задачу #{task_id}."
            )

        return True

    # ДОБАВЛЕНИЕ ЗАДАЧИ

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

    # ПОКАЗ ПАМЯТИ

    memory_phrases = {
        "/memory",
        "что ты помнишь",
        "покажи память",
        "покажи свою память",
        "что у тебя в памяти",
        "что ты обо мне помнишь",
        "что ты запомнила"
    }

    if clean in memory_phrases:
        show_memory(
            chat_id
        )

        return True

    # УДАЛЕНИЕ ИЗ ПАМЯТИ

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

    # СОХРАНЕНИЕ В ПАМЯТЬ

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
        "Masha 2.0 запущена. "
        "SQLite + OpenAI + "
        "Yandex Calendar read-only."
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

                # Сначала календарные команды
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

                # Обычный разговор с OpenAI
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
                        "но получила ошибку API. "
                        "Посмотри мой лог "
                        "в Railway."
                    )

                except Exception as e:
                    print(
                        "OpenAI error:",
                        repr(e)
                    )

                    send_message(
                        chat_id,
                        "У меня возникла "
                        "техническая ошибка "
                        "при обращении к ИИ."
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
