"""Бот воронки: призыв в ролике → лид-магнит → «Получилось посмотреть?» → анкета → заявка тебе в личный бот.
    python funnel.py              – слушать бота в этом окне (для проверки; остановить Ctrl + C)
    python funnel.py on           – включить: бот работает сам, пока компьютер включён, и стартует после перезагрузки
    python funnel.py off          – выключить
    python funnel.py status       – проверить, включён ли
    python funnel.py check        – проверить funnel.json и токен бота воронки, показать ссылки
    python funnel.py link reel_05 – ссылка на бота с меткой ролика
    python funnel.py итоги [дней] – сколько пришло, посмотрело и заполнило анкету, по меткам

Все тексты, вопросы, кодовые слова и лид-магнит – в funnel.json. Код трогать не нужно.
Владелец (TELEGRAM_CHAT_ID) может написать боту воронки «итоги» – бот пришлёт отчёт."""
import config  # первым: переключает на окружение .venv
import base64
import html
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from lib.common import IS_MAC, IS_WIN, log

NAME = "scenarios-funnel"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"com.{NAME}.plist"
TOKEN = os.getenv("FUNNEL_BOT_TOKEN", "").strip()
FUNNEL = config.ROOT / "funnel.json"
DB = config.DATA / "funnel.db"
OFFSET = config.DATA / "funnel_offset"
FILE_IDS = config.DATA / "funnel_files.json"   # загруженный файл лид-магнита второй раз не грузим
NO_LABEL = "без метки"
OWNER_DELAY_MIN = 1   # тебе самому вопрос приходит через минуту, чтобы проверить воронку быстро
POLL = 20             # секунд ждём новых сообщений, потом проверяем отложенные

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    label TEXT,            -- метка ролика, с которого человек пришёл первый раз
    started REAL,          -- пришёл
    magnet REAL,           -- получил лид-магнит (последний раз)
    ask_at REAL,           -- когда спросить «Получилось посмотреть?»
    asked REAL,            -- спросили
    said_no REAL,          -- ответил «Ещё нет»
    watched REAL,          -- ответил «Да, посмотрел»
    q INTEGER,             -- номер текущего вопроса анкеты, пусто – не в анкете
    answers TEXT,          -- ответы, JSON-список
    done REAL,             -- заполнил анкету
    notified REAL          -- заявка ушла тебе в личный бот
);
"""


# ---------- funnel.json ----------

def load_funnel():
    try:
        f = json.loads(FUNNEL.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"Нет файла {FUNNEL.name}. Скажи Claude: «заполни funnel.json по интервью со мной»")
    except json.JSONDecodeError as e:
        sys.exit(f"В {FUNNEL.name} ошибка в строке {e.lineno}: {e.msg}. Скажи Claude: «почини funnel.json»")
    f.setdefault("вопрос_через_минут", 15)
    f.setdefault("кодовые_слова", [])
    return f


def problems(f):
    """Что в funnel.json не заполнено или заполнено так, что бот не сможет работать."""
    out = []
    need = ["приветствие", "лид_магнит", "вопрос_посмотрел", "кнопка_да", "кнопка_нет", "ответ_нет",
            "перед_анкетой", "вопросы", "после_анкеты", "нажми_кнопку", "не_понял"]
    out += [f"нет поля «{k}»" for k in need if not f.get(k)]

    def walk(v, where):
        if isinstance(v, str) and re.search(r"<[^<>]{3,}>", v):
            out.append(f"не заполнено: {where}")
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{where} → {k}" if where else k)
        elif isinstance(v, list):
            for i, x in enumerate(v, 1):
                walk(x, f"{where} № {i}")
    m = f.get("лид_магнит") or {}
    kind = m.get("тип")
    unused = ("файл",) if kind == "ссылка" else ("ссылка", "кнопка")   # поле для другого типа не нужно
    walk({k: ({x: y for x, y in v.items() if x not in unused} if k == "лид_магнит" and isinstance(v, dict) else v)
          for k, v in f.items() if not k.startswith("_")}, "")

    if kind not in ("ссылка", "файл", "видео"):
        out.append("лид_магнит → тип: нужно «ссылка», «файл» или «видео»")
    elif kind == "ссылка" and not str(m.get("ссылка", "")).startswith("http"):
        out.append("лид_магнит → ссылка: нужна ссылка, которая начинается с http")
    elif kind in ("файл", "видео"):
        p = magnet_path(m)
        if not p or not p.exists():
            out.append(f"лид_магнит → файл: не нашёл {m.get('файл')} (путь от папки scenarios)")
        elif p.stat().st_size > 50 * 1024 * 1024:
            out.append("лид_магнит → файл больше 50 МБ: бот такой не отправит. Залей на Яндекс Диск "
                       "или YouTube и поставь тип «ссылка»")
    qs = f.get("вопросы") or []
    if not 3 <= len(qs) <= 6:
        out.append(f"вопросов {len(qs)}, нужно от 3 до 6")
    for i, q in enumerate(qs, 1):
        if not q.get("текст"):
            out.append(f"вопрос № {i}: нет текста")
        for o in q.get("кнопки") or []:
            if len(o) > 60:
                out.append(f"вопрос № {i}: кнопка длиннее 60 знаков – Telegram её обрежет: «{o[:30]}…»")
    for w in f.get("кодовые_слова") or []:
        if not norm(w) or " " in norm(w):
            out.append(f"кодовое слово «{w}»: нужно одно слово")
    if not isinstance(f.get("вопрос_через_минут"), (int, float)) or f["вопрос_через_минут"] < 1:
        out.append("вопрос_через_минут: нужно число минут, от 1")
    return out


def magnet_path(m):
    if not m.get("файл"):
        return None
    p = Path(m["файл"]).expanduser()
    return p if p.is_absolute() else config.ROOT / p


def norm(s):
    return re.sub(r"[^\w\s-]", "", str(s).lower().replace("ё", "е")).strip()


def clean_label(s):
    s = re.sub(r"[^\w-]", "", s or "")[:64]
    return s or NO_LABEL


# ---------- Telegram ----------

def api(token, method, files=None, **data):
    """Единственное место, где бот ходит в Telegram. В проверке без сети его подменяют."""
    for k, v in list(data.items()):
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v, ensure_ascii=False)
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files,
                      timeout=300 if files else 70).json()
    if not r.get("ok"):
        raise RuntimeError(f"Telegram {method}: {r.get('description')}")
    return r["result"]


def tg(method, **data):
    return api(TOKEN, method, **data)


def say(chat, text, buttons=None):
    """buttons – список рядов, в ряду пары (текст, callback_data)."""
    kb = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]} if buttons else None
    return tg("sendMessage", chat_id=chat, text=text, **({"reply_markup": kb} if kb else {}))


def to_owner(text):
    """Сообщение тебе в личный бот (тот же, куда приходят сценарии)."""
    if not (config.TG_TOKEN and config.TG_CHAT):
        raise RuntimeError("личный бот не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env)")
    api(config.TG_TOKEN, "sendMessage", chat_id=config.TG_CHAT, text=text, parse_mode="HTML",
        disable_web_page_preview="true")


def video_size(p):
    """Ширина, высота, длина – без них Telegram сплющивает вертикальное видео."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height:format=duration", "-of", "csv=p=0:s=,", str(p)],
                             capture_output=True, text=True, timeout=30).stdout.split()
        w, h = out[0].split(",")[:2]
        return {"width": int(w), "height": int(h), "duration": int(float(out[-1]))}
    except Exception:
        return {}


