"""Self-check модели (без GUI): python test_document.py"""
import os
import sys
import tempfile

import numpy as np
import pymupdf

import ocr
from document import OcrBlock, PdfDocument, _base14_key


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


def _html_pdf(path: str, html: str) -> None:
    doc = pymupdf.open()
    doc.new_page().insert_htmlbox(pymupdf.Rect(72, 80, 470, 150), html)
    doc.save(path)
    doc.close()


def _spans(path: str) -> list[tuple]:
    """Спаны первой страницы: (font, size, x, y, text)."""
    doc = pymupdf.open(path)
    out = [(s["font"], s["size"], s["origin"][0], s["origin"][1], s["text"])
           for b in doc[0].get_text("dict")["blocks"] if b["type"] == 0
           for line in b["lines"] for s in line["spans"]]
    doc.close()
    return out


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


def test_no_shrink_big_size() -> None:
    """Правка тем же текстом при 24pt+ не уменьшает размер: дефицит высоты
    плотного bbox закрывается расширением бокса вниз, не усадкой шрифта."""
    for size in (24.0, 36.0):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "t.pdf")
            doc = pymupdf.open()
            page = doc.new_page()
            page.insert_textbox(pymupdf.Rect(72, 72, 400, 300),
                                "Big roundtrip size " * 3,
                                fontname="helv", fontsize=size)
            doc.save(pdf)
            doc.close()
            for _ in range(3):
                ed = PdfDocument(pdf)
                b = ed.blocks(0)[0]
                ed.set_text(0, b, b.text)
                out = os.path.join(d, "o.pdf")
                ed.save_as(out)
                pdf = out
            chk = pymupdf.open(pdf)
            span = [s for blk in chk[0].get_text("dict")["blocks"] if blk["type"] == 0
                    for l in blk["lines"] for s in l["spans"]][0]
            assert abs(span["size"] - size) < 0.5, f"размер уменьшился: {span['size']}"

def test_longer_text_keeps_size() -> None:
    """Правка более длинным текстом сохраняет размер шрифта — текст
    может выходить ниже исходного блока за счёт расширения бокса."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        out = os.path.join(d, "o.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(72, 72, 300, 200), "short line",
                            fontname="helv", fontsize=18)
        doc.save(pdf)
        doc.close()
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        long_text = "a much longer replacement line " * 6
        ed.set_text(0, b, long_text)
        ed.save_as(out)
        chk = pymupdf.open(out)
        text = chk[0].get_text()
        assert "longer" in text, "нового текста нет"
        spans = [s for blk in chk[0].get_text("dict")["blocks"] if blk["type"] == 0
                 for l in blk["lines"] for s in l["spans"]]
        sizes = {s["size"] for s in spans}
        assert len(sizes) == 1 and abs(sizes.pop() - 18.0) < 0.5, \
            f"размер изменился: {sizes}"


def test_no_wrap_longer_text() -> None:
    """Правка более длинным текстом без "\\n": ровно ОДНА строка без
    переноса (бокс не ограничивает ширину), размер не меняется, bbox
    расширяется по ширине; повторные правки не двигают строку."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 200, 120), "short line",
                                      fontname="helv", fontsize=12)
        doc.save(pdf); doc.close()
        orig = pymupdf.open(pdf)
        ob = [b for b in orig[0].get_text("dict")["blocks"] if b["type"] == 0][0]
        orig_x1 = ob["bbox"][2]

        def spans_after(path):
            chk = pymupdf.open(path)
            blk = [b for b in chk[0].get_text("dict")["blocks"] if b["type"] == 0][0]
            return blk, [s for l in blk["lines"] for s in l["spans"]]

        long_text = "a much longer replacement line from the editor" * 2
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], long_text)
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        blk, spans = spans_after(out)
        assert len(spans) == 1, f"перенос строки: {len(spans)} строк"
        assert abs(spans[0]["size"] - 12.0) < 0.01, f"размер изменился: {spans[0]['size']}"
        assert blk["bbox"][2] > orig_x1, "bbox не расширился по ширине"
        line = [(spans[0]["origin"][0], spans[0]["origin"][1], blk["bbox"])]

        for _ in range(2):  # повторные правки тем же текстом
            ed = PdfDocument(out)
            ed.set_text(0, ed.blocks(0)[0], ed.blocks(0)[0].text)
            ed.save_as(out)
        blk, spans = spans_after(out)
        assert len(spans) == 1, f"повторный перенос: {len(spans)} строк"
        assert abs(spans[0]["size"] - 12.0) < 0.01
        assert line[0][0] == spans[0]["origin"][0] and line[0][1] == spans[0]["origin"][1], \
            f"позиция строки дрейфует: {spans[0]['origin']} != {line[0][:2]}"
        assert abs(pymupdf.Rect(blk["bbox"]).x1 - pymupdf.Rect(line[0][2]).x1) < 0.5
        print("test_no_wrap_longer_text: ok")


