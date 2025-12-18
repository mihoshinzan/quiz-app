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
# データ管理
# ===============================
rooms = {}


def load_questions():
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def emit_players(room):
    """参加者リスト配信（司会者除く）"""
    r = rooms.get(room)
    if not r:
        return

    players_only = {
        uid: p for uid, p in r["players"].items()
        if uid != r["master_user_id"]
    }
    return sio.emit("players", players_only, room=room)


# =====================================================
# ルーム作成
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
        "master_name": name,
        "players": {
            user_id: {"name": name, "score": 0, "sid": sid}
        },
        "questions": load_questions(),
        "current": -1,
        "quiz": None,
        "state": "init"  # ★ 状態管理変数を追加
    }

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)
    await sio.emit("role", {"isMaster": True}, to=sid)

    # 司会者情報を全体に通知
    await sio.emit("master_info", {"name": name}, room=room)
    await emit_players(room)


# =====================================================
# ルーム参加（兼 司会者復帰）
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

    # プレイヤー情報更新 or 新規登録
    if user_id in r["players"]:
        r["players"][user_id]["sid"] = sid
        # 名前が変更されている場合の対応
        r["players"][user_id]["name"] = name
    else:
        if name in [p["name"] for p in r["players"].values()]:
            await sio.emit("error_msg", "その名前は既に使われています", to=sid)
            return
        r["players"][user_id] = {"name": name, "score": 0, "sid": sid}

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)

    # 司会者かどうかの判定
    is_master = (user_id == r["master_user_id"])
    await sio.emit("role", {"isMaster": is_master}, to=sid)

    # 司会者情報の同期
    await sio.emit("master_info", {"name": r["master_name"]}, to=sid)

    # 参加者リスト更新
    await emit_players(room)

    # 問題番号同期
    if r["current"] >= 0:
        await sio.emit("counter", {"cur": r["current"] + 1}, to=sid)

    # ★ 状態同期（リカバリー処理）
    # 途中参加やリロードした人のために、現在の画面状態を送る
    q = r.get("quiz")

    # 1. 状態コードの送信 (司会者のボタン復帰用)
    if is_master:
        await sio.emit("sync_state", r["state"], to=sid)

    # 2. 画面表示の復元
    if q:
        # 問題文の復元（現在の表示文字数まで、または全文）
        display_text = q["text"][:q["index"]] if q["active"] else q["text"]

        display_data = {
            "question": display_text,
            "answer": f"正解：{q['answer']}" if not q["active"] and q["buzzed_sid"] is None and r[
                "state"] == "show_answer" else ""
        }
        await sio.emit("sync_display", display_data, to=sid)

        # 早押し状態の復元
        if q["buzzed_sid"]:
            buzzed_name = r["players"][q["buzzed_sid"]]["name"]
            await sio.emit("buzzed", {"name": buzzed_name}, to=sid)
        else:
            # 早押しボタンの有効化状態
            await sio.emit("enable_buzz", q["active"], to=sid)


# =====================================================
# 退室
# =====================================================
@sio.event
async def leave_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r:
        return

    user_id = next((uid for uid, p in r["players"].items() if p["sid"] == sid), None)
    if not user_id:
        return

    # 司会者の場合はルーム削除せず、単に切断扱いとする（復帰待ち）
    if user_id == r["master_user_id"]:
        # ここで delete してしまうと復帰できないので保持する
        # 必要であれば一定時間後のGCなどを実装するが、今回は簡易化のため保持
        return

    # 早押し中の人が抜けた場合
    q = r.get("quiz")
    if q and q.get("buzzed_sid") == user_id:
        q["buzzed_sid"] = None
        q["active"] = True
        r["state"] = "asking"  # 状態を戻す
        await sio.emit("clear_buzzed", room=room)
        await sio.emit("enable_buzz", True, room=room)
        # 司会者への状態同期
        master_sid = r["players"][r["master_user_id"]]["sid"]
        await sio.emit("sync_state", "asking", to=master_sid)

    del r["players"][user_id]
    await sio.leave_room(sid, room)
    await emit_players(room)


# =====================================================
# 出題
# =====================================================
@sio.event
async def next_question(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if not r or sid != r["players"][r["master_user_id"]]["sid"]:
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
    r["state"] = "asking"  # ★

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


# =====================================================
# 早押し
# =====================================================
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
    r["state"] = "buzzed"  # ★

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
        r["state"] = "wrong"  # ★
        await sio.emit("clear_buzzed", room=room)


@sio.event
async def resume(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = True
        r["state"] = "asking"  # ★
        await sio.emit("enable_buzz", True, room=room)
        sio.start_background_task(char_loop, room)


@sio.event
async def timeout(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and r["quiz"]:
        r["quiz"]["active"] = False
        r["quiz"]["buzzed_sid"] = None
        r["state"] = "timeout"  # ★
        await sio.emit("enable_buzz", False, room=room)
        await sio.emit("clear_buzzed", room=room)


# =====================================================
# 正解
# =====================================================
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

    # ★修正開始: 最終問題かどうかの判定分岐を追加
    if r["current"] == len(r["questions"]) - 1:
        # 最終問題の場合: 状態を 'all_done' にし、司会者に通知
        r["state"] = "all_done"

        # 司会者のボタン状態を強制的に更新（これで消去ボタンが無効化される）
        # ※ judgeイベントを送った直後、クライアントは一時的に show_answer になるが、
        #    即座にこの sync_state で all_done に上書きされます。
        master_sid = r["players"][r["master_user_id"]]["sid"]
        await sio.emit("sync_state", "all_done", to=master_sid)
    else:
        # 通常の場合
        r["state"] = "show_answer"

    await sio.emit("reveal", {"question": q["text"], "answer": q["answer"]}, room=room)
    await emit_players(room)
    await sio.emit("enable_buzz", False, room=room)
    await sio.emit("clear_buzzed", room=room)

    if r["current"] == len(r["questions"]) - 1:
        await sio.emit("enable_end", room=room)


# =====================================================
# 消去 / 結果 / 解散
# =====================================================
@sio.event
async def clear_display(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and sid == r["players"][r["master_user_id"]]["sid"]:
        r["quiz"] = None
        r["state"] = "init"  # ★
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
        r["state"] = "finished"  # ★
        await sio.emit("final", ranking, room=room)


@sio.event
async def close_room(sid, data):
    room = data["roomId"]
    r = rooms.get(room)
    if r and sid == r["players"][r["master_user_id"]]["sid"]:
        for p in r["players"].values():
            await sio.emit("room_closed", to=p["sid"])
        del rooms[room]