import os
import re
import time
import requests
from threading import Thread
BOT_TOKEN = os.getenv("BOT_TOKEN", "8749854506:AAHT_Uo73Rw9Mn4BHoUaQEj6KtqapDfPOHo")
CHAT_ID = os.getenv("CHAT_ID", "936459900")
# Каналы для мониторинга (кэшбэк дня, чеки, халява Т-Банка)
CHANNELS = [
    "t_bank_tinkoff_cashback_keshbek",
    "pepperru"
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
# Режим фильтрации по умолчанию: '80_100', '50_80', 'all'
current_filter = "80_100"
def get_menu_keyboard():
    f = current_filter
    b1 = ("✅ 80% - 100%" if f == "80_100" else "🔥 80% - 100%")
    b2 = ("✅ 50% - 80%" if f == "50_80" else "⚡ 50% - 80%")
    b3 = ("✅ Любой кэшбэк" if f == "all" else "📦 До 50% (Любой)")
    
    return {
        "keyboard": [
            [{"text": b1}, {"text": b2}],
            [{"text": b3}],
            [{"text": "ℹ️ Текущие настройки"}, {"text": "🔎 Проверить канал"}]
        ],
        "resize_keyboard": True
    }
def send_msg(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Ошибка отправки: {e}")
def extract_percent(text):
    """Ищет максимальный процент кэшбэка в тексте (от 1 до 100)"""
    # Ищем упоминания вида 100%, 50 %, 100 процентов
    found = re.findall(r'(\d{1,3})\s*(?:%|процент)', text.lower())
    percents = []
    for p in found:
        val = int(p)
        if 5 <= val <= 100:
            percents.append(val)
    if not percents:
        # Если явного числа нет, но написано "кэшбэк дня" / "бесплатно"
        if "кэшбэк дня" in text.lower() or "акция дня" in text.lower() or "бесплатно" in text.lower():
            return 100
        return None
    return max(percents)
def matches_filter(percent, mode):
    if percent is None:
        return False
    if mode == "80_100":
        return percent >= 80
    elif mode == "50_80":
        return 50 <= percent < 80
    elif mode == "all":
        return percent > 0
    return False
def check_channel(channel_name, last_id):
    url = f"https://t.me/s/{channel_name}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return last_id
        html = resp.text
        posts = re.findall(rf'data-post="{channel_name}/(\d+)"', html)
        if not posts:
            return last_id
        post_ids = sorted([int(p) for p in set(posts)])
        if last_id is None:
            return post_ids[-1]
        for pid in post_ids:
            if pid > last_id:
                pattern = rf'data-post="{channel_name}/{pid}".*?<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>'
                match = re.search(pattern, html, re.DOTALL)
                if match:
                    raw_text = match.group(1)
                    clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
                    lower_text = clean_text.lower()
                    # Фильтр для сторонних каналов (Pepper) — интересует только Т-Банк / Тинькофф
                    if channel_name == "pepperru":
                        if not any(w in lower_text for w in ["тинькофф", "т-банк", "тбанк", "тиньков"]):
                            last_id = pid
                            continue
                    pct = extract_percent(clean_text)
                    if matches_filter(pct, current_filter):
                        badge = "🔥 100% ХАЛЯВА" if pct == 100 else f"⚡ КЭШБЭК ~{pct}%"
                        msg = (
                            f"<b>{badge}</b>\n\n"
                            f"{clean_text}\n\n"
                            f"👉 <a href='https://t.me/{channel_name}/{pid}'>Открыть в источнике</a>"
                        )
                        send_msg(msg, get_menu_keyboard())
                last_id = pid
        return last_id
    except Exception as e:
        print(f"Ошибка канала {channel_name}: {e}")
        return last_id
def monitor_loop():
    """Фоновый цикл проверки каналов"""
    last_ids = {ch: None for ch in CHANNELS}
    for ch in CHANNELS:
        last_ids[ch] = check_channel(ch, last_ids[ch])
    while True:
        time.sleep(60)
        for ch in CHANNELS:
            last_ids[ch] = check_channel(ch, last_ids[ch])
def bot_updates_loop():
    """Обработка кнопок и команд в Telegram"""
    global current_filter
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
                msg = upd.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id"))
                if chat_id != str(CHAT_ID):
                    continue
                if "80" in text and "100" in text:
                    current_filter = "80_100"
                    send_msg("🎯 <b>Режим включен:</b> Только жирный кэшбэк <b>80% - 100%</b> (Акции дня, чеки со 100% возвратом).", get_menu_keyboard())
                elif "50" in text and "80" in text:
                    current_filter = "50_80"
                    send_msg("🎯 <b>Режим включен:</b> Кэшбэк <b>50% - 80%</b>.", get_menu_keyboard())
                elif "До 50" in text or "Любой" in text:
                    current_filter = "all"
                    send_msg("🎯 <b>Режим включен:</b> Все предложения (любой кэшбэк).", get_menu_keyboard())
                elif "Текущие" in text or "/status" in text:
                    names = {"80_100": "80% - 100%", "50_80": "50% - 80%", "all": "Все подряд"}
                    send_msg(f"⚙️ <b>Текущий фильтр:</b> {names.get(current_filter)}\n📡 <b>Каналов в мониторинге:</b> {len(CHANNELS)}\n⏱ Проверка каждые 60 секунд.", get_menu_keyboard())
                elif "Проверить" in text:
                    send_msg("🔍 Проверяю источники... Если появится подходящая акция — пришлю сразу!", get_menu_keyboard())
                elif text == "/start":
                    send_msg("👋 Бот обновлен! Выбирай нужный диапазон кэшбэка кнопками внизу 👇", get_menu_keyboard())
        except Exception as e:
            print(f"Ошибка в getUpdates: {e}")
            time.sleep(3)
if __name__ == "__main__":
    print("Бот с кнопками и фильтрами запущен!")
    send_msg("🚀 <b>Бот обновлен!</b>\nПоявились интерактивные кнопки. Выбирай нужный кэшбэк ниже 👇", get_menu_keyboard())
    # Запускаем мониторинг в отдельном потоке
    t = Thread(target=monitor_loop, daemon=True)
    t.start()
    # Запускаем чтение кнопок
    bot_updates_loop()
