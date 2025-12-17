const socket = io();

/* =====================================================
   userId（永続）
===================================================== */
let userId = localStorage.getItem("quiz_user_id");
if (!userId) {
  userId = crypto.randomUUID();
  localStorage.setItem("quiz_user_id", userId);
}

/* =====================================================
   DOM
===================================================== */
const entry = document.getElementById("entry");
const game = document.getElementById("game");

const nameInput = document.getElementById("name");
const roomInput = document.getElementById("room");

const questionArea = document.getElementById("questionArea");
const answerArea = document.getElementById("answerArea");
const buzzedArea = document.getElementById("buzzed");

const counter = document.getElementById("counter");
const players = document.getElementById("players");

const buzzBtn = document.getElementById("buzzBtn");
const leaveBtn = document.getElementById("leaveBtn");
const masterControls = document.getElementById("masterControls");

/* ★ ルームID表示 */
const roomInfo = document.getElementById("roomInfo");
const roomIdText = document.getElementById("roomIdText");

// 司会者ボタン
const btnNext    = masterControls.querySelector('button[onclick="nextQ()"]');
const btnWrong   = masterControls.querySelector('button[onclick="wrong()"]');
const btnResume  = masterControls.querySelector('button[onclick="resume()"]');
const btnTimeout = masterControls.querySelector('button[onclick="timeout()"]');
const btnCorrect = masterControls.querySelector('button[onclick="correct()"]');
const btnClear   = masterControls.querySelector('button[onclick="clearDisplay()"]');
const btnEnd     = masterControls.querySelector('button[onclick="end()"]');
const btnClose   = masterControls.querySelector('button[onclick="closeRoom()"]');

let currentRoom = null;
let isMaster = false;

/* ★ 大会開始フラグ（最初の出題後は true） */
let gameStarted = false;

/* =====================================================
   司会者ボタン状態
===================================================== */
const MasterButtonState = {
  init:        { next:true,  wrong:false, resume:false, timeout:false, correct:false, clear:false, end:false },
  asking:      { next:false, wrong:false, resume:false, timeout:true,  correct:false, clear:false, end:false },
  buzzed:      { next:false, wrong:true,  resume:false, timeout:false, correct:true,  clear:false, end:false },
  wrong:       { next:false, wrong:false, resume:true,  timeout:false, correct:false, clear:false, end:false },
  timeout:     { next:false, wrong:false, resume:false, timeout:false, correct:true,  clear:false, end:false },
  show_answer: { next:false, wrong:false, resume:false, timeout:false, correct:false, clear:true,  end:false },
  finished:    { next:false, wrong:false, resume:false, timeout:false, correct:false, clear:false, end:false },
};

function setState(state) {
  const s = MasterButtonState[state];
  if (!s) return;

  btnNext.disabled    = !s.next;
  btnWrong.disabled   = !s.wrong;
  btnResume.disabled  = !s.resume;
  btnTimeout.disabled = !s.timeout;
  btnCorrect.disabled = !s.correct;
  btnClear.disabled   = !s.clear;
  btnEnd.disabled     = !s.end;

  btnClose.disabled = false;
}

/* =====================================================
   入室
===================================================== */
function enter() {
  const name = nameInput.value.trim();
  const room = roomInput.value.trim();
  if (!name || !room) {
    alert("名前とルームIDを入力してください");
    return;
  }

  currentRoom = room;
  gameStarted = false;

  const mode = document.querySelector('input[name="mode"]:checked').value;

  socket.emit(
    mode === "create" ? "create_room" : "join_room",
    { roomId: room, name, userId }
  );
}

/* =====================================================
   退室（参加者用）
===================================================== */
function leaveRoom() {
  if (!confirm("ルームから退室しますか？")) return;

  socket.emit("leave_room", { roomId: currentRoom });
  resetToEntry();
}

/* =====================================================
   画面リセット
===================================================== */
function resetToEntry() {
  currentRoom = null;
  isMaster = false;
  gameStarted = false;

  entry.style.display = "block";
  game.style.display = "none";

  questionArea.textContent = "";
  answerArea.textContent = "";
  buzzedArea.innerHTML = "&nbsp;";
  counter.textContent = "";
  players.innerHTML = "";

  buzzBtn.disabled = true;
  buzzBtn.style.display = "inline";
  leaveBtn.style.display = "none";

  masterControls.style.display = "none";
  roomInfo.style.display = "none";
}

