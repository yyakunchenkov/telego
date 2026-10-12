"""Go rules and SGF navigation shared by every client through the API."""
import re


class IllegalMove(ValueError):
    pass


def neighbors(row, col, size):
    return [(r, c) for r, c in ((row-1, col), (row+1, col), (row, col-1), (row, col+1))
            if 0 <= r < size and 0 <= c < size]


def group(board, row, col):
    color = board[row][col]
    stones, liberties, pending = set(), set(), [(row, col)]
    while pending:
        point = pending.pop()
        if point in stones:
            continue
        stones.add(point)
        for r, c in neighbors(*point, len(board)):
            if board[r][c] == 0:
                liberties.add((r, c))
            elif board[r][c] == color and (r, c) not in stones:
                pending.append((r, c))
    return stones, liberties


def play(board, move, color, previous=None):
    """Return a new board; rejected moves never mutate the input. Simple Japanese ko."""
    result = [line[:] for line in board]
    if move is None:  # A pass advances the turn and releases a simple ko.
        return result
    row, col = move["row"], move["col"]
    if not (0 <= row < len(board) and 0 <= col < len(board)):
        raise IllegalMove("Ход за пределами доски.")
    if board[row][col]:
        raise IllegalMove("Это пересечение уже занято.")
    result[row][col] = color
    for r, c in neighbors(row, col, len(board)):
        if result[r][c] == 3 - color:
            stones, liberties = group(result, r, c)
            if not liberties:
                for sr, sc in stones:
                    result[sr][sc] = 0
    if not group(result, row, col)[1]:
        raise IllegalMove("Самоубийственный ход запрещён.")
    if previous is not None and result == previous:
        raise IllegalMove("Правило ко: немедленное обратное взятие запрещено.")
    return result


def correct(node):
    # Do not interpret 'incorrect', 'not correct', or references to a correct move as success.
    return bool(re.match(r"^(?:also\s+)?correct\b", node.get("comment", "").strip(), re.I))


def new_position(problem):
    return {"board": [row[:] for row in problem["board"]], "previous": None,
            "path": [], "status": "active", "started": False, "last_move": None}


def current_node(problem, position):
    node = {"children": problem["tree"]}
    for index in position["path"]:
        node = node["children"][index]
    return node


def advance(problem, position, index):
    node = current_node(problem, position)["children"][index]
    if node["color"] is not None:
        board = play(position["board"], node["move"], node["color"], position["previous"])
        position["previous"], position["board"] = position["board"], board
        position["last_move"] = node["move"]
    position["path"].append(index)


def settle(problem, position):
    """Follow the published first opponent variation, including passes and comment nodes."""
    while True:
        node = current_node(problem, position)
        if correct(node) or (problem.get("success_comment") and
                             node.get("comment") == problem["success_comment"] and not node["children"]):
            position["status"] = "solved"
            return
        if not node["children"]:
            position["status"] = "failed"
            return
        child = node["children"][0]
        if child["color"] == problem["to_play"]:
            return
        advance(problem, position, 0)


def move_position(problem, position, move):
    if position["status"] != "active":
        raise IllegalMove("Попытка завершена.")
    board = play(position["board"], move, problem["to_play"], position["previous"])
    position["started"] = True
    children = current_node(problem, position)["children"]
    index = next((i for i, node in enumerate(children)
                  if node["color"] == problem["to_play"] and node["move"] == move), None)
    if index is None:
        position["previous"], position["board"] = position["board"], board
        position["last_move"] = move
        position["status"] = "failed"
        return
    advance(problem, position, index)
    settle(problem, position)