def test_explicit_newlines() -> None:
    """Явный "\\n" в тексте пользователя = явный перенос: столько строк,
    сколько задал пользователь (и при повторной правке то же число)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 400, 140), "one line",
                                      fontname="helv", fontsize=12)
        doc.save(pdf); doc.close()
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], "строка раз\nстрока два\nстрока три")
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        assert b.text == "строка раз\nстрока два\nстрока три", f"строки: {b.text!r}"
        assert len(b.line_styles) == 3, f"стилей строк: {len(b.line_styles)}"
        ys = [s["origin"][1] for s in b.line_styles]
        assert ys == sorted(ys) and len(set(ys)) == 3, f"baseline: {ys}"
        # теперь 2 строки вместо 3
        ed = PdfDocument(out)
        ed.set_text(0, ed.blocks(0)[0], "first\nsecond")
        ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        assert b.text == "first\nsecond", f"строки: {b.text!r}"
        print("test_explicit_newlines: ok")


def test_line_styles_per_line() -> None:
    """Стили по строкам: строки наследуют шрифт/размер исходной строки,
    стабильны при повторных правках; новые строки наследуют последнюю."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100.0), "Line one helv", fontname="helv", fontsize=12)
        page.insert_text((72, 116.0), "Line two cour", fontname="cour", fontsize=16)
        doc.save(pdf); doc.close()
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        assert len(b.line_styles) == 2, f"строк: {len(b.line_styles)}"
        assert b.line_styles[0]["font"] == "Helvetica" and \
            b.line_styles[0]["size"] == 12.0
        assert b.line_styles[1]["font"] == "Courier" and \
            b.line_styles[1]["size"] == 16.0
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        first = [(l["font"], l["size"], l["origin"]) for l in b.line_styles]
        for _ in range(2):  # повторные правки тем же текстом
            ed = PdfDocument(out)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, b.text)
            ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        assert len(b.line_styles) == 2, f"строк после правок: {len(b.line_styles)}"
        for i, (fnt, sz, org) in enumerate(first):
            s = b.line_styles[i]
            assert s["font"] == fnt and abs(s["size"] - sz) < 0.01, \
                f"стиль строки {i} потерян: {s} != {(fnt, sz)}"
            assert all(abs(a - b2) < 0.5 for a, b2 in zip(s["origin"], org)), \
                f"line {i} поехала: {s['origin']} != {org}"
        # новых строк больше — наследуют стиль последней исходной строки
        ed = PdfDocument(out)
        b = ed.blocks(0)[0]
        ed.set_text(0, b, b.text + "\ntail")
        ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        assert len(b.line_styles) == 3, f"строк: {len(b.line_styles)}"
        assert b.line_styles[2]["font"] == "Courier" and \
            abs(b.line_styles[2]["size"] - 16.0) < 0.01, f"хвост: {b.line_styles[2]}"
        assert b.line_styles[2]["origin"][1] > b.line_styles[1]["origin"][1]
        print("test_line_styles_per_line: ok")


