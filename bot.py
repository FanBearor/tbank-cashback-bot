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

# Интервал автоматической фоновой проверки: 2 часа
CHECK_INTERVAL_SECONDS = 2 * 3600

# Источники для мониторинга: Telegram-каналы, Pepper.ru и агрегаторы акций
CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",  # Т-Банк акции и кэшбэк дня
    "pepperru",                         # Pepper.ru (все актуальные скидки и выгода)
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
        self.wfile.write(b"OK: T-Bank & Pepper Weekly Cashback Bot is running!")

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

# ==================== РАСЧЕТ И ФОРМАТИРОВАНИЕ ДАТ ====================
def clean_html(raw_html):
    """Очищает HTML-теги и декодирует спецсимволы"""
    text = re.sub(r'<br\s*/?>', '\n', raw_html)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return text.strip()

def format_relative_time(pub_dt, now):
    """Человекочитаемое время: '2 часа назад', '1 день назад' и т.д."""
    if not pub_dt:
        return "недавно"
    diff = now - pub_dt
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
    1. 'day'   - Акции дня (кэшбэк дня, действуют сегодня)
    2. 'goods' - Товары с кэшбэком и скидками (чеки, конкретные товары, маркетплейсы)
    3. 'week'  - Акции недели (общие предложения, банки, промокоды)
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
    """Инлайн-кнопки фильтрации по процентам кэшбэка"""
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

