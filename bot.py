import os
import re
import time
import requests
BOT_TOKEN = os.getenv("BOT_TOKEN", "8749854506:AAHT_Uo73Rw9Mn4BHoUaQEj6KtqapDfPOHo")
CHAT_ID = os.getenv("CHAT_ID", "936459900")
CHANNEL_URL = "https://t.me/s/t_bank_tinkoff_cashback_keshbek"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
KEYWORDS = ["100%", "сто процентов", "акция дня", "кэшбэк дня", "кэшбек дня", "кэшбэк 100%"]
def send_alert(text, post_link):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    message = (
        f"🔥 <b>НАЙДЕНА АКЦИЯ СО 100% КЭШБЭКОМ!</b>\n\n"
        f"{text}\n\n"
        f"👉 <a href='{post_link}'>Открыть пост в канале</a>"
    )
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка при отправке: {e}")
def check_channel(last_seen_id):
    try:
        response = requests.get(CHANNEL_URL, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            print(f"Ошибка запроса к каналу: {response.status_code}")
            return last_seen_id
        html = response.text
        posts = re.findall(r'data-post="t_bank_tinkoff_cashback_keshbek/(\d+)"', html)
        if not posts:
            return last_seen_id
        post_ids = sorted([int(p) for p in set(posts)])
        
        if last_seen_id is None:
            print(f"Первый запуск. Последний пост: {post_ids[-1]}")
            return post_ids[-1]
        for pid in post_ids:
            if pid > last_seen_id:
                pattern = rf'data-post="t_bank_tinkoff_cashback_keshbek/{pid}".*?<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>'
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    raw_text = match.group(1)
                    clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
                    lower_text = clean_text.lower()
                    if any(k in lower_text for k in KEYWORDS):
                        post_link = f"https://t.me/t_bank_tinkoff_cashback_keshbek/{pid}"
                        send_alert(clean_text, post_link)
                        print(f"Найдена и отправлена акция из поста {pid}")
                last_seen_id = pid
        return last_seen_id
    except Exception as e:
        print(f"Ошибка парсинга: {e}")
        return last_seen_id
if __name__ == "__main__":
    print("Бот мониторинга запущен и ожидает новые акции...")
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": "✅ Бот мониторинга кэшбэка 100% запущен в облаке и работает!"}
        )
    except:
        pass
    last_id = None
    last_id = check_channel(last_id)
    while True:
        time.sleep(60)
        last_id = check_channel(last_id)
