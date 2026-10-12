"""One bounded, serialized local GTP engine; SQLite remains the source of truth."""
import atexit
import copy
import logging
import math
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path

from game import IllegalMove, play

ROOT = Path(__file__).resolve().parent / "engines" / "katago"
LETTERS = "ABCDEFGHJKLMNOPQRST"
LOGGER = logging.getLogger(__name__)


class EngineUnavailable(RuntimeError):
    pass


class GTPRejected(RuntimeError):
    pass


def vertex(move, size=19):
    return "pass" if move is None else LETTERS[move["col"]] + str(size - move["row"])


def point(value, size=19):
    value = value.upper()
    if value == "PASS":
        return None
    try:
        col, row = LETTERS.index(value[0]), size - int(value[1:])
        if not 0 <= row < size or not 0 <= col < size:
            raise ValueError()
        return {"row": row, "col": col}
    except (IndexError, ValueError):
        raise EngineUnavailable("Unexpected KataGo move")


HANDICAPS = {9: 2, 13: 5, 19: 9}


def handicap_points(size):
    near, far, middle = (2 if size == 9 else 3), size - (3 if size == 9 else 4), size // 2
    if size == 9:
        pairs = [(near, far), (far, near)]
    elif size == 13:
        pairs = [(near, near), (near, far), (far, near), (far, far), (middle, middle)]
    else:
        pairs = [(r, c) for r in (near, middle, far) for c in (near, middle, far)]
    return [{"row": r, "col": c} for r, c in pairs]


def new_ai_position(size=19, player_color=1, handicap=False, komi=None):
    if type(size) is not int or size not in HANDICAPS or type(player_color) is not int or player_color not in (1, 2) or type(handicap) is not bool:
        raise ValueError("Некорректные настройки партии.")
    if handicap and player_color != 1:
        raise ValueError("Фора доступна при игре чёрными.")
    if komi is None:
        komi = 0.5 if handicap else 6.5
    if type(komi) not in (int, float) or komi not in (0.5, 6.5):
        raise ValueError("Выберите коми 6,5 или 0,5.")
    board = [[0] * size for _ in range(size)]
    if handicap:
        for p in handicap_points(size):
            board[p["row"]][p["col"]] = 1
    return {"board": board, "previous": None, "size": size,
            "player_color": player_color, "handicap": HANDICAPS[size] if handicap else 0,
            "rules": "japanese", "komi": komi, "territory": None,
            "moves": [], "status": "active", "last_move": None,
            "captures": {"black": 0, "white": 0}, "result": None}


def place(position, color, move):
    before = position["board"]
    after = play(before, move, color, position["previous"])
    removed = sum(v == 3 - color for row in before for v in row) - sum(v == 3 - color for row in after for v in row)
    position["captures"]["black" if color == 1 else "white"] += removed
    position["previous"], position["board"] = before, after
    position["last_move"] = move
    position["moves"].append(["B" if color == 1 else "W", vertex(move, len(before))])


