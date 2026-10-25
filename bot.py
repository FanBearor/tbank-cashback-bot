import os
import re
import html
import time
import requests
from datetime import datetime, timezone, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
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

# Кэш в памяти: { "timestamp": float, "deals": list }
CACHE = {
    "timestamp": 0,
    "deals": []
}

# --- МИНИ-СЕРВЕР ДЛЯ RENDER ---
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

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔥 Акции дня"}],
            [{"text": "📅 Акции месяца"}, {"text": "🛒 Товары с кэшбэком"}]
        ],
        "resize_keyboard": True
    }

def get_percent_inline_keyboard(category_prefix):
    """Категория: 'month' или 'product'"""
    return {
        "inline_keyboard": [
            [
                {"text": "🔥 80% - 100%", "callback_data": f"{category_prefix}_80_100"},
                {"text": "⚡ 50% - 80%", "callback_data": f"{category_prefix}_50_80"}
            ],
            [
                {"text": "📦 До 50% (Любой)", "callback_data": f"{category_prefix}_0_50"}
            ]
        ]
    }

# --- ОТПРАВКА И РЕДАКТИРОВАНИЕ СООБЩЕНИЙ ---
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
        print(f"Ошибка отправки сообщения: {e}")

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=5)
    except Exception:
        pass

# --- ПАРСИНГ И ИЗВЛЕЧЕНИЕ ДАННЫХ ---
def extract_percent(text):
    text_lower = text.lower()
    
    # 1. Специфичные фразы на 100%
    if any(k in text_lower for k in ["100%", "кэшбэк дня", "акция дня", "бесплатно", "возврат 100", "100 процентов"]):
        return 100
        
    # 2. Поиск чисел с %
    found = re.findall(r'(\d{1,3})\s*(?:%|процент)', text_lower)
    percents = []
    for p in found:
        val = int(p)
        if 5 <= val <= 100:
            percents.append(val)
            
    if percents:
        return max(percents)
    
    # 3. Дефолтный кэшбэк если есть слово кэшбэк/скидка, но процент не распознан
    if any(k in text_lower for k in ["кэшбэк", "кешбек", "скидк", "бонус"]):
        return 20
        
    return 0

def fetch_channel_posts_deep(channel, pages=3):
    """Листает ленту канала назад, чтобы достать посты за месяц"""
    all_posts = []
    before_param = ""
    seen_ids = set()

    for _ in range(pages):
        url = f"https://t.me/s/{channel}{before_param}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                break

            content = resp.text
            # Ищем карточки сообщений
            raw_matches = re.findall(
                rf'data-post="{channel}/(\d+)".*?<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                content,
                re.DOTALL
            )
            
            if not raw_matches:
                break

            new_found = 0
            min_id = float("inf")

            for pid_str, text_html in raw_matches:
                pid = int(pid_str)
                min_id = min(min_id, pid)

                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                new_found += 1

                # Очистка текста
                clean_text = re.sub(r'<br\s*/?>', '\n', text_html)
                clean_text = re.sub(r'<[^>]+>', '', clean_text)
                clean_text = html.unescape(clean_text).strip()
                lower_text = clean_text.lower()

                # Если pepperru — фильтруем только Т-Банк / Тинькофф
                if channel == "pepperru":
                    if not any(w in lower_text for w in ["тинькофф", "т-банк", "тбанк", "тиньков"]):
                        continue

                # Ищем дату (datetime в теге time)
                time_match = re.search(rf'data-post="{channel}/{pid}".*?<time datetime="([^"]+)"', content, re.DOTALL)
                post_dt = None
                if time_match:
                    try:
                        post_dt = datetime.fromisoformat(time_match.group(1).replace("Z", "+00:00"))
                    except Exception:
                        pass

                all_posts.append({
                    "id": pid,
                    "channel": channel,
                    "text": clean_text,
                    "percent": extract_percent(clean_text),
                    "datetime": post_dt,
                    "link": f"https://t.me/{channel}/{pid}"
                })

            if min_id != float("inf"):
                before_param = f"?before={min_id}"
            else:
                break

            if new_found == 0:
                break

            time.sleep(0.3)

        except Exception as e:
            print(f"Ошибка парсинга {channel}: {e}")
            break

    return all_posts

def get_all_deals_cached():
    """Возвращает закэшированные акции (кэш обновляется раз в 10 минут)"""
    now = time.time()
    if CACHE["deals"] and (now - CACHE["timestamp"] < 600):
        return CACHE["deals"]

    deals = []
    for ch in CHANNELS:
        deals.extend(fetch_channel_posts_deep(ch, pages=3))

    # Сортируем: сначала самые свежие
    deals.sort(key=lambda x: x["id"], reverse=True)
    CACHE["deals"] = deals
    CACHE["timestamp"] = now
    return deals

# --- ЛОГИКА ФИЛЬТРАЦИИ ПО КАТЕГОРИЯМ ---

def filter_by_percent(deals, mode):
    if mode == "80_100":
        return [d for d in deals if d["percent"] >= 80]
    elif mode == "50_80":
        return [d for d in deals if 50 <= d["percent"] < 80]
    elif mode == "0_50":
        return [d for d in deals if d["percent"] < 50]
    return deals

