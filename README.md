# teleGO — Telegram Mini App

A Russian-language Go problem trainer. The Telegram mini-app and local emulator use the same frontend, API, rules engine and SQLite database.

## Local development and emulation

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
TELEGO_DEV_MODE=1 uvicorn app:app --reload --host 127.0.0.1 --port 5000
```

- Main app: http://127.0.0.1:5000/
- Telegram-style emulator: http://127.0.0.1:5000/test_interface/

The emulator embeds the main app; no game code is duplicated. Local login is enabled only by `TELEGO_DEV_MODE=1` on localhost. Each browser gets a separate server-issued identity, persisted with a token in local storage. Main app and same-origin emulator share that identity. The old shared `local-guest` history stays in SQLite but is not assigned to new guests.

A separate static server on port 8080 can still serve [test_interface/](./test_interface); point its URL field at the FastAPI server. Do not open the main HTML as a file: it requires the API.

## Play with KataGo

Choose **Игра с ИИ** beside **Задачи**. Both the main app and emulator use the same local engine. In **Настройки новой партии**, choose a 9×9, 13×13 or 19×19 board and Black or White, then press **Новая партия**. Settings do not change an ongoing game until a new one is confirmed. If you choose White, KataGo plays Black's opening move automatically.

An optional standard handicap gives the user playing Black **2 stones on 9×9, 5 on 13×13, or 9 on 19×19** at fixed handicap points; KataGo then moves first as White. Handicap is unavailable when choosing White. New games use KataGo's **Japanese rules** (`japanese`, territory scoring, simple ko, seki tax, no suicide). Choose **6.5 or 0.5 komi** independently of handicap in the new-game settings. API clients omitting komi default to 6.5 without handicap or 0.5 with handicap. Handicap placements are setup stones and do not count as played moves or captures.

Pass, resignation and starting a new game are supported. Two consecutive passes finish the game and KataGo supplies the score, including its assessment of dead stones. Defeat highlights the board and result in red; a win highlights them in green. At game end, translucent black/white overlays show KataGo's predicted territory (also after resignation). Neutral/uncertain points and living stones are not shaded. This is a neural-network estimate, not an exact visualization of the score; after an early resignation it is preliminary. The board and captures are not modified by the overlay. If evaluation fails, the result is still saved and the UI reports that territory is unavailable.

The last game's size, color, handicap, rules, komi, position and final territory map resume from SQLite after reopening the app; these fields live in the existing `ai_games.position` JSON, so no table migration is required. Previously saved games without a rules field keep their original Chinese rules and komi (7.5 when absent), rather than changing scoring mid-game. Start a new game to use Japanese rules. Older games without size/color/handicap retain 19×19 / Black / no handicap defaults. AI games do not award puzzle points or change the puzzle calendar.

### Local engine installation (Apple Silicon, macOS 26)

The pinned package is stable **KataGo 1.18.2**, Homebrew revision 2, `arm64_tahoe`, using **Metal**, with the `kata1-b18c384nbt-s9996604416-d4316597426` network. The [upstream releases](https://github.com/lightvector/KataGo/releases) recommend Homebrew/Metal for macOS; the [Homebrew formula](https://formulae.brew.sh/formula/katago) builds the macOS backend from the stable tag. Upstream's downloadable 1.18.2 executables are CUDA-specific, whereas this Homebrew package is a native Metal build of that tag.

```sh
.venv/bin/python scripts/install_katago.py
TELEGO_DEV_MODE=1 .venv/bin/python -m uvicorn app:app --reload --host 127.0.0.1 --port 5000
```

The installer downloads the engine, bundled networks and runtime libraries into [engines/katago/](./engines/katago/), without installing Homebrew or changing system directories. It resumes partial downloads and verifies the SHA-256 pins in [packages.lock.json](./engines/katago/packages.lock.json) before extracting. The main package is about 345 MiB, plus dependencies; allow about 2 GB of free space for downloads, cached parts and extracted files. Downloaded binaries, models and runtime files are excluded from Git. Run the installer on a new checkout.

The first AI move may take longer while Metal initializes the network. [gtp.cfg](./engines/katago/gtp.cfg) limits search to 64 visits / 2 seconds per move, with pondering disabled. The server keeps one serialized engine process and restores each user's full move history before their turn. Database writes are short and happen after the engine responds. A failed engine response leaves the saved position unchanged; repeating a request does not play or score it twice. Engine details are logged to `engines/katago/runtime/engine.log`.

For another platform or an existing installation, set `KATAGO_EXECUTABLE`, `KATAGO_MODEL` and optionally `KATAGO_CONFIG` to absolute paths before starting the server. The bundled installer is specifically for Apple Silicon macOS 26 or newer. KataGo runs on the backend host, not in Telegram or the browser.

## Telegram deployment

Set `TELEGRAM_BOT_TOKEN` to the BotFather token in the server environment, leave `TELEGO_DEV_MODE` unset, serve through HTTPS and configure the bot's Mini App URL. Never put the bot token in frontend files.

The backend validates [Telegram initData](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), including its HMAC signature and authentication timestamp (24-hour maximum age), then issues a scoped expiring bearer session. Client-supplied user IDs, success flags, scores and attempt counts are not accepted. Existing numeric Telegram user IDs retain their progress.

## Game and scoring

- The server loads the SGF collection, including its 19×19 and 13×13 boards.
- Captures, suicide prohibition, occupied points and Japanese simple ko are checked before advancing.
- SGF passes advance the tree and turn. A Pass button appears only where the authored player continuation includes a pass. Opponent moves follow the first published variation.
- Success requires an explicit `Correct` / `Also correct` comment at the current node. The special 13×13 heart problem has a source-specific terminal marker, `Happy Valentine's Day!`: both authored solutions stop at an inescapable atari before the final capture. That exception is restricted to this problem. This is a puzzle solver, not a territory-scoring engine.
- A legal move outside the solution tree, or an unsuccessful variation ending, fails the attempt. Occupied points and illegal moves display feedback without changing the position.
- Failed attempts persist; switching tasks or reloading resumes the saved position. Retry resets a failed position. There is no client-side success endpoint.
- Solving after 0 / 1 / 2+ failures awards 100 / 40 / 10 points, once per user and problem.
- Moves and score updates are atomic. Request IDs deduplicate retries after a lost response; revisions reject stale moves from another tab.
- New task selection excludes solved problems and, when alternatives exist, the currently displayed problem. The final completion message uses membership in the current collection, not just historical counts.

## Data and migration

`TELEGO_DB_PATH` overrides the default SQLite file `telego.db`. Back up that file for deployment. Startup preserves existing users, scores and completion times; it adds any missing number column, backfills known problem numbers and creates session/game/request tables.

The stable SGF filename is the task identity: numerical problem numbers repeat across difficulties. Special unnumbered problems have a null number. Completion timestamps are stored in UTC. Calendar totals convert them to the browser's IANA timezone (including daylight-saving transitions); legacy SQLite timestamps are interpreted as UTC.

## Verification

```sh
pip install -r requirements-dev.txt
python -m unittest -v test_regressions test_katago
```

Tests use a temporary database, not user progress. They cover move rules, real SGF pass navigation, authentication, user isolation, scoring tiers, idempotency, transaction rollback, task selection, calendar dates and migration.

AI tests additionally cover coordinate conversion, full-history replay, two-pass scoring, resignation, engine failure rollback, game resumption, user isolation and puzzle-score separation. They mock engine responses so they run without a GPU; an actual engine smoke check can be run after installation:

```sh
.venv/bin/python scripts/check_katago.py
.venv/bin/python scripts/check_katago.py --all-settings
```