def send_magnet(chat, m):
    text = m.get("текст", "")
    kind = m["тип"]
    if kind == "ссылка":
        kb = {"inline_keyboard": [[{"text": m.get("кнопка") or "Открыть", "url": m["ссылка"]}]]}
        tg("sendMessage", chat_id=chat, text=text or m["ссылка"], reply_markup=kb)
        return
    p = magnet_path(m)
    if len(text) > 1000:   # подпись к файлу – до 1024 знаков, длинный текст отдельным сообщением
        say(chat, text)
        text = ""
    cache = json.loads(FILE_IDS.read_text(encoding="utf-8")) if FILE_IDS.exists() else {}
    key = f"{p}|{int(p.stat().st_mtime)}"
    method, field = ("sendVideo", "video") if kind == "видео" else ("sendDocument", "document")
    extra = {"supports_streaming": "true", **video_size(p)} if kind == "видео" else {}
    if key in cache:
        try:
            tg(method, chat_id=chat, caption=text, **{field: cache[key]}, **extra)
            return
        except RuntimeError:
            pass   # файл в Telegram устарел – грузим заново
    with open(p, "rb") as fh:
        res = tg(method, chat_id=chat, caption=text, files={field: (p.name, fh)}, **extra)
    fid = (res.get("video") or res.get("document") or {}).get("file_id")
    if fid:
        cache[key] = fid
        FILE_IDS.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- база ----------