def handle_day_deals(chat_id):
    deals = get_all_deals_cached()
    tz_msk = timezone(timedelta(hours=3))
    now_msk = datetime.now(tz_msk)

    # Акции дня: либо опубликованы за последние 36 часов, либо содержат слова «дня»
    day_deals = []
    for d in deals:
        is_recent = False
        if d["datetime"]:
            diff = now_msk - d["datetime"].astimezone(tz_msk)
            if diff.total_seconds() <= 36 * 3600:
                is_recent = True
        
        has_day_kw = any(k in d["text"].lower() for k in ["кэшбэк дня", "акция дня", "только сегодня", "сегодня"])
        
        if is_recent or has_day_kw:
            day_deals.append(d)

    # Если совсем пусто (бывает в выходные) — берем 5 последних акций со 100%
    if not day_deals:
        day_deals = [d for d in deals if d["percent"] >= 80][:5]

    day_deals.sort(key=lambda x: x["percent"], reverse=True)

    text = f"🔥 <b>АКТУАЛЬНЫЕ АКЦИИ ДНЯ:</b>\n<i>(найдено: {len(day_deals)})</i>\n\n"
    for d in day_deals[:8]:
        preview = d["text"].split("\n")[0][:110]
        pct = f"{d['percent']}%" if d['percent'] > 0 else "Акция"
        text += f"⚡ <b>[{pct}]</b> {preview}...\n👉 <a href='{d['link']}'>Смотреть в канале</a>\n\n"

    send_msg(text, chat_id, get_main_keyboard())

def send_filtered_deals(chat_id, deals, category_name, mode_name):
    if not deals:
        send_msg(f"😔 В категории <b>{category_name}</b> ({mode_name}) сейчас нет активных акций.", chat_id)
        return

    # Сортируем: максимальный кэшбэк сверху
    deals.sort(key=lambda x: (x["percent"], x["id"]), reverse=True)

    text = f"🎯 <b>{category_name} — {mode_name}:</b>\n\n"
    for d in deals[:7]:
        first_line = d["text"].split("\n")[0][:100]
        text += (
            f"🎁 <b>Кэшбэк {d['percent']}%</b>\n"
            f"{first_line}\n"
            f"👉 <a href='{d['link']}'>Открыть предложение</a>\n\n"
        )

    send_msg(text, chat_id)

# --- ГЛАВНЫЙ ЦИКЛ ОБРАБОТКИ ---
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

                # 1. ОБРАБОТКА НАЖАТИЙ НА ИНЛАЙН-КНОПКИ ВЫБОРА ПРОЦЕНТОВ
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    data = cq.get("data", "")
                    cq_id = cq.get("id")
                    cq_chat_id = cq.get("message", {}).get("chat", {}).get("id")

                    answer_callback(cq_id)

                    all_deals = get_all_deals_cached()

                    # Акции месяца
                    if data.startswith("month_"):
                        mode = data.replace("month_", "")
                        filtered = filter_by_percent(all_deals, mode)
                        names = {"80_100": "80% - 100%", "50_80": "50% - 80%", "0_50": "До 50%"}
                        send_filtered_deals(cq_chat_id, filtered, "Акции месяца", names.get(mode, ""))

                    # Товары с кэшбэком
                    elif data.startswith("product_"):
                        mode = data.replace("product_", "")
                        # Фильтруем товары (продукты, чеки, супермаркеты)
                        product_kws = ["чек", "чеки", "товар", "купи", "вкусвилл", "пятёрочк", "магнит", "лента", "перекресток", "возврат"]
                        product_deals = [d for d in all_deals if any(k in d["text"].lower() for k in product_kws) or d["percent"] > 0]
                        filtered = filter_by_percent(product_deals, mode)
                        names = {"80_100": "80% - 100%", "50_80": "50% - 80%", "0_50": "До 50%"}
                        send_filtered_deals(cq_chat_id, filtered, "Товары с кэшбэком", names.get(mode, ""))

                    continue

                # 2. ОБРАБОТКА ТЕКСТОВЫХ КОМАНД И НИЖНИХ КНОПОК
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if not chat_id:
                    continue

                if text == "/start":
                    send_msg(
                        "👋 <b>Привет!</b> Я мониторю все актуальные кэшбэки Т-Банка за этот месяц.\n\n"
                        "Выбирай нужный раздел внизу 👇",
                        chat_id=chat_id,
                        reply_markup=get_main_keyboard()
                    )

                elif "Акции дня" in text:
                    send_msg("🔎 Собираю свежие акции дня...", chat_id=chat_id)
                    handle_day_deals(chat_id)

                elif "Акции месяца" in text:
                    send_msg(
                        "📅 <b>Акции месяца:</b> выбери желаемый процент кэшбэка:",
                        chat_id=chat_id,
                        reply_markup=get_percent_inline_keyboard("month")
                    )

                elif "Товары с кэшбэком" in text:
                    send_msg(
                        "🛒 <b>Товары и чеки:</b> выбери процент возврата:",
                        chat_id=chat_id,
                        reply_markup=get_percent_inline_keyboard("product")
                    )

        except Exception as e:
            print(f"Ошибка в цикле getUpdates: {e}")
            time.sleep(2)

if __name__ == "__main__":
    print("Бот запущен с глубоким парсингом за месяц!")
    # Фоновый сервер для Render
    t_web = Thread(target=run_web_server, daemon=True)
    t_web.start()
    # Цикл бота
    bot_updates_loop()
