const socket = io();

/* =====================================================
   userId
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
const nameInput = document.getElementById("name");
const roomInput = document.getElementById("room");

const game = document.getElementById("game");
const roomInfo = document.getElementById("roomInfo");
const roomIdText = document.getElementById("roomIdText");

const counter = document.getElementById("counter");
const questionArea = document.getElementById("questionArea");
const answerArea = document.getElementById("answerArea");
const buzzedArea = document.getElementById("buzzed");

const players = document.getElementById("players");
const masterNameEl = document.getElementById("masterName");

const buzzBtn = document.getElementById("buzzBtn");
const leaveBtn = document.getElementById("leaveBtn");
const masterControls = document.getElementById("masterControls");

// リザルト関連
const resultOverlay = document.getElementById("resultOverlay");
const resultList = document.getElementById("resultList");

const btnNext    = masterControls.querySelector('button[onclick="nextQ()"]');
const btnWrong   = masterControls.querySelector('button[onclick="wrong()"]');
const btnResume  = masterControls.querySelector('button[onclick="resume()"]');
const btnTimeout = masterControls.querySelector('button[onclick="timeout()"]');
const btnCorrect = masterControls.querySelector('button[onclick="correct()"]');
const btnClear   = masterControls.querySelector('button[onclick="clearDisplay()"]');
const btnEnd     = masterControls.querySelector('button[onclick="end()"]');
const btnClose   = masterControls.querySelector('button[onclick="closeRoom()"]');

/* =====================================================
   State
===================================================== */
let currentRoom = null;
let isMaster = false;
let myName = "";
let gameStarted = false;
const DEFAULT_BUZZED_TEXT = "回答権獲得者";

/* =====================================================
   Button State
===================================================== */
const MasterButtonState = {
  init:        { next:true,  wrong:false, resume:false, timeout:false, correct:false, clear:false, end:false },
  asking:      { next:false, wrong:false, resume:false, timeout:true,  correct:false, clear:false, end:false },
  buzzed:      { next:false, wrong:true,  resume:false, timeout:false, correct:true,  clear:false, end:false },
  wrong:       { next:false, wrong:false, resume:true,  timeout:false, correct:false, clear:false, end:false },
  timeout:     { next:false, wrong:false, resume:false, timeout:false, correct:true,  clear:false, end:false },
  show_answer: { next:false, wrong:false, resume:false, timeout:false, correct:false, clear:true,  end:false },
  all_done:    { next:false, wrong:false, resume:false, timeout:false, correct:false, clear:false, end:true },
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

function resetBuzzedDisplay() {
  buzzedArea.textContent = DEFAULT_BUZZED_TEXT;
  buzzedArea.classList.remove("active");
}

/* =====================================================
   Action
===================================================== */
function enter() {
  const name = nameInput.value.trim();
  const room = roomInput.value.trim();
  if (!name || !room) {
    alert("名前とルームIDを入力してください");
    return;
  }

  myName = name;
  currentRoom = room;
  gameStarted = false;

  const mode = document.querySelector('input[name="mode"]:checked').value;
  socket.emit(
    mode === "create" ? "create_room" : "join_room",
    { roomId: room, name, userId }
  );
}

function leaveRoom() {
  if (!confirm("ルームから退室しますか？")) return;
  socket.emit("leave_room", { roomId: currentRoom });
  resetToEntry();
}

function resetToEntry() {
  currentRoom = null;
  isMaster = false;
  gameStarted = false;

  entry.style.display = "block";
  game.style.display = "none";
  resultOverlay.style.display = "none"; // リザルトも消す

  questionArea.textContent = "";
  answerArea.textContent = "";
  counter.textContent = "";
  players.innerHTML = "";
  masterNameEl.textContent = "—";

  resetBuzzedDisplay();

  buzzBtn.disabled = true;
  buzzBtn.style.display = "inline";
  leaveBtn.style.display = "none";
  masterControls.style.display = "none";
  roomInfo.style.display = "none";
}

function buzz() { socket.emit("buzz", { roomId: currentRoom }); }
function nextQ() {
  resetBuzzedDisplay();
  questionArea.textContent = "";
  answerArea.textContent = "";
  socket.emit("next_question", { roomId: currentRoom });
  setState("asking");
}
function wrong() { socket.emit("wrong", { roomId: currentRoom }); setState("wrong"); }
function resume() { socket.emit("resume", { roomId: currentRoom }); setState("asking"); }
function timeout() { socket.emit("timeout", { roomId: currentRoom }); setState("timeout"); }
function correct() { socket.emit("judge", { roomId: currentRoom }); setState("show_answer"); }
function clearDisplay() {
  socket.emit("clear_display", { roomId: currentRoom });
  setState("init");
}
function end() { socket.emit("end_game", { roomId: currentRoom }); }
function closeRoom() {
  if (!confirm("ルームを解散しますか？")) return;
  socket.emit("close_room", { roomId: currentRoom });
}
function closeResult() {
  resultOverlay.style.display = "none";
}

/* =====================================================
   Socket
===================================================== */
socket.on("joined", () => {
  entry.style.display = "none";
  game.style.display = "block";
  resetBuzzedDisplay();
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
    masterNameEl.textContent = myName;
  } else {
    buzzBtn.style.display = "inline";
    masterControls.style.display = "none";
    buzzBtn.disabled = true;
    leaveBtn.style.display = gameStarted ? "none" : "inline";
    roomInfo.style.display = "none";
  }
});

