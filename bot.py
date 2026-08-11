import os
import time
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_URL = "https://api.openai.com/v1/responses"


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=30
    ).raise_for_status()


def ask_openai(user_text):
    response = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "gpt-5.6",
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Ты Маша 2.0 — личный ассистент пользователя. "
                        "Отвечай по-русски, естественно, дружелюбно и по делу. "
                        "Пока ты работаешь в тестовом режиме."
                    )
                },
                {
                    "role": "user",
                    "content": user_text
                }
            ]
        },
        timeout=60
    )

    response.raise_for_status()
    data = response.json()

    # Responses API возвращает ответ в output
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    return content.get("text", "")

    return "Я получила ответ от ИИ, но не смогла разобрать текст."


def main():
    print("Masha 2.0 запущена с OpenAI")

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
                        "Привет! 👋 Я Маша 2.0.\n\n"
                        "Теперь у меня подключён ИИ. Можешь писать мне обычными словами."
                    )
                    continue

                send_message(chat_id, "Думаю…")

                try:
                    answer = ask_openai(text)
                    send_message(chat_id, answer)

                except requests.HTTPError as e:
                    print("OpenAI HTTP error:", e.response.status_code, e.response.text)

                    if e.response.status_code == 429:
                        send_message(
                            chat_id,
                            "OpenAI пока не дал мне ответ. "
                            "Скорее всего, нужно пополнить API-баланс."
                        )
                    else:
                        send_message(
                            chat_id,
                            "Я дошла до OpenAI, но получила ошибку API. "
                            "Посмотрим лог и быстро исправим."
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
