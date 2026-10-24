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
    key = _base14_key("ABCDEF+TimesNewRomanPS-BoldMT")
    assert key == "tibo" and pymupdf.Base14_fontdict[key] == "Times-Bold"
    assert _base14_key("Arial-Bold") == "hebo"
    key = _base14_key("Helvetica-Oblique")
    assert key == "heit" and pymupdf.Base14_fontdict[key] == "Helvetica-Oblique"
    print("test_base14_mapping: ok")


def test_no_shrink_and_move() -> None:
    """Повторные правки не уменьшают шрифт; перемещение сохраняет шрифт."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 400, 140), "Hello world test",
                            fontname="helv", fontsize=12)
        doc.save(pdf)
        doc.close()
        for _ in range(3):  # тройная правка тем же текстом
            ed = PdfDocument(pdf)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, "Hello world test")
            out = os.path.join(d, "o.pdf")
            ed.save_as(out)
            pdf = out
        chk = pymupdf.open(pdf)
        span = [s for blk in chk[0].get_text("dict")["blocks"] if blk["type"] == 0
                for l in blk["lines"] for s in l["spans"]][0]
        assert abs(span["size"] - 12.0) < 0.01, f"размер уменьшился: {span['size']}"

        # перемещение: тот же текст, другой прямоугольник
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        newr = pymupdf.Rect(b.rect.x0 + 50, b.rect.y0 + 40, b.rect.x1 + 50, b.rect.y1 + 40)
        ed.set_text(0, b, b.text, insert_rect=newr)
        out = os.path.join(d, "moved.pdf")
        ed.save_as(out)
        chk = pymupdf.open(out)
        w = [x for x in chk[0].get_text("words") if "Hello" in x[4]]
        assert w and w[0][0] > 100, "блок не переместился"
        span = [s for blk in chk[0].get_text("dict")["blocks"] if blk["type"] == 0
                for l in blk["lines"] for s in l["spans"]
                if newr.x0 - 1 <= s["bbox"][0] <= newr.x1 + 1][0]
        assert abs(span["size"] - 12.0) < 0.01 and span["font"] == "Helvetica"
        print("test_no_shrink_and_move: ok")


if __name__ == "__main__":
    test_blocks()
    test_edit_roundtrip()
    test_save_same_file()
    test_base14_mapping()
    test_no_shrink_and_move()
    print("ALL OK")
    sys.exit(0)
