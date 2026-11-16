"""Бот перенесених матчів: стежить за статусами футбольних матчів і шле сигнал, коли матч
перенесли, скасували, зупинили/перервали або змінили час початку.

Джерело: публічні дані FotMob (без акаунта й ключа) — ~150 ліг у вихідні, ~500 матчів на день.
Сьогодні й завтра перевіряються кожні 2 хв, наступні 5 днів — кожні 30 хв.

Запуск:
  py monitor.py            — працює безперервно (Ctrl+C — зупинити)
  py monitor.py once       — одна перевірка (для тесту)
  py monitor.py leagues    — список ліг на найближчі дні з їхніми id (щоб вибрати ліги татка)

Сигнали:  data/signals.csv (журнал) + logs/alerts.log + Telegram, якщо налаштовано (див. README.md).
Ліги:     config/leagues.json — {"only": []} = усі ліги; або список id/назв ліг, тоді лише вони.
"""
import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import notify

ROOT = Path(__file__).resolve().parent
DATA, CONFIG = ROOT / "data", ROOT / "config"
STATE = DATA / "state.json"
SIGNALS = ROOT / os.environ.get("SIGNALS_PATH", "data/signals.csv")   # на GitHub — journal/signals.csv у репозиторії
GAP_SEC = int(os.environ.get("GAP_MIN", "10")) * 60                    # перерва, після якої бот вважає, що «спав»
URL = "https://www.fotmob.com/api/data/matches?date={}&timezone=UTC&ccode3=UKR"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
      "Accept": "application/json", "Accept-Encoding": "gzip"}
FAST_EVERY, SLOW_EVERY, FAST_DAYS, SLOW_DAYS = 120, 1800, 2, 7
TIME_SHIFT_MIN = 15                     # зміна часу початку, яку записуємо в журнал
HOT_HOURS, HOT_SHIFT_MIN = 48, 60       # у Telegram — лише якщо матч мав бути протягом 48 год і зсув ≥ 1 год

NORMAL = {None, "FT", "AET", "Pen", "HT"}   # звичайні позначки (None — ще не почався або йде)
LABEL = {"PP": "ПЕРЕНЕСЕНО", "Can": "СКАСОВАНО", "Ab": "ЗУПИНЕНО (не дограли)", "Int": "ПЕРЕРВАНО",
         "Susp": "ПРИЗУПИНЕНО", "Del": "ЗАТРИМКА", "TBA": "ЧАС НЕВІДОМИЙ", "AW": "ТЕХНІЧНИЙ РЕЗУЛЬТАТ"}
COLS = ["detected_utc", "match_id", "league_id", "league", "country", "home", "away", "kickoff_utc",
        "event", "old", "new", "min_to_kickoff", "late", "link"]


def fetch(day):
    for i in range(3):
        try:
            r = urllib.request.urlopen(urllib.request.Request(URL.format(f"{day:%Y%m%d}"), headers=UA), timeout=20)
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except Exception:
            if i == 2:
                raise
            time.sleep(5)


def matches(day):
    """[(ключ_статусу, інфо)] для однієї дати UTC."""
    out = []
    for lg in fetch(day).get("leagues", []):
        for m in lg.get("matches", []):
            s = m.get("status", {})
            short = (s.get("reason") or {}).get("short")
            if s.get("awarded"):
                short = "AW"
            elif s.get("cancelled") and short in NORMAL:
                short = "Can"
            out.append({"id": str(m["id"]), "league_id": lg.get("primaryId") or lg.get("id"), "league": lg.get("name"),
                        "country": lg.get("ccode"), "home": m["home"]["name"], "away": m["away"]["name"],
                        "kickoff": s.get("utcTime"), "short": short,
                        "started": bool(s.get("started")), "finished": bool(s.get("finished"))})
    return out


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def wanted(m, only):
    if not only:
        return True
    return any(str(x) == str(m["league_id"]) or str(x).lower() == (m["league"] or "").lower() for x in only)


def ts(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:                       # на Windows без пакета tzdata — часовий пояс самого ПК (він у Києві)
    KYIV = None


def local(dt):
    """Київський час (з літнім/зимовим), навіть якщо бот працює на сервері в UTC."""
    return dt.astimezone(KYIV).strftime("%d.%m %H:%M") if dt else "?"


def signal(m, event, old, new, late, quiet=False):
    """Записує сигнал у журнал. Повертає текст для Telegram (або None, якщо сигнал «тихий» — лише в журнал)."""
    now = datetime.now(timezone.utc)
    ko = ts(m["kickoff"])
    mins = round((ko - now).total_seconds() / 60) if ko else ""
    row = [f"{now:%Y-%m-%d %H:%M:%S}", m["id"], m["league_id"], m["league"], m["country"], m["home"], m["away"],
           m["kickoff"], event, old, new, mins, int(late), f"https://www.fotmob.com/match/{m['id']}"]
    SIGNALS.parent.mkdir(parents=True, exist_ok=True)
    new_file = not SIGNALS.exists()
    with SIGNALS.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLS)
        w.writerow(row)
    kyiv = local(ko)
    when = (f"до початку {mins // 60} год {mins % 60} хв" if isinstance(mins, int) and mins >= 0
            else "під час гри / після часу початку")
    text = (f"⚽ {event}\n{m['home']} — {m['away']}\n{m['league']} ({m['country']})\n"
            f"Початок за розкладом: {kyiv} (Київ), {when}\n"
            + (f"Було: {old} → стало: {new}\n" if "ЧАС" in event else "")
            + ("(помічено із запізненням — бот не працював)\n" if late else "")
            + row[-1])
    print(("(тихо) " if quiet else "") + text.replace("\n", " | "), flush=True)
    return None if quiet else text