def test_cyrillic_embedded_stable() -> None:
    """Кириллица на встроенном шрифте: 3 повторные правки — размер,
    позиция baseline и шрифт строки стабильны (гарнитура сохраняется)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        buf = open("/usr/share/fonts/TTF/DejaVuSans.ttf", "rb").read()
        page.insert_font(fontname="EdFDejaVu", fontbuffer=buf)
        page.insert_text((72, 100.0), "Счёт: кириллица Тест 123",
                         fontname="EdFDejaVu", fontsize=12)
        # basefont буферного шрифта ("DejaVu Sans Book") не совпадает с
        # внутренним именем ("DejaVuSans") — совмещаем, чтобы блок шел по
        # пути встроенного шрифта, как в настоящих документах (Tahoma и др.).
        xref = page.get_fonts(full=True)[0][0]
        doc.xref_set_key(xref, "BaseFont", "/DejaVuSans")
        doc.save(pdf); doc.close()
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        assert any(ed._page_font_buffers(ed.doc[0]).values()), \
            "буфер встроенного шрифта не найден"
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        first = (b.line_styles[0]["font"], b.line_styles[0]["size"],
                 b.line_styles[0]["origin"])
        for _ in range(3):
            ed = PdfDocument(out)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, b.text)
            ed.save_as(out)
        ed = PdfDocument(out)
        b = ed.blocks(0)[0]
        s = b.line_styles[0]
        assert (s["font"], s["size"]) == (first[0], first[1]), \
            f"шрифт/размер изменились: {(s['font'], s['size'])} != {first[:2]}"
        assert all(abs(a - b2) < 0.25 for a, b2 in zip(s["origin"], first[2])), \
            f"baseline поехала: {s['origin']} != {first[2]}"
        assert "Счёт" in b.text, f"текст потерялся: {b.text!r}"
        print("test_cyrillic_embedded_stable: ok")


def test_cyrillic_htmlbox_stable() -> None:
    """Кириллица через htmlbox (helv + кириллица): 3 повторные правки —
    размер, baseline и число строк стабильны (без дрейфаStory-отступа)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 400, 140), "old latin",
                                      fontname="helv", fontsize=12)
        doc.save(pdf); doc.close()
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], "Счёт: Привет мир русские буквы")
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        first = (b.line_styles[0]["size"], b.line_styles[0]["origin"])
        assert abs(first[0] - 12.0) < 0.01, f"размер: {first[0]}"
        for _ in range(3):
            ed = PdfDocument(out)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, b.text)
            ed.save_as(out)
        b = PdfDocument(out).blocks(0)[0]
        assert abs(b.line_styles[0]["size"] - first[0]) < 0.01
        assert all(abs(a - b2) < 0.25 for a, b2 in zip(b.line_styles[0]["origin"],
                                                      first[1])), \
            f"baseline дрейфует: {b.line_styles[0]['origin']} != {first[1]}"
        assert "Привет" in b.text
        print("test_cyrillic_htmlbox_stable: ok")


MIXED = ('<span style="font-size:12pt"><b>Bold start</b> '
         'regular tail here</span>')


def test_mixed_weight_roundtrip() -> None:
    """Жирное начало + обычный хвост: правка тем же текстом сохраняет ДВА
    спана с исходными шрифтами/размерами/x (было: весь текст жирнел)."""
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        _html_pdf(pdf, MIXED)
        before = _spans(pdf)
        assert len(before) == 2 and "Bold" in before[0][0] \
            and "Bold" not in before[1][0], before
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        assert len(b.line_runs[0]) == 2, f"спанов строки: {b.line_runs[0]}"
        assert b.line_runs[0][1]["text"] == " regular tail here"
        src = pdf
        for _ in range(2):  # повторные правки тем же текстом
            ed = PdfDocument(src)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, b.text)
            ed.save_as(out)
            src = out
        after = _spans(out)
        assert len(after) == 2, after
        for a, c in zip(before, after):
            assert a[0] == c[0], f"шрифт: {a[0]} != {c[0]}"
            assert abs(a[1] - c[1]) < 0.01 and a[4] == c[4], (a, c)
            assert abs(a[2] - c[2]) < 0.01 and abs(a[3] - c[3]) < 0.01, \
                f"позиция: {(a[2], a[3])} != {(c[2], c[3])}"
        print("test_mixed_weight_roundtrip: ok")


