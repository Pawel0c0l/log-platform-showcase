from __future__ import annotations

import pandas as pd


def read_csv_loose(path: str) -> pd.DataFrame:
    return pd.read_csv(
        path,
        sep=";",
        header=None,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        encoding="utf-8-sig",
        engine="python",
    )


def _is_empty_row(row: pd.Series) -> bool:
    return all((str(v).strip() == "") for v in row.tolist())


def _is_text_like(value: str) -> bool:
    return any(ch.isalpha() for ch in value)


def _find_header_idx(block: pd.DataFrame) -> int | None:
    for idx in range(min(len(block), 25)):
        row = [str(v).strip() for v in block.iloc[idx].tolist()]
        non_empty = [v for v in row if v]
        if len(non_empty) < 3:
            continue
        if any(v.lower().startswith("strona ") for v in non_empty):
            continue
        text_like = sum(1 for v in non_empty if _is_text_like(v))
        if text_like < 3:
            continue
        if all(v.lower().startswith("unnamed:") for v in non_empty):
            continue
        return idx
    return None


def _build_table_from_block(block: pd.DataFrame) -> pd.DataFrame | None:
    header_idx = _find_header_idx(block)
    if header_idx is None or header_idx >= len(block) - 1:
        return None

    header = [str(v).strip() for v in block.iloc[header_idx].tolist()]
    body = block.iloc[header_idx + 1 :].copy()
    body.columns = header
    body = body.reset_index(drop=True)
    return body


def split_into_tables(df: pd.DataFrame) -> list[pd.DataFrame]:
    flattened = " ".join(str(v).strip().lower() for v in df.head(40).values.flatten().tolist())
    if "ecodriving" not in flattened:
        return [df]

    empty_rows = [i for i in range(len(df)) if _is_empty_row(df.iloc[i])]
    if not empty_rows:
        return [df]

    blocks: list[pd.DataFrame] = []
    start = 0
    for i in range(len(df)):
        if _is_empty_row(df.iloc[i]):
            if i > start:
                blocks.append(df.iloc[start:i].reset_index(drop=True))
            start = i + 1
    if start < len(df):
        blocks.append(df.iloc[start:].reset_index(drop=True))

    useful_blocks = [b for b in blocks if len(b) > 1]
    if len(useful_blocks) < 2:
        return [df]

    built: list[pd.DataFrame] = []
    for block in useful_blocks:
        t = _build_table_from_block(block)
        if t is None:
            return [df]
        built.append(t)

    built = [t for t in built if len(t) >= 2 and len(t.columns) >= 2]
    return built if len(built) >= 2 else [df]
