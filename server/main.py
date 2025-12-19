from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import socketio
import csv
import asyncio
import io
import time  # ★追加
from pathlib import Path
from contextlib import asynccontextmanager  # ★追加(起動時タスク用)

# ===============================
# 設定
# ===============================
BASE_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = BASE_DIR / "client"
SERVER_DIR = BASE_DIR / "server"

# 読み込み候補ファイル
QUESTIONS_CSV = SERVER_DIR / "questions.csv"
QUESTIONS_TXT = SERVER_DIR / "questions.txt"

# 自動解散までの猶予時間（秒）: 5分
ROOM_TIMEOUT = 300

# ===============================
# Socket.IO
# ===============================
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins="*"
)


# ===============================
# ライフサイクル管理（定期タスク起動）
# ===============================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時にクリーニングタスクを開始
    task = sio.start_background_task(cleanup_loop)
    yield
    # 終了時の処理（必要なら）


# ===============================
# FastAPI
# ===============================
fastapi_app = FastAPI(lifespan=lifespan)

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
# データ管理
# ===============================
rooms = {}


def parse_questions(file_content):
    """受信したテキストデータをCSVとしてパースする"""
    try:
        f = io.StringIO(file_content)
        return list(csv.DictReader(f))
    except Exception as e:
        print(f"Parse Error: {e}")
        return []


def emit_players(room):
    """参加者リスト配信（司会者除く）"""
    r = rooms.get(room)
    if not r: return

    players_only = {
        uid: p for uid, p in r["players"].items()
        if uid != r["master_user_id"]
    }
    return sio.emit("players", players_only, room=room)


# ★定期的にお掃除するループ
async def cleanup_loop():
    print("Cleanup task started.")
    while True:
        await asyncio.sleep(60)  # 1分ごとにチェック
        now = time.time()
        rooms_to_delete = []

        for room_id, r in rooms.items():
            # 参加者が0人かどうか
            if len(r["players"]) == 0:
                # empty_at が設定されていなければ設定
                if r.get("empty_at") is None:
                    r["empty_at"] = now
                # タイムアウト時間を超えていたら削除リスト入り
                elif now - r["empty_at"] > ROOM_TIMEOUT:
                    rooms_to_delete.append(room_id)
            else:
                # 人がいるならタイマーリセット
                r["empty_at"] = None

        for room_id in rooms_to_delete:
            print(f"Deleting empty room: {room_id}")
            del rooms[room_id]


# =====================================================
# 切断検知（ブラウザ閉じなど）
# =====================================================
@sio.event
async def disconnect(sid):
    # どのルームにいたか全検索（効率は悪いが人数が少なければ問題ない）
    target_room = None
    target_user_id = None

    for room_id, r in rooms.items():
        for uid, p in r["players"].items():
            if p["sid"] == sid:
                target_room = room_id
                target_user_id = uid
                break
        if target_room: break

    if target_room and target_user_id:
        # 退室処理を呼び出す
        await leave_room(sid, {"roomId": target_room})


# =====================================================
# ルーム作成
# =====================================================
@sio.event
async def create_room(sid, data):
    room = data["roomId"]
    name = data["name"]
    user_id = data["userId"]
    file_content = data.get("fileContent", "")

    if room in rooms:
        await sio.emit("error_msg", "そのルームIDは既に使われています", to=sid)
        return

    questions = parse_questions(file_content)
    if not questions:
        await sio.emit("error_msg", "問題ファイルの読み込みに失敗しました。", to=sid)
        return

    rooms[room] = {
        "master_user_id": user_id,
        "master_name": name,
        "players": {
            user_id: {"name": name, "score": 0, "sid": sid}
        },
        "questions": questions,
        "current": -1,
        "quiz": None,
        "state": "init",
        "empty_at": None  # ★空になった時刻
    }

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)
    await sio.emit("role", {"isMaster": True}, to=sid)
    await sio.emit("master_info", {"name": name}, room=room)
    await emit_players(room)


# =====================================================
# ルーム参加
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

    # 人が来たので削除タイマーリセット
    r["empty_at"] = None

    if user_id in r["players"]:
        r["players"][user_id]["sid"] = sid
        r["players"][user_id]["name"] = name
    else:
        if name in [p["name"] for p in r["players"].values()]:
            await sio.emit("error_msg", "その名前は既に使われています", to=sid)
            return
        r["players"][user_id] = {"name": name, "score": 0, "sid": sid}

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)

    is_master = (user_id == r["master_user_id"])
    await sio.emit("role", {"isMaster": is_master}, to=sid)
    await sio.emit("master_info", {"name": r["master_name"]}, to=sid)
    await emit_players(room)

    if r["current"] >= 0:
        await sio.emit("counter", {"cur": r["current"] + 1}, to=sid)

    # 状態復元
    if is_master:
        await sio.emit("sync_state", r["state"], to=sid)

    q = r.get("quiz")
    if q:
        display_text = q["text"][:q["index"]] if q["active"] else q["text"]
        answer_text = ""
        if r["state"] in ["show_answer", "all_done"]:
            answer_text = f"正解：{q['answer']}"

        display_data = {"question": display_text, "answer": answer_text}
        await sio.emit("sync_display", display_data, to=sid)

        if q["buzzed_sid"]:
            buzzed_name = r["players"][q["buzzed_sid"]]["name"]
            await sio.emit("buzzed", {"name": buzzed_name}, to=sid)
        elif r["state"] == "asking":
            await sio.emit("enable_buzz", True, to=sid)


