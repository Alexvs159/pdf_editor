"""Модель PDF-редактора: рендер, блоки текста, правка блоков. Без GUI."""
import html as _html
from dataclasses import dataclass

import pymupdf

# Имена Base-14 шрифтов — любые ключи/значения pymupdf.Base14_fontdict.
_BASE14 = pymupdf.Base14_fontdict


@dataclass
class TextBlock:
    rect: pymupdf.Rect
    text: str
    font: str      # имя шрифта первого спана
    size: float    # размер первого спана, pt
    color: tuple   # (r, g, b), значения 0..1


def _clean_font_name(name: str) -> str:
    """'ABCDEF+Arial-BoldMT' -> 'Arial-BoldMT'."""
    return name.split("+")[-1].strip()


def _base14_key(name: str) -> str:
    """Подобрать ключ Base-14 шрифта по имени шрифта из PDF."""
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
    """Reserved-имя встроенного CJK шрифта, если текст не латиница/кириллица."""
    for ch in text:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            return "japan"
        if 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F or 0xAC00 <= o <= 0xD7AF:
            return "korea"
        if 0x3400 <= o <= 0x9FFF:
            return "china-s"
    return None


def _encoding_flag(text: str) -> int:
    """Кодировка Base-14 для insert_textbox: греческий/кириллица или латынь."""
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            return pymupdf.TEXT_ENCODING_CYRILLIC
        if 0x0370 <= o <= 0x03FF:
            return pymupdf.TEXT_ENCODING_GREEK
    return pymupdf.TEXT_ENCODING_LATIN


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
        """Текстовые блоки страницы (только type=0, т.е. обычный текст)."""
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

    def _embedded_font_buffer(self, page, font_name: str) -> bytes | None:
        """Буфер встроенного шрифта, совпадающего по имени, или None."""
        target = _clean_font_name(font_name).lower()
        for xref, ext, _ftype, basefont, _refname, _enc in page.get_fonts(full=True):
            if ext == "n/a" or _clean_font_name(basefont).lower() != target:
                continue
            _name, _ext, _type, buf = self.doc.extract_font(xref)
            if buf:
                return buf
        return None

    def set_text(self, idx: int, block: TextBlock, new_text: str) -> None:
        """Заменить текст блока: удалить старый (redaction), вставить новый.

        Если new_text пустой, блок просто очищается.
        """
        page = self.doc[idx]
        page.add_redact_annot(block.rect, fill=False)
        # Картинки и векторную графику не трогаем, удаляем только текст.
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        if not new_text:
            return

        # Шрифт: 1) тот же встроенный, 2) Base-14 с тем же именем,
        # 3) похожий Base-14. Буфер субсета может не иметь нужных глифов
        # для новых символов — известное ограничение.
        clean = _clean_font_name(block.font)
        refname = None
        if clean.lower() not in _BASE14:
            # Для зарезервированных имён Base-14 insert_font игнорирует буфер.
            buf = self._embedded_font_buffer(page, block.font)
            if buf is not None:
                refname = "EdF" + "".join(ch for ch in clean if ch.isalnum())[:32]
                page.insert_font(fontname=refname, fontbuffer=buf)
        size = block.size
        rect = pymupdf.Rect(block.rect)
        if not new_text.isascii():
            # Греческий/кириллица/CJK: Base-14 вставляет '?', пишем через Story
            # (insert_htmlbox): unicode гарантирован, шрифт подбирает движок.
            r, g, b = block.color
            body = _html.escape(new_text, quote=False)
            page.insert_htmlbox(rect, (
                f'<span style="font-size:{block.size:.1f}pt;'
                f'color:rgb({r*255:.0f},{g*255:.0f},{b*255:.0f});">{body}</span>'
            ))
            return
        cjk = _cjk_font(new_text)
        enc = _encoding_flag(new_text)

        def insert(fs: float) -> float:
            if refname:  # тот же встроенный шрифт
                return page.insert_textbox(rect, new_text, fontname=refname,
                                           fontsize=fs, color=block.color,
                                           align=pymupdf.TEXT_ALIGN_LEFT)
            key = cjk or _base14_key(block.font)
            return page.insert_textbox(rect, new_text, fontname=key, encoding=enc,
                                       fontsize=fs, color=block.color,
                                       align=pymupdf.TEXT_ALIGN_LEFT)

        while size >= 4:  # уменьшаем, пока текст не влезет в блок (мин. 4pt)
            if insert(size) >= 0:
                return
            size -= 0.5
        insert(4)  # не влезло — вставляем всё равно в 4pt, как у add_redact_annot

    def save_as(self, path: str) -> None:
        if path == self.path:
            # Тот же файл: только инкрементально, с сохранением шифрования.
            self.doc.save(self.path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        else:
            self.doc.save(path)
