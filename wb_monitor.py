import json
import os
import requests
from datetime import date

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


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_product(nm_id):
    url = WB_API_URL.format(nm_id=nm_id)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        p = products[0]

        price = p.get("salePriceU", 0) / 100
        base_price = p.get("priceU", 0) / 100

        if base_price > 0:
            discount = round((1 - price / base_price) * 100)
        else:
            discount = p.get("sale", 0)

        sizes = p.get("sizes", [])
        available = any(size.get("stocks") for size in sizes)

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
        print(f"Ошибка при запросе {nm_id}: {e}")
        return None


def check_product(product_cfg, current, previous):
    alerts = []

    name = product_cfg["name"]
    nm_id = str(product_cfg["nm_id"])

    threshold_price = product_cfg.get("price_alert_threshold_pct", 10)
    threshold_discount = product_cfg.get("discount_alert_threshold_pct", 5)

    label = f"*{name}* \\(nm: {nm_id}\\)"

    if current is None:
        missing = previous.get("missing_days", 0) + 1

        if missing >= MISSING_DAYS_ALERT:
            alerts.append(
                f"{label}\n"
                f"🚨 Данные недоступны {missing} дня подряд. Проверь карточку вручную."
            )

        return alerts, missing

    if not current["available"] and previous.get("available", True):
        alerts.append(
            f"{label}\n"
            f"🚨 Товар пропал из продажи"
        )

    prev_price = previous.get("price")
    if prev_price and prev_price > 0:
        price_change_pct = (current["price"] - prev_price) / prev_price * 100

        if price_change_pct >= threshold_price:
            alerts.append(
                f"{label}\n"
                f"🚨 Цена выросла: {prev_price:.0f} ₽ → "
                f"{current['price']:.0f} ₽ "
                f"\\(+{price_change_pct:.1f}%\\)"
            )

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

    prev_rating = previous.get("rating")
    if prev_rating:
        rating_drop = prev_rating - current["rating"]

        if rating_drop >= RATING_DROP_THRESHOLD:
            alerts.append(
                f"{label}\n"
                f"⚠️ Рейтинг упал: "
                f"{prev_rating} → {current['rating']}"
            )

    prev_reviews = previous.get("reviews", 0)
    reviews_delta = current["reviews"] - prev_reviews

    if reviews_delta >= REVIEWS_SPIKE_THRESHOLD:
        alerts.append(
            f"{label}\n"
            f"ℹ️ Резкий рост отзывов: "
            f"+{reviews_delta} за день "
            f"\\(было {prev_reviews}, стало {current['reviews']}\\)"
        )

    return alerts, 0


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram не настроен — вывожу в консоль:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print("Telegram: сообщение отправлено")
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")
        print(text)


def main():
    today = str(date.today())
    print(f"=== WB Monitor запущен: {today} ===")

    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    all_alerts = []
    new_state = {}

    for product in products:
        nm_id = str(product["nm_id"])
        name = product["name"]

        print(f"Проверяю: {name} ({nm_id})")

        previous = state.get(nm_id, {})
        current = fetch_product(product["nm_id"])

        alerts, missing_days = check_product(product, current, previous)
        all_alerts.extend(alerts)

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
            new_state[nm_id] = {
                **previous,
                "missing_days": missing_days,
            }

    save_json(STATE_FILE, new_state)
    print(f"Состояние сохранено: {STATE_FILE}")

    if all_alerts:
        header = f"🔔 *WB Monitor — {today}*\n\n"
        message = header + "\n\n".join(all_alerts)
        send_telegram(message)
        print(f"Отправлено алертов: {len(all_alerts)}")
    else:
        print("Изменений нет — алерты не отправляются")


if __name__ == "__main__":
    main()
