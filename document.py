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


def _base14_key(name: str) -> str:
    n = _clean_font_name(name).lower()
    if n in _BASE14:
        return n
    bold = "bold" in n
    italic = "italic" in n or "oblique" in n
    mono = any(w in n for w in ("mono", "courier", "consolas"))
    serif = any(w in n for w in ("times", "georgia", "serif", "roman"))
    base = "courier" if mono else ("times" if serif else "helvetica")
    if bold and italic:
        return base + "-bolditalic"
    if bold:
        return base + "-bold"
    if italic:
        return base + "-italic"
    return base


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
    """Вставить текст, уменьшая шрифт до 4pt. True, если влезло."""
    s = start_size
    while s >= 4:
        if page.insert_textbox(rect, text, fontname=fontname, encoding=encoding,
                             fontsize=s, color=color,
                             align=pymupdf.TEXT_ALIGN_LEFT) >= 0:
            return True
        s -= 0.5
    page.insert_textbox(rect, text, fontname=fontname, encoding=encoding,
                       fontsize=4, color=color, align=pymupdf.TEXT_ALIGN_LEFT)
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

    def set_text(self, idx: int, block: TextBlock, new_text: str,
                 font_choice: str | None = None) -> None:
        page = self.doc[idx]
        page.add_redact_annot(block.rect, fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        if not new_text:
            return

        clean = _clean_font_name(block.font)
        size = block.size
        rect = pymupdf.Rect(block.rect)
        r, g, b = block.color

        def htmlbox() -> None:
            body = _html.escape(new_text, quote=False)
            page.insert_htmlbox(rect, (
                f'<span style="font-size:{block.size:.1f}pt;'
                f'color:rgb({r*255:.0f},{g*255:.0f},{b*255:.0f});">{body}</span>'
            ))

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
                    _insert_fit(page, rect, new_text, ref, pymupdf.TEXT_ENCODING_LATIN,
                                size, block.color)
                    return
            else:
                # Base-14 / CJK. Кириллица/греческий в Base-14 дают '?',
                # поэтому для не-ascii (кроме CJK) используем htmlbox.
                if not new_text.isascii() and font_choice not in (
                        "china-s", "china-ss", "japan", "korea"):
                    htmlbox()
                    return
                _insert_fit(page, rect, new_text, font_choice,
                            pymupdf.TEXT_ENCODING_LATIN, size, block.color)
                return
            htmlbox()
            return

        # 2. Авто: сохранить исходный встроенный шрифт.
        if original_refname is not None:
            _insert_fit(page, rect, new_text, original_refname,
                        pymupdf.TEXT_ENCODING_LATIN, size, block.color)
            return

        # 3. Авто: CJK — reserved name.
        cjk = _cjk_font(new_text) if not new_text.isascii() else None
        if cjk is not None:
            _insert_fit(page, rect, new_text, cjk, pymupdf.TEXT_ENCODING_LATIN,
                        size, block.color)
            return
        # Кириллица/греческий без встроенного шрифта — только htmlbox (unicode).
        if not new_text.isascii():
            htmlbox()
            return
        # ASCII — похожий Base-14.
        key = _base14_key(block.font)
        _insert_fit(page, rect, new_text, key, pymupdf.TEXT_ENCODING_LATIN,
                    size, block.color)

    def save_as(self, path: str) -> None:
        if path == self.path:
            self.doc.save(self.path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        else:
            self.doc.save(path)