def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def lead(con, uid):
    return con.execute("SELECT * FROM leads WHERE user_id=?", (uid,)).fetchone()


def setf(con, uid, **kv):
    con.execute(f"UPDATE leads SET {', '.join(k + '=?' for k in kv)} WHERE user_id=?", (*kv.values(), uid))
    con.commit()


# ---------- воронка ----------

def start(con, f, user, label):
    uid = user["id"]
    row = lead(con, uid)
    names = (user.get("username"), user.get("first_name"), user.get("last_name"))
    if row is None:
        con.execute("INSERT INTO leads(user_id, username, first_name, last_name, label, started, answers) "
                    "VALUES (?, ?, ?, ?, ?, ?, '[]')", (uid, *names, label, time.time()))
        con.commit()
        log("Воронка: новый человек", uid, "метка", label)
    else:
        setf(con, uid, username=names[0], first_name=names[1], last_name=names[2])
        log("Воронка: снова пришёл", uid, "метка", label)
    say(uid, f["приветствие"])
    send_magnet(uid, f["лид_магнит"])
    setf(con, uid, magnet=time.time())
    row = lead(con, uid)
    if row["watched"] is None:
        minutes = OWNER_DELAY_MIN if str(uid) == config.TG_CHAT else f["вопрос_через_минут"]
        setf(con, uid, ask_at=time.time() + minutes * 60, asked=None)
    elif row["q"] is not None:
        ask(con, f, uid)   # анкету уже начал – продолжаем с того же вопроса


def ask(con, f, uid):
    """Задать текущий вопрос анкеты."""
    i = lead(con, uid)["q"]
    q = f["вопросы"][i]
    opts = q.get("кнопки") or []
    head = f"Вопрос {i + 1} из {len(f['вопросы'])}\n\n{q['текст']}"
    say(uid, head, [[(o, f"a:{i}:{k}")] for k, o in enumerate(opts)] or None)


def watched(con, f, uid):
    row = lead(con, uid)
    if row is None:
        return
    if row["done"] is not None:
        say(uid, f["после_анкеты"])
        return
    if row["watched"] is None:
        setf(con, uid, watched=time.time(), ask_at=None, q=0, answers="[]")
        say(uid, f["перед_анкетой"])
    ask(con, f, uid)


def answer(con, f, uid, value):
    row = lead(con, uid)
    i = row["q"]
    answers = json.loads(row["answers"] or "[]")[:i] + [value]
    if i + 1 < len(f["вопросы"]):
        setf(con, uid, answers=json.dumps(answers, ensure_ascii=False), q=i + 1)
        ask(con, f, uid)
        return
    setf(con, uid, answers=json.dumps(answers, ensure_ascii=False), q=None, done=time.time())
    log("Воронка: анкета заполнена", uid)
    say(uid, f["после_анкеты"])
    notify(con, f, uid)