# =====================================================
# 退室
# =====================================================
@sio.event
async def leave_room(sid, data):
    room = data.get("roomId")
    r = rooms.get(room)
    if not r: return

    user_id = next((uid for uid, p in r["players"].items() if p["sid"] == sid), None)
    if not user_id: return

    # 早押し中の人が抜けた場合のケア
    q = r.get("quiz")
    if q and q.get("buzzed_sid") == user_id:
        q["buzzed_sid"] = None
        q["active"] = True
        r["state"] = "asking"
        await sio.emit("clear_buzzed", room=room)
        await sio.emit("enable_buzz", True, room=room)

        # 司会者がいるなら状態同期
        if r["master_user_id"] in r["players"]:
            master_sid = r["players"][r["master_user_id"]]["sid"]
            await sio.emit("sync_state", "asking", to=master_sid)

    # プレイヤー削除
    del r["players"][user_id]
    await sio.leave_room(sid, room)
    await emit_players(room)

    # ★もし誰もいなくなったらタイマーセット
    if len(r["players"]) == 0:
        r["empty_at"] = time.time()


# =====================================================
# ゲーム進行系イベント（変更なし）
# =====================================================
@sio.event
async def next_question(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r or sid != r["players"][r["master_user_id"]]["sid"]: return

    r["current"] += 1
    if r["current"] >= len(r["questions"]): return

    qdata = r["questions"][r["current"]]
    r["quiz"] = {
        "text": qdata["question"],
        "answer": qdata["answer"],
        "index": 0,
        "active": True,
        "buzzed_sid": None
    }
    r["state"] = "asking"

    await sio.emit("counter", {"cur": r["current"] + 1}, room=room)
    await sio.emit("enable_buzz", True, room=room)
    sio.start_background_task(char_loop, room)


async def char_loop(room):
    while True:
        r = rooms.get(room)
        if not r: break
        q = r["quiz"]
        if not q or not q["active"]: break
        if q["index"] >= len(q["text"]): break

        await sio.emit("char", q["text"][q["index"]], room=room)
        q["index"] += 1
        await asyncio.sleep(1.0)


@sio.event
async def buzz(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r: return
    q = r["quiz"]
    if not q or not q["active"] or q["buzzed_sid"]: return
    user_id = next((uid for uid, p in r["players"].items() if p["sid"] == sid), None)
    if not user_id: return

    q["active"] = False
    q["buzzed_sid"] = user_id
    r["state"] = "buzzed"
    await sio.emit("buzzed", {"name": r["players"][user_id]["name"]}, room=room)
    await sio.emit("enable_buzz", False, room=room)


@sio.event
async def wrong(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["buzzed_sid"] = None
        r["state"] = "wrong"
        await sio.emit("clear_buzzed", room=room)


@sio.event
async def resume(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = True
        r["state"] = "asking"
        await sio.emit("enable_buzz", True, room=room)
        sio.start_background_task(char_loop, room)


@sio.event
async def timeout(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = False
        r["quiz"]["buzzed_sid"] = None
        r["state"] = "timeout"
        await sio.emit("enable_buzz", False, room=room)
        await sio.emit("clear_buzzed", room=room)


@sio.event
async def judge(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r or not r["quiz"]: return
    q = r["quiz"]
    if q["buzzed_sid"]:
        r["players"][q["buzzed_sid"]]["score"] += 10
    q["active"] = False
    q["buzzed_sid"] = None

    if r["current"] == len(r["questions"]) - 1:
        r["state"] = "all_done"
        master_sid = r["players"][r["master_user_id"]]["sid"]
        await sio.emit("sync_state", "all_done", to=master_sid)
    else:
        r["state"] = "show_answer"

    await sio.emit("reveal", {"question": q["text"], "answer": q["answer"]}, room=room)
    await emit_players(room)
    await sio.emit("enable_buzz", False, room=room)
    await sio.emit("clear_buzzed", room=room)


@sio.event
async def clear_display(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and sid == r["players"][r["master_user_id"]]["sid"]:
        r["quiz"] = None
        r["state"] = "init"
        await sio.emit("clear_display", room=room)


@sio.event
async def end_game(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r:
        ranking = sorted(
            [p for uid, p in r["players"].items() if uid != r["master_user_id"]],
            key=lambda p: p["score"],
            reverse=True
        )
        r["state"] = "finished"
        await sio.emit("final", ranking, room=room)


@sio.event
async def close_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and sid == r["players"][r["master_user_id"]]["sid"]:
        for p in r["players"].values():
            await sio.emit("room_closed", to=p["sid"])
        del rooms[room]