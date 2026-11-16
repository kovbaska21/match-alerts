"""Сповіщення: завжди пишуться в logs/alerts.log, а якщо налаштовано Telegram — ще й на телефон
(можна кільком людям, наприклад тобі й татові).

Налаштування Telegram (робиш сам; токен нікому не надсилай, і мені теж):
  1. У Telegram відкрий @BotFather → /newbot → дай назву → отримаєш токен виду 123456:ABC...
  2. Скопіюй config/telegram.example.json у config/telegram.json і встав туди свій токен.
  3. Кожен, хто хоче отримувати сигнали (ти, тато), пише новому боту будь-яке повідомлення.
  4. Запусти: py notify.py setup   → скрипт сам знайде всі чати й збереже їх у файл.
  5. Перевір:  py notify.py test
Скрипт ніколи не виводить токен на екран і нікуди, крім api.telegram.org, його не надсилає.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = ROOT / "config" / "telegram.json"
LOG = ROOT / "logs" / "alerts.log"


def _cfg():
    # На GitHub токен і чати беруться з секретів репозиторію (змінні середовища), а не з файлу
    if os.environ.get("TELEGRAM_TOKEN"):
        ids = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]
        return {"token": os.environ["TELEGRAM_TOKEN"], "chat_ids": ids}
    try:
        c = json.loads(CFG.read_text(encoding="utf-8"))
        if c.get("token") and "ВСТАВ" not in c["token"]:
            return c
    except Exception:
        pass
    return None


def configured():
    c = _cfg()
    return bool(c and c.get("chat_ids"))


def _api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15).read())


def send(text):
    """Записати в журнал і, якщо налаштовано, надіслати в Telegram. Помилки мережі не зупиняють бота."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {text}\n\n")
    c = _cfg()
    if not c or not c.get("chat_ids"):
        return False
    ok = False
    for chat in c["chat_ids"]:
        try:
            _api(c["token"], "sendMessage", {"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"})
            ok = True
        except Exception as e:
            with LOG.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] (не вдалося надіслати в чат {chat}: {type(e).__name__})\n")
    return ok


def setup():
    c = _cfg()
    if not c:
        print("Спершу створи config/telegram.json зі своїм токеном (див. config/telegram.example.json).")
        return
    ups = _api(c["token"], "getUpdates").get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"] for u in ups if "message" in u}
    if not chats:
        print("Бот ще не отримав жодного повідомлення. Напиши йому щось у Telegram і запусти setup ще раз.")
        return
    ids = list(dict.fromkeys((c.get("chat_ids") or []) + list(chats)))
    c["chat_ids"] = ids
    CFG.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    names = [ch.get("first_name") or ch.get("title") or str(i) for i, ch in chats.items()]
    print(f"Готово: сигнали йтимуть у {len(ids)} чат(и): {', '.join(names)}.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        setup()
    elif cmd == "test":
        print("Надіслано в Telegram." if send("Тест: бот перенесених матчів працює.") else
              "Записано лише в logs/alerts.log (Telegram ще не налаштовано або немає зв'язку).")
    else:
        print(__doc__)
