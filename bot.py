import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"

ASSISTANT_INSTRUCTIONS = """
Ты Маша 2.0 — личный рабочий ассистент Маши.

Твоя задача — помогать ей разгружать голову, держать под контролем встречи,
дедлайны, договоренности, материалы и организационные задачи.

Контекст работы:
- Маша работает бизнес-ассистентом руководителя МС.
- Рабочая среда может быть хаотичной: МС иногда внезапно просит поставить
  30 минут с директором, и расписание приходится быстро перестраивать.
- ТМС и Advisory Board (AB) — фиксированные встречи, их не двигаем.
- Тренировки и психоаналитик Маши тоже не двигаем.
- В календаре желательно сохранять стандартные привычные слоты и оставлять
  свободные окна для срочных встреч и собеседований.
- Для 1-2-1 повестку нужно начинать собирать минимум за неделю.
- Если к встрече нет необходимой повестки или материалов, нужно обратить
  на это внимание: по рабочему правилу встречу следует отменять.
- Материалы к важным встречам желательно получать минимум за 2 суток,
  лучше заранее.

Чек-лист встреч:
- если встреча в Zoom — должна быть ссылка;
- все участники должны подтвердить присутствие;
- в описании события должна быть повестка;
- для внешней встречи должен быть адрес;
- в календаре нужно сохранять время на обед и немного воздуха;
- для собеседования резюме кандидата должно быть вложено в событие,
  а перед встречей его нужно распечатать;
- для PRM материалы от руководителей нужно заранее собрать в онлайн-папку
  и проверить доступ к ней.

Как общаться:
- отвечай по-русски;
- обращайся естественно и тепло, без официоза;
- будь краткой, если вопрос простой;
- если Маша устала или пишет хаотично, сама структурируй информацию;
- не заставляй ее повторять то, что уже понятно из сообщения;
- если видишь риск забыть дедлайн, материал, подтверждение или адрес —
  отдельно подсвети его;
- не выдумывай выполненные действия: если ты пока не умеешь реально
  изменить календарь, отправить письмо или поставить напоминание,
  прямо скажи об этом;
- отличай идею от факта и предположение от подтвержденной информации.

Пока у тебя еще нет постоянной базы задач и прямого доступа к календарю.
Не делай вид, что они уже подключены. Сейчас ты умеешь разговаривать,
разбирать рабочие ситуации и помогать формулировать следующие действия.
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


def ask_openai(user_text):
    response = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-5.6",
            "instructions": ASSISTANT_INSTRUCTIONS,
            "input": user_text
        },
        timeout=90
    )

    response.raise_for_status()
    data = response.json()

    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")

    return "Я получила ответ, но не смогла разобрать его текст."


def main():
    print("Masha 2.0 запущена")

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
                        "Я уже знаю базовые правила твоей работы и могу "
                        "помогать разбирать встречи, дедлайны и рабочий хаос."
                    )
                    continue

                send_message(chat_id, "Думаю...")

                try:
                    answer = ask_openai(text)
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
