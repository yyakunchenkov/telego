import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from game import IllegalMove, current_node, move_position, new_position
from katago_engine import EngineUnavailable, engine, new_ai_position
from sgf import problems

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("TELEGO_DB_PATH", BASE_DIR / "telego.db"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DEV_MODE = os.environ.get("TELEGO_DEV_MODE") == "1"
app = FastAPI(title="teleGO", version="3.0.0")


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS problem_progress (
                user_id TEXT NOT NULL, problem_id TEXT NOT NULL, problem_number INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                score INTEGER NOT NULL DEFAULT 0, completed_at TEXT,
                PRIMARY KEY (user_id, problem_id), FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, problem_id TEXT NOT NULL,
                position TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, problem_id), FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS requests (
                user_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                response TEXT NOT NULL, PRIMARY KEY(user_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS ai_games (
                game_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                position TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS ai_games_user ON ai_games(user_id);
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(problem_progress)")}
        if "problem_number" not in columns:
            db.execute("ALTER TABLE problem_progress ADD COLUMN problem_number INTEGER")
        for problem in problems():
            db.execute("UPDATE problem_progress SET problem_number = ? WHERE problem_id = ? AND problem_number IS NULL",
                       (problem["number"], problem["id"]))


initialize_database()


def verified_telegram_user(init_data):
    if not BOT_TOKEN:
        raise HTTPException(503, "Вход через Telegram пока не настроен.")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError()
        signature = fields.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        age = time.time() - int(fields["auth_date"])
        if not -30 <= age <= 86400:
            raise ValueError()
        user = json.loads(fields["user"])
        if type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError()
        return str(user["id"]), str(user.get("first_name") or "игрок")[:128]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(401, "Не удалось подтвердить вход. Откройте приложение заново в Telegram.")


def authenticated(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    with database() as db:
        row = db.execute(
            "SELECT users.* FROM sessions JOIN users USING(user_id) WHERE token_hash = ? AND expires > ?",
            (hashlib.sha256(token.encode()).hexdigest(), int(time.time())),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Сессия истекла. Войдите снова.")
    return dict(row)


def user_zone(request):
    try:
        return ZoneInfo(request.headers.get("x-timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, "Неизвестный часовой пояс.")


def catalog():
    return {problem["id"]: problem for problem in problems()}


def progress_summary(db, user_id, zone):
    rows = db.execute("SELECT * FROM problem_progress WHERE user_id = ?", (user_id,)).fetchall()
    solved = {row["problem_id"] for row in rows if row["completed"]}
    daily = {}
    for row in rows:
        if row["completed"] and row["completed_at"]:
            # Legacy CURRENT_TIMESTAMP values are UTC, too.
            stamp = datetime.fromisoformat(row["completed_at"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            day = stamp.astimezone(zone).date().isoformat()
            daily[day] = daily.get(day, 0) + 1
    ids = set(catalog())
    return {"total_score": sum(row["score"] for row in rows), "solved_count": len(solved),
            "daily_solved": daily, "completed_all": bool(ids) and ids <= solved,
            "remaining": len(ids - solved)}


@app.post("/api/session")
def login(payload: dict, request: Request):
    init_data = payload.get("init_data", "")
    if not isinstance(init_data, str):
        raise HTTPException(400, "Неверные данные входа.")
    if init_data:
        user_id, name = verified_telegram_user(init_data)
    elif DEV_MODE and request.url.hostname in {"localhost", "127.0.0.1", "::1", "testserver"}:
        # Anonymous local users get unguessable independent identities, never a shared guest.
        user_id, name = "dev:" + secrets.token_hex(16), "тестовый игрок"
    else:
        raise HTTPException(401, "Откройте teleGO через Telegram.")
    token = secrets.token_urlsafe(32)
    with database() as db:
        db.execute("""INSERT INTO users(user_id, display_name) VALUES (?, ?)
                      ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,
                      updated_at=CURRENT_TIMESTAMP""", (user_id, name))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                   (hashlib.sha256(token.encode()).hexdigest(), user_id, int(time.time()) + (2592000 if user_id.startswith("dev:") else 86400)))
    return {"token": token, "name": name}


@app.get("/api/me")
def me(request: Request, user=Depends(authenticated)):
    with database() as db:
        return {"user_id": user["user_id"], "name": user["display_name"],
                "progress": progress_summary(db, user["user_id"], user_zone(request))}


def request_key(payload):
    key = payload.get("request_id")
    if not isinstance(key, str) or not 16 <= len(key) <= 100:
        raise HTTPException(400, "Неверный идентификатор запроса.")
    return key


def replay(db, user_id, payload, route):
    key = request_key(payload)
    fingerprint = hashlib.sha256((route + json.dumps(payload, sort_keys=True)).encode()).hexdigest()
    row = db.execute("SELECT * FROM requests WHERE user_id=? AND request_id=?", (user_id, key)).fetchone()
    if row:
        if row["fingerprint"] != fingerprint:
            raise HTTPException(409, "Этот запрос уже использован.")
        return key, fingerprint, json.loads(row["response"])
    return key, fingerprint, None


def remember(db, user_id, key, fingerprint, result):
    db.execute("INSERT INTO requests VALUES (?, ?, ?, ?)", (user_id, key, fingerprint, json.dumps(result)))
    return result


def game_view(db, row, problem, zone, notice=""):
    position = json.loads(row["position"])
    children = current_node(problem, position)["children"]
    return {
        "game": {"id": row["game_id"], "revision": row["revision"], "problem_id": problem["id"],
                 "size": problem["size"], "difficulty": problem["difficulty"], "to_play": problem["to_play"],
                 "board": position["board"], "status": position["status"], "last_move": position["last_move"],
                 "can_pass": position["status"] == "active" and any(n["color"] == problem["to_play"] and n["move"] is None for n in children)},
        "progress": progress_summary(db, row["user_id"], zone), "notice": notice,
        "date": datetime.now(zone).date().isoformat(),
    }


@app.post("/api/game")
def select_game(payload: dict, request: Request, user=Depends(authenticated)):
    user_id, zone = user["user_id"], user_zone(request)
    items = catalog()
    if not items:
        raise HTTPException(503, "Коллекция задач недоступна.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        key, fingerprint, cached = replay(db, user_id, payload, "/api/game")
        if cached is not None:
            return cached
        solved = {r["problem_id"] for r in db.execute(
            "SELECT problem_id FROM problem_progress WHERE user_id=? AND completed=1", (user_id,))}
        remaining = [p for p in items.values() if p["id"] not in solved]
        if not remaining:
            return remember(db, user_id, key, fingerprint,
                            {"game": None, "progress": progress_summary(db, user_id, zone),
                             "date": datetime.now(zone).date().isoformat()})
        selected = next((p for p in remaining if p["id"] == payload.get("problem_id")), None)
        if payload.get("mode") == "resume" and selected is not None:
            problem = selected
        elif payload.get("mode") == "next":
            candidates = [p for p in remaining if p["id"] != payload.get("exclude")] or remaining
            problem = secrets.choice(candidates)
        else:
            day = datetime.now(zone).date().isoformat()
            index = int.from_bytes(hashlib.sha256(f"{user_id}:{day}".encode()).digest()[:4], "big")
            problem = remaining[index % len(remaining)]
        row = db.execute("SELECT * FROM games WHERE user_id=? AND problem_id=?", (user_id, problem["id"])).fetchone()
        if row is None:
            game_id = secrets.token_hex(16)
            db.execute("INSERT INTO games(game_id,user_id,problem_id,position) VALUES (?,?,?,?)",
                       (game_id, user_id, problem["id"], json.dumps(new_position(problem))))
            row = db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return remember(db, user_id, key, fingerprint, game_view(db, row, problem, zone))


def record_result(db, user_id, problem, status):
    db.execute("""INSERT INTO problem_progress(user_id,problem_id,problem_number)
                  VALUES (?,?,?) ON CONFLICT(user_id,problem_id) DO NOTHING""",
               (user_id, problem["id"], problem["number"]))
    old = db.execute("SELECT * FROM problem_progress WHERE user_id=? AND problem_id=?",
                     (user_id, problem["id"])).fetchone()
    if old["completed"]:
        return
    attempt = old["attempts"] + 1
    if status == "solved":
        score = 100 if attempt == 1 else 40 if attempt == 2 else 10
        db.execute("""UPDATE problem_progress SET attempts=?, completed=1, score=?, completed_at=?,
                      problem_number=? WHERE user_id=? AND problem_id=?""",
                   (attempt, score, datetime.now(timezone.utc).isoformat(), problem["number"], user_id, problem["id"]))
    elif status == "failed":
        db.execute("UPDATE problem_progress SET attempts=? WHERE user_id=? AND problem_id=?",
                   (attempt, user_id, problem["id"]))


@app.post("/api/game/{game_id}/action")
def game_action(game_id: str, payload: dict, request: Request, user=Depends(authenticated)):
    user_id, zone = user["user_id"], user_zone(request)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        key, fingerprint, cached = replay(db, user_id, payload, game_id)
        if cached is not None:
            return cached
        row = db.execute("SELECT * FROM games WHERE game_id=? AND user_id=?", (game_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Задача не найдена.")
        if payload.get("revision") != row["revision"]:
            raise HTTPException(409, "Позиция уже изменилась. Обновите задачу.")
        problem = catalog().get(row["problem_id"])
        if problem is None:
            raise HTTPException(404, "Задача больше не доступна.")
        position = json.loads(row["position"])
        notice = ""
        if payload.get("kind") == "retry":
            if position["status"] != "failed":
                raise HTTPException(409, "Повтор доступен после неудачной попытки.")
            position = new_position(problem)
        elif payload.get("kind") == "move":
            if position["status"] != "active":
                raise HTTPException(409, "Попытка уже завершена.")
            move = payload.get("move")
            if move is not None and (not isinstance(move, dict) or set(move) != {"row", "col"}
                                     or any(type(v) is not int for v in move.values())):
                raise HTTPException(400, "Неверные координаты.")
            try:
                move_position(problem, position, move)
            except IllegalMove as error:
                # A user move can fail before mutation; an invalid authored reply is a data error.
                if position["started"] != json.loads(row["position"])["started"] or position["path"] != json.loads(row["position"])["path"]:
                    raise HTTPException(422, "В варианте задачи найден недопустимый ход. Выберите другую задачу.")
                notice = str(error)
            else:
                if position["status"] in {"solved", "failed"}:
                    record_result(db, user_id, problem, position["status"])
        else:
            raise HTTPException(400, "Неизвестное действие.")
        db.execute("UPDATE games SET position=?, revision=revision+1 WHERE game_id=?",
                   (json.dumps(position), game_id))
        row = db.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        return remember(db, user_id, key, fingerprint, game_view(db, row, problem, zone, notice))


def ai_view(db, row, zone, notice=""):
    position = json.loads(row["position"])
    return {"mode": "ai", "game": {
        "kind": "ai",
        "id": row["game_id"], "revision": row["revision"], "size": len(position["board"]),
        "board": position["board"], "status": position["status"], "to_play": position.get("player_color", 1),
        "last_move": position["last_move"], "can_pass": position["status"] == "active",
        "captures": position["captures"], "move_count": len(position["moves"]),
        "result": position["result"], "rules": position.get("rules", "chinese-ogs"), "komi": position.get("komi", 7.5),
        "territory": position.get("territory") if position["status"] == "finished" else None,
        "handicap": position.get("handicap", 0),
    }, "progress": progress_summary(db, row["user_id"], zone), "notice": notice}


@app.post("/api/ai/game")
def select_ai_game(payload: dict, request: Request, user=Depends(authenticated)):
    user_id, zone = user["user_id"], user_zone(request)
    if type(payload.get("new", False)) is not bool:
        raise HTTPException(400, "Неверный запрос новой партии.")
    try:
        position = new_ai_position(payload.get("size", 19), payload.get("player_color", 1),
                                   payload.get("handicap", False), payload.get("komi"))
    except ValueError as error:
        raise HTTPException(400, str(error))
    route = "/api/ai/game"
    # An opening move can take time. Keep all engine work outside SQLite transactions.
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        key, fingerprint, cached = replay(db, user_id, payload, route)
        if cached is not None:
            return cached
        row = db.execute("SELECT * FROM ai_games WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (user_id,)).fetchone()
        if row is not None and not payload.get("new"):
            return remember(db, user_id, key, fingerprint, ai_view(db, row, zone))
        original = (row["game_id"], row["revision"]) if row else None
    if not engine.available():
        raise HTTPException(503, "KataGo пока недоступен. Попробуйте подключиться ещё раз позже.")
    try:
        position = engine.begin(position)
    except EngineUnavailable:
        raise HTTPException(503, "KataGo не смог начать партию. Предыдущая партия сохранена — повторите запрос.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        key, fingerprint, cached = replay(db, user_id, payload, route)
        if cached is not None:
            return cached
        row = db.execute("SELECT * FROM ai_games WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (user_id,)).fetchone()
        current = (row["game_id"], row["revision"]) if row else None
        if current != original:
            raise HTTPException(409, "Партия изменилась. Загрузите её снова.")
        if row is not None:
            old = json.loads(row["position"])
            if old["status"] == "active":
                old["status"] = "abandoned"
                db.execute("UPDATE ai_games SET position=?,revision=revision+1 WHERE game_id=?",
                           (json.dumps(old), row["game_id"]))
        game_id = secrets.token_hex(16)
        db.execute("INSERT INTO ai_games(game_id,user_id,position) VALUES (?,?,?)",
                   (game_id, user_id, json.dumps(position)))
        row = db.execute("SELECT * FROM ai_games WHERE game_id=?", (game_id,)).fetchone()
        return remember(db, user_id, key, fingerprint, ai_view(db, row, zone))


@app.post("/api/ai/game/{game_id}/action")
def ai_action(game_id: str, payload: dict, request: Request, user=Depends(authenticated)):
    user_id, zone = user["user_id"], user_zone(request)
    route = "/api/ai/game/" + game_id + "/action"
    # Never hold a database write lock while the GPU is thinking.
    with database() as db:
        key, fingerprint, cached = replay(db, user_id, payload, route)
        if cached is not None:
            return cached
        row = db.execute("SELECT * FROM ai_games WHERE game_id=? AND user_id=?", (game_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Партия не найдена.")
        revision = row["revision"]
        if type(payload.get("revision")) is not int or payload["revision"] != revision:
            raise HTTPException(409, "Позиция изменилась. Загрузите партию снова.")
        position = json.loads(row["position"])
    if position["status"] != "active":
        raise HTTPException(409, "Партия уже завершена.")
    notice = ""
    if payload.get("kind") == "resign":
        position = engine.resign(position)
    elif payload.get("kind") == "move":
        move = payload.get("move")
        if "move" not in payload or (move is not None and (
            not isinstance(move, dict) or set(move) != {"row", "col"}
            or any(type(v) is not int or not 0 <= v < len(position["board"]) for v in move.values())
        )):
            raise HTTPException(400, "Неверные координаты.")
        try:
            position = engine.turn(position, move)
        except IllegalMove as error:
            notice = str(error)
        except EngineUnavailable:
            raise HTTPException(503, "KataGo не смог ответить. Повторите ход — сохранённая позиция не изменится.")
    else:
        raise HTTPException(400, "Неизвестное действие.")
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        key, fingerprint, cached = replay(db, user_id, payload, route)
        if cached is not None:
            return cached
        updated = db.execute("UPDATE ai_games SET position=?,revision=revision+1 WHERE game_id=? AND revision=?",
                             (json.dumps(position), game_id, revision))
        if updated.rowcount != 1:
            raise HTTPException(409, "Позиция изменилась. Загрузите партию снова.")
        row = db.execute("SELECT * FROM ai_games WHERE game_id=?", (game_id,)).fetchone()
        return remember(db, user_id, key, fingerprint, ai_view(db, row, zone, notice))


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/test_interface/")
def emulator():
    return FileResponse(BASE_DIR / "test_interface/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/{filename:path}")
def static_file(filename: str):
    allowed = {"app.js", "styles.css", "test_interface/app.js", "test_interface/styles.css"}
    if filename not in allowed:
        raise HTTPException(404, "Not found")
    return FileResponse(BASE_DIR / filename, headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), reload=True)
