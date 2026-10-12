const $ = (id) => document.getElementById(id);
const appUrl = $('app-url');
if (location.pathname.startsWith('/test_interface/')) appUrl.value = location.origin + '/';

function openApp() {
  let url = appUrl.value.trim() || 'http://127.0.0.1:5000';
  if (!/^https?:\/\//i.test(url)) url = `http://${url}`;
  try {
    const target = new URL(url);
    if (!['http:', 'https:'].includes(target.protocol)) throw new Error();
    // This is the actual mini-app, not a copied UI or a separate rules engine.
    $('app-frame').src = target.href;
  } catch {
    $('connection-label').textContent = 'Укажите корректный адрес приложения';
    return;
  }
  $('mini-app').classList.remove('hidden');
  $('bot-status').textContent = 'мини-приложение';
  $('connection-label').textContent = 'приложение открыто';
}

function closeApp() {
  $('mini-app').classList.add('hidden');
  $('bot-status').textContent = 'бот';
  $('connection-label').textContent = 'готово';
}

function sendMessage(text) {
  const bubble = $('user-bubble');
  bubble.textContent = text;
  bubble.classList.remove('hidden');
}

$('open-button').addEventListener('click', openApp);
$('launch-card').addEventListener('click', openApp);
$('close-app').addEventListener('click', closeApp);
$('back-button').addEventListener('click', closeApp);
$('reset-button').addEventListener('click', () => { closeApp(); $('user-bubble').classList.add('hidden'); });
document.querySelector('[data-message]').addEventListener('click', (event) => sendMessage(event.currentTarget.dataset.message));
appUrl.addEventListener('keydown', (event) => { if (event.key === 'Enter') openApp(); });
