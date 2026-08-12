import os
import re
import time
import sqlite3
import requests
from datetime import datetime

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"

DB_PATH = "/data/masha.db"


ASSISTANT_INSTRUCTIONS = """
Ты Маша 2.0 — личный рабочий ассистент Маши.

Главная цель:
разгружать Маше голову и помогать держать под контролем встречи,
задачи, дедлайны, материалы, договоренности и организационный хаос.

Контекст работы:
- Маша работает бизнес-ассистентом руководителя МС.
- МС может внезапно попросить 30 минут с кем-то из директоров,
  поэтому важно сохранять привычные слоты и свободные окна под срочное.
- ТМС и Advisory Board (AB) не двигаем.
- Тренировки и психоаналитик Маши тоже не двигаем.
- Для 1-2-1 повестку нужно начинать собирать за неделю.
- Если к встрече нет повестки или необходимых материалов,
  нужно обратить на это внимание: по рабочему правилу встречу отменяем.
- Материалы к встречам желательно получать минимум за 2 суток,
  лучше заранее.

Чек-лист календаря и встреч:
- Zoom: должна быть ссылка.
- Все участники должны подтвердить присутствие.
- В описании события должна быть повестка.
- Внешняя встреча: обязательно должен быть адрес.
- В календаре должны оставаться обед и немного воздуха.
- Собеседование: резюме вложено в событие и распечатано перед встречей.
- PRM: заранее собрать материалы руководителей в онлайн-папку
  и проверить доступ к ней.

Как работать с Машей:
- отвечай по-русски;
- говори естественно, тепло и без канцелярита;
- на простой вопрос отвечай коротко;
- если сообщение хаотичное, сама структурируй его;
- учитывай предыдущие сообщения диалога;
- учитывай постоянную память и открытые задачи, которые переданы тебе;
- не заставляй Машу повторять уже сказанное;
- если видишь риск забыть дедлайн, повестку, материалы,
  подтверждение, ссылку или адрес — отдельно подсвети его;
- можешь помогать сформулировать письмо, сообщение, план или список действий;
- не утверждай, что реально выполнила действие, если оно не выполнено;
- пока у тебя нет прямого доступа к календарю и почте.

Если Маша пишет задачу или договоренность, помоги привести ее
к понятному виду: что сделать, для кого, к какому сроку и чего не хватает.

ВАЖНО:
Если в сообщении ниже есть раздел "ПОСТОЯННАЯ ПАМЯТЬ",
считай эти сведения сохраненными фактами.

Если есть раздел "ОТКРЫТЫЕ ЗАДАЧИ",
это реальные незакрытые задачи Маши.
"""


# ----------------------------
# БАЗА ДАННЫХ
# ----------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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


# ----------------------------
# ЗАДАЧИ
# ----------------------------

def add_task(chat_id, text):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO tasks (chat_id, text, status, created_at)
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
        WHERE chat_id = ? AND status = 'open'
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
        WHERE id = ?
          AND chat_id = ?
          AND status = 'open'
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            task_id,
            chat_id
        )
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# ----------------------------
# ПОСТОЯННАЯ ПАМЯТЬ
# ----------------------------

def add_memory(chat_id, text):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO memories (chat_id, text, created_at)
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
        WHERE id = ? AND chat_id = ?
        """,
        (memory_id, chat_id)
    )

    changed = cursor.rowcount

    conn.commit()
    conn.close()

    return changed > 0


# ----------------------------
# ПАМЯТЬ ТЕКУЩЕГО ДИАЛОГА
# ----------------------------

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
        INSERT INTO chat_state (chat_id, previous_response_id)
        VALUES (?, ?)
        ON CONFLICT(chat_id)
        DO UPDATE SET previous_response_id = excluded.previous_response_id
        """,
        (chat_id, response_id)
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


# ----------------------------
# TELEGRAM
# ----------------------------

def send_message(chat_id, text):
    # Telegram ограничивает длину одного сообщения.
    # Делим длинные ответы на части.
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


# ----------------------------
# КОНТЕКСТ ДЛЯ OPENAI
# ----------------------------

def build_saved_context(chat_id):
    memories = get_memories(chat_id)
    tasks = get_open_tasks(chat_id)

    sections = []

    if memories:
        memory_text = "\n".join(
            f"- {row['text']}"
            for row in memories
        )

        sections.append(
            "ПОСТОЯННАЯ ПАМЯТЬ:\n" + memory_text
        )

    if tasks:
        task_text = "\n".join(
            f"- Задача #{row['id']}: {row['text']}"
            for row in tasks
        )

        sections.append(
            "ОТКРЫТЫЕ ЗАДАЧИ:\n" + task_text
        )

    if not sections:
        return ""

    return "\n\n".join(sections)


# ----------------------------
# OPENAI
# ----------------------------

