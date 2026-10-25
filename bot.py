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

# Безопасное получение токена и ID чата из переменных Render
BOT_TOKEN = os.getenv("BOT_TOKEN", "8749854506:AAEv_vWyqT_rVUI4dIBMVidlyt_BEglKn74")
CHAT_ID = os.getenv("CHAT_ID", "936459900")
PORT = int(os.getenv("PORT", 10000))

# Интервал автоматической фоновой проверки (3 часа)
CHECK_INTERVAL_SECONDS = 3 * 3600

# Источники для сбора (Telegram-каналы, Pepper.ru и топовые агрегаторы акций/кэшбэка)
CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",  # Официальный и партнерский кэшбэк Т-Банка
    "pepperru",                         # Pepper.ru — скидки, акции и кэшбэки
    "halyavshiki",                      # Халявщики — акции, чеки, суперскидки
    "promokody"                         # Промокоды, доставка, банки и кэшбэк
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# Кэш постов (за последние 7 дней)
cached_posts = []
last_cache_update = 0

# ==================== DUMMY HTTP SERVER ДЛЯ RENDER ====================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK: T-Bank Cashback & Pepper Bot is running!")

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

def extract_percent(text):
    """
    Определяет процент выгоды/кэшбэка:
    1. 100% за халяву, кэшбэк дня, за 1₽ или 0₽
    2. Явный процент: 50%, 80% и т.д.
    3. Разница цен из Pepper: "за 28₽ (везде 74₽)" -> 62%
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
    1. 'day'   - Акции дня (действуют сегодня / кэшбэк дня)
    2. 'goods' - Товары с кэшбэком (товары, чеки, маркетплейсы, магазины)
    3. 'week'  - Акции недели (общие предложения, банковские спецкатегории, сервисы)
    """
    tl = text.lower()

    # Акции дня
    if any(w in tl for w in ["кэшбэк дня", "акция дня", "только сегодня", "только 1 день", "только 24 часа"]):
        return "day"
    if re.search(r'только\s+\d{1,2}\.\d{1,2}', tl):
        return "day"

    # Товары с кэшбэком и скидками
    if any(w in tl for w in [
        "товары с кэшбэком", "товар с кэшбэком", "сканируй чек", "за чек", "кэшбэк за покупку товара",
        "купи", "товар", "пачка", "банка", "штук", "крем", "кастрюля", "напиток", "сок", "шапка",
        "холодильник", "пылесос", "wildberries", "ozon", "яндекс маркет", "самокат", "лента", "перекрёсток", "магнит"
    ]):
        return "goods"

    return "week"

def is_post_active_week(text, pub_datetime):
    """
    Проверяет активность акции в пределах недели (7 дней):
    - Акции дня: проверяем, что сегодня или за последние 36 часов.
    - Акции с дедлайном "до ДД.ММ" / "по ДД.ММ": если срок прошел — отсекаем.
    - Все остальные посты: строго не старше 7 дней!
    """
    now = datetime.now(timezone.utc)
    category = categorize_post(text)

    # Акции дня: сегодня или вчера (в пределах 36 часов)
    if category == "day":
        date_match = re.search(r'только\s+(\d{1,2})\.(\d{1,2})', text.lower())
        if date_match:
            d, m = int(date_match.group(1)), int(date_match.group(2))
            if now.day == d and now.month == m:
                return True
        if pub_datetime:
            return (now - pub_datetime).total_seconds() <= 36 * 3600
        return False

    # Проверка срока действия "до ДД.ММ"
    until_match = re.search(r'(?:до|по)\s+(\d{1,2})\.(\d{1,2})', text.lower())
    if until_match:
        try:
            d, m = int(until_match.group(1)), int(until_match.group(2))
            year = now.year
            if m < now.month and now.month == 12:
                year += 1
            expiry_date = datetime(year, m, d, 23, 59, 59, tzinfo=timezone.utc)
            if now > expiry_date:
                return False
        except Exception:
            pass

    # Ограничение по диапазону: строго за последние 7 дней (неделя)
    if pub_datetime:
        if (now - pub_datetime).days > 7:
            return False

    return True

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🔥 Акции дня"}, {"text": "📅 Акции недели"}],
            [{"text": "🛒 Товары с кэшбэком"}, {"text": "⚡ Все акции сразу"}],
            [{"text": "🔄 Обновить базу"}, {"text": "ℹ️ Помощь"}]
        ],
        "resize_keyboard": True
    }