def test_mixed_weight_insert_regular() -> None:
    """Вставка в НЕжирную часть: новые символы обычные, жирное начало
    остаётся жирным."""
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        _html_pdf(pdf, MIXED)
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], "Bold start regXYZular tail here")
        ed.save_as(out)
        sp = _spans(out)
        assert "".join(s[4] for s in sp) == "Bold start regXYZular tail here", sp
        assert len(sp) == 2, sp
        assert "Bold" in sp[0][0] and sp[0][4] == "Bold start", sp
        assert "Bold" not in sp[1][0] and "regXYZ" in sp[1][4], sp
        print("test_mixed_weight_insert_regular: ok")


def test_mixed_weight_replace_bold() -> None:
    """Замена текста внутри жирной части остаётся жирной."""
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        _html_pdf(pdf, MIXED)
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], "Bold ZZ regular tail here")
        ed.save_as(out)
        sp = _spans(out)
        assert sp[0][4] == "Bold ZZ" and "Bold" in sp[0][0], sp
        assert "Bold" not in sp[1][0] and "regular tail here" in sp[1][4], sp
        print("test_mixed_weight_replace_bold: ok")


def test_mixed_weight_append() -> None:
    """Добавление в конец строки наследует стиль последнего run, а не
    первого."""
    html = ('<span style="font-size:12pt"><b>Bold</b> regular '
            '<b>bold tail</b></span>')
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        _html_pdf(pdf, html)
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        assert [len(lr) for lr in b.line_runs] == [3], b.line_runs
        ed.set_text(0, b, b.text + " MORE")
        ed.save_as(out)
        sp = _spans(out)
        assert sp[-1][4].endswith("MORE"), sp
        assert "Bold" in sp[-1][0], f"хвост не жирный: {sp}"
        assert "Bold" not in sp[1][0], sp
        print("test_mixed_weight_append: ok")


def test_mixed_sizes_roundtrip() -> None:
    """Три разных размера в одной строке: правка тем же текстом сохраняет
    размер и x каждого спана."""
    html = ('<span style="font-size:10pt">ten</span>'
            '<span style="font-size:16pt">SIXTEEN</span>'
            '<span style="font-size:10pt">ten</span>')
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        _html_pdf(pdf, html)
        before = _spans(pdf)
        assert [round(s[1]) for s in before] == [10, 16, 10], before
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        ed.set_text(0, b, b.text)
        ed.save_as(out)
        after = _spans(out)
        assert len(after) == 3, after
        for a, c in zip(before, after):
            assert abs(a[1] - c[1]) < 0.01 and a[4] == c[4], (a, c)
            assert abs(a[2] - c[2]) < 0.01, f"x: {a[2]} != {c[2]}"
        print("test_mixed_sizes_roundtrip: ok")


def test_cyrillic_embedded_mixed() -> None:
    """Кириллица на встроенных TTF (обычный + жирный в одной строке):
    3 повторные правки сохраняют начертания и позиции обоих спанов."""
    regular = "/usr/share/fonts/TTF/DejaVuSans.ttf"
    bold = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
    if not (os.path.exists(regular) and os.path.exists(bold)):
        print("test_cyrillic_embedded_mixed: skipped (нет DejaVu)")
        return
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        bold_buf = open(bold, "rb").read()
        page.insert_font(fontname="EdFBold", fontbuffer=bold_buf)
        page.insert_font(fontname="EdFReg", fontbuffer=open(regular, "rb").read())
        head = "Счёт жирный"
        page.insert_text((72, 100.0), head, fontname="EdFBold", fontsize=12)
        page.insert_text((72 + pymupdf.Font(fontbuffer=bold_buf).text_length(
            head, fontsize=12), 100.0), " обычный текст", fontname="EdFReg",
            fontsize=12)
        doc.save(pdf)
        doc.close()
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        first = [[(r["font"], r["size"], r["origin"]) for r in lr]
                 for lr in b.line_runs]
        assert len(first) == 1 and len(first[0]) == 2, first
        assert "Bold" in first[0][0][0], first
        src = pdf
        for _ in range(3):
            ed = PdfDocument(src)
            b = ed.blocks(0)[0]
            ed.set_text(0, b, b.text)
            ed.save_as(out)
            src = out
        b = PdfDocument(out).blocks(0)[0]
        assert b.text == "Счёт жирный обычный текст", f"текст: {b.text!r}"
        assert [len(lr) for lr in b.line_runs] == [2], b.line_runs
        for a, c in zip(first[0], b.line_runs[0]):
            assert a[0] == c["font"], f"начертание: {a[0]} != {c['font']}"
            assert abs(a[1] - c["size"]) < 0.01, (a, c)
            assert all(abs(p - q) < 0.01 for p, q in zip(a[2], c["origin"])), \
                f"позиция: {a[2]} != {c['origin']}"
        print("test_cyrillic_embedded_mixed: ok")


