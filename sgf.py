import re
from pathlib import Path
from functools import lru_cache

BOARD_SIZE = 19
PROBLEMS_DIR = Path(__file__).resolve().parent / 'problems' / 'weekly-go-problems'
SGF_FILES = sorted(PROBLEMS_DIR.glob('**/*.sgf'))

def parse_properties(text: str, index: int):
    properties = {}
    while index < len(text) and text[index] not in ";()":
        while index < len(text) and text[index].isspace():
            index += 1
        match = re.match(r"([A-Za-z]+)", text[index:])
        if not match:
            break
        name = match.group(1).upper()
        index += len(match.group(1))
        values = []
        while index < len(text) and text[index] == "[":
            index += 1
            value = []
            while index < len(text):
                char = text[index]
                index += 1
                if char == "]":
                    break
                if char == "\\" and index < len(text):
                    char = text[index]
                    index += 1
                    if char == "\n":
                        continue
                value.append(char)
            values.append("".join(value))
        if values:
            properties.setdefault(name, []).extend(values)
    return properties, index


def parse_sgf(text: str):
    index = 0

    def tree():
        nonlocal index
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != "(":
            raise ValueError("SGF game tree must start with '('")
        index += 1
        root = current = None
        while index < len(text):
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                break
            if text[index] == ";":
                index += 1
                properties, index = parse_properties(text, index)
                node = {"properties": properties, "children": []}
                if root is None:
                    root = node
                else:
                    current["children"].append(node)
                current = node
            elif text[index] == "(":
                child = tree()
                if current is not None and child is not None:
                    current["children"].append(child)
            elif text[index] == ")":
                index += 1
                return root
            else:
                index += 1
        return root

    return tree()


def coordinate(value: str, size: int = 19):
    if len(value) != 2 or not value.isalpha():
        return None
    col, row = ord(value[0].lower()) - 97, ord(value[1].lower()) - 97
    if 0 <= row < size and 0 <= col < size:
        return {"row": row, "col": col}
    return None


def compact_node(node: dict, size: int):
    props = node["properties"]
    move = None
    color = None
    for key in ("B", "W"):
        if key in props:
            color, move = (1 if key == "B" else 2), coordinate(props[key][0], size)
            break
    return {
        "color": color,
        "move": move,
        "comment": " ".join(props.get("C", [])).strip(),
        "children": [compact_node(child, size) for child in node["children"]],
    }


def problem_from_file(path: Path):
    root = parse_sgf(path.read_text(encoding="utf-8"))
    if root is None:
        raise ValueError(f"Empty SGF: {path}")
    props = root["properties"]
    size = int(props.get("SZ", [BOARD_SIZE])[0])
    if size not in {13, 19}:
        raise ValueError(f"Unsupported board size in {path}: {size}")
    board = [[0 for _ in range(size)] for _ in range(size)]
    for key, color in (("AB", 1), ("AW", 2)):
        for value in props.get(key, []):
            point = coordinate(value, size)
            if point:
                board[point["row"]][point["col"]] = color
    match = re.search(r"ggg-(easy|intermediate|hard|other)-(\d+)", path.stem)
    difficulty = match.group(1) if match else path.parent.name
    number = int(match.group(2)) if match else None
    to_play = 1 if "PL" not in props or props["PL"][0] == "B" else 2
    return {
        "id": path.stem,
        "number": number,
        "title": f"{difficulty.title()} problem {number}",
        "difficulty": difficulty,
        "size": size,
        "board": board,
        "to_play": to_play,
        # Both authored solutions end in an inescapable atari, before the capture.
        # Only this source uses a greeting as its solution marker.
        "success_comment": "Happy Valentine's Day!" if path.stem == "heart-go-problem" else None,
        "tree": [compact_node(child, size) for child in root["children"]],
    }


@lru_cache(maxsize=1)
def problems():
    result = []
    for path in SGF_FILES:
        try:
            result.append(problem_from_file(path))
        except (OSError, ValueError) as error:
            print(f"Skipping {path}: {error}")
    return result