def notify(con, f, uid):
    row = lead(con, uid)
    e = html.escape
    name = " ".join(x for x in (row["first_name"], row["last_name"]) if x) or "без имени"
    nick = f"@{e(row['username'])}" if row["username"] else f"без ника (id {uid})"
    lines = ["📩 <b>Заявка из воронки</b>", "",
             f"<b>{e(name)}</b>, {nick}",
             f"Ролик: <b>{e(row['label'])}</b>", ""]
    for k, (q, a) in enumerate(zip(f["вопросы"], json.loads(row["answers"])), 1):
        title = q.get("коротко") or q["текст"].split("\n")[0][:80]
        lines.append(f"{k}. {e(title)}\n<b>{e(a)}</b>")
    try:
        to_owner("\n".join(lines))
        setf(con, uid, notified=time.time())
        log("Воронка: заявка отправлена тебе", uid)
    except Exception as ex:
        log("Воронка: заявка не ушла в личный бот, повторю позже:", ex)


def due(con, f):
    """Отложенные сообщения: «Получилось посмотреть?» и заявки, которые не ушли с первого раза."""
    for row in con.execute("SELECT user_id FROM leads WHERE ask_at <= ? AND asked IS NULL AND watched IS NULL",
                           (time.time(),)).fetchall():
        uid = row["user_id"]
        try:
            say(uid, f["вопрос_посмотрел"], [[(f["кнопка_да"], "w:yes")], [(f["кнопка_нет"], "w:no")]])
        except RuntimeError as ex:
            log("Воронка: не спросил", uid, ex)   # человек заблокировал бота – больше не пробуем
        setf(con, uid, asked=time.time(), ask_at=None)
    for row in con.execute("SELECT user_id FROM leads WHERE done IS NOT NULL AND notified IS NULL").fetchall():
        notify(con, f, row["user_id"])


def on_message(con, f, m):
    uid = (m.get("from") or {}).get("id")
    if not uid or (m.get("chat") or {}).get("type") != "private":
        return
    text = (m.get("text") or "").strip()
    is_owner = str(uid) == config.TG_CHAT
    if is_owner and (norm(text).split() or [""])[0] in ("итоги", "stats"):
        say(uid, report(con, *([int(x) for x in text.split()[1:2] if x.isdigit()])))
        return
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        start(con, f, m["from"], clean_label(parts[1] if len(parts) > 1 else ""))
        return
    row = lead(con, uid)
    if row is not None and row["q"] is not None:
        q = f["вопросы"][row["q"]]
        if q.get("кнопки"):
            say(uid, f["нажми_кнопку"])
        elif text:
            answer(con, f, uid, text)
        else:
            say(uid, "Напиши ответ текстом 👇")
        return
    words = set(norm(text).split())
    hit = next((w for w in f["кодовые_слова"] if norm(w) in words), None)
    if hit:
        start(con, f, m["from"], clean_label(norm(hit)))
    elif row is None:
        start(con, f, m["from"], NO_LABEL)   # написал что-то своё – всё равно отдаём лид-магнит
    else:
        say(uid, f["не_понял"])


def on_callback(con, f, c):
    uid = c["from"]["id"]
    data = c.get("data") or ""
    msg = c.get("message") or {}
    note = ""
    row = lead(con, uid)
    if row is None:
        note = "Напиши /start"
    elif data in ("w:yes", "w:no"):
        drop_buttons(msg)
        if data == "w:yes":
            watched(con, f, uid)
        else:
            setf(con, uid, said_no=time.time())
            say(uid, f["ответ_нет"], [[(f.get("кнопка_посмотрел") or f["кнопка_да"], "w:yes")]])
    elif data.startswith("a:"):
        _, i, k = data.split(":")
        i, k = int(i), int(k)
        if row["q"] != i:
            note = "Этот вопрос уже позади"
        else:
            value = f["вопросы"][i]["кнопки"][k]
            mark_answer(msg, value)
            answer(con, f, uid, value)
    try:
        tg("answerCallbackQuery", callback_query_id=c["id"], text=note)
    except Exception:
        pass