def ask_openai(chat_id, user_text):
    saved_context = build_saved_context(chat_id)

    if saved_context:
        full_input = (
            saved_context
            + "\n\n"
            + "ТЕКУЩЕЕ СООБЩЕНИЕ МАШИ:\n"
            + user_text
        )
    else:
        full_input = user_text

    payload = {
        "model": "gpt-5.6",
        "instructions": ASSISTANT_INSTRUCTIONS,
        "input": full_input,
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

    # Если старая цепочка OpenAI почему-то больше недоступна,
    # начинаем новую, но задачи и постоянная память остаются.
    if response.status_code == 400 and previous_id:
        print(
            "Previous response unavailable. "
            "Starting a new conversation."
        )

        clear_previous_response_id(chat_id)

        payload.pop("previous_response_id", None)

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
        save_previous_response_id(chat_id, response_id)

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")

    return "Я получила ответ, но не смогла разобрать его текст."


# ----------------------------
# КОМАНДЫ
# ----------------------------

def try_handle_command(chat_id, text):
    clean = text.strip()
    lower = clean.lower()

    # /start
    if lower == "/start":
        send_message(
            chat_id,
            "Привет 👋 Я Маша 2.0.\n\n"
            "Теперь у меня есть постоянная память и база задач."
        )
        return True

    # Новый диалог
    if lower == "/new":
        clear_previous_response_id(chat_id)

        send_message(
            chat_id,
            "Готово. Начинаем новый разговор.\n\n"
            "Задачи и постоянную память я НЕ забыла."
        )
        return True

    # Список задач
    if lower in (
        "/tasks",
        "задачи",
        "мои задачи",
        "покажи задачи",
        "что у меня в задачах",
        "какие у меня задачи"
    ):
        tasks = get_open_tasks(chat_id)

        if not tasks:
            send_message(
                chat_id,
                "Сейчас открытых задач нет."
            )
            return True

        result = ["Твои открытые задачи:"]

        for row in tasks:
            result.append(
                f"\n#{row['id']} — {row['text']}"
            )

        send_message(
            chat_id,
            "\n".join(result)
        )

        return True

    # Добавление задачи
    task_patterns = [
        r"^задача[:\s]+(.+)$",
        r"^запиши задачу[:\s]+(.+)$",
        r"^добавь задачу[:\s]+(.+)$",
        r"^создай задачу[:\s]+(.+)$"
    ]

    for pattern in task_patterns:
        match = re.match(
            pattern,
            clean,
            flags=re.IGNORECASE | re.DOTALL
        )

        if match:
            task_text = match.group(1).strip()

            task_id = add_task(
                chat_id,
                task_text
            )

            send_message(
                chat_id,
                f"Записала ✅\n"
                f"Задача #{task_id}: {task_text}"
            )

            return True

    # Закрытие задачи
    done_match = re.match(
        r"^(?:закрой задачу|выполни задачу|готово задача|/done)\s*#?(\d+)",
        clean,
        flags=re.IGNORECASE
    )

    if done_match:
        task_id = int(done_match.group(1))

        if complete_task(chat_id, task_id):
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

    # Сохранение постоянной памяти
    memory_patterns = [
        r"^запомни[:\s]+(.+)$",
        r"^запомни,?\s+что\s+(.+)$",
        r"^сохрани в память[:\s]+(.+)$"
    ]

    for pattern in memory_patterns:
        match = re.match(
            pattern,
            clean,
            flags=re.IGNORECASE | re.DOTALL
        )

        if match:
            memory_text = match.group(1).strip()

            memory_id = add_memory(
                chat_id,
                memory_text
            )

            send_message(
                chat_id,
                f"Запомнила 🧠\n"
                f"#{memory_id}: {memory_text}"
            )

            return True

    # Просмотр памяти
    if lower in (
        "/memory",
        "что ты помнишь",
        "покажи память",
        "что ты обо мне помнишь"
    ):
        memories = get_memories(chat_id)

        if not memories:
            send_message(
                chat_id,
                "В постоянной памяти пока ничего нет."
            )
            return True

        result = ["Вот что хранится в моей постоянной памяти:"]

        for row in memories:
            result.append(
                f"\n#{row['id']} — {row['text']}"
            )

        send_message(
            chat_id,
            "\n".join(result)
        )

        return True

    # Удаление записи из памяти
    forget_match = re.match(
        r"^(?:забудь|удали из памяти)\s*#?(\d+)",
        clean,
        flags=re.IGNORECASE
    )

    if forget_match:
        memory_id = int(forget_match.group(1))

        if delete_memory(chat_id, memory_id):
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

    return False


# ----------------------------
# ОСНОВНОЙ ЦИКЛ
# ----------------------------

def main():
    init_db()

    print(
        "Masha 2.0 запущена "
        "с постоянной памятью SQLite"
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

            for update in data.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")

                if not message:
                    continue

                chat_id = message["chat"]["id"]
                text = message.get("text", "")

                if not text:
                    continue

                # Сначала проверяем команды
                if try_handle_command(chat_id, text):
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
                        e.response.status_code,
                        e.response.text
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

        except Exception as e:
            print(
                "Telegram error:",
                repr(e)
            )

            time.sleep(3)


if __name__ == "__main__":
    main()
