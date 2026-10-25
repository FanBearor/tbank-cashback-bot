import os
import re
import html
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
# --- КОНФИГУРАЦИЯ ИЗ ENVIRONMENT (БЕЗ УТЕЧЕК) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения BOT_TOKEN не установлена! Задай ее в настройках Render.")
CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",
    "pepperru"
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
# --- МИНИ-СЕРВЕР ДЛЯ RENDER (чтобы веб-сервис не падал) ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
def run_web_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    server.serve_forever()
# --- КЛАВИАТУРА ИЗ 3 КНОПОК ---
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
# --- ПАРСИНГ И АНАЛИЗ ПОСТОВ ---
def extract_percent(text):
    """Определяет процент кэшбэка"""
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
    """Собирает и фильтрует актуальные посты из каналов"""
    deals = []
    # Московское время (UTC+3)
    tz_msk = timezone(timedelta(hours=3))
    today_date = datetime.now(tz_msk).date()
    for channel in CHANNELS:
        url = f"https://t.me/s/{channel}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            content = resp.text
            # Ищем все сообщения в ленте
            raw_posts = re.findall(
                rf'<div class="tgme_widget_message\s+js-widget_message"[^>]*data-post="{channel}/(\d+)"(.*?)<div class="tgme_widget_message_footer',
                content,
                re.DOTALL
            )
            for pid, post_html in raw_posts:
                text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', post_html, re.DOTALL)
                if not text_match:
                    continue
                raw_text = text_match.group(1)
                clean_text = re.sub(r'<br\s*/?>', '\n', raw_text)
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                clean_text = html.unescape(clean_text).strip()
                lower_text = clean_text.lower()
                # Если канал Pepper — только посты про Т-Банк
                if channel == "pepperru":
                    if not any(w in lower_text for w in ["тинькофф", "т-банк", "тбанк", "тиньков"]):
                        continue
                # Извлекаем дату поста
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
            print(f"Ошибка сбора постов {channel}: {e}")
    return deals
# --- ОБРАБОТЧИКИ 3 КАТЕГОРИЙ ---
def handle_day_deals(chat_id):
    deals = fetch_recent_deals()
    day_deals = [d for d in deals if d["is_today"] or any(k in d["text"].lower() for k in ["кэшбэк дня", "акция дня", "только сегодня"])]
    if not day_deals:
        send_msg("🔥 <b>На сегодня акций пока нет</b> или они еще не опубликованы в каналах.", chat_id)
        return
    # Сортируем: сначала с наибольшим процентом
    day_deals.sort(key=lambda x: x["percent"], reverse=True)
    text = f"🔥 <b>АКТУАЛЬНЫЕ АКЦИИ ДНЯ:</b>\n<i>(найдено: {len(day_deals)})</i>\n\n"
    for d in day_deals[:10]:
        preview = d["text"].split("\n")[0][:120]
        badge = f"⚡ {d['percent']}%" if d['percent'] > 0 else "💥 Акция"
        text += f"• <b>{badge}</b>: {preview}...\n👉 <a href='{d['link']}'>Смотреть акцию</a>\n\n"
    send_msg(text, chat_id)
def handle_month_deals(chat_id):
    deals = fetch_recent_deals()
    if not deals:
        send_msg("📅 Акций за этот месяц не найдено.", chat_id)
        return
    # Ранжирование по процентам
    top_100 = [d for d in deals if d["percent"] >= 80]
    mid_deals = [d for d in deals if 50 <= d["percent"] < 80]
    other_deals = [d for d in deals if 0 < d["percent"] < 50]
    text = "📅 <b>АКЦИИ МЕСЯЦА (С РАНЖИРОВАНИЕМ):</b>\n\n"
    if top_100:
        text += "🔥 <b>Кэшбэк 80% - 100%:</b>\n"
        for d in top_100[:5]:
            preview = d["text"].split("\n")[0][:90]
            text += f"• {preview} 👉 <a href='{d['link']}'>Открыть</a>\n"
        text += "\n"
    if mid_deals:
        text += "⚡ <b>Кэшбэк 50% - 80%:</b>\n"
        for d in mid_deals[:5]:
            preview = d["text"].split("\n")[0][:90]
            text += f"• [{d['percent']}%] {preview} 👉 <a href='{d['link']}'>Открыть</a>\n"
        text += "\n"
    if other_deals:
        text += "📦 <b>Кэшбэк до 50%:</b>\n"
        for d in other_deals[:4]:
            preview = d["text"].split("\n")[0][:80]
            text += f"• [{d['percent']}%] {preview} 👉 <a href='{d['link']}'>Открыть</a>\n"
    send_msg(text, chat_id)
def handle_product_deals(chat_id):
    deals = fetch_recent_deals()
    # Ищем чеки, продукты, супермаркеты, конкретные товары
    keywords = ["чек", "чеки", "товар", "купи", "вкусвилл", "пятёрочк", "магнит", "лента", "перекресток", "возврат"]
    products = [d for d in deals if any(k in d["text"].lower() for k in keywords) or d["percent"] > 0]
    if not products:
        send_msg("🛒 Товаров с кэшбэком сейчас не найдено.", chat_id)
        return
    # Ранжирование по убыванию процента
    products.sort(key=lambda x: x["percent"], reverse=True)
    text = "🛒 <b>ТОВАРЫ С КЭШБЭКОМ (ПО УБЫВАНИЮ %):</b>\n\n"
    for d in products[:10]:
        first_line = d["text"].split("\n")[0][:100]
        pct = f"<b>{d['percent']}%</b>" if d['percent'] > 0 else "Скидка"
        text += f"🎁 {pct} — {first_line}\n👉 <a href='{d['link']}'>Подробнее</a>\n\n"
    send_msg(text, chat_id)
# --- ЦИКЛ ОБРАБОТКИ TELEGRAM UPDATES ---
def bot_updates_loop():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if not res.get("ok"):
                continue
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")
                if not chat_id:
                    continue
                if text == "/start":
                    send_msg(
                        "👋 Привет! Выбирай категорию кнопками ниже, и я покажу актуальные акции на данный момент:",
                        chat_id=chat_id,
                        reply_markup=get_main_keyboard()
                    )
                elif "Акции дня" in text:
                    send_msg("🔍 Ищу актуальные акции дня...", chat_id=chat_id)
                    handle_day_deals(chat_id)
                elif "Акции месяца" in text:
                    send_msg("🔍 Собираю акции месяца с ранжированием...", chat_id=chat_id)
                    handle_month_deals(chat_id)
                elif "Товары с кэшбэком" in text:
                    send_msg("🔍 Сортирую товары с кэшбэком...", chat_id=chat_id)
                    handle_product_deals(chat_id)
        except Exception as e:
            print(f"Ошибка в getUpdates: {e}")
if __name__ == "__main__":
    print("Бот запущен!")
    
    # Запускаем фейковый HTTP-сервер для Render в отдельном потоке
    t_web = Thread(target=run_web_server, daemon=True)
    t_web.start()
    # Запускаем цикл телеграма
    bot_updates_loop()
