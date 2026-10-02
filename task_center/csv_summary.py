"""csv_summary 任务：提交时的路径校验与执行时的内容汇总。"""

import re
from pathlib import Path

HEADER = "category,amount"
AMOUNT_RE = re.compile(r"[0-9]+")


class TaskInputError(Exception):
    """执行时输入文件不可读（对应错误码 input_error）。"""


class TaskDataError(Exception):
    """文件内容不合法（对应错误码 data_error）。"""


def resolve_input(project_root: Path, raw_path: str) -> Path:
    """提交时校验输入路径：相对项目目录解释，且必须位于 demo 目录内的普通文件。"""
    root = Path(project_root).resolve()
    demo_dir = (root / "demo").resolve()

    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (root / candidate).resolve()

    # 必须位于 demo 目录之内；resolve 会穿透符号链接，挡住指向目录外的链接。
    if not _is_within(resolved, demo_dir):
        raise ValueError("path outside demo directory")
    if not resolved.exists():
        raise ValueError("input file does not exist")
    if not resolved.is_file():
        raise ValueError("input is not a regular file")
    return resolved


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def run_csv_summary(path: Path) -> dict:
    """读取并汇总 CSV，返回 {row_count, total_amount, categories}。

    任何内容问题抛 TaskDataError；文件不可读抛 TaskInputError。
    校验全部通过后才构造结果，不保留部分结果。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        if isinstance(exc, UnicodeDecodeError):
            raise TaskDataError("file is not valid UTF-8") from exc
        raise TaskInputError("input file is not readable") from exc

    lines = text.splitlines()
    if not lines or lines[0] != HEADER:
        raise TaskDataError("first line must be the header 'category,amount'")

    categories: dict[str, int] = {}
    row_count = 0
    total_amount = 0

    for line in lines[1:]:
        if line == "":
            # 完全空行忽略（仅空白的行不算空行，将因列数不符报错）。
            continue
        fields = line.split(",")
        if len(fields) != 2:
            raise TaskDataError("each data row must have exactly two columns")

        category = fields[0].strip()
        amount_text = fields[1].strip()
        if not category:
            raise TaskDataError("category must not be empty")
        if not AMOUNT_RE.fullmatch(amount_text):
            raise TaskDataError("amount must be a non-negative integer")

        amount = int(amount_text)
        row_count += 1
        total_amount += amount
        categories[category] = categories.get(category, 0) + amount

    return {
        "row_count": row_count,
        "total_amount": total_amount,
        "categories": categories,
    }