def test_htmlbox_line_mixed_sizes_stable() -> None:
    """htmlbox-путь со смешанными размерами в строке: baseline входит по
    отступу САМОГО крупного спана и не дрейфует при повторных правках."""
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 100.0), "??", fontname="helv", fontsize=20)
        page.insert_text((72 + pymupdf.get_text_length(
            "??", fontname="helv", fontsize=20), 100.0), " abc",
            fontname="helv", fontsize=12)
        doc.save(pdf)
        doc.close()
        src = pdf
        for _ in range(3):
            ed = PdfDocument(src)
            ed.set_text(0, ed.blocks(0)[0], "ЖЖ abc")
            ed.save_as(out)
            src = out
        sp = _spans(out)
        assert [round(s[1]) for s in sp] == [20, 12], sp
        assert all(abs(s[3] - 100.0) < 0.01 for s in sp), f"baseline: {sp}"
        print("test_htmlbox_line_mixed_sizes_stable: ok")


def test_htmlbox_long_line_keeps_size() -> None:
    """htmlbox-путь: длинная строка растёт вправо за край бокса, а не
    усаживается (scale_low) из-за того, что бокс обрезан краем страницы."""
    with tempfile.TemporaryDirectory() as d:
        pdf, out = os.path.join(d, "t.pdf"), os.path.join(d, "o.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_textbox(pymupdf.Rect(72, 72, 300, 120), "ЖЖ",
                                      fontname="helv", fontsize=12)
        doc.save(pdf)
        doc.close()
        ed = PdfDocument(pdf)
        b = ed.blocks(0)[0]
        ed.set_text(0, b, b.text + " длинный текст" * 8)
        ed.save_as(out)
        sp = _spans(out)
        assert len(sp) == 1, f"перенос строки: {len(sp)}"
        assert abs(sp[0][1] - 12.0) < 0.5, f"размер усажен: {sp[0][1]}"
        print("test_htmlbox_long_line_keeps_size: ok")


def test_save_after_undo() -> None:
    """После undo документ открыт из памяти: сохранение в тот же файл
    должно идти полной записью, а не падать на инкрементальной."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), "Hello", fontname="helv", fontsize=12)
        doc.save(pdf)
        doc.close()
        ed = PdfDocument(pdf)
        ed.set_text(0, ed.blocks(0)[0], "Hello world")
        ed.save_as(pdf)  # инкрементально — обычный путь
        # undo в GUI переоткрывает документ из памяти
        data = ed.doc.tobytes()
        ed.doc.close()
        ed.doc = pymupdf.open(stream=data, filetype="pdf")
        ed.save_as(pdf)
        assert "Hello world" in pymupdf.open(pdf)[0].get_text(), "текст не сохранён"
        assert ed.path == pdf
        out = os.path.join(d, "o.pdf")
        ed.save_as(out)
        assert ed.path == out and os.path.exists(out), "путь не обновился"
        print("test_save_after_undo: ok")


def test_save_unwritable_raises() -> None:
    """Сохранение в недоступный путь поднимает ошибку — GUI её перехватывает
    (иначе исключение в слоте Qt роняет приложение)."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 100), "Hello", fontname="helv", fontsize=12)
        doc.save(pdf)
        doc.close()
        ed = PdfDocument(pdf)
        try:
            ed.save_as(os.path.join(d, "no-such-dir", "x.pdf"))
        except Exception:
            print("test_save_unwritable_raises: ok")
        else:
            raise AssertionError("ошибка не поднялась")


