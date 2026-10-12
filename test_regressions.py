import hashlib
import copy
import hmac
import json
import os
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

_temp = tempfile.TemporaryDirectory()
os.environ["TELEGO_DB_PATH"] = _temp.name + "/test.db"
os.environ["TELEGO_DEV_MODE"] = "1"

import app
from fastapi.testclient import TestClient
from game import IllegalMove, correct, move_position, new_position, play


def empty():
    return [[0] * 19 for _ in range(19)]


def task(identifier):
    return {"id": identifier, "number": 1, "size": 19, "difficulty": "easy",
            "to_play": 1, "board": empty(),
            "tree": [{"color": 1, "move": {"row": 9, "col": 9}, "comment": "Correct",
                      "children": []}]}


class RulesTests(unittest.TestCase):
    def test_capture_suicide_and_occupied(self):
        board = empty()
        board[0][0] = board[0][1] = 2
        board[1][0] = board[1][1] = 1
        result = play(board, {"row": 0, "col": 2}, 1)
        self.assertEqual(result[0][:3], [0, 0, 1])
        self.assertEqual(board[0][:3], [2, 2, 0])
        with self.assertRaises(IllegalMove):
            play(result, {"row": 0, "col": 2}, 2)
        board = empty()
        board[0][1] = board[1][0] = 2
        with self.assertRaises(IllegalMove):
            play(board, {"row": 0, "col": 0}, 1)
        self.assertEqual(board[0][0], 0)

    def test_ko_and_pass(self):
        board = empty()
        for r, c in [(0, 1), (1, 0), (2, 1)]:
            board[r][c] = 1
        for r, c in [(1, 1), (0, 2), (2, 2), (1, 3)]:
            board[r][c] = 2
        captured = play(board, {"row": 1, "col": 2}, 1)
        with self.assertRaises(IllegalMove):
            play(captured, {"row": 1, "col": 1}, 2, board)
        self.assertEqual(play(captured, None, 2, board), captured)
        self.assertEqual(play(captured, {"row": 1, "col": 1}, 2, captured), board)

    def test_real_sgf_pass_continuation(self):
        problem = app.catalog()["ggg-easy-44"]
        position = new_position(problem)
        move_position(problem, position, {"row": 12, "col": 2})  # B[cm]
        move_position(problem, position, {"row": 13, "col": 4})  # B[en], W[]
        self.assertEqual(position["status"], "active")
        self.assertIsNone(app.current_node(problem, position)["move"])
        move_position(problem, position, {"row": 15, "col": 2})  # B[cp], W[do]
        self.assertEqual(position["status"], "failed")

    def test_success_marker_and_white_player(self):
        self.assertFalse(correct({"comment": "Incorrect"}))
        self.assertFalse(correct({"comment": "Not correct"}))
        self.assertTrue(correct({"comment": "Also correct."}))
        problem = task("white")
        problem["to_play"] = problem["tree"][0]["color"] = 2
        position = new_position(problem)
        move_position(problem, position, {"row": 9, "col": 9})
        self.assertEqual(position["status"], "solved")
        self.assertEqual(position["board"][9][9], 2)

    def test_all_source_files_loaded(self):
        from sgf import SGF_FILES
        self.assertEqual(len(app.problems()), len(SGF_FILES))
        self.assertEqual(app.catalog()["heart-go-problem"]["size"], 13)

    def test_entire_collection_has_legal_reachable_solutions(self):
        for problem in app.problems():
            with self.subTest(problem=problem["id"]):
                pending = [new_position(problem)]
                solved = False
                while pending:
                    position = pending.pop()
                    for child in app.current_node(problem, position)["children"]:
                        self.assertEqual(child["color"], problem["to_play"])
                        branch = copy.deepcopy(position)
                        move_position(problem, branch, child["move"])
                        if branch["status"] == "active":
                            pending.append(branch)
                        solved |= branch["status"] == "solved"
                self.assertTrue(solved, "No reachable solution")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.token = self.client.post("/api/session", json={}).json()["token"]
        self.headers = {"Authorization": "Bearer " + self.token, "X-Timezone": "Europe/Istanbul"}
        self.fixtures = {"task-one": task("task-one"), "task-two": task("task-two")}
        self.catalog_patch = patch.object(app, "catalog", return_value=self.fixtures)
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.addCleanup(self.client.close)

    def post(self, url, **body):
        body.setdefault("request_id", str(uuid.uuid4()))
        response = self.client.post(url, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), body

    def select(self, **kwargs):
        return self.post("/api/game", **kwargs)[0]

    def action(self, game, **kwargs):
        return self.post("/api/game/" + game["id"] + "/action", revision=game["revision"], **kwargs)

    def test_auth_isolation_and_removed_trusting_endpoints(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.post("/api/progress/complete", json={"user_id":"someone"}).status_code, 405)
        game = self.select()["game"]
        other = self.client.post("/api/session", json={}).json()["token"]
        response = self.client.post("/api/game/" + game["id"] + "/action",
                                    headers={"Authorization":"Bearer " + other},
                                    json={"request_id":str(uuid.uuid4()), "revision":0, "kind":"retry"})
        self.assertEqual(response.status_code, 404)

    def test_scoring_retry_and_deduplication(self):
        game = self.select()["game"]
        failed, body = self.action(game, kind="move", move={"row":0,"col":0})
        self.assertEqual(failed["game"]["status"], "failed")
        duplicate = self.client.post("/api/game/" + game["id"] + "/action", json=body, headers=self.headers)
        self.assertEqual(duplicate.json(), failed)
        retry, _ = self.action(failed["game"], kind="retry")
        solved, body = self.action(retry["game"], kind="move", move={"row":9,"col":9})
        self.assertEqual(solved["progress"]["total_score"], 40)
        duplicate = self.client.post("/api/game/" + game["id"] + "/action", json=body, headers=self.headers)
        self.assertEqual(duplicate.json()["progress"]["total_score"], 40)
        next_task = self.select(mode="next", exclude=game["problem_id"])
        self.assertNotEqual(next_task["game"]["problem_id"], game["problem_id"])
        complete, _ = self.action(next_task["game"], kind="move", move={"row":9,"col":9})
        self.assertTrue(complete["progress"]["completed_all"])
        self.assertEqual(complete["progress"]["total_score"], 140)
        self.assertIsNone(self.select()["game"])

    def test_third_attempt_ten_points_and_resume(self):
        game = self.select()["game"]
        for _ in range(2):
            failed, _ = self.action(game, kind="move", move={"row":0,"col":0})
            self.assertEqual(self.select()["game"]["status"], "failed")
            retried, _ = self.action(failed["game"], kind="retry")
            game = retried["game"]
        solved, _ = self.action(game, kind="move", move={"row":9,"col":9})
        self.assertEqual(solved["progress"]["total_score"], 10)

    def test_next_excludes_current_and_calendar_local_midnight(self):
        first = self.select()["game"]
        second = self.select(mode="next", exclude=first["problem_id"])["game"]
        self.assertNotEqual(first["problem_id"], second["problem_id"])
        self.action(first, kind="move", move={"row":9,"col":9})
        with app.database() as db:
            db.execute("UPDATE problem_progress SET completed_at='2026-10-08 22:30:00' WHERE problem_id=?",
                       (first["problem_id"],))
        progress = self.client.get("/api/me", headers=self.headers).json()["progress"]
        self.assertEqual(progress["daily_solved"].get("2026-10-09"), 1)

    def test_transaction_rolls_back_bad_authored_reply(self):
        problem = self.fixtures[self.select()["game"]["problem_id"]]
        problem["tree"][0]["comment"] = ""
        problem["tree"][0]["children"] = [{"color":2,"move":{"row":9,"col":9},"comment":"","children":[]}]
        game = self.select()["game"]
        response = self.client.post("/api/game/" + game["id"] + "/action", headers=self.headers,
                                    json={"request_id":str(uuid.uuid4()),"revision":game["revision"],
                                          "kind":"move","move":{"row":9,"col":9}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.select()["game"]["board"], empty())

    def test_illegal_user_move_does_not_advance_or_count_attempt(self):
        for problem in self.fixtures.values():
            problem["board"][0][1] = problem["board"][1][0] = 2
        game = self.select()["game"]
        for move in [{"row":0,"col":1}, {"row":0,"col":0}, {"row":-1,"col":0}]:
            result, _ = self.action(game, kind="move", move=move)
            self.assertTrue(result["notice"])
            self.assertEqual(result["game"]["board"], game["board"])
            self.assertEqual(result["game"]["status"], "active")
            game = result["game"]
        solved, _ = self.action(game, kind="move", move={"row":9,"col":9})
        self.assertEqual(solved["progress"]["total_score"], 100)

    def test_stale_revision_and_reused_request_id_are_rejected(self):
        game = self.select()["game"]
        failed, body = self.action(game, kind="move", move={"row":0,"col":0})
        url = "/api/game/" + game["id"] + "/action"
        response = self.client.post(url, headers=self.headers, json={
            "request_id":str(uuid.uuid4()), "revision":game["revision"], "kind":"retry"})
        self.assertEqual(response.status_code, 409)
        body["move"] = {"row":9,"col":9}
        self.assertEqual(self.client.post(url, headers=self.headers, json=body).status_code, 409)
        self.assertEqual(self.select()["game"], failed["game"])

    def test_telegram_signature_and_expiry(self):
        def signed(stamp):
            data = {"auth_date":str(stamp), "user":json.dumps({"id":1234,"first_name":"Test"})}
            key = hmac.new(b"WebAppData", b"test-token", hashlib.sha256).digest()
            check = "\n".join(f"{k}={v}" for k,v in sorted(data.items()))
            data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
            return urlencode(data)
        with patch.object(app, "BOT_TOKEN", "test-token"), patch.object(app, "DEV_MODE", False):
            self.assertEqual(self.client.post("/api/session", json={}).status_code, 401)
            self.assertEqual(self.client.post("/api/session", json={"init_data":signed(int(time.time()))}).status_code, 200)
            self.assertEqual(self.client.post("/api/session", json={"init_data":signed(1)}).status_code, 401)
            self.assertEqual(self.client.post("/api/session", json={"init_data":signed(int(time.time()))+"x"}).status_code, 401)

    def test_migration_backfills_without_changing_points(self):
        with app.database() as db:
            db.execute("INSERT OR IGNORE INTO users(user_id) VALUES ('legacy')")
            db.execute("""INSERT OR REPLACE INTO problem_progress
                          (user_id,problem_id,attempts,completed,score,completed_at)
                          VALUES ('legacy','ggg-easy-44',2,1,40,'2026-10-08 10:00:00')""")
        app.initialize_database()
        with app.database() as db:
            row = db.execute("SELECT * FROM problem_progress WHERE user_id='legacy'").fetchone()
            self.assertEqual((row["problem_number"],row["score"]), (44,40))


if __name__ == "__main__":
    unittest.main()
