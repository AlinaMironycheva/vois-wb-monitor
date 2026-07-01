import json
import os
import requests
from datetime import date

# ───────────────────────────────────────────
# НАСТРОЙКИ
# ───────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"

WB_API_URL = (
    "https://card.wb.ru/cards/v2/detail"
    "?appType=1&curr=rub&dest=-1257786&nm={nm_id}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

RATING_DROP_THRESHOLD = 0.2
REVIEWS_SPIKE_THRESHOLD = 20
MISSING_DAYS_ALERT = 2


# ───────────────────────────────────────────
# ЗАГРУЗКА ФАЙЛОВ
# ───────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ───────────────────────────────────────────
# ПОЛУЧЕНИЕ ДАННЫХ С WB
# ───────────────────────────────────────────

def fetch_product(nm_id):
    """
    Возвращает словарь с данными о товаре или None если не удалось получить.
    """
    url = WB_API_URL.format(nm_id=nm_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        p = products[0]

        # Цены в WB API возвращаются умноженными на 100
        price_raw = p.get("salePriceU", 0)
        base_price_raw = p.get("priceU", 0)

        price = price_raw / 100
        base_price = base_price_raw / 100

        # Скидка: считаем из цен, если нет отдельного поля
        if base_price > 0:
            discount = round((1 - price / base_price) * 100)
        else:
            discount = p.get("sale", 0)

        # Наличие: есть ли хоть одна позиция в stocks
        sizes = p.get("sizes", [])
        available = any(
            s.get("stocks") for s in sizes
        )

        return {
            "price": price,
            "base_price": base_price,
            "discount": discount,
            "rating": p.get("reviewRating", 0),
            "reviews": p.get("feedbacks", 0),
            "available": available,
            "name": p.get("name", ""),
        }

    except Exception as e:
        print(f"  Ошибка при запросе {nm_id}: {e}")
        return None


# ───────────────────────────────────────────
# ФОРМИРОВАНИЕ АЛЕРТОВ
# ───────────────────────────────────────────

def check_product(product_cfg, current, previous):
    """
    Сравнивает текущие данные с предыдущими.
    Возвращает список строк с алертами.
    """
    alerts = []
    name = product_cfg["name"]
    nm_id = product_cfg["nmId"]
    threshold_price = product_cfg.get("price_alert_threshold_pct", 10)
    threshold_discount = product_cfg.get("discount_alert_threshold_pct", 5)

    label = f"*{name}* (nm: {nm_id})"

    # Нет данных
    if current is None:
        missing = previous.get("missing_days", 0) + 1
        if missing >= MISSING_DAYS_ALERT:
            alerts.append(
                f"{label}\n"
                f"🚨 Данные недоступны {missing} дня подряд. "
                f"Проверь карточку вручную."
            )
        return alerts, missing

    # Товар недоступен
    if not current["available"] and previous.get("available", True):
        alerts.append(
            f"{label}\n"
            f"🚨 Товар пропал из продажи"
        )

    # Цена выросла
    prev_price = previous.get("price")
    if prev_price and prev_price > 0:
        price_change_pct = (current["price"] - prev_price) / prev_price * 100
        if price_change_pct >= threshold_price:
            alerts.append(
                f"{label}\n"
                f"🚨 Цена выросла: {prev_price:.0f} ₽ → "
                f"{current['price']:.0f} ₽ "
                f"(+{price_change_pct:.1f}%)"
            )

    # Скидка изменилась
    prev_discount = previous.get("discount")
    if prev_discount is not None:
        discount_delta = prev_discount - current["discount"]
        if abs(discount_delta) >= threshold_discount:
            direction = "упала" if discount_delta > 0 else "выросла"
            alerts.append(
                f"{label}\n"
                f"⚠️ Скидка {direction}: "
                f"{prev_discount}% → {current['discount']}%"
            )

    # Рейтинг упал
    prev_rating = previous.get("rating")
    if prev_rating:
        rating_drop = prev_rating - current["rating"]
        if rating_drop >= RATING_DROP_THRESHOLD:
            alerts.append(
                f"{label}\n"
                f"⚠️ Рейтинг упал: "
                f"{prev_rating} → {current['rating']}"
            )

    # Всплеск отзывов
    prev_reviews = previous.get("reviews", 0)
    reviews_delta = current["reviews"] - prev_reviews
    if reviews_delta >= REVIEWS_SPIKE_THRESHOLD:
        alerts.append(
            f"{label}\n"
            f"ℹ️ Резкий рост отзывов: "
            f"+{reviews_delta} за день "
            f"(было {prev_reviews}, стало {current['reviews']})"
        )

    return alerts, 0


# ───────────────────────────────────────────
# ОТПРАВКА В TELEGRAM
# ───────────────────────────────────────────

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram не настроен — вывожу в консоль:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print("Telegram: сообщение отправлено")
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")


# ───────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ
# ───────────────────────────────────────────

def main():
    today = str(date.today())
    print(f"=== WB Monitor запущен: {today} ===")

    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    all_alerts = []
    new_state = {}

    for product in products:
        nm_id = str(product["nmId"])
        name = product["name"]
        print(f"Проверяю: {name} ({nm_id})")

        previous = state.get(nm_id, {})
        current = fetch_product(product["nmId"])

        alerts, missing_days = check_product(product, current, previous)
        all_alerts.extend(alerts)

        # Сохраняем новое состояние
        if current is not None:
            new_state[nm_id] = {
                "price": current["price"],
                "base_price": current["base_price"],
                "discount": current["discount"],
                "rating": current["rating"],
                "reviews": current["reviews"],
                "available": current["available"],
                "last_seen": today,
                "missing_days": 0,
            }
        else:
            # Сохраняем старое состояние, увеличиваем счётчик
            new_state[nm_id] = {
                **previous,
                "missing_days": missing_days,
            }

    save_json(STATE_FILE, new_state)
    print(f"Состояние сохранено: {STATE_FILE}")

    # Отправляем алерты
    if all_alerts:
        header = f"🔔 *WB Monitor — {today}*\n{'─' * 30}\n\n"
        message = header + "\n\n".join(all_alerts)
        send_telegram(message)
        print(f"Отправлено алертов: {len(all_alerts)}")
    else:
        print("Изменений нет — алерты не отправляются")
        # Раз в день можно слать "всё ок" — закомментировано, раскомментируй если нужно
        # send_telegram(f"✅ WB Monitor {today}: изменений нет")


if __name__ == "__main__":
    main()
