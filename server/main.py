from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import socketio
import csv
import asyncio
import io
import time
from pathlib import Path
from contextlib import asynccontextmanager

# ===============================
# 設定
# ===============================
BASE_DIR = Path(__file__).resolve().parent.parent
CLIENT_DIR = BASE_DIR / "client"

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
    """
    受信したデータ(bytesまたはstr)を適切なエンコーディングでデコードし、
    CSVとしてパースする
    """
    text = ""

    # 既に文字列ならそのまま使う
    if isinstance(file_content, str):
        text = file_content
    else:
        # バイナリならデコードを試みる
        try:
            # 1. UTF-8 (BOM付き対応)
            text = file_content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                # 2. Shift-JIS (Excel標準)
                text = file_content.decode("cp932")
            except UnicodeDecodeError:
                print("Decode Error: Neither UTF-8 nor CP932")
                return []
        except AttributeError:
            # 万が一その他の型が来た場合
            text = str(file_content)

    try:
        # 文字列をファイルオブジェクトのように扱う
        # strip()で前後の余計な空白や改行を除去
        f = io.StringIO(text.strip())
        reader = csv.DictReader(f)

        if not reader.fieldnames:
            return [], "CSVエラー：ヘッダー（1行目）が見つかりません。"

        normalized_headers = [h.strip().replace('\ufeff', '') for h in reader.fieldnames]
        reader.fieldnames = normalized_headers

        if "question" not in normalized_headers or "answer" not in normalized_headers:
            return [], f"フォーマットエラー：必須列(question, answer)がありません。現在の列: {normalized_headers}"

        questions = list(reader)
        if not questions:
            return [], "データエラー：問題データが0件です。"

        return questions, None

    except Exception as e:
        print(f"Parse Error: {e}")
        return [], f"解析エラー：{str(e)}"


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
            # アクティブな（接続中の）ユーザー数をカウント
            # sid が None でないユーザーを探す
            active_count = sum(1 for p in r["players"].values() if p.get("sid") is not None)

            if active_count == 0:
                if r.get("empty_at") is None:
                    r["empty_at"] = now
                elif now - r["empty_at"] > ROOM_TIMEOUT:
                    rooms_to_delete.append(room_id)
            else:
                r["empty_at"] = None

        for room_id in rooms_to_delete:
            print(f"Deleting empty room: {room_id}")
            del rooms[room_id]


# =====================================================
# 切断検知
# =====================================================
@sio.event
async def disconnect(sid):
    # リロード等で切断された場合、データを削除せず「オフライン」としてマークするだけにする
    for room_id, r in rooms.items():
        for uid, p in r["players"].items():
            if p["sid"] == sid:
                p["sid"] = None  # オフライン状態にする
                # leave_room（データ削除）は呼び出さない！
                return


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

    questions, error_msg = parse_questions(file_content)
    if error_msg:
        await sio.emit("error_msg", error_msg, to=sid)
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
        "empty_at": None
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

    r["empty_at"] = None

    # --- ユーザー登録ロジック ---
    if user_id in r["players"]:
        # 1. 既存のIDで戻ってきた場合
        r["players"][user_id]["sid"] = sid
        r["players"][user_id]["name"] = name
    else:
        # 2. 新しいIDだが、名前が重複しているかチェック
        existing_uid = next((uid for uid, p in r["players"].items() if p["name"] == name), None)

        if existing_uid:
            if r["players"][existing_uid]["sid"] is None:
                # データ引き継ぎ（成り代わり）
                r["players"][user_id] = r["players"][existing_uid]
                r["players"][user_id]["sid"] = sid
                del r["players"][existing_uid]

                # 特殊権限IDの更新
                if r["master_user_id"] == existing_uid:
                    r["master_user_id"] = user_id
                if r.get("quiz") and r["quiz"].get("buzzed_sid") == existing_uid:
                    r["quiz"]["buzzed_sid"] = user_id
            else:
                await sio.emit("error_msg", "その名前は既に使われています", to=sid)
                return
        else:
            # 3. 完全新規
            r["players"][user_id] = {"name": name, "score": 0, "sid": sid}

    await sio.enter_room(sid, room)
    await sio.emit("joined", to=sid)

    is_master = (user_id == r["master_user_id"])
    await sio.emit("role", {"isMaster": is_master}, to=sid)
    await sio.emit("master_info", {"name": r["master_name"]}, to=sid)
    await emit_players(room)

    if r["current"] >= 0:
        await sio.emit("counter", {"cur": r["current"] + 1}, to=sid)

    if is_master:
        await sio.emit("sync_state", r["state"], to=sid)

    if r["state"] == "finished":
        ranking = sorted(
            [p for uid, p in r["players"].items() if uid != r["master_user_id"]],
            key=lambda p: p["score"],
            reverse=True
        )
        await sio.emit("final", ranking, to=sid)

    # 画面状態の復元
    q = r.get("quiz")
    if q:
        # 途中経過か全文かを判断
        if r["state"] in ["asking", "buzzed", "wrong", "timeout"]:
            display_text = q["text"][:q["index"]]
        else:
            display_text = q["text"]

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
# 退室（明示的な退室ボタンでの操作）
# =====================================================
@sio.event
async def leave_room(sid, data):
    room = data.get("roomId")
    r = rooms.get(room)
    if not r: return

    user_id = next((uid for uid, p in r["players"].items() if p["sid"] == sid), None)
    if not user_id: return

    # 司会者は削除しない
    if user_id == r["master_user_id"]:
        return

    # 回答中の人が退室ボタンを押した場合は状態リセット
    q = r.get("quiz")
    if q and q.get("buzzed_sid") == user_id:
        q["buzzed_sid"] = None
        q["active"] = True
        r["state"] = "asking"
        await sio.emit("clear_buzzed", room=room)
        await sio.emit("enable_buzz", True, room=room)
        if r["master_user_id"] in r["players"]:
            master_sid = r["players"][r["master_user_id"]]["sid"]
            await sio.emit("sync_state", "asking", to=master_sid)

    del r["players"][user_id]
    await sio.leave_room(sid, room)
    await emit_players(room)

    active_count = sum(1 for p in r["players"].values() if p.get("sid") is not None)
    if active_count == 0:
        r["empty_at"] = time.time()


# =====================================================
# ゲーム進行
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


# =====================================================
# ★追加: 再同期リクエスト（スマホのバックグラウンド復帰対策）
# =====================================================
@sio.event
async def request_sync(sid, data):
    room = data.get("roomId")
    r = rooms.get(room)
    if not r: return

    # 司会者には状態コードも送る
    if sid == r["players"].get(r["master_user_id"], {}).get("sid"):
        await sio.emit("sync_state", r["state"], to=sid)

    q = r.get("quiz")
    if q:
        # 現在の状態に合わせて表示テキストを生成
        if r["state"] in ["asking", "buzzed", "wrong", "timeout"]:
            # 出題中などは、現在進んでいる文字数までを表示
            display_text = q["text"][:q["index"]]
        else:
            # 正解表示後などは全文表示
            display_text = q["text"]

        answer_text = ""
        if r["state"] in ["show_answer", "all_done"]:
            answer_text = f"正解：{q['answer']}"

        display_data = {"question": display_text, "answer": answer_text}
        await sio.emit("sync_display", display_data, to=sid)

        # 早押しボタンの状態も復元
        if q["buzzed_sid"]:
            buzzed_name = r["players"][q["buzzed_sid"]]["name"]
            await sio.emit("buzzed", {"name": buzzed_name}, to=sid)
        elif r["state"] == "asking":
            await sio.emit("enable_buzz", True, to=sid)