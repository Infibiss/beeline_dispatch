"""Read original synthetic CSV without repairing or silently dropping job rows."""
import csv
import io
from datetime import datetime
from pathlib import Path

from pydantic import Field

from .models import Model, TimeWindow

REQUIRED = {"Заявка", "Тип заявки BK", "Тип заявки HD", "Начало", "Окончание", "Район", "Адрес"}


class RawRequest(Model):
    id: str
    bk_type: str
    hd_type: str
    window: TimeWindow
    date: str
    locality: str
    address: str
    source_line: int
    raw: dict[str, str]


class ImportIssue(Model):
    line: int
    message: str
    raw: str


class ImportResult(Model):
    source: str
    encoding: str
    office_address: str = ""
    requests: list[RawRequest] = Field(default_factory=list)
    errors: list[ImportIssue] = Field(default_factory=list)
    blank_rows: int = 0


def import_csv(path: Path) -> ImportResult:
    path = Path(path)
    if "контроль" in path.name.lower():
        raise ValueError("Контрольные данные не разрешены для планирования")
    blob = path.read_bytes()
    try:
        text = blob.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = blob.decode("cp1251")
        encoding = "cp1251"
    reader = csv.reader(io.StringIO(text), delimiter=";", strict=True)
    try:
        headers = [h.strip() for h in next(reader)]
    except (StopIteration, csv.Error) as exc:
        raise ValueError("CSV пуст или заголовок поврежден") from exc
    if "Бригада" in headers or "Статус BK" in headers:
        raise ValueError("Контрольная схема не разрешена для планирования")
    if not REQUIRED.issubset(headers) or len(set(headers)) != len(headers):
        raise ValueError(f"Некорректные заголовки CSV; нужны {sorted(REQUIRED)}")
    jobs, issues, seen = [], [], set()
    office, blanks = "", 0
    while True:
        line = reader.line_num + 1
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            issues.append(ImportIssue(line=line, message=f"Поврежден CSV: {exc}", raw=""))
            break  # Cannot recover record boundaries safely after malformed quoting.
        raw = ";".join(row)
        if not any(cell.strip() for cell in row):
            blanks += 1
            continue
        if row[0].strip().lower() == "адрес офиса":
            if len(row) < 2 or not row[1].strip() or office:
                issues.append(ImportIssue(line=line, message="Нет адреса офиса или он повторен", raw=raw))
            else:
                office = row[1].strip()
            continue
        try:
            if len(row) != len(headers):
                raise ValueError(f"Ожидалось {len(headers)} столбцов, получено {len(row)}")
            record = dict(zip(headers, (c.strip() for c in row)))
            if any(not record[k] for k in REQUIRED):
                raise ValueError("Не заполнено обязательное поле")
            if record["Заявка"] in seen:
                raise ValueError("Повторный ID заявки")
            start = datetime.strptime(record["Начало"], "%d.%m.%Y %H:%M")
            end = datetime.strptime(record["Окончание"], "%d.%m.%Y %H:%M")
            if start.date() != end.date() or end < start:
                raise ValueError("Окно должно быть упорядочено внутри одного дня")
            jobs.append(RawRequest(id=record["Заявка"], bk_type=record["Тип заявки BK"],
                hd_type=record["Тип заявки HD"], window=TimeWindow(start=start.hour*60+start.minute,
                end=end.hour*60+end.minute), date=start.date().isoformat(), locality=record["Район"],
                address=record["Адрес"], source_line=line, raw=record))
            seen.add(record["Заявка"])
        except ValueError as exc:
            issues.append(ImportIssue(line=line, message=str(exc), raw=raw))
    return ImportResult(source=str(path), encoding=encoding, requests=jobs, errors=issues,
                        office_address=office, blank_rows=blanks)
