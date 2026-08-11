import os
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_URL = f"https://api.telegram.org/bot{TOKEN}"


def send_message(chat_id, text):
    requests.post(
        f"{API_URL}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text
        },
        timeout=30
    )


def main():
    print("Masha2.0 запущена")

    offset = None

    while True:
        params = {
            "timeout": 30
        }

        if offset is not None:
            params["offset"] = offset

        try:
            response = requests.get(
                f"{API_URL}/getUpdates",
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

                if text == "/start":
                    send_message(
                        chat_id,
                        "Привет! 👋 Я Маша 2.0.\n\n"
                        "Я запущена и уже умею получать твои сообщения."
                    )
                elif text:
                    send_message(
                        chat_id,
                        "Я работаю! 🎉\n\n"
                        f"Ты написала: {text}"
                    )

        except Exception as e:
            print("Ошибка:", e)


if __name__ == "__main__":
    main()
