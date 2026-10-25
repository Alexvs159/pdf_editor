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
        assert ed._embedded_font_buffer(ed.doc[0], b.font) is not None, \
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
    print("ALL OK")
    sys.exit(0)
