"""Fast, data-only reader for Scenathon database XLSX Indicators sheets.

Returns the same six-column reporting contract as the ordinary report export.
The Trade sheet is never parsed; missing event/snapshot metadata is not invented.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_RELS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_SOURCE = ("pathway", "location", "tradeadjustment", "year", "calcallagrico2e", "calcalllandco2e")
_DEST = ("pathway_name_default", "country", "trade_adj", "year", "calcallagrico2e", "calcalllandco2e")
_CANONICAL = {"currenttrends": "Current Trends", "current trends": "Current Trends"}
_COL = re.compile(r"^[A-Z]+")


def _column(ref: str) -> int:
    found = _COL.match(ref)
    if found is None:
        raise ValueError("Bad XLSX cell reference.")
    result = 0
    for letter in found.group():
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def _sheet_paths(archive: ZipFile) -> dict[str, str]:
    root = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.findall(_PKG_RELS + "Relationship")}
    out = {}
    names = set(archive.namelist())
    for sheet in root.iter(_MAIN + "sheet"):
        target = targets.get(sheet.get(_DOC_RELS + "id"))
        if not target:
            continue
        path = (target.lstrip("/") if target.startswith("/")
                else posixpath.normpath(posixpath.join("xl", target)))
        if path.startswith("xl/worksheets/") and path in names:
            out[sheet.get("name", "").strip().casefold()] = path
    return out


def _raw_rows(archive: ZipFile, path: str):
    with archive.open(path) as source:
        for _, element in ET.iterparse(source, events=("end",)):
            if element.tag != _MAIN + "row":
                continue
            result = {}
            for cell in element.iter(_MAIN + "c"):
                number = _column(cell.get("r", ""))
                value = cell.find(_MAIN + "v")
                cell_type = cell.get("t", "n")
                if value is not None and value.text is not None:
                    result[number] = (cell_type, value.text)
                elif cell_type == "inlineStr":
                    inline = cell.find(_MAIN + "is")
                    result[number] = ("inlineStr", "" if inline is None else "".join(inline.itertext()))
            yield result
            element.clear()


def _shared(archive: ZipFile, indices: set[int]) -> dict[int, str]:
    if not indices:
        return {}
    if "xl/sharedStrings.xml" not in archive.namelist():
        raise ValueError("The workbook has no shared strings table.")
    found = {}
    with archive.open("xl/sharedStrings.xml") as source:
        number = 0
        for _, element in ET.iterparse(source, events=("end",)):
            if element.tag != _MAIN + "si":
                continue
            if number in indices:
                found[number] = "".join(node.text or "" for node in element.iter(_MAIN + "t"))
            number += 1
            element.clear()
            if len(found) == len(indices):
                break
    if len(found) != len(indices):
        raise ValueError("The workbook contains an invalid shared-string reference.")
    return found


def _decode(value, strings):
    if value is None:
        return None
    kind, raw = value
    if kind == "s":
        return strings[int(raw)]
    if kind == "b":
        return raw == "1"
    if kind == "e":
        raise ValueError(f"Excel cell contains an error: {raw}")
    if kind in ("str", "inlineStr"):
        return raw
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except (ValueError, OverflowError):
        return raw


def _trade_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        if value.strip().casefold() in ("true", "yes", "1", "1.0"):
            return True
        if value.strip().casefold() in ("false", "no", "0", "0.0"):
            return False
    raise ValueError(f"Unrecognized database tradeadjustment value {value!r}.")


def _pathway(label, after_trade):
    text = str(label or "").strip()
    if not text:
        raise ValueError("Database contains an empty pathway name.")
    if text.casefold().endswith(" - adjusted"):
        if not after_trade:
            raise ValueError("A pathway labelled adjusted must have tradeadjustment=1.")
        text = text[:-len(" - adjusted")].rstrip()
    return _CANONICAL.get(text.casefold(), text)


def database_rows(blob: bytes, *, max_rows=60_000):
    """Return normalized database rows, or None for a different workbook format.

    Reads only Indicators and its six required columns; it never reads Trade.
    Does not fabricate event or snapshot identifiers. The caller must require
    Scenathon confirmation when metadata is missing. Calculations and frozen
    allowances remain the responsibility of the unchanged Fair Share engine.
    """
    try:
        with ZipFile(BytesIO(blob)) as archive:
            paths = _sheet_paths(archive)
            if "indicators" not in paths:
                return None
            indicator_path = paths["indicators"]
            header_raw = next(_raw_rows(archive, indicator_path), {})
            header_strings = _shared(archive, {
                int(v[1]) for v in header_raw.values() if v[0] == "s"
            })
            headers = {col: _decode(value, header_strings) for col, value in header_raw.items()}
            counts = Counter(str(x).casefold().strip() for x in headers.values() if x is not None)
            if not set(_SOURCE).issubset(counts):
                return None
            if any(counts[k] != 1 for k in _SOURCE):
                raise ValueError("The database Indicators sheet has duplicate required columns.")
            positions = {
                name: next(i for i, header in headers.items()
                           if str(header).strip().casefold() == name)
                for name in _SOURCE
            }
            picked = []
            needed = set()
            for number, row in enumerate(_raw_rows(archive, indicator_path)):
                if number == 0:
                    continue
                if number > max_rows:
                    raise ValueError("Too many database Indicators rows for this Scenathon release.")
                values = [row.get(positions[name]) for name in _SOURCE]
                if all(value is None for value in values):
                    continue
                picked.append(values)
                needed.update(int(v[1]) for v in values if v is not None and v[0] == "s")
            strings = _shared(archive, needed)
            result = []
            for raw in picked:
                row = dict(zip(_DEST, (_decode(v, strings) for v in raw)))
                row["trade_adj"] = _trade_flag(row["trade_adj"])
                row["pathway_name_default"] = _pathway(row["pathway_name_default"], row["trade_adj"])
                result.append(row)
            if not result:
                raise ValueError("The database Indicators sheet has no data rows.")
            return result
    except BadZipFile as exc:
        raise ValueError("The file is not a readable .xlsx workbook.") from exc