# --- OCR ----------------------------------------------------------------------

_OCR_ZOOM = 2.0


def _ocr_raster() -> pymupdf.Pixmap:
    """Растр примера страницы: два абзаца по две строки (тёмные полосы)."""
    W, H = 595, 842
    img = np.full((int(H * _OCR_ZOOM), int(W * _OCR_ZOOM), 3), 255,
                  dtype=np.uint8)
    for y_pt, lines in ((80, ["line one text", "line two text"]),
                        (180, ["second para first", "second second"])):
        for i, txt in enumerate(lines):
            y0 = int((y_pt + i * 17) * _OCR_ZOOM)
            h = int(11 * _OCR_ZOOM)
            x1 = int(70 * _OCR_ZOOM) + int(len(txt) * 11 * 0.55 * _OCR_ZOOM)
            img[y0:y0 + h, int(70 * _OCR_ZOOM):x1] = 0
    return pymupdf.Pixmap(pymupdf.csRGB, img.shape[1], img.shape[0],
                          img.tobytes(), 0)


def _ocr_pdf(path: str) -> None:
    """PDF, где страницы 0 и 1 — растр _ocr_raster."""
    pix = _ocr_raster()
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()


def test_ocr_detection_synthetic() -> None:
    blocks = ocr.detect_text_blocks(_ocr_raster().samples,
                                   _ocr_raster().width,
                                   _ocr_raster().height, _OCR_ZOOM)
    assert len(blocks) == 2, "две строки абзаца склеиваются в блок"
    got = [tuple(round(v, 1) for v in b.rect) for b in blocks]
    want = [(70.0, 80.0, 148.5, 108.0), (70.0, 180.0, 172.5, 208.0)]
    for g, w in zip(got, want):
        assert all(abs(a - b) <= 1.0 for a, b in zip(g, w)), f"{g} != {w}"
        assert len(g) == 4
    assert all(len(b.lines) == 2 for b in blocks)
    assert 11.0 <= blocks[0].size <= 14.0, "размер строки ~11pt из высоты"
    print("test_ocr_detection_synthetic: ok")


class _FakeClient:
    """Подменяет ocr.OcrClient: canned-ответы или raise."""
    canned: dict[int, str] = {}
    styles: dict[int, dict] = {}
    fail_all: bool = False
    n: int = 0

    def __init__(self, cfg):
        self.cfg = cfg

    def transcribe_crop(self, png: bytes) -> tuple[str, dict]:
        if _FakeClient.fail_all:
            raise ocr.OcrError("fake API error")
        _FakeClient.n += 1
        i = _FakeClient.n - 1
        return _FakeClient.canned.get(i, ""), _FakeClient.styles.get(i, {})


def _reset_fake(canned=None, fail_all: bool = False, styles=None) -> None:
    ocr.OcrClient = _FakeClient  # изоляция от сети для всех OCR-тестов
    _FakeClient.canned = canned or {}
    _FakeClient.styles = styles or {}
    _FakeClient.fail_all = fail_all
    _FakeClient.n = 0


def test_ocr_apply_invisible() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        _ocr_pdf(path)
        doc = PdfDocument(path)
        pix = _ocr_raster()
        _reset_fake({i: t for i, t in enumerate(
            ["line one text\nline two text",
             "second para first\nsecond second"])})
        cfg = ocr.OcrConfig("http://x", "m", "k", "")
        res = ocr.run_page_ocr(cfg, pix, _OCR_ZOOM, max_workers=1)
        doc.apply_ocr(0, res)
        text = doc.doc[0].get_text()
        assert "line one text" in text and "second second" in text
        con = doc.doc[0].read_contents()
        assert b"3 Tr" in con, "текст невидим (render_mode=3)"
        # GUI получает блоки через.blocks() как обычные.
        gui = doc.blocks(0)
        assert any("line one text" in b.text for b in gui)
        # Кириллица — через системный буфер шрифта.
        doc.apply_ocr(1, [OcrBlock(rect=pymupdf.Rect(70, 60, 400, 90),
                                   text="Счёт на оплату", size=12,
                                   lines=[pymupdf.Rect(70, 60, 400, 90)])])
        assert "Счёт на оплату" in doc.doc[1].get_text()
        out = os.path.join(d, "out.pdf")
        doc.save_as(out)
        reread = pymupdf.open(out)
        assert "Счёт на оплату" in reread[1].get_text()
        print("test_ocr_apply_invisible: ok")


