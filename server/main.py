from fastapi import FastAPI
import socketio, csv, asyncio

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI()
app.mount("/", socketio.ASGIApp(sio, other_asgi_app=app))

rooms = {}

def load_questions():
    with open("questions.csv", encoding="utf-8") as f:
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
# ルーム参加 / 再接続
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

    if r["current"] >= 0:
        await sio.emit("counter", {"cur": r["current"] + 1}, to=sid)

    q = r["quiz"]
    if q:
        await sio.emit(
            "restore_question",
            {
                "text": q["text"][:q["index"]],
                "answer": q["answer"] if not q["active"] else None,
                "buzzed_name": (
                    r["players"][q["buzzed_sid"]]["name"]
                    if q["buzzed_sid"] else None
                ),
                "enable_buzz": q["active"]
            },
            to=sid
        )

# =====================================================
# 出題
# =====================================================
@sio.event
async def next_question(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    master_sid = r["players"][r["master_user_id"]]["sid"]
    if sid != master_sid:
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
        "buzzed_sid": None
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
# 正解（最終問題のみ結果ボタン有効）
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
    q["buzzed_sid"] = None

    await sio.emit("reveal", {"question": q["text"], "answer": q["answer"]}, room=room)
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

    master_sid = r["players"][r["master_user_id"]]["sid"]
    if sid != master_sid:
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
# ルーム解散（★ 確実通知版）
# =====================================================
@sio.event
async def close_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    master_sid = r["players"][r["master_user_id"]]["sid"]
    if sid != master_sid:
        return

    # ★ 全参加者の sid に直接送信（参加者側も必ず届く）
    for p in r["players"].values():
        await sio.emit("room_closed", to=p["sid"])

    del rooms[room]
