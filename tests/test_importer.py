from pathlib import Path

import pytest

from field_router.importer import import_csv

DATA = Path(__file__).resolve().parents[1] / "data" / "Обезличивание"


@pytest.mark.parametrize("region,count", [("Восток", 66), ("Юго-восток", 83), ("Югоцентр", 56)])
def test_original_synthetic_files(region, count):
    result = import_csv(DATA / f"{region} Синтетические данные.csv")
    assert len(result.requests) == count
    assert result.office_address
    assert result.encoding == "cp1251"
    assert not result.errors
    assert result.blank_rows == 2
    assert len({r.id for r in result.requests}) == count
    assert len({r.address for r in result.requests}) < count
    assert all(r.date == "2026-08-17" for r in result.requests)


def test_bad_rows_reported_with_line_and_raw_value(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("Заявка;Тип заявки BK;Тип заявки HD;Начало;Окончание;Район;Адрес\n"
                    "1;Подключение;x;broken;17.08.2026 12:00;r;a\n"
                    "2;Подключение;x;17.08.2026 10:00;17.08.2026 12:00;r;a;extra\n", encoding="utf-8")
    result = import_csv(path)
    assert not result.requests
    assert [e.line for e in result.errors] == [2, 3]
    assert "broken" in result.errors[0].raw


def test_controls_rejected():
    with pytest.raises(ValueError, match="Контроль"):
        import_csv(next(DATA.glob("*Контроль*")))
