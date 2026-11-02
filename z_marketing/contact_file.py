"""The contact file: read an uploaded .xlsx or .csv into rows, and build the blank template staff fill in."""
from __future__ import annotations

import csv
import io
import zipfile
from typing import Any, BinaryIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2000
HEADERS = {
    "first_name": ("first name", "firstname", "first", "name", "contact name"),
    "firm": ("firm", "firm name", "company", "company name", "business", "business name"),
    "email": ("email", "email address", "e-mail", "work email", "mail"),
    "city": ("city", "town", "location"),
}
REQUIRED = {"first_name": "First name", "firm": "Firm", "email": "Email"}
TEMPLATE_COLUMNS = ["First name", "Firm", "Email", "City"]


class FileProblem(Exception):
    """The file can't be read, with a message for staff."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _columns(header: list[str]) -> dict[str, int]:
    """Which column holds which field, whatever the case or spacing of the headers."""
    found: dict[str, int] = {}
    for at, title in enumerate(header):
        clean = " ".join(title.lower().replace("_", " ").split())
        for field, names in HEADERS.items():
            if clean in names and field not in found:
                found[field] = at
    missing = [label for field, label in REQUIRED.items() if field not in found]
    if missing:
        raise FileProblem(f"This file has no {', '.join(missing)} column. Use the template, or rename the column.")
    return found


def _read_rows(name: str, data: bytes) -> list[list[str]]:
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        return [[_text(v) for v in row] for row in csv.reader(io.StringIO(text))]
    if name.endswith(".xlsx"):
        try:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
            raise FileProblem("This file can't be opened. Save it again as .xlsx and retry.") from exc
        sheet = book.worksheets[0]
        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append([_text(v) for v in row])
            if len(rows) > MAX_ROWS + 1:
                break
        book.close()
        return rows
    raise FileProblem("Upload an .xlsx or .csv file. For an old .xls file, save it as .xlsx first.")


def parse(upload: BinaryIO, name: str, size: int) -> list[dict[str, Any]]:
    """Rows as {row, first_name, firm, email, city}. `row` is the line number staff see in their file."""
    if size > MAX_BYTES:
        raise FileProblem("This file is over 2 MB. Split it into smaller files.")
    rows = _read_rows(name.lower(), upload.read())
    start = next((i for i, r in enumerate(rows) if any(r)), None)
    if start is None:
        raise FileProblem("This file is empty.")
    columns = _columns(rows[start])
    body = rows[start + 1 :]
    if len(body) > MAX_ROWS:
        raise FileProblem(f"This file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    out = []
    for at, row in enumerate(body, start=start + 2):
        if not any(row):
            continue
        cell = lambda field: row[columns[field]] if field in columns and columns[field] < len(row) else ""  # noqa: E731, B023
        out.append({"row": at, "first_name": cell("first_name"), "firm": cell("firm"), "email": cell("email"), "city": cell("city")})
    if not out:
        raise FileProblem("This file has headers but no contacts.")
    return out


def template() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Contacts"
    sheet.append(TEMPLATE_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for letter, width in zip("ABCD", (18, 30, 34, 18), strict=True):
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A2"
    guide = book.create_sheet("How to fill this in")
    for line in (
        ["Keep the four headers in the Contacts sheet. Add one person per row, starting on row 2."],
        ["First name, Firm and Email are needed. City is optional."],
        ["Use a named work address, not info@ or a free mail address."],
        ["One person per firm: only the first address at each company domain is added."],
        ["Example row: Dana | Mesa Ridge CPAs | dana@mesaridge.example | Dallas"],
    ):
        guide.append(line)
    guide.column_dimensions["A"].width = 90
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()
