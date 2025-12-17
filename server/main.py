from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import socketio
import csv
import asyncio
from pathlib import Path

# ===============================
# パス設定
# ===============================
BASE_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = BASE_DIR / "client"
QUESTIONS_FILE = BASE_DIR / "server" / "questions.csv"

# ===============================
# Socket.IO
# ===============================
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*"
)

# ===============================
# FastAPI
# ===============================
fastapi_app = FastAPI()

fastapi_app.mount(
    "/static",
    StaticFiles(directory=CLIENT_DIR),
    name="static"
)

@fastapi_app.get("/")
async def index():
    return FileResponse(CLIENT_DIR / "index.html")

# ===============================
# ASGI 統合
# ===============================
app = socketio.ASGIApp(
    sio,
    other_asgi_app=fastapi_app
)

# ===============================
# データ
# ===============================
rooms = {}

def load_questions():
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))

# =====================================================
# ルーム作成（司会者）
# =====================================================
@sio.event
async def create_room(sid, data):
    room = data["roomId"]
    name = data["name"]
    user_id = data["userId"]

    if room in rooms:
        await sio.emit("error_msg", "そのルームIDは既に使われています", to=sid)
        return

    rooms[room] = {
        "master_user_id": user_id,
        "players": {
            user_id: {"name": name, "score": 0, "sid": sid}
        },
        "questions": load_questions(),
        "current": -1,
        "quiz": None
    }

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)
    await sio.emit("role", {"isMaster": True}, to=sid)
    await sio.emit("players", rooms[room]["players"], room=room)

# =====================================================
# ルーム参加（★完全同期対応）
# =====================================================
@sio.event
async def join_room(sid, data):
    room = data["roomId"]
    name = data["name"]
    user_id = data["userId"]

    r = rooms.get(room)
    if not r:
        await sio.emit("error_msg", "存在しないルームIDです", to=sid)
        return

    if user_id in r["players"]:
        r["players"][user_id]["sid"] = sid
    else:
        if name in [p["name"] for p in r["players"].values()]:
            await sio.emit("error_msg", "その名前は既に使われています", to=sid)
            return
        r["players"][user_id] = {"name": name, "score": 0, "sid": sid}

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)
    await sio.emit("role", {"isMaster": user_id == r["master_user_id"]}, to=sid)
    await sio.emit("players", r["players"], room=room)

    # ===== 現在の状態を完全同期 =====
    if r["current"] >= 0:
        await sio.emit("counter", {"cur": r["current"] + 1}, to=sid)

    q = r.get("quiz")
    if q:
        await sio.emit(
            "sync_state",
            {
                "questionText": q["text"][:q["index"]],
                "answer": q["answer"] if q.get("revealed") else None,
                "enableBuzz": q["active"] and not q["buzzed_sid"],
                "buzzedName": (
                    r["players"][q["buzzed_sid"]]["name"]
                    if q["buzzed_sid"] else None
                )
            },
            to=sid
        )

# =====================================================
# 退室（参加者）
# =====================================================
@sio.event
async def leave_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    user_id = next(
        (uid for uid, p in r["players"].items() if p["sid"] == sid),
        None
    )
    if not user_id or user_id == r["master_user_id"]:
        return

    q = r.get("quiz")
    if q and q.get("buzzed_sid") == user_id:
        q["buzzed_sid"] = None
        q["active"] = True
        await sio.emit("clear_buzzed", room=room)
        await sio.emit("enable_buzz", True, room=room)

    del r["players"][user_id]
    await sio.leave_room(sid, room)
    await sio.emit("players", r["players"], room=room)

# =====================================================
# 出題
# =====================================================
@sio.event
async def next_question(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    if sid != r["players"][r["master_user_id"]]["sid"]:
        return

    r["current"] += 1
    if r["current"] >= len(r["questions"]):
        return

    qdata = r["questions"][r["current"]]
    r["quiz"] = {
        "text": qdata["question"],
        "answer": qdata["answer"],
        "index": 0,
        "active": True,
        "buzzed_sid": None,
        "revealed": False
    }

    await sio.emit("counter", {"cur": r["current"] + 1}, room=room)
    await sio.emit("enable_buzz", True, room=room)
    sio.start_background_task(char_loop, room)

async def char_loop(room):
    while True:
        r = rooms.get(room)
        if not r:
            break

        q = r["quiz"]
        if not q or not q["active"]:
            break
        if q["index"] >= len(q["text"]):
            break

        await sio.emit("char", q["text"][q["index"]], room=room)
        q["index"] += 1
        await asyncio.sleep(0.8)

# =====================================================
# 早押し
# =====================================================
@sio.event
async def buzz(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    q = r["quiz"]
    if not q or not q["active"] or q["buzzed_sid"]:
        return

    user_id = next(
        (uid for uid, p in r["players"].items() if p["sid"] == sid),
        None
    )
    if not user_id:
        return

    q["active"] = False
    q["buzzed_sid"] = user_id

    await sio.emit("buzzed", {"name": r["players"][user_id]["name"]}, room=room)
    await sio.emit("enable_buzz", False, room=room)

# =====================================================
# 誤答 / 再開 / 時間切れ
# =====================================================
@sio.event
async def wrong(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["buzzed_sid"] = None
        await sio.emit("clear_buzzed", room=room)

@sio.event
async def resume(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = True
        await sio.emit("enable_buzz", True, room=room)
        sio.start_background_task(char_loop, room)

@sio.event
async def timeout(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = False
        r["quiz"]["buzzed_sid"] = None
        await sio.emit("enable_buzz", False, room=room)
        await sio.emit("clear_buzzed", room=room)

# =====================================================
# 正解
# =====================================================
@sio.event
async def judge(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    q = r["quiz"]
    if not q:
        return

    if q["buzzed_sid"]:
        r["players"][q["buzzed_sid"]]["score"] += 10

    q["active"] = False
    q["revealed"] = True
    q["buzzed_sid"] = None

    await sio.emit(
        "reveal",
        {"question": q["text"], "answer": q["answer"]},
        room=room
    )
    await sio.emit("players", r["players"], room=room)
    await sio.emit("enable_buzz", False, room=room)
    await sio.emit("clear_buzzed", room=room)

    if r["current"] == len(r["questions"]) - 1:
        await sio.emit("enable_end", room=room)

# =====================================================
# 消去
# =====================================================
@sio.event
async def clear_display(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    if sid != r["players"][r["master_user_id"]]["sid"]:
        return

    r["quiz"] = None
    await sio.emit("clear_display", room=room)

# =====================================================
# 結果
# =====================================================
@sio.event
async def end_game(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    ranking = sorted(
        r["players"].values(),
        key=lambda p: p["score"],
        reverse=True
    )

    await sio.emit("final", ranking, room=room)

# =====================================================
# ルーム解散
# =====================================================
@sio.event
async def close_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    if sid != r["players"][r["master_user_id"]]["sid"]:
        return

    for p in r["players"].values():
        await sio.emit("room_closed", to=p["sid"])

    del rooms[room]
