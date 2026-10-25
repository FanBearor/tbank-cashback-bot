import html
import os
import re
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
import requests
from dotenv import load_dotenv

load_dotenv()

# Безопасное получение токена и ID чата
BOT_TOKEN = os.getenv("BOT_TOKEN", "8749854506:AAEv_vWyqT_rVUI4dIBMVidlyt_BEglKn74")
CHAT_ID = os.getenv("CHAT_ID", "936459900")
PORT = int(os.getenv("PORT", 10000))

# Московское время (UTC+3) для точного определения текущего дня
MSK = timezone(timedelta(hours=3))

# Интервал автоматической фоновой проверки (2 часа)
CHECK_INTERVAL_SECONDS = 2 * 3600

# Источники для мониторинга
CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",  # Т-Банк акции и кэшбэк дня
    "pepperru",                         # Pepper.ru (скидки и выгода)
    "halyavshiki",                      # Халявщики (акции, чеки, суперскидки)
    "promokody"                         # Промокоды, доставка, банки и кэшбэк
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Кэш постов
cached_posts = []
last_cache_update = 0

# ==================== ВЕБ-СЕРВЕР ДЛЯ RENDER (HEALTHCHECK) ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK: T-Bank & Pepper Cashback Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        print(f"🌐 HTTP-сервер для Render запущен на порту {PORT}")
        server.serve_forever()
    except Exception as e:
        print(f"Предупреждение по HTTP серверу: {e}")

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def clean_html(raw_html):
    """Очищает HTML-теги и декодирует спецсимволы"""
    text = re.sub(r'<br\s*/?>', '\n', raw_html)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return text.strip()

def format_relative_time(pub_dt, now_msk):
    """Человекочитаемое время публикации: '2 часа назад', '1 день назад'"""
    if not pub_dt:
        return "недавно"
    pub_msk = pub_dt.astimezone(MSK)
    diff = now_msk - pub_msk
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    hours = seconds // 3600
    days = seconds // 86400

    if minutes < 60:
        return f"{minutes} мин. назад"
    elif hours < 24:
        if hours % 10 == 1 and hours % 100 != 11:
            h_str = "час"
        elif 2 <= hours % 10 <= 4 and not (12 <= hours % 100 <= 14):
            h_str = "часа"
        else:
            h_str = "часов"
        return f"{hours} {h_str} назад"
    else:
        if days % 10 == 1 and days % 100 != 11:
            d_str = "день"
        elif 2 <= days % 10 <= 4 and not (12 <= days % 100 <= 14):
            d_str = "дня"
        else:
            d_str = "дней"
        return f"{days} {d_str} назад"

def extract_percent(text):
    """
    Определяет процент выгоды/кэшбэка:
    1. 100% за халяву, кэшбэк дня, за 1₽ или 0₽
    2. Явный процент: 50%, 80% и т.д.
    3. Разница цен из Pepper: 'за 28₽ (везде 74₽)' -> 62%
    """
    tl = text.lower()

    if any(k in tl for k in ["за 1₽", "за 1 ₽", "за 0₽", "за 0 ₽", "бесплатно", "кэшбэк дня", "100% халява", "100%"]):
        return 100

    found = re.findall(r'(\d{1,3})\s*(?:%|процент)', tl)
    percents = [int(p) for p in found if 5 <= int(p) <= 100]
    if percents:
        return max(percents)

    m = re.search(r'за\s+([\d\s]+)\s*₽.*?везде:?\s*([\d\s]+)', tl)
    if m:
        try:
            p_cur = int(m.group(1).replace(' ', ''))
            p_old = int(m.group(2).replace(' ', ''))
            if p_old > p_cur > 0:
                pct = round((p_old - p_cur) / p_old * 100)
                if 5 <= pct <= 100:
                    return pct
        except Exception:
            pass

    return 0

