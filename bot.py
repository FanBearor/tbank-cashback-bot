import os
import re
import html
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- ТОКЕН И ID ИЗ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в переменных окружения!")

CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",
    "pepperru"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# --- ФЕЙК СЕРВЕР ДЛЯ RENDER ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()

# --- 3 НОВЫЕ КНОПКИ ---
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔥 Акции дня"}],
            [{"text": "📅 Акции месяца"}],
            [{"text": "🛒 Товары с кэшбэком"}]
        ],
        "resize_keyboard": True
    }

def send_msg(text, chat_id=None, reply_markup=None):
    cid = chat_id or CHAT_ID
    if not cid:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def extract_percent(text):
    found = re.findall(r'(\d{1,3})\s*(?:%|процент)', text.lower())
    percents = []
    for p in found:
        val = int(p)
        if 5 <= val <= 100:
            percents.append(val)
    if percents:
        return max(percents)
    if any(k in text.lower() for k in ["кэшбэк дня", "акция дня", "бесплатно", "100%"]):
        return 100
    return 0

def fetch_recent_deals():
    deals = []
    tz_msk = timezone(timedelta(hours=3))
    today_date = datetime.now(tz_msk).date()

    for channel in CHANNELS:
        url = f"https://t.me/s/{channel}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue

            content = resp.text
            raw_posts = re.findall(
                rf'<div class="tgme_widget_message\s+js-widget_message"[^>]*data-post="{channel}/(\d+)"(.*?)<div class="tgme_widget_message_footer',
                content,
                re.DOTALL
            )

            for pid, post_html in raw_posts:
                text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', post_html, re.DOTALL)
                if not text_match:
                    continue

                clean_text = re.sub(r'<br\s*/?>', '\n', text_match.group(1))
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                clean_text = html.unescape(clean_text).strip()
                lower_text = clean_text.lower()

                if channel == "pepperru":
                    if not any(w in lower_text for w in ["тинькофф", "т-банк", "тбанк", "тиньков"]):
                        continue

                time_match = re.search(r'<time datetime="([^"]+)"', post_html)
                post_dt = None
                if time_match:
                    try:
                        post_dt = datetime.fromisoformat(time_match.group(1).replace("Z", "+00:00")).astimezone(tz_msk)
                    except Exception:
                        pass

                is_today = (post_dt.date() == today_date) if post_dt else False
                percent = extract_percent(clean_text)

                deals.append({
                    "id": pid,
                    "channel": channel,
                    "text": clean_text,
                    "percent": percent,
                    "is_today": is_today,
                    "datetime": post_dt,
                    "link": f"https://t.me/{channel}/{pid}"
                })

        except Exception as e:
            print(f"Ошибка канала {channel}: {e}")

    return deals

def handle_day_deals(chat_id):
    deals = fetch_recent_deals()
    day_deals = [d for d in deals if d["is_today"] or any(k in d["text"].lower() for k in ["кэшбэк дня", "акция дня", "только сегодня"])]

    if not day_deals:
        send_msg("🔥 <b>На сегодня акций пока нет</b> или каналы еще не выложили их.", chat_id)
        return

    day_deals.sort(key=lambda x: x["percent"], reverse=True)
    text = f"🔥