/* =====================================================
   操作
===================================================== */
function buzz() {
  socket.emit("buzz", { roomId: currentRoom });
}

function nextQ() {
  questionArea.textContent = "";
  answerArea.textContent = "";
  buzzedArea.innerHTML = "&nbsp;";

  /* ★ 最初の出題で大会開始 */
  gameStarted = true;
  leaveBtn.style.display = "none";

  socket.emit("next_question", { roomId: currentRoom });
  setState("asking");
}

function wrong() {
  socket.emit("wrong", { roomId: currentRoom });
  setState("wrong");
}

function resume() {
  socket.emit("resume", { roomId: currentRoom });
  setState("asking");
}

function timeout() {
  socket.emit("timeout", { roomId: currentRoom });
  setState("timeout");
}

function correct() {
  socket.emit("judge", { roomId: currentRoom });
  setState("show_answer");
}

function clearDisplay() {
  socket.emit("clear_display", { roomId: currentRoom });
  setState("init");
}

function end() {
  socket.emit("end_game", { roomId: currentRoom });
}

function closeRoom() {
  if (!confirm("ルームを解散しますか？")) return;
  socket.emit("close_room", { roomId: currentRoom });
}

/* =====================================================
   socket events
===================================================== */
socket.on("joined", () => {
  entry.style.display = "none";
  game.style.display = "block";
});

socket.on("role", data => {
  isMaster = data.isMaster;

  if (isMaster) {
    buzzBtn.style.display = "none";
    leaveBtn.style.display = "none";
    masterControls.style.display = "flex";
    setState("init");

    roomIdText.textContent = currentRoom;
    roomInfo.style.display = "block";
  } else {
    buzzBtn.style.display = "inline";
    masterControls.style.display = "none";
    buzzBtn.disabled = true;

    /* ★ 出題前のみ退室可 */
    leaveBtn.style.display = gameStarted ? "none" : "inline";
    roomInfo.style.display = "none";
  }
});

socket.on("char", c => {
  questionArea.textContent += c;
});

socket.on("counter", c => {
  counter.textContent = c.cur ? `第 ${c.cur} 問` : "";
});

socket.on("buzzed", data => {
  buzzedArea.innerHTML = `💡 <strong>${data.name}</strong>さんが回答者です！`;
  if (isMaster) setState("buzzed");
});

socket.on("clear_buzzed", () => {
  buzzedArea.innerHTML = "&nbsp;";
});

socket.on("reveal", data => {
  questionArea.textContent = data.question;
  answerArea.textContent = `正解：${data.answer}`;
});

socket.on("clear_display", () => {
  questionArea.textContent = "";
  answerArea.textContent = "";
  counter.textContent = "";
  buzzedArea.innerHTML = "&nbsp;";
  if (isMaster) setState("init");
});

socket.on("enable_buzz", flag => {
  buzzBtn.disabled = !flag;
});

/* ===== 得点 ===== */
socket.on("players", ps => {
  players.innerHTML = "";
  Object.values(ps).forEach(p => {
    players.innerHTML += `<li>${p.name} : ${p.score}</li>`;
  });
});

/* ===== 結果 ===== */
socket.on("final", result => {
  players.innerHTML = "";
  const max = Math.max(...result.map(p => p.score));
  result.forEach(p => {
    const mark = p.score === max ? "🏆️ " : "";
    players.innerHTML += `<li>${mark}${p.name} : ${p.score}</li>`;
  });
  setState("finished");
});

socket.on("enable_end", () => {
  btnEnd.disabled = false;
});

/* ===== エラー ===== */
socket.on("error_msg", msg => {
  alert(msg);
  resetToEntry();
});

/* ===== ルーム解散 ===== */
socket.on("room_closed", () => {
  const message = isMaster
    ? "ルームを解散しました"
    : "司会者がルームを解散しました";

  alert(message);
  resetToEntry();
});
