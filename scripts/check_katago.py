"""Exercise the installed engine, without accessing the application database."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from katago_engine import engine, new_ai_position, place


def main():
    try:
        initial = new_ai_position()
        first = engine.turn(initial, {"row": 3, "col": 3})
        assert len(first["moves"]) == 2 and initial["moves"] == []
        # Starting another turn exercises full replay and multi-user-safe restoration.
        move = next({"row": r, "col": c} for r, c in [(15, 15), (3, 15), (15, 3)]
                    if first["board"][r][c] == 0)
        second = engine.turn(first, move)
        assert len(second["moves"]) == 4 and len(first["moves"]) == 2
        rules = json.loads(engine._command("kata-get-rules"))
        assert rules["ko"] == "SIMPLE" and rules["scoring"] == "TERRITORY"
        assert rules["tax"] == "SEKI" and rules["suicide"] is False
        assert float(engine._command("get_komi")) == 6.5
        print("KataGo", engine._command("version"))
        print("Real engine replies:", first["moves"][-1], second["moves"][-1])
        print("Rules:", rules)
        for size in (9, 13, 19):
            for komi in (0.5, 6.5):
                start = new_ai_position(size, komi=komi)
                ended = engine.resign(start)
                assert ended["result"] == "W+R" and len(ended["territory"]) == size
                assert float(engine._command("get_komi")) == komi
                assert all(len(row) == size for row in ended["territory"])
                print(f"Verified territory {size}x{size}, komi={komi}", flush=True)
        endgame = new_ai_position(9)
        for color, move in [(1, {"row":2,"col":2}), (2, {"row":6,"col":6}),
                            (1, {"row":2,"col":3}), (2, None)]:
            place(endgame, color, move)
        scored = engine.turn(endgame, None)
        assert scored["status"] == "finished" and scored["result"].startswith(("B+", "W+"))
        assert len(scored["moves"]) == 5 and len(scored["territory"]) == 9
        assert all(v in (0, 1, 2) for row in scored["territory"] for v in row)
        print("Verified two-pass Japanese score and territory:", scored["result"], flush=True)
        if "--all-settings" in sys.argv:
            for size in (9, 13, 19):
                for color, handicap in [(1, False), (2, False), (1, True)]:
                    start = new_ai_position(size, color, handicap)
                    opened = engine.begin(start)
                    assert len(opened["moves"]) == int(color == 2 or handicap)
                    move = next({"row": r, "col": c} for r, c in [(0,0),(0,size-1),(size-1,0),(size-1,size-1)]
                                if opened["board"][r][c] == 0)
                    played = engine.turn(opened, move)
                    assert len(played["moves"]) == len(opened["moves"]) + 2
                    assert played["board"][move["row"]][move["col"]] == color
                    assert float(engine._command("get_komi")) == start["komi"]
                    print(f"Verified {size}x{size}, player={color}, handicap={start['handicap']}: {played['moves'][-1]}", flush=True)
        print("Local engine smoke check passed; no user database was opened.")
    finally:
        engine.close()


if __name__ == "__main__":
    main()
