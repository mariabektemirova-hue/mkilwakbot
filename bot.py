import os
import re
import time
import sqlite3
import requests
from datetime import datetime


# =========================================================
# НАСТРОЙКИ
# =========================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"

# Railway Volume у нас смонтирован в /data
DB_PATH = "/data/masha.db"


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
- Пока у тебя нет прямого доступа к календарю и почте.
- Не говори, что поставила напоминание, если реальное напоминание
  технически не создано.
- Если Маша пишет задачу или договоренность, помоги определить:
  что сделать, для кого, к какому сроку и чего не хватает.

ВАЖНО О ФОРМАТЕ

Telegram сейчас получает обычный текст.
Не используй Markdown-разметку:
не ставь **, *, ### и другие Markdown-символы для оформления.
Используй обычный текст, абзацы и эмодзи умеренно.

ВАЖНО О ПАМЯТИ

Если ниже есть раздел ПОСТОЯННАЯ ПАМЯТЬ,
это реально сохраненные сведения.

Если ниже есть раздел ОТКРЫТЫЕ ЗАДАЧИ,
это реально сохраненные незакрытые задачи.

Не говори, что сохранила новый факт или новую задачу,
если они не были сохранены программой.
"""


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def ensure_data_directory():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(directory, exist_ok=True)


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

    # Задачи
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

    # Постоянная память
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Текущая цепочка разговора OpenAI
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
    text = text.strip()

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
            text,
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
        SELECT
            id,
            text,
            created_at
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
        SET
            status = 'done',
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
    text = text.strip()

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
            text,
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
        SELECT
            id,
            text,
            created_at
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

    if not row:
        return None

    return row["previous_response_id"]


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
    if text is None:
        text = ""

    text = str(text)

    # Telegram ограничивает размер сообщения.
    max_length = 4000

    if not text:
        parts = [""]
    else:
        parts = [
            text[i:i + max_length]
            for i in range(0, len(text), max_length)
        ]

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
# КОНТЕКСТ ИЗ БАЗЫ
# =========================================================

def build_saved_context(chat_id):
    memories = get_memories(chat_id)
    tasks = get_open_tasks(chat_id)

    sections = []

    if memories:
        memory_lines = []

        for row in memories:
            memory_lines.append(
                f"- {row['text']}"
            )

        sections.append(
            "ПОСТОЯННАЯ ПАМЯТЬ:\n"
            + "\n".join(memory_lines)
        )

    if tasks:
        task_lines = []

        for row in tasks:
            task_lines.append(
                f"- Задача #{row['id']}: {row['text']}"
            )

        sections.append(
            "ОТКРЫТЫЕ ЗАДАЧИ:\n"
            + "\n".join(task_lines)
        )

    return "\n\n".join(sections)


# =========================================================
# OPENAI
# =========================================================

def extract_openai_text(data):
    texts = []

    for item in data.get("output", []):
        if item.get("type") != "message":
            continue

        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text", "")

                if text:
                    texts.append(text)

    if texts:
        return "\n".join(texts)

    return "Я получила ответ, но не смогла разобрать его текст."


def ask_openai(chat_id, user_text):
    saved_context = build_saved_context(chat_id)

    if saved_context:
        current_input = (
            saved_context
            + "\n\n"
            + "ТЕКУЩЕЕ СООБЩЕНИЕ МАШИ:\n"
            + user_text
        )
    else:
        current_input = user_text

    payload = {
        "model": "gpt-5.6",
        "instructions": ASSISTANT_INSTRUCTIONS,
        "input": current_input,
        "store": True
    }

    previous_id = get_previous_response_id(chat_id)

    if previous_id:
        payload["previous_response_id"] = previous_id

    response = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=90
    )

    # Если старая OpenAI-цепочка больше недоступна,
    # начинаем новый разговор.
    # При этом задачи и постоянная память остаются.
    if response.status_code == 400 and previous_id:
        print(
            "Previous response unavailable. "
            "Starting new OpenAI conversation."
        )

        clear_previous_response_id(chat_id)

        payload.pop(
            "previous_response_id",
            None
        )

        response = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=90
        )

    response.raise_for_status()

    data = response.json()

    response_id = data.get("id")

    if response_id:
        save_previous_response_id(
            chat_id,
            response_id
        )

    return extract_openai_text(data)


# =========================================================
# СЛУЖЕБНЫЕ КОМАНДЫ
# =========================================================

def show_tasks(chat_id):
    tasks = get_open_tasks(chat_id)

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
            f"{row['id']}. {row['text']}"
        )

    send_message(
        chat_id,
        "\n\n".join(lines)
    )


def show_memory(chat_id):
    memories = get_memories(chat_id)

    if not memories:
        send_message(
            chat_id,
            "В постоянной памяти пока ничего нет."
        )
        return

    lines = [
        "Вот что хранится в моей постоянной памяти:"
    ]

    for row in memories:
        lines.append(
            f"{row['id']}. {row['text']}"
        )

    send_message(
        chat_id,
        "\n\n".join(lines)
    )


# =========================================================
# РАСПОЗНАВАНИЕ КОМАНД
# =========================================================

def try_handle_command(chat_id, text):
    clean = text.strip()

    # Для распознавания убираем лишние пробелы.
    normalized = re.sub(
        r"\s+",
        " ",
        clean
    ).strip()

    lower = normalized.lower()


    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    if lower == "/start":
        send_message(
            chat_id,
            "Привет 👋 Я Маша 2.0.\n\n"
            "У меня есть ИИ, постоянная память "
            "и база рабочих задач."
        )
        return True


    # -----------------------------------------------------
    # HEALTH CHECK
    # -----------------------------------------------------

    if lower == "/health":
        send_message(
            chat_id,
            "Я работаю ✅\n"
            "База данных доступна."
        )
        return True


    # -----------------------------------------------------
    # НОВЫЙ РАЗГОВОР
    # -----------------------------------------------------

    if lower == "/new":
        clear_previous_response_id(chat_id)

        send_message(
            chat_id,
            "Готово. Начинаем новый разговор.\n\n"
            "Постоянную память и задачи я не забыла."
        )
        return True


    # -----------------------------------------------------
    # СПИСОК ЗАДАЧ
    # -----------------------------------------------------

    task_list_phrases = {
        "/tasks",
        "задачи",
        "мои задачи",
        "покажи задачи",
        "покажи мои задачи",
        "что у меня в задачах",
        "какие у меня задачи",
        "что у меня по задачам",
        "что мне нужно сделать"
    }

    if lower in task_list_phrases:
        show_tasks(chat_id)
        return True


    # -----------------------------------------------------
    # ЗАКРЫТИЕ ЗАДАЧИ
    # -----------------------------------------------------

    done_match = re.match(
        r"^(?:"
        r"закрой задачу|"
        r"закрыть задачу|"
        r"выполни задачу|"
        r"выполнена задача|"
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
                f"Не нашла открытую задачу #{task_id}."
            )

        return True


    # -----------------------------------------------------
    # ДОБАВЛЕНИЕ ЗАДАЧИ
    # -----------------------------------------------------

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
            flags=re.IGNORECASE | re.DOTALL
        )

        if not match:
            continue

        task_text = match.group(1).strip()

        if not task_text:
            continue

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


    # -----------------------------------------------------
    # ПРОСМОТР ПОСТОЯННОЙ ПАМЯТИ
    # -----------------------------------------------------

    memory_list_phrases = {
        "/memory",
        "что ты помнишь",
        "покажи память",
        "покажи свою память",
        "что у тебя в памяти",
        "что ты обо мне помнишь",
        "что ты запомнила"
    }

    if lower in memory_list_phrases:
        show_memory(chat_id)
        return True


    # -----------------------------------------------------
    # УДАЛЕНИЕ ИЗ ПАМЯТИ
    # -----------------------------------------------------

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
                f"Удалила запись #{memory_id} из памяти."
            )
        else:
            send_message(
                chat_id,
                f"Не нашла запись #{memory_id}."
            )

        return True


    # -----------------------------------------------------
    # СОХРАНЕНИЕ В ПОСТОЯННУЮ ПАМЯТЬ
    #
    # ВАЖНО:
    # Теперь распознаются:
    #
    # Запомни, Эльдар...
    # Запомни: Эльдар...
    # Запомни Эльдар...
    # Запомни, что Эльдар...
    # Сохрани в память...
    # -----------------------------------------------------

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
            flags=re.IGNORECASE | re.DOTALL
        )

        if not match:
            continue

        memory_text = match.group(1).strip()

        # Если использовали "Запомни, что..."
        # в память кладем сам факт, без лишнего "что".
        if not memory_text:
            continue

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
        "Masha 2.0 запущена. "
        f"SQLite: {DB_PATH}"
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
                    update["update_id"] + 1
                )

                message = update.get(
                    "message"
                )

                if not message:
                    continue

                chat_id = message[
                    "chat"
                ]["id"]

                text = message.get(
                    "text",
                    ""
                )

                if not text:
                    continue

                print(
                    "Incoming message:",
                    chat_id,
                    repr(text)
                )

                # Сначала проверяем локальные команды.
                # Поэтому "Запомни..." и "Задача..."
                # сохраняются БЕЗ обращения к OpenAI.
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
                    status_code = None
                    error_text = ""

                    if e.response is not None:
                        status_code = (
                            e.response.status_code
                        )

                        error_text = (
                            e.response.text
                        )

                    print(
                        "OpenAI HTTP error:",
                        status_code,
                        error_text
                    )

                    send_message(
                        chat_id,
                        "Я дошла до OpenAI, "
                        "но получила ошибку API. "
                        "Посмотри мой лог в Railway."
                    )

                except Exception as e:
                    print(
                        "OpenAI error:",
                        repr(e)
                    )

                    send_message(
                        chat_id,
                        "У меня возникла техническая ошибка "
                        "при обращении к ИИ."
                    )

        except requests.HTTPError as e:
            print(
                "Telegram HTTP error:",
                repr(e)
            )

            time.sleep(3)

        except requests.RequestException as e:
            print(
                "Telegram network error:",
                repr(e)
            )

            time.sleep(3)

        except Exception as e:
            print(
                "Telegram unexpected error:",
                repr(e)
            )

            time.sleep(3)


if __name__ == "__main__":
    main()