def get_tier_inline_keyboard(category_prefix):
    """Инлайн-кнопки для ранжирования по процентам"""
    return {
        "inline_keyboard": [
            [
                {"text": "🥇 80% — 100%", "callback_data": f"{category_prefix}_80_100"},
                {"text": "🥈 50% — 80%", "callback_data": f"{category_prefix}_50_80"}
            ],
            [
                {"text": "🥉 До 50%", "callback_data": f"{category_prefix}_0_50"},
                {"text": "📋 Все сразу (по убыванию)", "callback_data": f"{category_prefix}_all"}
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

# ==================== СБОР ДАННЫХ (НЕДЕЛЬНЫЙ ДИАПАЗОН) ====================
def fetch_channel_posts_week(channel_name, max_pages=4):
    """
    Глубокий парсинг канала в глубь до 7 дней назад
    """
    url = f"https://t.me/s/{channel_name}"
    items = []
    now = datetime.now(timezone.utc)
    week_limit = now - timedelta(days=7)

    for _ in range(max_pages):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12)
            if resp.status_code != 200:
                break

            chunks = resp.text.split('class="tgme_widget_message_wrap')
            if len(chunks) <= 1:
                break

            first_id = None
            oldest_dt = None

            for c in chunks[1:]:
                post_m = re.search(r'data-post="([^"]+)"', c)
                time_m = re.search(r'datetime="([^"]+)"', c)
                text_m = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', c, re.DOTALL)

                if not post_m or not text_m:
                    continue

                try:
                    p_id = int(post_m.group(1).split('/')[-1])
                    if first_id is None or p_id < first_id:
                        first_id = p_id
                except Exception:
                    pass

                pub_dt = None
                if time_m:
                    try:
                        dt_str = time_m.group(1).replace("Z", "+00:00")
                        pub_dt = datetime.fromisoformat(dt_str)
                        oldest_dt = pub_dt
                    except Exception:
                        pass

                clean_txt = clean_html(text_m.group(1))

                # Если дата старше недели — пропускаем
                if pub_dt and pub_dt < week_limit:
                    continue

                items.append({
                    "post_ref": post_m.group(1),
                    "text": clean_txt,
                    "datetime": pub_dt,
                    "channel": channel_name,
                    "percent": extract_percent(clean_txt),
                    "category": categorize_post(clean_txt)
                })

            # Если на странице посты уже старше 7 дней — дальше в прошлое листать нет смысла
            if oldest_dt and oldest_dt < week_limit:
                break

            if first_id:
                url = f"https://t.me/s/{channel_name}?before={first_id}"
            time.sleep(0.3)

        except Exception as e:
            print(f"Ошибка парсинга @{channel_name}: {e}")
            break

    return items

def update_posts_cache():
    """Сбор свежих акций за неделю со всех источников"""
    global cached_posts, last_cache_update
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Сбор акций за неделю из источников (Pepper, Т-Банк и др.)...")
    all_items = []

    for ch in CHANNELS:
        all_items.extend(fetch_channel_posts_week(ch, max_pages=4))

    # Удаление дубликатов
    seen = set()
    unique = []
    for item in all_items:
        if item["post_ref"] not in seen:
            seen.add(item["post_ref"])
            unique.append(item)

    cached_posts = unique
    last_cache_update = time.time()
    print(f"✅ Сбор завершен! Всего актуальных акций за неделю: {len(cached_posts)}")

def get_filtered_posts(category=None, tier="all"):
    """
    Фильтрация и ранжирование акций за неделю:
    - tier='80_100': от 80% до 100%
    - tier='50_80': от 50% до 80%
    - tier='0_50': до 50%
    - tier='all': все акции с сортировкой по проценту (сначала жирные)
    """
    result = []
    for p in cached_posts:
        if not is_post_active_week(p["text"], p["datetime"]):
            continue

        if category and p["category"] != category:
            continue

        pct = p["percent"]
        if tier == "80_100" and pct < 80:
            continue
        elif tier == "50_80" and not (50 <= pct < 80):
            continue
        elif tier == "0_50" and pct >= 50:
            continue

        result.append(p)

    # Ранжирование: сначала максимальный процент, затем новизна
    result.sort(
        key=lambda x: (x["percent"], x["datetime"] or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True
    )
    return result

def format_card(p):
    """Красивое оформление карточки акции"""
    cat = p["category"]
    pct = p["percent"]
    ch = p["channel"]

    cat_title = {
        "day": "🔥 АКЦИЯ ДНЯ",
        "goods": "🛒 ТОВАР С КЭШБЭКОМ",
        "week": "📅 АКЦИЯ НЕДЕЛИ"
    }.get(cat, "⚡ ВЫГОДНОЕ ПРЕДЛОЖЕНИЕ")

    pct_badge = f" • <b>{pct}% Выгода/Кэшбэк</b>" if pct > 0 else ""
    date_str = p["datetime"].strftime("%d.%m.%Y") if p["datetime"] else ""

    header = f"<b>{cat_title}</b>{pct_badge}\n<i>Опубликовано: {date_str} (Источник: @{ch})</i>\n\n"
    source = f"\n\n👉 <a href='https://t.me/{p['post_ref']}'>Открыть акцию в источнике</a>"

    max_len = 3900 - len(header) - len(source)
    txt = p["text"]
    if len(txt) > max_len:
        txt = txt[:max_len] + "..."

    return header + txt + source

def send_batch(chat_id, posts, empty_text, limit=6):
    if not posts:
        send_msg(chat_id, empty_text, get_main_keyboard())
        return

    send_msg(chat_id, f"🔎 Найдено предложений: <b>{len(posts)}</b>", get_main_keyboard())
    for p in posts[:limit]:
        send_msg(chat_id, format_card(p))
        time.sleep(0.3)

# ==================== ФОНОВЫЙ МОНИТОРИНГ ====================
def monitor_loop():
    """Фоновый сбор раз в 3 часа с автоуведомлением о новых акциях дня"""
    update_posts_cache()
    known_ids = {p["post_ref"] for p in cached_posts}

    while True:
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
            prev_ids = set(known_ids)
            update_posts_cache()

            for p in cached_posts:
                if p["post_ref"] not in prev_ids:
                    known_ids.add(p["post_ref"])
                    # Новая акция дня отправляется сразу
                    if p["category"] == "day" and is_post_active_week(p["text"], p["datetime"]):
                        send_msg(CHAT_ID, "🚨 <b>НОВАЯ АКЦИЯ ДНЯ!</b>\n\n" + format_card(p))
        except Exception as e:
            print(f"Ошибка в monitor_loop: {e}")
            time.sleep(30)

# ==================== ОБРАБОТКА ОБНОВЛЕНИЙ И КНОПОК ====================
def bot_updates_loop():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if not res.get("ok"):
                time.sleep(3)
                continue

            for upd in res.get("result", []):
                offset = upd["update_id"] + 1

                # 1. Обработка инлайн-кнопок ранжирования (Callback Query)
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cq_id = cq["id"]
                    cq_data = cq.get("data", "")
                    chat_id = cq.get("message", {}).get("chat", {}).get("id")
                    answer_callback(cq_id)

                    if cq_data.startswith("week_"):
                        tier = cq_data.replace("week_", "")
                        posts = get_filtered_posts(category="week", tier=tier)
                        tier_names = {"80_100": "80% — 100%", "50_80": "50% — 80%", "0_50": "до 50%", "all": "все"}
                        send_msg(chat_id, f"📅 <b>Акции недели с кэшбэком {tier_names.get(tier, tier)}:</b>")
                        send_batch(chat_id, posts, f"😔 В диапазоне {tier_names.get(tier)} акций за неделю не найдено.")

                    elif cq_data.startswith("goods_"):
                        tier = cq_data.replace("goods_", "")
                        posts = get_filtered_posts(category="goods", tier=tier)
                        tier_names = {"80_100": "80% — 100%", "50_80": "50% — 80%", "0_50": "до 50%", "all": "все"}
                        send_msg(chat_id, f"🛒 <b>Товары с кэшбэком {tier_names.get(tier, tier)}:</b>")
                        send_batch(chat_id, posts, f"😔 В диапазоне {tier_names.get(tier)} товаров с кэшбэком не найдено.")
                    continue

                # 2. Обработка текстовых сообщений и меню
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if not chat_id or not text:
                    continue

                if text == "/start":
                    start_text = (
                        "👋 <b>Привет! Бот кэшбэка и акций обновлен!</b>\n\n"
                        "🔍 <b>Диапазон поиска:</b> за последнюю <b>неделю</b>.\n"
                        "🌐 <b>Источники:</b> Т-Банк, Pepper.ru, Халявщики, Промокоды.\n\n"
                        "📂 <b>Разделы:</b>\n"
                        "1️⃣ <b>🔥 Акции дня</b> — всё, что действует сегодня/сейчас.\n"
                        "2️⃣ <b>📅 Акции недели</b> — с выбором ранжирования по % (80-100%, 50-80%, 0-50%).\n"
                        "3️⃣ <b>🛒 Товары с кэшбэком</b> — чеки и товары со скидками и кэшбэком (с ранжированием).\n\n"
                        "Выбирай кнопку внизу 👇"
                    )
                    send_msg(chat_id, start_text, get_main_keyboard())

                elif "Акции дня" in text:
                    posts = get_filtered_posts(category="day", tier="all")
                    if not posts:
                        posts = [p for p in cached_posts if p.get("datetime") and (datetime.now(timezone.utc) - p["datetime"]).total_seconds() <= 36*3600]
                    send_batch(
                        chat_id,
                        posts,
                        "😔 <b>На сегодня акций дня пока нет.</b>\nОни обычно появляются в первой половине дня. Бот пришлет сразу, как только выйдет!"
                    )

                elif "Акции недели" in text or "Акции месяца" in text:
                    send_msg(
                        chat_id,
                        "📅 <b>Акции недели:</b> выбери желаемый процент кэшбэка / выгоды 👇",
                        get_tier_inline_keyboard("week")
                    )

                elif "Товары с кэшбэком" in text:
                    send_msg(
                        chat_id,
                        "🛒 <b>Товары с кэшбэком:</b> выбери желаемый процент выгоды 👇",
                        get_tier_inline_keyboard("goods")
                    )

                elif "Все акции сразу" in text:
                    posts = get_filtered_posts(tier="all")
                    send_batch(chat_id, posts, "😔 Акций за неделю не найдено.", limit=8)

                elif "Обновить базу" in text or "Обновить сейчас" in text:
                    send_msg(chat_id, "⏳ Обновляю базу акций за неделю из всех источников...")
                    update_posts_cache()
                    send_msg(chat_id, f"✅ База обновлена! Найдено активных акций: <b>{len(cached_posts)}</b>.", get_main_keyboard())

                elif "Помощь" in text or "/help" in text:
                    info = (
                        "ℹ️ <b>Как работает бот:</b>\n\n"
                        "• <b>Период:</b> бот анализирует все акции за последние <b>7 дней (неделя)</b>.\n"
                        "• <b>Источники:</b> Т-Банк, Pepper.ru, каналы с промокодами и чеками.\n"
                        "• <b>Ранжирование:</b> в разделах недели и товаров можно фильтровать по процентам: 80-100%, 50-80%, до 50%.\n"
                        "• <b>Обновление:</b> раз в 3 часа автоматически или по кнопке 'Обновить базу'."
                    )
                    send_msg(chat_id, info, get_main_keyboard())

        except Exception as e:
            print(f"Ошибка в bot_updates_loop: {e}")
            time.sleep(3)

if __name__ == "__main__":
    print("🚀 Запуск бота...")
    # 1. Запуск легковесного HTTP-сервера для Render
    Thread(target=start_http_server, daemon=True).start()

    # 2. Первоначальный сбор и фоновый мониторинг
    Thread(target=monitor_loop, daemon=True).start()

    # 3. Прием команд и кликов от пользователей в Telegram
    bot_updates_loop()
