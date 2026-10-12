"""AI API tests use a temporary database; protocol tests do not need a GPU."""
import copy
import json
import unittest
import uuid
from unittest.mock import patch

import test_regressions as regression
from katago_engine import EngineUnavailable, GTPRejected, KataGo, new_ai_position, place, point, vertex, handicap_points
from game import IllegalMove

app = regression.app


def reply(position, move):
    result = copy.deepcopy(position)
    place(result, 1, move)
    place(result, 2, {"row": 3, "col": 3})
    return result


def opening(position):
    result = copy.deepcopy(position)
    if position["player_color"] == 2 or position["handicap"]:
        place(result, 3 - position["player_color"], {"row":0,"col":0})
    return result


def resignation(position):
    result = copy.deepcopy(position)
    result.update(status="finished", result="W+R" if position["player_color"] == 1 else "B+R",
                  territory=[[0] * len(position["board"]) for _ in position["board"]])
    result["territory"][0][0] = 2
    return result


class AiApiTests(unittest.TestCase):
    def setUp(self):
        regression.ApiTests.setUp(self)
        self.available = patch.object(app.engine, "available", return_value=True)
        self.available.start()
        self.addCleanup(self.available.stop)
        self.resign = patch.object(app.engine, "resign", side_effect=resignation)
        self.resign.start()
        self.addCleanup(self.resign.stop)

    post = regression.ApiTests.post

    def select(self, **body):
        return self.post("/api/ai/game", **body)[0]

    def action(self, game, **body):
        return self.post("/api/ai/game/" + game["id"] + "/action", revision=game["revision"], **body)

    def test_play_resume_deduplicate_and_keep_puzzle_scores_separate(self):
        first = self.select()
        self.assertEqual(first["game"]["kind"], "ai")
        self.assertEqual(first["game"]["rules"], "japanese")
        with patch.object(app.engine, "turn", side_effect=reply) as engine:
            result, body = self.action(first["game"], kind="move", move={"row":0,"col":0})
            duplicate, _ = self.post("/api/ai/game/" + first["game"]["id"] + "/action", **body)
            self.assertEqual(engine.call_count, 1)
            self.assertEqual(result, duplicate)
        self.assertEqual(result["game"]["move_count"], 2)
        self.assertEqual(self.select()["game"], result["game"])
        self.assertEqual(result["progress"]["total_score"], 0)
        self.assertEqual(result["progress"]["solved_count"], 0)

    def test_engine_failure_rolls_back_and_same_request_can_retry(self):
        game = self.select()["game"]
        url = "/api/ai/game/" + game["id"] + "/action"
        body = {"request_id":str(uuid.uuid4()), "revision":0, "kind":"move", "move":{"row":0,"col":0}}
        with patch.object(app.engine, "turn", side_effect=EngineUnavailable("offline")):
            self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 503)
        self.assertEqual(self.select()["game"], game)
        with patch.object(app.engine, "turn", side_effect=reply):
            self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 200)

    def test_resign_new_game_stale_revision_and_user_isolation(self):
        game = self.select()["game"]
        finished, _ = self.action(game, kind="resign")
        self.assertEqual(finished["game"]["result"], "W+R")
        self.assertEqual(finished["game"]["territory"][0][0], 2)
        self.assertEqual(self.select()["game"], finished["game"])
        second = self.select(new=True)["game"]
        self.assertNotEqual(second["id"], game["id"])
        self.assertEqual(second["move_count"], 0)
        abandoned = self.select(new=True)["game"]
        body = {"request_id":str(uuid.uuid4()), "kind":"resign", "revision":0}
        url = "/api/ai/game/" + second["id"] + "/action"
        self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 409)
        token = self.client.post("/api/session", json={}).json()["token"]
        url = "/api/ai/game/" + abandoned["id"] + "/action"
        self.assertEqual(self.client.post(url, headers={"Authorization":"Bearer " + token}, json=body).status_code, 404)

    def test_engine_rejects_superko_without_changing_position(self):
        game = self.select()["game"]
        with patch.object(app.engine, "turn", side_effect=IllegalMove("Правило ко")):
            result, _ = self.action(game, kind="move", move={"row":0,"col":0})
        self.assertEqual(result["notice"], "Правило ко")
        self.assertEqual(result["game"]["board"], game["board"])
        self.assertEqual(result["game"]["move_count"], 0)

    def test_returning_from_ai_resumes_selected_puzzle_but_not_completed(self):
        first, _ = self.post("/api/game")
        second, _ = self.post("/api/game", mode="next", exclude=first["game"]["problem_id"])
        self.select()
        resumed, _ = self.post("/api/game", mode="resume", problem_id=second["game"]["problem_id"])
        self.assertEqual(resumed["game"], second["game"])
        self.post("/api/game/" + second["game"]["id"] + "/action", revision=0,
                  kind="move", move={"row":9,"col":9})
        resumed, _ = self.post("/api/game", mode="resume", problem_id=second["game"]["problem_id"])
        self.assertEqual(resumed["game"]["problem_id"], first["game"]["problem_id"])

    def test_board_color_and_handicap_settings_persist(self):
        with patch.object(app.engine, "begin", side_effect=opening):
            for size, count in [(9,2), (13,5), (19,9)]:
                for color in (1,2):
                    for handicap in ([False,True] if color == 1 else [False]):
                        with self.subTest(size=size, color=color, handicap=handicap):
                            game = self.select(new=True, size=size, player_color=color, handicap=handicap)["game"]
                            self.assertEqual((game["size"], game["to_play"], game["handicap"]), (size,color,count if handicap else 0))
                            self.assertEqual(len(game["board"]), size)
                            self.assertEqual(game["komi"], 0.5 if handicap else 6.5)
                            self.assertEqual(game["move_count"], int(color == 2 or handicap))
                            self.assertEqual(self.select()["game"], game)
                            ended, _ = self.action(game, kind="resign")
                            self.assertEqual(ended["game"]["result"], "W+R" if color == 1 else "B+R")

    def test_invalid_settings_and_small_board_bounds(self):
        for options in [{"size":10}, {"size":True}, {"player_color":3}, {"player_color":True},
                        {"handicap":9}, {"player_color":2,"handicap":True},
                        {"komi":7.5}, {"komi":True}, {"komi":"6.5"}, {"komi":[]}]:
            response = self.client.post("/api/ai/game", headers=self.headers,
                                        json={"new":True,"request_id":str(uuid.uuid4()),**options})
            self.assertEqual(response.status_code, 400)
        game = self.select(new=True,size=9)["game"]
        with patch.object(app.engine, "turn") as engine:
            response = self.client.post("/api/ai/game/" + game["id"] + "/action", headers=self.headers,
                json={"request_id":str(uuid.uuid4()),"revision":0,"kind":"move","move":{"row":9,"col":0}})
            self.assertEqual(response.status_code, 400)
            engine.assert_not_called()

    def test_komi_choice_is_independent_of_handicap_and_persists(self):
        with patch.object(app.engine, "begin", side_effect=opening):
            for handicap in (False, True):
                for komi in (0.5, 6.5):
                    game = self.select(new=True, size=9, handicap=handicap, komi=komi)["game"]
                    self.assertEqual(game["komi"], komi)
                    self.assertEqual(game["rules"], "japanese")
                    self.assertIsNone(game["territory"])
                    self.assertEqual(self.select()["game"], game)

    def test_old_game_keeps_chinese_rules_and_komi(self):
        game = self.select()["game"]
        with app.database() as db:
            row = db.execute("SELECT position FROM ai_games WHERE game_id=?", (game["id"],)).fetchone()
            position = json.loads(row["position"])
            position.pop("rules")
            position["komi"] = 7.5
            db.execute("UPDATE ai_games SET position=? WHERE game_id=?", (json.dumps(position),game["id"]))
        resumed = self.select()["game"]
        self.assertEqual((resumed["rules"],resumed["komi"]), ("chinese-ogs",7.5))

    def test_opening_failure_preserves_previous_game(self):
        old = self.select()["game"]
        with patch.object(app.engine, "begin", side_effect=EngineUnavailable("offline")):
            response = self.client.post("/api/ai/game", headers=self.headers,
                json={"new":True,"player_color":2,"request_id":str(uuid.uuid4())})
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.select()["game"], old)

    def test_concurrent_move_during_opening_is_not_overwritten(self):
        old = self.select()["game"]
        def concurrent(position):
            with app.database() as db:
                db.execute("UPDATE ai_games SET revision=revision+1 WHERE game_id=?", (old["id"],))
            return opening(position)
        with patch.object(app.engine, "begin", side_effect=concurrent):
            response = self.client.post("/api/ai/game", headers=self.headers,
                json={"new":True,"player_color":2,"request_id":str(uuid.uuid4())})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.select()["game"]["id"], old["id"])