class KataGo:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.selector = None
        self.log = None
        self.buffer = b""
        self.sequence = 0
        self.deadline = None

    def paths(self):
        binary = os.environ.get("KATAGO_EXECUTABLE") or next(ROOT.glob("packages/katago/*/bin/katago"), "")
        model = os.environ.get("KATAGO_MODEL") or next(ROOT.glob("packages/katago/*/share/katago/**/kata1-b18*.bin.gz"), "")
        config = os.environ.get("KATAGO_CONFIG", str(ROOT / "gtp.cfg"))
        if not binary or not model or not all(Path(p).is_file() for p in (binary, model, config)):
            raise EngineUnavailable("KataGo binary, neural network or configuration is missing")
        return str(Path(binary).resolve()), str(Path(model).resolve()), str(Path(config).resolve())

    def available(self):
        try:
            self.paths()
            return True
        except EngineUnavailable:
            return False

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        binary, model, config = self.paths()
        (ROOT / "runtime").mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        libraries = [str(p.resolve()) for p in ROOT.glob("packages/*/*/lib")]
        if libraries:
            environment["DYLD_LIBRARY_PATH"] = os.pathsep.join(libraries)
        self.log = (ROOT / "runtime" / "engine.log").open("ab")
        try:
            self.process = subprocess.Popen(
                [binary, "gtp", "-model", model, "-config", config],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                cwd=ROOT, env=environment, bufsize=0,
            )
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.buffer = b""
            self._command("protocol_version")
        except (OSError, EngineUnavailable, GTPRejected) as error:
            self.close()
            raise EngineUnavailable("Cannot start KataGo; see engines/katago/runtime/engine.log") from error

    def _command(self, command):
        self.sequence += 1
        number = str(self.sequence)
        try:
            self.process.stdin.write((number + " " + command + "\n").encode())
            deadline = self.deadline or time.monotonic() + 60
            while b"\n\n" not in self.buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.selector.select(remaining):
                    raise EngineUnavailable("KataGo response timed out")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise EngineUnavailable("KataGo exited unexpectedly")
                self.buffer += chunk.replace(b"\r\n", b"\n")
            block, self.buffer = self.buffer.split(b"\n\n", 1)
            response = block.decode().strip()
            if response.startswith("?" + number):
                raise GTPRejected(response[len(number) + 1:].strip())
            if not response.startswith("=" + number):
                raise EngineUnavailable("Unexpected GTP response")
            return response[len(number) + 1:].strip()
        except (OSError, UnicodeError) as error:
            raise EngineUnavailable("KataGo communication failed") from error

    def _restore(self, position):
        size = len(position["board"])
        self._command(f"boardsize {size}")
        self._command("clear_board")
        # Pre-existing games keep the rules under which they were started.
        self._command("kata-set-rules " + position.get("rules", "chinese-ogs"))
        self._command(f"komi {position.get('komi', 7.5)}")
        if position.get("handicap", 0):
            vertices = " ".join(vertex(p, size) for p in handicap_points(size))
            self._command("set_free_handicap " + vertices)
        for color, move in position["moves"]:
            self._command(f"play {color} {move}")

    def _territory(self, position):
        """Predicted ownership, not a replacement for final_score's rules-aware score.

        Raw NN ownership is always White-positive, top-left row-major. Suppress
        uncertain/neutral points and living stones; show opposing ownership over
        likely dead stones without changing the saved board or capture counts.
        """
        size = len(position["board"])
        tokens = self._command("kata-raw-nn 0").split()
        try:
            start = tokens.index("whiteOwnership") + 1
            values = [float(v) for v in tokens[start:start + size * size]]
        except ValueError as error:
            raise EngineUnavailable("Invalid KataGo ownership map") from error
        if len(values) != size * size or any(not math.isfinite(v) or not -1 <= v <= 1 for v in values):
            raise EngineUnavailable("Invalid KataGo ownership map")
        territory = [[0] * size for _ in range(size)]
        for i, value in enumerate(values):
            row, col = divmod(i, size)
            owner = 2 if value >= 0.7 else 1 if value <= -0.7 else 0
            if owner != position["board"][row][col]:
                territory[row][col] = owner
        position["territory"] = territory

    def _annotate_result(self, position):
        # A display-only failure must not undo a completed game or resignation.
        try:
            self._territory(position)
        except (EngineUnavailable, GTPRejected):
            position["territory"] = None
            LOGGER.exception("KataGo territory evaluation failed")
            self.close()

    def resign(self, position):
        result = copy.deepcopy(position)
        result["status"] = "finished"
        result["result"] = "W+R" if result.get("player_color", 1) == 1 else "B+R"
        result["territory"] = None
        if not self.lock.acquire(timeout=5):
            return result
        try:
            self.deadline = time.monotonic() + 65
            self._start()
            self._restore(result)
            self._annotate_result(result)
        except (EngineUnavailable, GTPRejected):
            LOGGER.exception("KataGo resignation territory unavailable")
            self.close()
        finally:
            self.deadline = None
            self.lock.release()
        return result

    def _reply(self, result):
        color = 3 - result.get("player_color", 1)
        reply = self._command("genmove " + ("B" if color == 1 else "W"))
        if reply.lower() == "resign":
            result["status"], result["result"] = "finished", ("W+R" if color == 1 else "B+R")
            self._annotate_result(result)
            return
        try:
            place(result, color, point(reply, len(result["board"])))
        except IllegalMove as error:
            raise EngineUnavailable("KataGo returned an illegal move") from error
        if len(result["moves"]) >= 2 and all(m[1] == "pass" for m in result["moves"][-2:]):
            result["status"], result["result"] = "finished", self._command("final_score")
            self._annotate_result(result)

    def begin(self, position):
        """Make the opponent's opening move for White players or handicap games."""
        if position.get("player_color", 1) == 1 and not position.get("handicap", 0):
            return copy.deepcopy(position)
        if not self.lock.acquire(timeout=5):
            raise EngineUnavailable("KataGo is busy")
        try:
            self.deadline = time.monotonic() + 65
            self._start()
            self._restore(position)
            result = copy.deepcopy(position)
            self._reply(result)
            return result
        except (EngineUnavailable, GTPRejected) as error:
            self.close()
            LOGGER.exception("KataGo opening failed")
            raise EngineUnavailable("KataGo is not available") from error
        finally:
            self.deadline = None
            self.lock.release()

    def turn(self, position, move):
        # Check captures/suicide/simple ko first; KataGo enforces the saved rules.
        color, size = position.get("player_color", 1), len(position["board"])
        play(position["board"], move, color, position["previous"])
        if not self.lock.acquire(timeout=5):
            raise EngineUnavailable("KataGo is busy")
        try:
            self.deadline = time.monotonic() + 65
            self._start()
            self._restore(position)
            result = copy.deepcopy(position)
            try:
                self._command("play " + ("B" if color == 1 else "W") + " " + vertex(move, size))
            except GTPRejected as error:
                raise IllegalMove("Этот ход запрещён правилами го: ко или повтор позиции.") from error
            place(result, color, move)
            if len(result["moves"]) >= 2 and all(m[1] == "pass" for m in result["moves"][-2:]):
                result["status"], result["result"] = "finished", self._command("final_score")
                self._annotate_result(result)
                return result
            self._reply(result)
            return result
        except (EngineUnavailable, GTPRejected) as error:
            self.close()
            LOGGER.exception("KataGo turn failed")
            raise EngineUnavailable("KataGo is not available") from error
        finally:
            self.deadline = None
            self.lock.release()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process.stdin.close()
            self.process.stdout.close()
            self.process = None
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        if self.log is not None:
            self.log.close()
            self.log = None


engine = KataGo()
atexit.register(engine.close)