def categorize_post(text):
    """
    Категории:
    1. 'day'   - Акции дня (кэшбэк дня, только сегодня)
    2. 'goods' - Товары с кэшбэком и скидками (чеки, конкретные товары, маркетплейсы)
    3. 'week'  - Акции недели (общие скидки, банки, промокоды)
    """
    tl = text.lower()

    if any(w in tl for w in ["кэшбэк дня", "акция дня", "только сегодня", "только 1 день", "только 24 часа"]):
        return "day"
    if re.search(r'только\s+\d{1,2}\.\d{1,2}', tl):
        return "day"

    if any(w in tl for w in [
        "товары с кэшбэком", "товар с кэшбэком", "сканируй чек", "за чек", "кэшбэк за покупку товара",
        "купи", "товар", "пачка", "банка", "штук", "крем", "кастрюля", "напиток", "сок", "шапка",
        "холодильник", "пылесос", "wildberries", "ozon", "яндекс маркет", "самокат", "лента", "перекрёсток", "магнит"
    ]):
        return "goods"

    return "week"

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔥 Акции дня"}, {"text": "📅 Акции недели (7 дней)"}],
            [{"text": "🛒 Товары с кэшбэком"}, {"text": "⚡ Все акции за неделю"}],
            [{"text": "🔄 Обновить базу"}, {"text": "ℹ️ Помощь"}]
        ],
        "resize_keyboard": True
    }

def get_tier_inline_keyboard(category_prefix):
    """Инлайн-кнопки выбора процента кэшбэка"""
    return {
        "inline_keyboard": [
            [
                {"text": "🥇 80% — 100%", "callback_data": f"{category_prefix}_80_100"},
                {"text": "🥈 50% — 80%", "callback_data": f"{category_prefix}_50_80"}
            ],
            [
                {"text": "🥉 До 50%", "callback_data": f"{category_prefix}_0_50"},
                {"text": "📋 Все сразу (по убыванию %)", "callback_data": f"{category_prefix}_all"}
            ]
        ]
    }

# ==================== TELEGRAM API ====================
def send_msg(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=12)
        return r.json()
    except Exception as e:
        print(f"Ошибка sendMessage: {e}")
        return None

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=6)
    except Exception:
        pass

# ==================== ПАРСИНГ ====================
def fetch_channel_posts_week(channel_name, max_pages=4):
    url = f"https://t.me/s/{channel_name}"
    items = []
    now_msk = datetime.now(MSK)
    week_limit = now_msk - timedelta(days=7)

    for _ in range(max_pages):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                break

            chunks = resp.text.split('class="tgme_widget_message_wrap')
            if len(chunks) <= 1:
                break

            first_id = None
            oldest_on_page = None

            for c in chunks[1:]:
                post_m = re.search(r'data-post="([^"]+)"', c)
                time_m = re.search(r'datetime="([^"]+)"', c)
                text_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', c, re.DOTALL)

                if not post_m or not text_m or not time_m:
                    continue

                try:
                    p_id = int(post_m.group(1).split('/')[-1])
                    if first_id is None or p_id < first_id:
                        first_id = p_id
                except Exception:
                    pass

                pub_dt = None
                try:
                    dt_str = time_m.group(1).replace("Z", "+00:00")
                    pub_dt = datetime.fromisoformat(dt_str)
                    pub_msk = pub_dt.astimezone(MSK)
                    if oldest_on_page is None or pub_msk < oldest_on_page:
                        oldest_on_page = pub_msk
                except Exception:
                    continue

                # Отсекаем посты старше 7 дней при сборе
                if pub_msk < week_limit or pub_msk > now_msk:
                    continue

                clean_txt = clean_html(text_m.group(1))

                items.append({
                    "post_ref": post_m.group(1),
                    "text": clean_txt,
                    "datetime": pub_dt,
                    "channel": channel_name,
                    "percent": extract_percent(clean_txt),
                    "category": categorize_post(clean_txt)
                })

            if oldest_on_page and oldest_on_page < week_limit:
                break

            if first_id:
                url = f"https://t.me/s/{channel_name}?before={first_id}"
            time.sleep(0.3)

        except Exception as e:
            print(f"Ошибка парсинга @{channel_name}: {e}")
            break

    return items

def update_posts_cache():
    """Сбор постов за последнюю неделю"""
    global cached_posts, last_cache_update
    print(f"[{datetime.now(MSK).strftime('%H:%M:%S')}] Сбор акций за последние 7 дней...")
    all_items = []

    for ch in CHANNELS:
        all_items.extend(fetch_channel_posts_week(ch, max_pages=4))

    seen = set()
    unique = []
    for item in all_items:
        if item["post_ref"] not in seen:
            seen.add(item["post_ref"])
            unique.append(item)

    cached_posts = unique
    last_cache_update = time.time()
    print(f"✅ База обновлена! Актуальных акций в памяти: {len(cached_posts)}")

def get_filtered_posts(category=None, tier="all"):
    """
    СТРОГАЯ ФИ