class ProtocolTests(unittest.TestCase):
    def test_coordinates_skip_i_and_flip_vertical_axis(self):
        for size in (9,13,19):
            for row in range(size):
                for col in range(size):
                    move = {"row":row,"col":col}
                    self.assertEqual(point(vertex(move,size),size), move)
        self.assertEqual(vertex({"row":0,"col":18}), "T19")
        self.assertIsNone(point("pass"))
        for bad in ["I1", "T20", "A0", "resign", ""]:
            with self.assertRaises(EngineUnavailable):
                point(bad)

    def engine(self, responses):
        engine = KataGo()
        self.addCleanup(engine.close)
        self.start = patch.object(engine, "_start")
        self.start.start()
        self.addCleanup(self.start.stop)
        commands = []
        def command(value):
            commands.append(value)
            if value.startswith("boardsize "):
                engine.test_size = int(value.split()[1])
            default = "whiteOwnership " + "0 " * engine.test_size ** 2 if value == "kata-raw-nn 0" else ""
            answer = responses.get(value, default)
            if isinstance(answer, Exception):
                raise answer
            return answer
        engine._command = command
        return engine, commands

    def test_restores_history_and_two_passes_score_without_extra_ai_move(self):
        position = new_ai_position()
        place(position, 1, {"row":3,"col":3})
        place(position, 2, None)
        engine, commands = self.engine({"final_score":"B+2.5"})
        result = engine.turn(position, None)
        self.assertIn("kata-set-rules japanese", commands)
        self.assertIn("komi 6.5", commands)
        self.assertIn("kata-raw-nn 0", commands)
        self.assertEqual(len(result["territory"]), 19)
        self.assertIn("play B D16", commands)
        self.assertIn("play W pass", commands)
        self.assertNotIn("genmove W", commands)
        self.assertEqual(result["result"], "B+2.5")
        self.assertEqual(len(position["moves"]), 2)

    def test_ai_pass_and_resignation(self):
        engine, _ = self.engine({"genmove W":"pass", "final_score":"W+7.5"})
        self.assertEqual(engine.turn(new_ai_position(), None)["result"], "W+7.5")
        engine, _ = self.engine({"genmove W":"resign"})
        self.assertEqual(engine.turn(new_ai_position(), {"row":3,"col":3})["result"], "B+R")

    def test_illegal_reply_cannot_mutate_saved_position(self):
        position = new_ai_position()
        engine, _ = self.engine({"play B D16":GTPRejected("illegal move")})
        with self.assertRaises(IllegalMove):
            engine.turn(position, {"row":3,"col":3})
        self.assertEqual(position, new_ai_position())

    def test_white_player_opening_and_following_turn(self):
        responses = {"genmove B":"D4"}
        engine, commands = self.engine(responses)
        initial = new_ai_position(9,2)
        started = engine.begin(initial)
        self.assertIn("boardsize 9", commands)
        self.assertEqual(started["moves"], [["B","D4"]])
        responses["genmove B"] = "F6"
        result = engine.turn(started, None)
        self.assertIn("play W pass", commands)
        self.assertEqual(result["moves"], [["B","D4"],["W","pass"],["B","F6"]])
        responses["genmove B"] = "resign"
        self.assertEqual(engine.turn(result, {"row":0,"col":0})["result"], "W+R")

    def test_fixed_handicap_is_setup_not_move_history(self):
        for size, count in [(9,2),(13,5),(19,9)]:
            engine, commands = self.engine({"genmove W":"A1"})
            initial = new_ai_position(size,1,True)
            started = engine.begin(initial)
            self.assertEqual(sum(v == 1 for row in started["board"] for v in row), count)
            self.assertEqual(len(handicap_points(size)), count)
            self.assertEqual(started["moves"], [["W","A1"]])
            self.assertIn("komi 0.5", commands)
            self.assertTrue(any(command.startswith("set_free_handicap ") for command in commands))
            engine._restore(started)
            self.assertIn("play W A1", commands)

    def test_legacy_saved_game_keeps_defaults(self):
        old = new_ai_position()
        for key in ["size", "player_color", "handicap", "komi", "rules", "territory"]:
            old.pop(key)
        engine, commands = self.engine({"genmove W":"Q16"})
        result = engine.turn(old, {"row":3,"col":3})
        self.assertIn("boardsize 19", commands)
        self.assertIn("kata-set-rules chinese-ogs", commands)
        self.assertIn("komi 7.5", commands)
        self.assertEqual(result["moves"], [["B","D16"],["W","Q16"]])

    def test_territory_white_perspective_orientation_and_living_stones(self):
        position = new_ai_position(9)
        position["board"][1][0] = 1  # living Black: no territory overlay
        position["board"][1][1] = 2  # predicted dead White: Black overlay
        values = [-0.9, 0.9, 0.1] + [0] * 6 + [-0.9,-0.9] + [0] * 69 + [0.95]
        engine, _ = self.engine({"kata-raw-nn 0":"symmetry 0 whiteOwnership\n" + "\t".join(map(str,values)) + "\npolicyPass 0.2"})
        engine.test_size = 9
        engine._territory(position)
        self.assertEqual(position["territory"][0][:3], [1,2,0])
        self.assertEqual(position["territory"][1][:2], [0,1])
        self.assertEqual(position["territory"][8][8], 2)
        self.assertEqual(position["board"][1][:2], [1,2])

    def test_resignation_territory_and_display_failure_preserve_result(self):
        for color in (1,2):
            engine, commands = self.engine({})
            position = new_ai_position(9,color)
            result = engine.resign(position)
            self.assertEqual(result["result"], "W+R" if color == 1 else "B+R")
            self.assertEqual(len(result["territory"]), 9)
            self.assertEqual(position["status"], "active")
            self.assertFalse(any(command.startswith("genmove") for command in commands))
        for raw in ("", "whiteOwnership nan", "whiteOwnership " + "2 " * 81, "whiteOwnership 0.1"):
            engine, _ = self.engine({"genmove W":"pass", "final_score":"W+6.5", "kata-raw-nn 0":raw})
            with self.assertLogs("katago_engine", level="ERROR"):
                result = engine.turn(new_ai_position(9), None)
            self.assertEqual(result["result"], "W+6.5")
            self.assertEqual(result["status"], "finished")
            self.assertIsNone(result["territory"])


if __name__ == "__main__":
    unittest.main()