def drop_buttons(msg):
    try:
        tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           reply_markup={"inline_keyboard": []})
    except Exception:
        pass


def mark_answer(msg, value):
    """Под вопросом остаётся выбранный ответ вместо кнопок."""
    try:
        tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
           text=f"{msg.get('text', '')}\n\n→ {value}")
    except Exception:
        drop_buttons(msg)


def handle(con, f, u):
    try:
        if "message" in u:
            on_message(con, f, u["message"])
        elif "callback_query" in u:
            on_callback(con, f, u["callback_query"])
    except Exception as ex:
        log("Воронка: ошибка на сообщении:", repr(ex))


# ---------- итоги ----------

def report(con, days=None):
    since = time.time() - days * 86400 if days else 0
    rows = con.execute("""SELECT label, COUNT(*) n, COUNT(watched) w, COUNT(done) d FROM leads
                          WHERE started >= ? GROUP BY label ORDER BY n DESC""", (since,)).fetchall()
    period = f"за {days} дн." if days else "за всё время"
    if not rows:
        return f"Итоги воронки {period}: пока никто не пришёл."
    pct = lambda a, b: f"{round(100 * a / b)}%" if b else "–"
    out = [f"Итоги воронки {period}", "пришли → посмотрели → заполнили анкету", ""]
    for r in rows:
        out.append(f"{r['label']}: {r['n']} → {r['w']} → {r['d']} ({pct(r['d'], r['n'])} дошли до заявки)")
    n, w, d = (sum(r[k] for r in rows) for k in ("n", "w", "d"))
    out += ["", f"Всего: {n} → {w} → {d} ({pct(d, n)})",
            "Человек считается по ролику, с которого пришёл первый раз."]
    return "\n".join(out)


# ---------- запуск ----------

def listen():
    if not TOKEN:
        sys.exit("Нет FUNNEL_BOT_TOKEN в .env – это токен бота воронки от @BotFather")
    if TOKEN == config.TG_TOKEN:
        sys.exit("FUNNEL_BOT_TOKEN совпадает с личным ботом. Для воронки нужен отдельный бот")
    f = load_funnel()
    bad = problems(f)
    if bad:
        sys.exit("funnel.json заполнен не до конца:\n- " + "\n- ".join(bad))
    con = connect()
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    log("Бот воронки слушает")
    while True:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                             params={"offset": offset, "timeout": POLL,
                                     "allowed_updates": '["message","callback_query"]'}, timeout=POLL + 20).json()
            if r.get("error_code") == 409:
                sys.exit("Бот воронки уже запущен в другом месте. Проверь: python funnel.py status")
            if r.get("error_code") == 401:
                sys.exit("Telegram не принял FUNNEL_BOT_TOKEN. Возьми токен заново в @BotFather")
            if FUNNEL.stat().st_mtime > f.get("_loaded", 0):   # тексты поправили – подхватываем без перезапуска
                fresh = load_funnel()
                if problems(fresh):
                    log("funnel.json с ошибкой, работаю на старых текстах. Проверь: python funnel.py check")
                else:
                    if "_loaded" in f:
                        log("funnel.json обновлён")
                    f = fresh
                f["_loaded"] = FUNNEL.stat().st_mtime
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                OFFSET.write_text(str(offset))
                handle(con, f, u)
            due(con, f)
        except requests.RequestException as ex:
            log("Нет связи с Telegram:", ex)
            time.sleep(10)


