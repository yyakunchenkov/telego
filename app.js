const $ = (id) => document.getElementById(id);
const telegram = window.Telegram?.WebApp;
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
let game = null;
let busy = false;
let noticeTimer;
let pending = null;
let token = '';
let recovery = null;
let mode = readStorage('telego.mode') === 'ai' ? 'ai' : 'problems';
let confirmedAction = null;
let currentUser = null;
let lastProblem = null;

function readStorage(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writeStorage(key, value) {
  try { value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value); } catch {}
}
function savePending(value) {
  pending = value;
  try { value ? sessionStorage.setItem('telego.pending', JSON.stringify(value)) : sessionStorage.removeItem('telego.pending'); } catch {}
}
function setBusy(value) {
  busy = value;
  $('board').setAttribute('aria-busy', String(value));
  $('random-button').disabled = value || Boolean(pending);
  $('pass-button').disabled = value || Boolean(pending);
  $('retry-button').disabled = value;
  $('retry-inline').disabled = value || Boolean(pending);
  $('connection-retry').disabled = value;
  $('ai-settings').disabled = value || Boolean(pending);
  for (const id of ['mode-problems', 'mode-ai', 'ai-new', 'ai-resign', 'ai-confirm-yes']) {
    $(id).disabled = value || Boolean(pending);
  }
  if (mode === 'ai') renderAiStatus();
}
function clearNotice() {
  clearTimeout(noticeTimer);
  $('move-notice').classList.add('hidden');
  $('board').classList.remove('invalid');
}
function showNotice(text) {
  clearNotice();
  $('move-notice').textContent = text;
  $('move-notice').classList.remove('hidden');
  $('board').classList.add('invalid');
  noticeTimer = setTimeout(clearNotice, 2800);
}
function showError(text, retry) {
  recovery = retry;
  $('connection-error').textContent = text;
  $('connection-panel').classList.remove('hidden');
  $('connection-retry').classList.toggle('hidden', !retry);
}
function clearError() {
  recovery = null;
  $('connection-panel').classList.add('hidden');
}
async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), path.startsWith('/api/ai/') ? 90000 : 12000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Timezone': zone, ...(token ? { Authorization: 'Bearer ' + token } : {}) },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос.');
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}
function renderWeek(counts = {}) {
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
  $('week').replaceChildren();
  ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].forEach((name, index) => {
    const day = new Date(monday);
    day.setDate(day.getDate() + index);
    const key = [day.getFullYear(), String(day.getMonth() + 1).padStart(2, '0'), String(day.getDate()).padStart(2, '0')].join('-');
    const count = counts[key] || 0;
    const element = document.createElement('div');
    element.className = 'day' + (count ? ' active' : '') + (day.toDateString() === now.toDateString() ? ' today' : '');
    element.textContent = name;
    element.setAttribute('aria-label', key + ': решено задач — ' + count);
    const value = document.createElement('i');
    value.textContent = count;
    element.append(value);
    $('week').append(element);
  });
}
function updateProgress(progress) {
  $('total-score').textContent = progress.total_score;
  renderWeek(progress.daily_solved);
}
function aiOutcome() {
  if (game?.kind !== 'ai' || game.status !== 'finished' || !/^[BW]\+/.test(game.result || '')) return null;
  return game.result.startsWith((game.to_play === 1 ? 'B' : 'W') + '+') ? 'won' : 'lost';
}
function renderBoard() {
  const board = $('board');
  board.replaceChildren();
  const solved = game?.status === 'solved';
  const outcome = aiOutcome();
  const territory = game?.kind === 'ai' && game.status === 'finished' ? game.territory : null;
  board.classList.toggle('solved', solved || outcome === 'won');
  board.classList.toggle('failed', game?.status === 'failed' || outcome === 'lost');
  $('problem-card').classList.toggle('solved', solved || outcome === 'won');
  $('problem-card').classList.toggle('lost', outcome === 'lost');
  $('territory-legend').classList.toggle('hidden', !territory);
  $('solved-badge').classList.toggle('hidden', !solved);
  $('retry-inline').classList.toggle('hidden', game?.status !== 'failed');
  $('pass-button').classList.toggle('hidden', !game?.can_pass);
  if (!game) return;
  board.style.setProperty('--size', game.size);
  board.setAttribute('aria-label', 'Доска го ' + game.size + ' на ' + game.size);
  const stars = game.size === 19 ? [3, 9, 15] : game.size === 13 ? [3, 6, 9] : [2, 4, 6];
  game.board.forEach((row, r) => row.forEach((color, c) => {
    const cell = document.createElement('button');
    cell.type = 'button';
    cell.className = 'cell';
    if (r === 0) cell.classList.add('first-row');
    if (r === game.size - 1) cell.classList.add('last-row');
    if (c === 0) cell.classList.add('first-col');
    if (c === game.size - 1) cell.classList.add('last-col');
    cell.setAttribute('aria-label', 'Линия ' + (r + 1) + ', столбец ' + (c + 1) + (color === 1 ? ', чёрный камень' : color === 2 ? ', белый камень' : ', свободно'));
    cell.onclick = () => userMove({row:r, col:c});
    if (stars.includes(r) && stars.includes(c) && (game.size === 19 || r === c || r + c === game.size - 1)) {
      const star = document.createElement('span');
      star.className = 'hoshi';
      star.setAttribute('aria-hidden', 'true');
      cell.append(star);
    }
    if (color) {
      const stone = document.createElement('span');
      stone.className = 'stone ' + (color === 1 ? 'black' : 'white');
      if (game.last_move?.row === r && game.last_move?.col === c) stone.classList.add('last-move');
      cell.append(stone);
    }
    const owner = territory?.[r]?.[c];
    if (owner === 1 || owner === 2) {
      const overlay = document.createElement('span');
      overlay.className = 'territory-overlay territory-' + (owner === 1 ? 'black' : 'white');
      overlay.setAttribute('aria-hidden', 'true');
      cell.append(overlay);
      cell.setAttribute('aria-label', cell.getAttribute('aria-label') + ', оценка территории ' + (owner === 1 ? 'чёрных' : 'белых'));
    }
    board.append(cell);
  }));
}
function openDialog(id) {
  if (!$(id).open) $(id).showModal();
}
function closeDialogs() {
  for (const id of ['retry-modal', 'all-complete-modal', 'ai-confirm']) if ($(id).open) $(id).close();
}
function renderMode() {
  const ai = mode === 'ai';
  for (const [id, selected] of [['mode-ai', ai], ['mode-problems', !ai]]) {
    $(id).classList.toggle('selected', selected);
    $(id).setAttribute('aria-pressed', String(selected));
  }
  $('mode-heading').textContent = ai ? 'ИГРА С ИИ' : 'ЗАДАЧА ДНЯ';
  $('daily-date').classList.toggle('hidden', ai);
  $('random-button').classList.toggle('hidden', ai);
  $('ai-status').classList.toggle('hidden', !ai);
  $('ai-settings').classList.toggle('hidden', !ai);
  $('ai-details').classList.toggle('hidden', !ai || !game);
  $('ai-actions').classList.toggle('hidden', !ai || !game);
  document.querySelector('.progress-section').classList.toggle('hidden', ai);
}
function renderAiStatus() {
  const thinking = busy && pending?.path.startsWith('/api/ai/') && (pending.body.kind === 'move' || pending.body.new);
  $('ai-status').classList.toggle('thinking', thinking);
  $('ai-status').classList.toggle('lost', aiOutcome() === 'lost');
  $('ai-resign').classList.toggle('hidden', game?.status !== 'active');
  if (thinking) $('ai-status').textContent = 'KataGo думает…';
  else if (!game) $('ai-status').textContent = busy ? 'Подключаемся к KataGo…' : 'Начните партию с KataGo';
  else if (game.status === 'active') $('ai-status').textContent = 'Ваш ход · Вы играете ' + (game.to_play === 1 ? 'чёрными' : 'белыми');
  else {
    const result = game.result || '';
    const player = game.to_play === 1 ? 'B' : 'W';
    const winner = result.startsWith(player + '+') ? 'Вы победили' : /^[BW]\+/.test(result) ? 'Победил KataGo' : 'Ничья';
    const margin = result.slice(2);
    $('ai-status').textContent = game.status === 'abandoned' ? 'Партия завершена' : winner + (margin === 'R' ? ' · сдача' : margin ? ' · разница ' + margin.replace('.', ',') : '');
  }
  const yours = game?.to_play === 1 ? 'black' : 'white';
  const theirs = yours === 'black' ? 'white' : 'black';
  $('ai-details').textContent = game ? game.size + '×' + game.size + ' · Коми ' + String(game.komi).replace('.', ',') +
    (game.handicap ? ' · Фора: ' + game.handicap : '') + (game.rules === 'japanese' ? ' · Японские правила' : ' · Китайские правила (ранее начатая партия)') + ' · Ходов: ' + game.move_count +
    ' · Взято камней: вы ' + game.captures[yours] + ', ИИ ' + game.captures[theirs] : '';
}
function aiOptions() {
  return {size:Number($('ai-size').value), player_color:Number($('ai-color').value), handicap:$('ai-handicap').checked, komi:Number($('ai-komi').value)};
}
function updateAiOptions() {
  const white = $('ai-color').value === '2';
  $('ai-handicap').disabled = white;
  if (white) $('ai-handicap').checked = false;
  const count = ({9:2,13:5,19:9})[$('ai-size').value];
  $('ai-handicap-label').textContent = 'Фора: ' + count + (count === 2 ? ' камня' : ' камней');
  $('ai-settings-hint').textContent = white ? 'Фора доступна при игре чёрными. Нажмите «Новая партия», чтобы применить настройки.' : 'Выберите настройки и нажмите «Новая партия». С форой KataGo ходит первым.';
}
function acceptResult(data) {
  clearNotice();
  closeDialogs();
  const previousGame = game;
  game = data.game;
  mode = game?.kind === 'ai' ? 'ai' : 'problems';
  if (mode === 'problems' && game) {
    lastProblem = game.problem_id;
    writeStorage('telego.problem.' + currentUser, lastProblem);
  }
  writeStorage('telego.mode', mode);
  renderMode();
  if (mode === 'ai' && previousGame?.id !== game.id) {
    $('ai-size').value = String(game.size);
    $('ai-color').value = String(game.to_play);
    $('ai-handicap').checked = Boolean(game.handicap);
    $('ai-komi').value = String([0.5, 6.5].includes(game.komi) ? game.komi : 6.5);
    updateAiOptions();
  }
  updateProgress(data.progress);
  $('daily-date').textContent = (data.date || '').split('-').reverse().join('.');
  const names = {easy:'ЛЁГКАЯ', intermediate:'СРЕДНЯЯ', hard:'СЛОЖНАЯ', other:'ОСОБАЯ'};
  $('difficulty').textContent = mode === 'ai' ? 'KATAGO' : game ? names[game.difficulty] || 'ЗАДАЧА' : 'КОЛЛЕКЦИЯ ПРОЙДЕНА';
  $('board-note').textContent = mode === 'ai' ? (game.status === 'finished' ?
    (game.territory ? (game.result?.endsWith('+R') ? 'После сдачи показана предварительная оценка территории.' : 'Подсчёт выполнен KataGo. Подсветка — оценка территории, а не отдельный подсчёт очков.') : 'Партия завершена. Оценка территории недоступна.') :
    (game.rules === 'japanese' ? 'Два паса подряд завершают партию. Территорию и пленных подсчитывает KataGo.' : 'Два паса подряд завершают партию. Перед пасом снимите мёртвые камни.')) : game ? (game.to_play === 1 ? 'Вы играете чёрными' : 'Вы играете белыми') + ' · Найдите Тэсудзи' : '';
  renderBoard();
  if (mode === 'ai') renderAiStatus();
  else if (data.progress.completed_all) openDialog('all-complete-modal');
  else if (game?.status === 'failed') openDialog('retry-modal');
  if (data.notice) showNotice(data.notice);
}
async function sendPending() {
  if (!pending || busy) return;
  const operation = pending;
  setBusy(true);
  clearError();
  try {
    const result = await api(operation.path, operation.body);
    savePending(null);
    acceptResult(result);
  } catch (error) {
    if (error.status && error.status < 500) {
      savePending(null);
      if (error.status === 401) {
        token = '';
        writeStorage('telego.token', null);
        showError(error.message, initialize);
      } else {
        showError(error.message, () => loadMode());
      }
    } else if (error.status === 503 && operation.path.startsWith('/api/ai/')) {
      // The server guarantees no saved move on engine failure; allow leaving AI mode.
      savePending(null);
      showError(error.message, () => requestOperation(operation.path, operation.body));
    } else {
      showError('Нет связи с сервером. Повторите запрос — попытка и баллы не будут учтены дважды.', sendPending);
    }
  } finally { setBusy(false); }
}
function requestOperation(path, body) {
  if (busy || pending) return;
  savePending({owner:currentUser, path, body:{...body, request_id:crypto.randomUUID()}});
  return sendPending();
}
function selectProblem(mode = 'daily') {
  return requestOperation('/api/game', {mode, problem_id:lastProblem, exclude:game?.problem_id});
}
function loadMode() {
  return mode === 'ai' ? requestOperation('/api/ai/game', {}) : selectProblem(lastProblem ? 'resume' : 'daily');
}
function switchMode(next) {
  if (busy || pending || next === mode) return;
  closeDialogs();
  clearNotice();
  clearError();
  mode = next;
  writeStorage('telego.mode', mode);
  game = null;
  renderMode();
  renderBoard();
  $('board-note').textContent = '';
  $('difficulty').textContent = mode === 'ai' ? 'KATAGO' : 'ЗАДАЧА';
  return loadMode();
}
function confirmAiAction(kind) {
  if (busy || pending || !game || mode !== 'ai') return;
  confirmedAction = kind;
  if (kind === 'new' && game.status !== 'active') return performAiAction();
  $('ai-confirm-title').textContent = kind === 'new' ? 'Начать новую партию?' : 'Сдаться?';
  const options = aiOptions();
  $('ai-confirm-text').textContent = kind === 'new' ? 'Текущая партия будет завершена. Новая: ' + options.size + '×' + options.size +
    ', вы играете ' + (options.player_color === 1 ? 'чёрными' : 'белыми') + (options.handicap ? ', с форой.' : ', без форы.') : 'Победа будет засчитана KataGo.';
  openDialog('ai-confirm');
}
function performAiAction() {
  if (busy || pending || !game || mode !== 'ai') return;
  closeDialogs();
  return confirmedAction === 'new' ? requestOperation('/api/ai/game', {new:true, ...aiOptions()}) :
    requestOperation('/api/ai/game/' + game.id + '/action', {kind:'resign', revision:game.revision});
}
function userMove(move) {
  if (busy || pending || !game) return;
  if (game.status === 'failed') return openDialog('retry-modal');
  if (game.status !== 'active') return;
  if (move && game.board[move.row][move.col]) return showNotice('Это пересечение уже занято.');
  return requestOperation((mode === 'ai' ? '/api/ai/game/' : '/api/game/') + game.id + '/action', {kind:'move', move, revision:game.revision});
}
function retryProblem() {
  if (!game || game.status !== 'failed') return;
  closeDialogs();
  return requestOperation('/api/game/' + game.id + '/action', {kind:'retry', revision:game.revision});
}
async function initialize() {
  if (busy) return;
  setBusy(true);
  clearError();
  try {
    // Telegram always authenticates the current signed user, never a previous browser user.
    if (telegram?.initData) {
      const session = await api('/api/session', {init_data:telegram.initData});
      token = session.token;
    } else {
      token = readStorage('telego.token') || '';
      if (token) {
        try { await api('/api/me'); } catch (error) { if (error.status === 401) token = ''; else throw error; }
      }
      if (!token) {
        const session = await api('/api/session', {init_data:''});
        token = session.token;
        writeStorage('telego.token', token);
      }
    }
    const profile = await api('/api/me');
    currentUser = profile.user_id;
    lastProblem = readStorage('telego.problem.' + currentUser);
    $('user-name').textContent = profile.name;
    updateProgress(profile.progress);
    try { pending = JSON.parse(sessionStorage.getItem('telego.pending') || 'null'); } catch { savePending(null); }
    if (pending && pending.owner !== currentUser) savePending(null);
    if (pending) mode = pending.path.startsWith('/api/ai/') ? 'ai' : 'problems';
    renderMode();
    setBusy(false);
    if (pending) await sendPending();
    else await loadMode();
  } catch (error) {
    showError(error.status ? error.message : 'Не удалось загрузить приложение. Проверьте соединение.', initialize);
  } finally { setBusy(false); }
}
document.addEventListener('DOMContentLoaded', () => {
  telegram?.ready();
  telegram?.expand();
  renderWeek();
  renderMode();
  updateAiOptions();
  $('ai-size').onchange = updateAiOptions;
  $('ai-color').onchange = updateAiOptions;
  $('mode-problems').onclick = () => switchMode('problems');
  $('mode-ai').onclick = () => switchMode('ai');
  $('ai-new').onclick = () => confirmAiAction('new');
  $('ai-resign').onclick = () => confirmAiAction('resign');
  $('ai-confirm-yes').onclick = performAiAction;
  $('ai-confirm-no').onclick = () => $('ai-confirm').close();
  $('random-button').onclick = () => selectProblem('next');
  $('pass-button').onclick = () => userMove(null);
  $('retry-button').onclick = retryProblem;
  $('retry-inline').onclick = retryProblem;
  $('retry-close').onclick = () => $('retry-modal').close();
  $('all-complete-close').onclick = () => $('all-complete-modal').close();
  $('connection-retry').onclick = () => recovery?.();
  initialize();
});
