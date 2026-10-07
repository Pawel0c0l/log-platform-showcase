#!/usr/bin/env python3
import csv
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _normalize_csv_bytes_to_canonical


def _assert_utf8_bom(path: Path) -> None:
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), f"Brak BOM UTF-8: {path}"


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="csv-normalize-") as tmp:
        out_dir = Path(tmp)

        cases = [
            (
                "comma_utf8.csv",
                "imie,nazwisko,miasto\nJan,Kowalski,Łódź\n".encode("utf-8"),
                "Łódź",
            ),
            (
                "tab_utf8.csv",
                "imie\tnazwisko\tmiasto\nAnna\tNowak\tKraków\n".encode("utf-8"),
                "Kraków",
            ),
            (
                "semi_cp1250.csv",
                "imie;nazwisko;miasto\nŻaneta;Śliwa;Białystok\n".encode("cp1250"),
                "Żaneta",
            ),
            (
                "semi_iso8859_2.csv",
                "imie;nazwisko;miasto\nŁukasz;Ćwikła;Poznań\n".encode("iso-8859-2"),
                "Łukasz",
            ),
        ]

        for name, raw, expected_token in cases:
            out_path = out_dir / f"normalized__{name}"
            _normalize_csv_bytes_to_canonical(raw, out_path)
            _assert_utf8_bom(out_path)

            rows = _read_rows(out_path)
            assert rows, f"Brak danych w {out_path}"
            assert len(rows[0]) == 3, f"Niepoprawny delimiter po normalizacji w {out_path}: {rows[0]}"

            text = out_path.read_text(encoding="utf-8-sig")
            assert "\t" not in text, f"Pozostały tabulatory w {out_path}"
            assert expected_token in text, f"Brak polskich znaków/tokenu '{expected_token}' w {out_path}"

            print(f"PASS: {out_path}")


if __name__ == "__main__":
    main()