def test_invisible_layer_flag() -> None:
    """Блоки невидимого слоя распознаются по char_flags; видимый текст — нет."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        pg = doc.new_page(width=200, height=120)
        pg.insert_text((20, 40), "visible normal", fontname="helv", fontsize=12)
        pg.insert_text((20, 90), "invisible ocr", fontname="helv", fontsize=12,
                       render_mode=3)
        doc.save(path)
        doc.close()
        doc = PdfDocument(path)
        blocks = doc.blocks(0)
        by_text = {b.text: b for b in blocks}
        assert by_text["visible normal"].invisible is False
        assert by_text["invisible ocr"].invisible is True
        print("test_invisible_layer_flag: ok")


def test_ocr_edit_clears_raster() -> None:
    """Правка блока невидимого слоя затирает растровый текст под блоком,
    остальное изображение и соседние картинки не трогаются."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        doc = pymupdf.open()
        pg = doc.new_page(width=200, height=120)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 40))
        pix.set_rect(pix.irect, (200, 30, 30))  # "фон" — красная плашка
        pg.insert_image(pymupdf.Rect(20, 20, 120, 60), pixmap=pix)
        pg.insert_text((25, 50), "raster text", fontname="helv", fontsize=10,
                       render_mode=3)
        doc.save(path)
        doc.close()

        doc = PdfDocument(path)
        block = doc.blocks(0)[0]
        assert block.invisible, "блок OCR-слоя должен считаться невидимым"
        doc.set_text(0, block, "replaced text")
        pg = doc.doc[0]
        # Новый текст видим (не невидимый слой).
        spans = [s for b in pg.get_text("dict")["blocks"] if b["type"] == 0
                 for l in b["lines"] for s in l["spans"]]
        assert any("replaced text" in s["text"] and s["char_flags"] != 0
                   for s in spans)
        # Растр в зоне блока очищен, вне зоны картинка цела.
        r = pg.get_pixmap()
        def px(x: float, y: float) -> tuple:
            return tuple(r.pixel(int(x), int(y))[:3])
        bbox = block.rect
        assert px((bbox.x0 + bbox.x1) / 2, bbox.y0 + 2) != (200, 30, 30), \
            "растровый текст не затёрт"
        assert px(bbox.x1 + 10, (bbox.y0 + bbox.y1) / 2) == (200, 30, 30), \
            "соседняя область картинки пострадала"
        print("test_ocr_edit_clears_raster: ok")


def test_ocr_page_abort_policy() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        _ocr_pdf(path)
        doc = PdfDocument(path)
        _reset_fake(fail_all=True)
        cfg = ocr.OcrConfig("http://x", "m", "k", "")
        try:
            ocr.run_page_ocr(cfg, _ocr_raster(), _OCR_ZOOM, max_workers=1)
        except ocr.OcrError:
            pass
        else:
            raise AssertionError("ошибка не поднялась")
        assert doc.blocks(0) == [], "страница не тронута при сбое"
        print("test_ocr_page_abort_policy: ok")


def test_ocr_clean_text() -> None:
    assert ocr._clean_text("привет") == "привет"
    assert ocr._clean_text("```json\nпривет\n```") == "привет"
    assert ocr._clean_text("```\nпривет\n```") == "привет"
    assert ocr._clean_text("  привет  ") == "привет"
    print("test_ocr_clean_text: ok")