# ==================== ПАРСИНГ СТРОГО ЗА 7 ДНЕЙ ====================
def fetch_channel_posts_week(channel_name, max_pages=4):
    """
    Сбор постов из канала строго за последние 7 дней от момента запуска
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
                    if oldest_on_page is None or pub_dt < oldest_on_page:
                        oldest_on_page = pub_dt
                except Exception:
                    continue

                # СТРОГАЯ ФИЛЬТРАЦИЯ: только за последние 7 дней!
                if pub_dt < week_limit or pub_dt > now:
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

            # Если самые старые посты на странице уже старше 7 дней — в прошлое больше не листаем
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
    """Сбор постов со всех каналов строго за последние 7 дней"""
    global cached_posts, last_cache_update
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Сбор акций за последние 7 дней (Pepper, Т-Банк и др.)...")
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
    print(f"✅ База обновлена! Всего акций за 7 дней: {len(cached_posts)}")

def get_filtered_posts(category=None, tier="all"):
    """
    Фильтрация СТРОГО от текущего момента запроса:
    - Любой пост старше 7 дней от секунды запроса отсекается.
    - Проверяется дедлайн 'до ДД.ММ'.
    - Ранжируется по проценту (от большего к меньшему).
    """
    now = datetime.now(timezone.utc)
    week_limit = now - timedelta(days=7)

    result = []
    for p in cached_posts:
        pub_dt = p.get("datetime")

        # 1. СТРОГО: дата должна быть известна и не старше 7 дней от момента запроса!
        if not pub_dt or pub_dt < week_limit or pub_dt > now:
            continue

        # 2. Проверка срока 'до ДД.ММ' / 'по ДД.ММ'
        until_match = re.search(r'(?:до|по)\s+(\d{1,2})\.(\d{1,2})', p["text"].lower())
        if until_match:
            try:
                d, m = int(until_match.group(1)), int(until_match.group(2))
                year = now.year
                if m < now.month and now.month == 12:
                    year += 1
                expiry_date = datetime(year, m, d, 23, 59, 59, tzinfo=timezone.utc)
                if now > expiry_date:
                    continue
            except Exception:
                pass

        # 3. Фильтр по категории
        if category and p["category"] != category:
            continue

        # 4. Фильтр по процентам
        pct = p["percent"]
        if tier == "80_100" and pct < 80:
            continue
        elif tier == "50_80" and not (50 <= pct < 80):
            continue
        elif tier == "0_50" and pct >= 50:
            continue

        result.append(p)

    # Ранжирование: сначала максимальный %, затем самые свежие
    result.sort(
        key=lambda x: (x["percent"], x["datetime"]),
        reverse=True
    )
    return result

def format_card(p, now):
    """Оформление карточки акции с указанием точной даты и сколько дней/часов назад"""
    cat = p["category"]
    pct = p["percent"]
    ch = p["channel"]
    dt = p["datetime"]

    cat_title = {
        "day": "🔥 АКЦИЯ ДНЯ",
        "goods": "🛒 ТОВАР С КЭШБЭКОМ",
        "week": "📅 АКЦИЯ ЗА НЕДЕЛЮ"
    }.get(cat, "⚡ ВЫГОДНОЕ ПРЕДЛОЖЕНИЕ")

    pct_badge = f" • <b>{pct}% Выгода/Кэшбэк</b>" if pct > 0 else ""
    date_str = dt.strftime("%d.%m.%Y в %H:%M") if dt else "Недавно"
    rel_str = format_relative_time(dt, now)

    header = f"<b>{cat_title}</b>{pct_badge}\n<i>🕒 {date_str} ({rel_str}) • @{ch}</i>\n\n"
    source = f"\n\n👉 <a href='https://t.me/{p['post_ref']}'>Открыть предложение в источнике</a>"

    max_len = 3900 - len(header) - len(source)
    txt = p["text"]
    if len(txt) > max_len:
        txt = txt[:max_len] + "..."

    return header + txt + source

def send_batch(chat_id, posts, empty_text, now, limit=6):
    if not posts:
        send_msg(chat_id, empty_text, get_main_keyboard())
        return

    send_msg(chat_id, f"🔎 Найдено предложений за неделю: <b>{len(posts)}</b>", get_main_keyboard())
    for p in posts[:limit]:
        send_msg(chat_id, format_card(p, now))
        time.sleep(0.3)

# ==================== ФОНОВЫЙ МОНИТОРИНГ ====================
def monitor_loop():
    """Фоновое обновление раз в 2 часа"""
    update_posts_cache()
    known_ids = {p["post_ref"] for p in cached_posts}

    while True:
        try:
            time.sleep(CHECK_INTERVAL_SECONDS)
            prev_ids = set(known_ids)
            update_posts_cache()

            now = datetime.now(timezone.utc)
            for p in cached_posts:
                if p["post_ref"] not in prev_ids:
                    known_ids.add(p["post_ref"])
                    # Новая акция дня уведомляет сразу
                    if p["category"] == "day":
                        send_msg(CHAT_ID, "🚨 <b>НОВАЯ АКЦИЯ ДНЯ!</b>\n\n" + format_card(p, now))
        except Exception as e:
            print(f"Ошибка в monitor_loop: {e}")
            time.sleep(30)

# ==================== ОБРАБОТКА КОМАНД И КНОПОК ====================
def bot_updates_loop():
    offset = 0
    while True:
        try:
            # Если кэш старше 15 минут — освежаем
            if time.time() - last_cache_update > 900 and last_cache_update > 0:
                update_posts_cache()

            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if not res.get("ok"):
                time.sleep(3)
                continue

            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                now = datetime.now(timezone.utc)
                week_start = now - timedelta(days=7)

                # 1. Инлайн-кнопки (выбор процента)
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    cq_id = cq["id"]
                    cq_data = cq.get("data", "")
                    chat_id = cq.get("message", {}).get("chat", {}).get("id")
                    answer_callback(cq_id)

                    tier_names = {"80_100": "80% — 100%", "50_80": "50% — 80%", "0_50": "до 50%", "all": "все"}

                    if cq_data.startswith("week_"):
                        tier = cq_data.replace("week_", "")
                        posts = get_filtered_posts(category="week", tier=tier)
                        send_msg(
                            chat_id,
                            f"📅 <b>Акции за последние 7 дней ({week_start.strftime('%d.%m')} — {now.strftime('%d.%m')}):\nКэшбэк: {tier_names.get(tier, tier)}</b>"
                        )
                        send_batch(chat_id, posts, f"😔 В диапазоне {tier_names.get(tier)} за последние 7 дней предложений не найдено.", now)

                    elif cq_data.startswith("goods_"):
                        tier = cq_data.replace("goods_", "")
                        posts = get_filtered_posts(category="goods", tier=tier)
                        send_msg(
                            chat_id,
                            f"🛒 <b>Товары с кэшбэком за последние 7 дней ({week_start.strftime('%d.%m')} — {now.strftime('%d.%m')}):\nВыгода: {tier_names.get(tier, tier)}</b>"
                        )
                        send_batch(chat_id, posts, f"😔 В диапазоне {tier_names.get(tier)} за последние 7 дней товаров не найдено.", now)
                    continue

                # 2. Текстовые сообщения
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if not chat_id or not text:
                    continue

                if text == "/start":
                    start_text = (
                        "👋 <b>Привет! Бот кэшбэка и акций обновлен!</b>\n\n"
                        f"⏱ <b>Диапазон поиска:</b> строго за <b>последние 7 дней</b> от даты запроса "
                        f"(с <b>{week_start.strftime('%d.%m.%Y')}</b> по <b>{now.strftime('%d.%m.%Y')}</b>).\n"
                        "🌐 <b>Источники:</b> Т-Банк, Pepper.ru, Халявщики, Промокоды.\n\n"
                        "📂 <b>Разделы:</b>\n"
                        "1️⃣ <b>🔥 Акции дня</b> — предложения за последние 24-36 часов.\n"
                        "2️⃣ <b>📅 Акции недели (7 дней)</b> — акции за неделю с выбором кэшбэка (80-100%, 50-80%, до 50%).\n"
                        "3️⃣ <b>🛒 Товары с кэшбэком</b> — чеки и товары за неделю с ранжированием.\n\n"
                        "Выбирай кнопку внизу 👇"
                    )
                    send_msg(chat_id, start_text, get_main_keyboard())

                elif "Акции дня" in text:
                    posts = get_filtered_posts(category="day", tier="all")
                    if not posts:
                        posts = [p for p in cached_posts if (now - p["datetime"]).total_seconds() <= 36*3600]
                    send_batch(
                        chat_id,
                        posts,
                        "😔 <b>На сегодня акций дня пока нет.</b>\nОни обычно появляются в течение дня. Как только появится — бот пришлет!",
                        now
                    )

                elif any(k in text for k in ["Акции недели", "Акции месяца", "акции за неделю"]):
                    send_msg(
                        chat_id,
                        f"📅 <b>Акции за последние 7 дней (с {week_start.strftime('%d.%m')} по {now.strftime('%d.%m.%Y')}):</b>\n"
                        "Выбери желаемый процент кэшбэка / выгоды от даты запроса 👇",
                        get_tier_inline_keyboard("week")
                    )

                elif "Товары с кэшбэком" in text:
                    send_msg(
                        chat_id,
                        f"🛒 <b>Товары с кэшбэком за последние 7 дней (с {week_start.strftime('%d.%m')} по {now.strftime('%d.%m.%Y')}):</b>\n"
                        "Выбери желаемый процент выгоды 👇",
                        get_tier_inline_keyboard("goods")
                    )

                elif any(k in text for k in ["Все акции", "Все акции за неделю", "Все акции сразу"]):
                    posts = get_filtered_posts(tier="all")
                    send_batch(chat_id, posts, "😔 Акций за последние 7 дней не найдено.", now, limit=8)

                elif "Обновить базу" in text or "Обновить сейчас" in text:
                    send_msg(chat_id, "⏳ Обновляю базу акций за последние 7 дней со всех сайтов...")
                    update_posts_cache()
                    send_msg(chat_id, f"✅ База обновлена! Найдено акций за неделю: <b>{len(cached_posts)}</b>.", get_main_keyboard())

                elif "Помощь" in text or "/help" in text:
                    info = (
                        "ℹ️ <b>Как работает бот:</b>\n\n"
                        "• <b>Период:</b> строго за последние <b>7 дней от момента твоего запроса</b>.\n"
                        "• <b>Источники:</b> Т-Банк, Pepper.ru, каналы со скидками и чеками.\n"
                        "• <b>Ранжирование:</b> выбирай процент (80-100%, 50-80%, до 50%), бот покажет сначала самые выгодные предложения."
                    )
                    send_msg(chat_id, info, get_main_keyboard())

        except Exception as e:
            print(f"Ошибка в bot_updates_loop: {e}")
            time.sleep(3)

if __name__ == "__main__":
    print("🚀 Запуск бота кэшбэка за неделю...")
    # 1. Веб-сервер для Render
    Thread(target=start_http_server, daemon=True).start()

    # 2. Мониторинг и сбор базы
    Thread(target=monitor_loop, daemon=True).start()

    # 3. Прием команд в Telegram
    bot_updates_loop()