def deliver(texts):
    """До 3 сигналів — окремими повідомленнями; більше — одним зведенням (щоб не було 30 повідомлень підряд)."""
    if len(texts) <= 3:
        for t in texts:
            notify.send(t)
        return
    head, chunk = f"⚽ {len(texts)} змін одразу (зведення):\n\n", ""
    for t in texts:
        if len(head) + len(chunk) + len(t) > 3800:
            notify.send(head + chunk)
            head, chunk = "⚽ (продовження)\n\n", ""
        chunk += t + "\n\n"
    notify.send(head + chunk)


def check(days, state, only, first_run, gap=False):
    """Порівнює свіжі статуси з попередніми. Повертає (матчів, сигналів у Telegram)."""
    now = datetime.now(timezone.utc)
    n = 0
    texts = []
    for d in days:
        for m in matches(now + timedelta(days=d)):
            if not wanted(m, only):
                continue
            n += 1
            prev = state.get(m["id"])
            state[m["id"]] = {"short": m["short"], "kickoff": m["kickoff"], "seen": f"{now:%Y-%m-%d}"}
            bad = m["short"] not in NORMAL
            ko = ts(m["kickoff"])
            if prev is None:
                # новий для нас матч: сигнал лише після перерви в роботі бота (ПК спав), якщо він уже «поганий»
                # і ще не мав початися. Без перерви це просто новий день у вікні перевірки — не новина.
                # і лише для матчів найближчих 2 діб: новий для нас «поганий» матч далі — це новий день у вікні
                if bad and ko and now < ko < now + timedelta(days=FAST_DAYS) and not first_run and gap:
                    texts.append(signal(m, LABEL.get(m["short"], f"СТАТУС {m['short']}"), "—", m["short"], late=True))
                continue
            if m["short"] != prev["short"] and bad:
                texts.append(signal(m, LABEL.get(m["short"], f"СТАТУС {m['short']}"),
                                    prev["short"] or "за розкладом", m["short"], gap))
            elif (not bad and not m["started"] and prev.get("kickoff") and m["kickoff"]
                  and abs((ts(m["kickoff"]) - ts(prev["kickoff"])).total_seconds()) >= TIME_SHIFT_MIN * 60):
                # Більшість змін часу — ліга уточнює розклад за кілька днів. У Telegram — лише «гарячі»:
                # матч мав початися протягом 48 год, а його посунули на ≥ 1 год. Решта — тихо в журнал.
                old_ko = ts(prev["kickoff"])
                hot = old_ko < now + timedelta(hours=HOT_HOURS) and abs((ko - old_ko).total_seconds()) >= HOT_SHIFT_MIN * 60
                texts.append(signal(m, "ЗМІНИЛИ ЧАС ПОЧАТКУ" if hot else "ЧАС УТОЧНЕНО", local(old_ko), local(ko),
                                    gap, quiet=not hot))
    texts = [t for t in texts if t]
    deliver(texts)
    return n, len(texts)


def prune(state):
    cutoff = f"{datetime.now(timezone.utc) - timedelta(days=10):%Y-%m-%d}"
    return {k: v for k, v in state.items() if isinstance(v, dict) and v.get("seen", "") >= cutoff}


def save(state):
    DATA.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE)


def run(once=False):
    only = load_json(CONFIG / "leagues.json", {}).get("only", [])
    state = load_json(STATE, {})
    first_run = not state
    last_ok = state.pop("_last_ok", 0)
    print(f"Бот перенесених матчів працює. Ліги: {'усі' if not only else ', '.join(map(str, only))}. "
          f"Telegram: {'так' if notify.configured() else 'ні (сигнали лише в logs/alerts.log)'}", flush=True)
    last_slow = 0
    while True:
        t0 = time.time()
        days = list(range(-1, FAST_DAYS))                       # учора (матчі, що йдуть після півночі) + сьогодні + завтра
        if t0 - last_slow >= SLOW_EVERY:
            days += list(range(FAST_DAYS, SLOW_DAYS))
            last_slow = t0
        try:
            gap = time.time() - last_ok > GAP_SEC               # перерва (сон ПК, вимкнення, збій GitHub)
            if gap and not first_run:
                days = sorted(set(days) | set(range(FAST_DAYS, SLOW_DAYS)))   # після перерви — увесь тиждень
            n, sent = check(days, state, only, first_run, gap)
            first_run = False
            last_ok = time.time()
            state = prune(state)
            save({**state, "_last_ok": last_ok})
            print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC] перевірено матчів: {n}, сигналів: {sent}", flush=True)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc):%H:%M}] помилка: {e}", flush=True)
        if once:
            return
        time.sleep(max(10, FAST_EVERY - (time.time() - t0)))


def leagues():
    seen = {}
    now = datetime.now(timezone.utc)
    for d in range(0, SLOW_DAYS):
        for m in matches(now + timedelta(days=d)):
            seen.setdefault((m["league_id"], m["league"], m["country"]), 0)
            seen[(m["league_id"], m["league"], m["country"])] += 1
    for (i, name, cc), cnt in sorted(seen.items(), key=lambda x: (x[0][2] or "", x[0][1] or "")):
        print(f"{i:>8}  {cc or '':4} {name}  (матчів за 7 днів: {cnt})")
    print(f"Разом ліг: {len(seen)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "leagues":
        leagues()
    else:
        run(once=(cmd == "once"))