def test_ocr_parse_reply() -> None:
    t, s = ocr._parse_reply('{"text": "привет", "bold": true, "italic": false}')
    assert t == "привет" and s == {"bold": True, "italic": False}
    t, s = ocr._parse_reply('```json\n{"text": "x", "serif": true}\n```')
    assert t == "x" and s == {"serif": True}
    t, s = ocr._parse_reply("просто текст")
    assert t == "просто текст" and s == {}
    t, s = ocr._parse_reply('{"no_text": 1}')
    assert s == {}
    print("test_ocr_parse_reply: ok")


def test_ocr_style_mapping() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        _ocr_pdf(path)
        doc = PdfDocument(path)
        doc.apply_ocr(0, [OcrBlock(
            rect=pymupdf.Rect(70, 60, 300, 90), text="Bold sample", size=12,
            lines=[pymupdf.Rect(70, 60, 300, 90)], style={"bold": True})])
        doc.apply_ocr(1, [OcrBlock(
            rect=pymupdf.Rect(70, 60, 300, 90), text="Italic serif", size=12,
            lines=[pymupdf.Rect(70, 60, 300, 90)],
            style={"serif": True, "italic": True})])

        def fonts(i):
            return [s["font"]
                    for b in doc.doc[i].get_text("dict")["blocks"]
                    if b["type"] == 0 for l in b["lines"] for s in l["spans"]]

        assert any("Bold" in f for f in fonts(0)), fonts(0)
        assert any("Times" in f and "Italic" in f for f in fonts(1)), fonts(1)
        print("test_ocr_style_mapping: ok")


def _style_pixmap(bold: bool, italic: bool) -> pymupdf.Pixmap:
    """Растр строки «текста»: вертикальные штрихи (полосы %) по строке."""
    z = 2.0
    W, H = 300, 160
    img = np.full((int(H * z), int(W * z), 3), 255, dtype=np.uint8)
    h = int(16 * z)
    thick = 5 if bold else 2
    for r in range(h):
        off = int(40 * r / h) if italic else 0
        row = img[int(40 * z) + r]
        xs = np.arange(200)
        row[off:off + len(xs)][(xs % 10) < thick] = 0
    return pymupdf.Pixmap(pymupdf.csRGB, img.shape[1], img.shape[0],
                          img.tobytes(), 0)


def test_ocr_style_heuristics() -> None:
    for bold, italic in ((False, False), (True, False), (False, True)):
        pm = _style_pixmap(bold, italic)
        blocks = ocr.detect_text_blocks(pm.samples, pm.width, pm.height, 2.0)
        assert len(blocks) == 1, f"ожидался один блок, {len(blocks)}"
        style = blocks[0].style
        assert bool(style.get("bold")) == bold, f"bold={bold}, {style}"
        assert bool(style.get("italic")) == italic, f"italic={italic}, {style}"
    print("test_ocr_style_heuristics: ok")


if __name__ == "__main__":
    test_blocks()
    test_edit_roundtrip()
    test_save_same_file()
    test_base14_mapping()
    test_no_shrink_and_move()
    test_no_shrink_big_size()
    test_longer_text_keeps_size()
    test_no_wrap_longer_text()
    test_explicit_newlines()
    test_line_styles_per_line()
    test_cyrillic_embedded_stable()
    test_cyrillic_htmlbox_stable()
    test_mixed_weight_roundtrip()
    test_mixed_weight_insert_regular()
    test_mixed_weight_replace_bold()
    test_mixed_weight_append()
    test_mixed_sizes_roundtrip()
    test_cyrillic_embedded_mixed()
    test_htmlbox_line_mixed_sizes_stable()
    test_htmlbox_long_line_keeps_size()
    test_save_after_undo()
    test_save_unwritable_raises()
    test_ocr_detection_synthetic()
    test_ocr_apply_invisible()
    test_ocr_page_abort_policy()
    test_invisible_layer_flag()
    test_ocr_edit_clears_raster()
    test_ocr_clean_text()
    test_ocr_parse_reply()
    test_ocr_style_mapping()
    test_ocr_style_heuristics()
    print("ALL OK")
    sys.exit(0)
