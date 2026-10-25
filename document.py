"""Модель PDF-редактора: рендер, блоки текста, правка блоков. Без GUI."""
import html as _html
from dataclasses import dataclass

import pymupdf

_BASE14 = pymupdf.Base14_fontdict


@dataclass
class TextBlock:
    rect: pymupdf.Rect
    text: str
    font: str
    size: float
    color: tuple


def _clean_font_name(name: str) -> str:
    return name.split("+")[-1].strip()


# Короткие зарезервированные имена Base-14: MuPDF считает метрики по-разному
# для "helv" и "helvetica", длинное имя даёт ложный дефицит ширины и вечную
# усадку при повторных правках. Вставляем только по коротким именам.
_FULL_TO_SHORT = {
    "helvetica": "helv", "helvetica-oblique": "heit",
    "helvetica-bold": "hebo", "helvetica-boldoblique": "hebi",
    "courier": "cour", "courier-oblique": "coit",
    "courier-bold": "cobo", "courier-boldoblique": "cobi",
    "times-roman": "tiro", "times-italic": "tiit",
    "times-bold": "tibo", "times-bolditalic": "tibi",
    "symbol": "symb", "zapfdingbats": "zadb",
}


def _base14_key(name: str) -> str:
    n = _clean_font_name(name).lower()
    if n in _FULL_TO_SHORT:
        return _FULL_TO_SHORT[n]
    if n in _BASE14:  # уже короткое зарезервированное имя
        return n
    bold = "bold" in n
    italic = "italic" in n or "oblique" in n
    mono = any(w in n for w in ("mono", "courier", "consolas"))
    serif = any(w in n for w in ("times", "georgia", "serif", "roman"))
    fam = "co" if mono else ("ti" if serif else "he")
    if bold and italic:
        return fam + "bi"
    if bold:
        return fam + "bo"
    if italic:
        return fam + "it"
    return fam


def _cjk_font(text: str) -> str | None:
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            return "japan"
        if 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F or 0xAC00 <= o <= 0xD7AF:
            return "korea"
        if 0x3400 <= o <= 0x9FFF:
            return "china-s"
    return None


def _insert_fit(page, rect, text, fontname, encoding, start_size, color) -> bool:
    """Вставить текст. rc<0 у insert_textbox = дефицит высоты в пунктах:
    расширяем бокс вниз на недостающее (глифы идут от origin, поэтому
    позиция первой строки не меняется). Шрифт уменьшаем только когда
    расширенный бокс выходит за страницу. Ничего не вставляет, если не
    влезло даже так (вызывающий решает fallback); True, если влезло."""
    s = start_size
    page_y1 = page.rect.y1
    while s >= 4:
        rc = page.insert_textbox(rect, text, fontname=fontname, encoding=encoding,
                                 fontsize=s, color=color,
                                 align=pymupdf.TEXT_ALIGN_LEFT)
        if rc >= 0:
            return True
        bigger = pymupdf.Rect(rect)
        bigger.y1 = min(bigger.y1 - rc + 2, page_y1)
        if page.insert_textbox(bigger, text, fontname=fontname, encoding=encoding,
                               fontsize=s, color=color,
                               align=pymupdf.TEXT_ALIGN_LEFT) >= 0:
            return True
        s -= 0.5
    return False


