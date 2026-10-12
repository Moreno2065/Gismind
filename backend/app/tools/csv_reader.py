"""CSV/TSV parsing that keeps physical column names and code-like text intact."""

from __future__ import annotations

import csv
import re
from pathlib import Path


_LEADING_ZERO_CODE = re.compile(r"[+-]?0\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")


def read_csv_frame(path: str | Path, *, encoding: str = "utf-8", delimiter: str = ","):
    """Read a table without silently renaming columns or coercing ID codes.

    Pandas remains responsible for ordinary numeric inference, so longitude,
    latitude, counts and measurements keep their numeric types. Only a column
    containing an integer-like lexical value with leading zeroes is forced to
    text. Empty cells remain missing; literal strings such as ``NA`` do not.
    """
    import pandas as pd

    if delimiter not in {",", "\t", ";"}:
        raise ValueError("CSV delimiter is unsupported")
    with Path(path).open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream, delimiter=delimiter, strict=True)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("CSV 缺少表头") from exc
        normalized = [name.strip().casefold() for name in header]
        if any(not name for name in normalized):
            raise ValueError("CSV 表头不能包含空字段名")
        if len(set(normalized)) != len(header):
            raise ValueError("CSV 表头包含重复字段名")
        text_columns: set[str] = set()
        for row in reader:
            if len(row) != len(header):
                raise ValueError("CSV 行的字段数与表头不一致")
            for name, value in zip(header, row):
                if _LEADING_ZERO_CODE.fullmatch(value.strip()):
                    text_columns.add(name)
    frame = pd.read_csv(
        path, encoding=encoding, sep=delimiter,
        dtype={name: "string" for name in text_columns},
        keep_default_na=False, na_values=[""], low_memory=False,
    )
    if list(frame.columns) != header:
        raise ValueError("CSV 读取后的字段名与物理表头不一致")
    return frame
