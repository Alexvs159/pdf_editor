"""Self-check модели (без GUI): python test_document.py"""
import os
import sys
import tempfile

import pymupdf

from document import PdfDocument, _base14_key


def make_pdf(path: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()  # A4
    page.insert_textbox(
        pymupdf.Rect(72, 72, 400, 240),
        "Hello PDF editor. " * 12,
        fontname="helv", fontsize=12,
    )
    # Страница без текста (картинка не обязательна)
    doc.new_page()
    doc.save(path)
    doc.close()


def test_blocks() -> None:
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        make_pdf(pdf)
        doc = PdfDocument(pdf)
        assert doc.page_count == 2
        blocks = doc.blocks(0)
        assert len(blocks) >= 1, "должен найтись текстовый блок"
        assert "Hello PDF editor." in blocks[0].text
        assert blocks[0].font == "Helvetica" and blocks[0].size == 12.0
        assert doc.blocks(1) == [], "на пустой странице блоков нет"
        print("test_blocks: ok")


def test_edit_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "t.pdf")
        out = os.path.join(d, "out.pdf")
        make_pdf(src)
        doc = PdfDocument(src)
        b = doc.blocks(0)[0]
        doc.set_text(0, b, "Отредактировано: РедОк 12345")
        doc.save_as(out)
        check = pymupdf.open(out)
        text = check[0].get_text()
        assert "Отредактировано: РедОк 12345" in text, f"нового текста нет: {text!r}"
        assert "Hello PDF editor" not in text, "старый текст остался (not in text!)"
        print("test_edit_roundtrip: ok")


def test_save_same_file() -> None:
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        make_pdf(pdf)
        doc = PdfDocument(pdf)
        b = doc.blocks(0)[0]
        doc.set_text(0, b, "same file edit")
        doc.save_as(pdf)  # тот же путь — инкрементальное сохранение
        check = pymupdf.open(pdf)
        assert "same file edit" in check[0].get_text()
        print("test_save_same_file: ok")


def test_base14_mapping() -> None:
    assert _base14_key("ABCDEF+TimesNewRomanPS-BoldMT") == "times-romanbold"
    assert _base14_key("Arial-Bold") == "helvetica-bold"
    # точное имя из словаря возвращается как есть
    key = _base14_key("Helvetica-Oblique")
    assert pymupdf.Base14_fontdict[key] == "Helvetica-Oblique"
    print("test_base14_mapping: ok")


if __name__ == "__main__":
    test_blocks()
    test_edit_roundtrip()
    test_save_same_file()
    test_base14_mapping()
    print("ALL OK")
    sys.exit(0)