class PdfDocument:
    def __init__(self, path: str):
        self.path = path
        self.doc = pymupdf.open(path)

    @property
    def page_count(self) -> int:
        return len(self.doc)

    def page_pixmap(self, idx: int, zoom: float = 2.0) -> pymupdf.Pixmap:
        return self.doc[idx].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)

    def thumbnail_pixmap(self, idx: int) -> pymupdf.Pixmap:
        return self.page_pixmap(idx, zoom=0.2)

    def blocks(self, idx: int) -> list[TextBlock]:
        out = []
        for b in self.doc[idx].get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            spans = [s for line in b["lines"] for s in line["spans"] if s["text"]]
            if not spans:
                continue
            text = "\n".join(
                "".join(s["text"] for s in line["spans"]) for line in b["lines"]
            )
            c = spans[0]["color"]
            out.append(TextBlock(
                rect=pymupdf.Rect(b["bbox"]),
                text=text,
                font=spans[0]["font"],
                size=spans[0]["size"],
                color=((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255),
            ))
        return out

    def embedded_font_names(self) -> list[str]:
        """Уникальные имена встроенных шрифтов документа (очищенные)."""
        out = []
        seen = set()
        for page in self.doc:
            for row in page.get_fonts(full=True):
                _xref, ext, _ftype, basefont, _ref, _enc = row[:6]
                if ext == "n/a":
                    continue
                name = _clean_font_name(basefont)
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        return out

    def _embedded_font_buffer(self, page, font_name: str) -> bytes | None:
        target = _clean_font_name(font_name).lower()
        for row in page.get_fonts(full=True):
            xref, ext, _ftype, basefont, _refname, _enc = row[:6]
            if ext == "n/a" or _clean_font_name(basefont).lower() != target:
                continue
            _name, _ext, _type, buf = self.doc.extract_font(xref)
            if buf:
                return buf
        return None

    def _first_span_bbox(self, page, near: pymupdf.Rect) -> pymupdf.Rect | None:
        """bbox первого (верхнего левого) спана с центром внутри near."""
        best = None
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                for s in line["spans"]:
                    if not s["text"].strip():
                        continue
                    r = pymupdf.Rect(s["bbox"])
                    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                    if near.x0 <= cx <= near.x1 and near.y0 <= cy <= near.y1:
                        if best is None or (r.y0, r.x0) < (best.y0, best.x0):
                            best = r
        return best

    def set_text(self, idx: int, block: TextBlock, new_text: str,
                 font_choice: str | None = None,
                 font_size: float | None = None,
                 insert_rect: pymupdf.Rect | None = None) -> None:
        page = self.doc[idx]
        # Redaction всегда по исходному прямоугольнику блока.
        page.add_redact_annot(block.rect, fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        if not new_text:
            return

        size = font_size if font_size else block.size
        target = pymupdf.Rect(insert_rect) if insert_rect is not None else pymupdf.Rect(block.rect)
        # Глифы стартуют от origin бокса вставки, а плотный bbox из get_text
        # не оставляет места на межстрочный интервал; после apply_redactions
        # MuPDF ещё и пересоздаёт ресурс шрифта с большей метрикой — поэтому
        # запас вправо/вниз, но не влево/вверх (иначе блок "уползает" и
        # текст уменьшается при каждой правке).
        box = target + (0, 0, 6, 6)
        clean = _clean_font_name(block.font)
        r, g, b = block.color

        def htmlbox() -> None:
            body = _html.escape(new_text, quote=False)
            html = (
                f'<span style="font-size:{size:.1f}pt;'
                f'color:rgb({r*255:.0f},{g*255:.0f},{b*255:.0f});">{body}</span>'
            )

            def do(bx: pymupdf.Rect) -> None:
                rc = page.insert_htmlbox(bx, html, scale_low=1.0)
                # rc[0]<0 = дефицит высоты: расширяем бокс вниз и пробуем
                # снова; scale_low=0 (беззвучная усадка) — только если
                # расширенный бокс упёрся в низ страницы.
                if isinstance(rc, tuple) and rc and rc[0] < 0:
                    bx = pymupdf.Rect(bx)
                    bx.y1 = min(bx.y1 - rc[0] + 2, page.rect.y1)
                    rc = page.insert_htmlbox(bx, html, scale_low=1.0)
                    if isinstance(rc, tuple) and rc and rc[0] < 0:
                        page.insert_htmlbox(bx, html, scale_low=0.0)

            do(box)
            # Story кладёт глифы со своим внутренним отступом (зависит от
            # шрифта/size, магической константы нет): измеряем и перставляем
            # со сдвигом, иначе блок "уползает" при каждой правке.
            bb = self._first_span_bbox(page, box)
            if bb is not None:
                dx, dy = target.x0 - bb.x0, target.y0 - bb.y0
                if abs(dx) > 0.25 or abs(dy) > 0.25:
                    page.add_redact_annot(bb + (-0.5, -0.5, 0.5, 0.5), fill=False)
                    page.apply_redactions(
                        images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    )
                    do(pymupdf.Rect(box.x0 + dx, box.y0 + dy, box.x1 + dx, box.y1 + dy))

        # shortcut: redaction применён до вставки; если вставка упадёт, блок
        # уже пуст. Вызывающие (GUI) обязаны сохранить снимок до вызова;
        # библиотечные вызовы без снимка теряют текст при сбое.
        # Встроенный шрифт оригинального блока (если есть).
        original_refname = None
        if clean.lower() not in _BASE14:
            buf = self._embedded_font_buffer(page, block.font)
            if buf is not None:
                original_refname = "EdF" + "".join(ch for ch in clean if ch.isalnum())[:32]
                page.insert_font(fontname=original_refname, fontbuffer=buf)
                # shortcut: встроенный субсет может не содержать глифов для
                # новых символов -> tofu; апгрейд: htmlbox-fallback при обнаружении.

        # 1. Явно выбранный пользователем шрифт.
        if font_choice and font_choice not in ("auto", None):
            if font_choice.startswith("emb:"):
                name = font_choice[4:]
                buf = self._embedded_font_buffer(page, name)
                if buf is not None:
                    ref = "EdF" + "".join(ch for ch in name if ch.isalnum())[:32]
                    page.insert_font(fontname=ref, fontbuffer=buf)
                    if _insert_fit(page, box, new_text, ref, pymupdf.TEXT_ENCODING_LATIN,
                                   size, block.color):
                        return
            else:
                # Base-14 / CJK. Кириллица/греческий в Base-14 дают '?',
                # поэтому для не-ascii (кроме CJK) используем htmlbox.
                if not new_text.isascii() and font_choice not in (
                        "china-s", "china-ss", "japan", "korea"):
                    htmlbox()
                    return
                if not _insert_fit(page, box, new_text, _base14_key(font_choice),
                                   pymupdf.TEXT_ENCODING_LATIN, size, block.color):
                    htmlbox()
                return
            htmlbox()
            return

        # 2. Авто: сохранить исходный встроенный шрифт.
        if original_refname is not None:
            if _insert_fit(page, box, new_text, original_refname,
                        pymupdf.TEXT_ENCODING_LATIN, size, block.color):
                return
            # субсет не содержит нужных глифов — пробуем дальше

        # 3. Авто: CJK — reserved name.
        cjk = _cjk_font(new_text) if not new_text.isascii() else None
        if cjk is not None:
            _insert_fit(page, box, new_text, cjk, pymupdf.TEXT_ENCODING_LATIN,
                        size, block.color)
            return
        # Кириллица/греческий без встроенного шрифта — только htmlbox (unicode).
        if not new_text.isascii():
            htmlbox()
            return
        # ASCII — похожий Base-14; не влезло — htmlbox (сохранит размер).
        key = _base14_key(block.font)
        if not _insert_fit(page, box, new_text, key, pymupdf.TEXT_ENCODING_LATIN,
                           size, block.color):
            htmlbox()

    def save_as(self, path: str) -> None:
        if path == self.path:
            self.doc.save(self.path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        else:
            self.doc.save(path)