socket.on("master_info", data => { masterNameEl.textContent = data.name; });
socket.on("counter", c => {
  counter.textContent = c.cur ? `第 ${c.cur} 問` : "";
  if (c.cur && !gameStarted) {
    gameStarted = true;
    leaveBtn.style.display = "none";
  }
});
socket.on("char", c => { questionArea.textContent += c; });

socket.on("buzzed", data => {
  buzzedArea.innerHTML = `💡 ${data.name} さんが回答者です！`;
  buzzedArea.classList.add("active");
  if (isMaster) setState("buzzed");
});

socket.on("clear_buzzed", () => { resetBuzzedDisplay(); });
socket.on("reveal", data => {
  questionArea.textContent = data.question;
  answerArea.textContent = `正解：${data.answer}`;
});
socket.on("clear_display", () => {
  questionArea.textContent = "";
  answerArea.textContent = "";
  counter.textContent = "";
  resetBuzzedDisplay();
  if (isMaster) setState("init");
});
socket.on("enable_buzz", flag => { buzzBtn.disabled = !flag; });

socket.on("players", ps => {
  players.innerHTML = "";
  Object.values(ps).forEach(p => {
    if (isMaster && p.name === myName) return;
    players.innerHTML += `<li>${p.name} : ${p.score}</li>`;
  });
});

/* =====================================================
   ★リザルト演出（修正箇所）
===================================================== */
socket.on("final", result => {
  // 1. 通常のリスト更新
  players.innerHTML = "";
  const filtered = isMaster ? result.filter(p => p.name !== myName) : result;

  // 参加者ゼロの場合のガード
  if (filtered.length === 0) {
    players.innerHTML = "<li>参加者なし</li>";
    setState("finished");
    return;
  }

  const max = Math.max(...filtered.map(p => p.score));
  filtered.forEach(p => {
    const mark = p.score === max ? "🏆️ " : "";
    players.innerHTML += `<li>${mark}${p.name} : ${p.score}</li>`;
  });

  // 2. リザルトオーバーレイの構築
  resultList.innerHTML = "";
  // 順位付け（同点対応なしの単純ソート済みリストと仮定）
  filtered.forEach((p, index) => {
    const rank = index + 1;
    const isWinner = (p.score === max && p.score > 0);
    const li = document.createElement("li");

    li.className = "result-card";
    if (isWinner) li.classList.add("winner");

    // アニメーションの遅延（上位ほど後から、あるいは順に）
    li.style.animationDelay = `${index * 0.1}s`;

    li.innerHTML = `
      <span class="rank-name">
        <span class="rank-badge">${rank}.</span> ${p.name}
      </span>
      <span class="score">${p.score}pts</span>
    `;
    resultList.appendChild(li);
  });

  // 3. 表示と紙吹雪
  resultOverlay.style.display = "flex";
  setState("finished");

  // 紙吹雪エフェクト (canvas-confetti)
  // 左右から発射
  const count = 200;
  const defaults = {
    origin: { y: 0.7 }
  };

  function fire(particleRatio, opts) {
    confetti(Object.assign({}, defaults, opts, {
      particleCount: Math.floor(count * particleRatio)
    }));
  }

  fire(0.25, { spread: 26, startVelocity: 55 });
  fire(0.2, { spread: 60 });
  fire(0.35, { spread: 100, decay: 0.91, scalar: 0.8 });
  fire(0.1, { spread: 120, startVelocity: 25, decay: 0.92, scalar: 1.2 });
  fire(0.1, { spread: 120, startVelocity: 45 });
});

socket.on("sync_state", state => { if (isMaster) setState(state); });
socket.on("sync_display", data => {
  if (data.question) questionArea.textContent = data.question;
  if (data.answer) answerArea.textContent = data.answer;
});
socket.on("error_msg", msg => { alert(msg); resetToEntry(); });
socket.on("room_closed", () => {
  const message = isMaster ? "ルームを解散しました" : "司会者がルームを解散しました";
  alert(message);
  resetToEntry();
});