def check():
    ok = True
    f = load_funnel()
    bad = problems(f)
    if bad:
        ok = False
        print("❌ funnel.json:\n- " + "\n- ".join(bad))
    else:
        print(f"✅ funnel.json: {len(f['вопросы'])} вопросов, лид-магнит – {f['лид_магнит']['тип']}, "
              f"вопрос «посмотрел?» через {f['вопрос_через_минут']} мин.")
    if not (config.TG_TOKEN and config.TG_CHAT):
        ok = False
        print("❌ Личный бот не настроен: нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env. Заявкам некуда приходить")
    else:
        print("✅ Заявки придут в личный бот (TELEGRAM_CHAT_ID)")
    if not TOKEN:
        print("❌ Нет FUNNEL_BOT_TOKEN в .env")
        sys.exit(1)
    if TOKEN == config.TG_TOKEN:
        print("❌ FUNNEL_BOT_TOKEN совпадает с личным ботом. Для воронки нужен отдельный бот")
        sys.exit(1)
    try:
        me = tg("getMe")
    except Exception as ex:
        print("❌ Telegram не принял FUNNEL_BOT_TOKEN:", ex)
        sys.exit(1)
    print(f"✅ Бот воронки: @{me['username']}")
    print(f"\nСсылка для шапки профиля: https://t.me/{me['username']}")
    for w in f["кодовые_слова"]:
        print(f"Кодовое слово «{w}» → метка {clean_label(norm(w))}")
    print(f"Ссылка с меткой ролика: https://t.me/{me['username']}?start=reel_01")
    sys.exit(0 if ok else 1)


def link(label):
    me = tg("getMe")
    lb = clean_label(label)
    if lb != label:
        print(f"В метке можно только латиницу, цифры, _ и -. Взял: {lb}")
    print(f"https://t.me/{me['username']}?start={lb}")


def on():
    if not TOKEN:
        sys.exit("Нет FUNNEL_BOT_TOKEN в .env")
    if IS_MAC:
        dirs = [str(Path(p).parent) for p in (shutil.which("ffprobe"), sys.executable) if p]
        path = ":".join(dict.fromkeys(dirs + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))
        log_file = str(config.LOGS / "funnel.log")
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps({
            "Label": f"com.{NAME}", "ProgramArguments": [sys.executable, "-u", str(Path(__file__).resolve())],
            "WorkingDirectory": str(config.ROOT), "RunAtLoad": True, "KeepAlive": True,
            "StandardOutPath": log_file, "StandardErrorPath": log_file, "EnvironmentVariables": {"PATH": path}}))
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    elif IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")   # без чёрного окна
        exe = pyw if pyw.exists() else Path(sys.executable)
        q = lambda v: str(v).replace("'", "''")  # апостроф в пути для PowerShell удваивается
        ps = ("$ProgressPreference = 'SilentlyContinue';"
              f"$a = New-ScheduledTaskAction -Execute '{q(exe)}' -Argument '\"{q(Path(__file__).resolve())}\"' "
              f"-WorkingDirectory '{q(config.ROOT)}';"
              "$t = New-ScheduledTaskTrigger -AtLogOn;"
              "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 "
              "-RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 0);"
              f"Register-ScheduledTask -TaskName '{NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null;"
              f"Start-ScheduledTask -TaskName '{NAME}'")
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()   # кавычки и кириллица в пути не ломают PowerShell
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", enc], capture_output=True)
        if r.returncode != 0:
            sys.exit("Не получилось включить: " + r.stderr.decode("utf-8", "replace")[-500:])
    else:
        sys.exit("Поддерживаются macOS и Windows")
    print("Включено: бот воронки отвечает, пока компьютер включён, и сам стартует после перезагрузки.")


def off():
    if IS_MAC:
        subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
        PLIST.unlink(missing_ok=True)
    elif IS_WIN:
        subprocess.run(["schtasks", "/End", "/TN", NAME], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/TN", NAME, "/F"], capture_output=True)
    print("Выключено.")


def status():
    if IS_MAC:
        r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        print("Включено" if f"com.{NAME}" in r.stdout else "Выключено")
    elif IS_WIN:
        r = subprocess.run(["schtasks", "/Query", "/TN", NAME], capture_output=True)
        print("Включено" if r.returncode == 0 else "Выключено")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("итоги", "stats"):
        days = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
        print(report(connect(), days))
    elif cmd == "link":
        link(sys.argv[2] if len(sys.argv) > 2 else "reel_01")
    else:
        {"on": on, "off": off, "status": status, "check": check}.get(cmd, listen)()
