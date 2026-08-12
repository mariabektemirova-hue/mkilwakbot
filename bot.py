import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"

# Для каждого Telegram-чата храним ID последнего ответа OpenAI.
# Это и дает нам непрерывный диалог.
LAST_RESPONSE_ID = {}

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
- учитывай предыдущие сообщения этого диалога;
- не заставляй Машу повторять уже сказанное;
- если видишь риск забыть дедлайн, повестку, материалы,
  подтверждение, ссылку или адрес — отдельно подсвети его;
- можешь помогать сформулировать письмо, сообщение, план или список действий;
- не утверждай, что реально выполнила действие, если оно не выполнено;
- пока у тебя нет прямого доступа к календарю, почте и постоянной базе задач.

Если Маша пишет задачу или договоренность, помоги привести ее
к понятному виду: что сделать, для кого, к какому сроку и чего не хватает.
"""


def send_message(chat_id, text):
    response = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=30
    )
    response.raise_for_status()


def ask_openai(chat_id, user_text):
    payload = {
        "model": "gpt-5.6",
        "instructions": ASSISTANT_INSTRUCTIONS,
        "input": user_text
    }

    # Если с этим чатом уже разговаривали —
    # продолжаем предыдущую цепочку.
    previous_id = LAST_RESPONSE_ID.get(chat_id)

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

    response.raise_for_status()
    data = response.json()

    # Запоминаем этот ответ для следующего сообщения.
    response_id = data.get("id")
    if response_id:
        LAST_RESPONSE_ID[chat_id] = response_id

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")

    return "Я получила ответ, но не смогла разобрать его текст."


def main():
    print("Masha 2.0 запущена с памятью диалога")

    offset = None

    while True:
        params = {"timeout": 30}

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

                if text == "/start":
                    send_message(
                        chat_id,
                        "Привет 👋 Я Маша 2.0.\n\n"
                        "Я знаю базовые правила твоей работы "
                        "и теперь помню контекст нашего текущего диалога."
                    )
                    continue

                if text == "/new":
                    LAST_RESPONSE_ID.pop(chat_id, None)
                    send_message(
                        chat_id,
                        "Готово. Начинаем новый разговор — "
                        "предыдущий контекст я больше не учитываю."
                    )
                    continue

                send_message(chat_id, "Думаю...")

                try:
                    answer = ask_openai(chat_id, text)
                    send_message(chat_id, answer)

                except requests.HTTPError as e:
                    print(
                        "OpenAI HTTP error:",
                        e.response.status_code,
                        e.response.text
                    )
                    send_message(
                        chat_id,
                        "Я дошла до OpenAI, но получила ошибку API. "
                        "Посмотри мой лог в Railway."
                    )

                except Exception as e:
                    print("OpenAI error:", repr(e))
                    send_message(
                        chat_id,
                        "У меня возникла техническая ошибка при обращении к ИИ."
                    )

        except Exception as e:
            print("Telegram error:", repr(e))
            time.sleep(3)


if __name__ == "__main__":
    main()
