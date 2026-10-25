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
    # Стиль по строкам (первый span каждой строки): font, size, color,
    # origin=(x, y) базовой линии — для сохранения позиций строк при правке.
    # None = не извлекалось (TextBlock создан вручную).
    line_styles: list[dict] | None = None


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
    # без суффикса — регулярное начертание семейства ("he" — не имя)
    return {"co": "cour", "ti": "tiro", "he": "helv"}[fam]


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


def _insert_text_fit(page, refname, text, x, y, size, color) -> bool:
    """Одна строка от базовой линии (x, y) БЕЗ переноса по ширине
    (insert_text позиционирует глифы по точке и ширину строки не
    ограничивает; "\n" в тексте недопустим — вызывающий разбил по строкам).
    Шрифт уменьшаем только когда baseline целиком уходит за низ страницы:
    позицию (x, y) не двигаем — иначе блок дрейфует при повторных правках.
    rc<0 у insert_text = сбой шрифта; возвращает True, если влезло."""
    s = size
    while s >= 4 and y > page.rect.y1 - s * 0.3:
        s -= 0.5
    if s < 4:
        return False
    return page.insert_text((x, y), text, fontname=refname, fontsize=s,
                            color=color,
                            encoding=pymupdf.TEXT_ENCODING_LATIN) >= 0


_story_inset_cache: dict[float, tuple] = {}


def _story_inset(size: float) -> tuple:
    """Внутренний отступ Story-спана от бокса вставки (x, y), зависит
    только от size. Измеряется один раз на отдельном документе."""
    if size not in _story_inset_cache:
        with pymupdf.open() as tmp:
            page = tmp.new_page()
            box = pymupdf.Rect(0, 0, 200, size * 2)
            html = (f'<span style="font-size:{size:.1f}pt;'
                    f'color:rgb(0,0,0);">Ij</span>')
            page.insert_htmlbox(box, html, scale_low=0.0)
            found = [s for b in page.get_text("dict")["blocks"] if b["type"] == 0
                     for l in b["lines"] for s in l["spans"]]
            if found:
                _story_inset_cache[size] = (found[0]["origin"][0] - box.x0,
                                            found[0]["origin"][1] - box.y0)
            else:
                _story_inset_cache[size] = (0.0, 0.0)
    return _story_inset_cache[size]


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
            line_styles = []
            for line in b["lines"]:
                spans = line["spans"]
                if spans:
                    s0 = spans[0]  # shortcut: смешение стилей внутри одной
                    # строки сведено к первому span; апгрейд: посимвольные стили
                    c = s0["color"]
                    line_styles.append({
                        "font": s0["font"],
                        "size": s0["size"],
                        "color": ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255,
                                  (c & 255) / 255),
                        "origin": tuple(s0["origin"]),
                    })
                else:
                    # пустая строка наследует стиль предыдущей
                    line_styles.append(dict(line_styles[-1]) if line_styles else {})
            if not line_styles:
                continue
            text = "\n".join(
                # shortcut: TTF-буферы с LATIN-кодировкой извлекают пробел
                # как \xa0 (артефакт MuPDF); нормализуем, иначе после правки
                # \xa0 попадает в редактор и в следующие вставки.
                "".join(s["text"] for s in line["spans"]).replace("\xa0", " ")
                for line in b["lines"]
            )
            if not text:
                continue
            out.append(TextBlock(
                rect=pymupdf.Rect(b["bbox"]),
                text=text,
                font=line_styles[0]["font"],
                size=line_styles[0]["size"],
                color=line_styles[0]["color"],
                line_styles=line_styles,
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

    def _page_font_buffers(self, page) -> dict[str, bytes]:
        """Буферы встроенных шрифтов страницы. Снимок ДО redaction:
        MuPDF может удалить ресурс шрифта, если красактом вырезан весь
        его текст — тогда буфер уже не получить. Ключи — очищенное имя
        и нормализованное (без пробелов/дефисов): внутреннее имя спана
        может отличаться от /BaseFont ('DejaVuSans' vs 'DejaVu Sans Book')."""
        out: dict[str, bytes] = {}
        for row in page.get_fonts(full=True):
            xref, ext, _ftype, basefont, _ref, _enc = row[:6]
            if ext == "n/a":
                continue
            _n, _e, _t, buf = self.doc.extract_font(xref)
            if not buf:
                continue
            key = _clean_font_name(basefont).lower()
            for k in (key, key.replace(" ", "").replace("-", "")):
                out.setdefault(k, buf)
        return out

    def set_text(self, idx: int, block: TextBlock, new_text: str,
                 font_choice: str | None = None,
                 font_size: float | None = None,
                 insert_rect: pymupdf.Rect | None = None) -> None:
        page = self.doc[idx]
        # Буферы шрифтов — до красакта (см. _page_font_buffers).
        font_buffers = self._page_font_buffers(page)
        # Redaction всегда по исходному прямоугольнику блока.
        page.add_redact_annot(block.rect, fill=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        )
        if not new_text:
            return

        target = pymupdf.Rect(insert_rect) if insert_rect is not None else pymupdf.Rect(block.rect)
        dx, dy = target.x0 - block.rect.x0, target.y0 - block.rect.y0
        styles = block.line_styles or [
            {"font": block.font, "size": block.size, "color": block.color,
             "origin": ()}]

        def src_style(i: int) -> dict:
            """Стиль строки i: строка i наследует исходную строку i; новых
            строк больше — стиль последней исходной строки."""
            if i < len(styles):
                s = styles[i]
            else:
                s = styles[-1]
            if not s:
                s = {"font": block.font, "size": block.size, "color": block.color,
                     "origin": ()}
            return s

        lines = new_text.split("\n")  # "\n" пользователя = явный перенос

        # Базовые линии: то же число строк — исходные baseline каждой строки
        # (x и y строки сохраняются при повторных правках); иначе
        # последовательная укладка от первой baseline шагом fontsize*1.2.
        have_orig = (len(lines) == len(styles)
                     and all(len(s.get("origin", ())) == 2 for s in styles))
        o0 = styles[0].get("origin", ())
        if len(o0) == 2:
            pts = [(o0[0] + dx, o0[1] + dy)]
        else:
            # TextBlock без извлечённых origin (создан вручную)
            pts = [(target.x0, target.y0 + (font_size or block.size))]
        if not have_orig:
            for i in range(1, len(lines)):
                step_size = font_size or src_style(i - 1)["size"]
                pts.append((pts[0][0], pts[-1][1] + step_size * 1.2))
        else:
            pts = [(s["origin"][0] + dx, s["origin"][1] + dy) for s in styles]

        font_memo: dict[str, str | None] = {}

        def emb_ref(font_name: str) -> str | None:
            """refname встроенного шрифта страницы (вставляется один раз)."""
            clean = _clean_font_name(font_name)
            key = clean.lower()
            if key in _BASE14:
                return None
            if key not in font_memo:
                norm = key.replace(" ", "").replace("-", "")
                buf = (font_buffers.get(key) or font_buffers.get(norm))
                if not buf:
                    # префиксный матч: имя спана короче /BaseFont
                    for k, b in font_buffers.items():
                        if k.startswith(norm) or norm.startswith(k):
                            buf = b
                            break
                ref = None
                if buf:
                    ref = "EdF" + "".join(ch for ch in clean if ch.isalnum())[:32]
                    page.insert_font(fontname=ref, fontbuffer=buf)
                font_memo[key] = ref
            return font_memo[key]

        def html_line(text: str, size: float, color: tuple, x: float, y: float) -> None:
            r, g, b = color
            body = _html.escape(text, quote=False)
            html = (
                f'<span style="font-size:{size:.1f}pt;'
                f'color:rgb({r*255:.0f},{g*255:.0f},{b*255:.0f});">{body}</span>'
            )
            # Story кладёт глифы со своим внутренним отступом от бокса,
            # зависящим только от size: измеряем его на отдельном документе
            # (с кэшем) и входим в baseline (x, y) с первого раза. Redact-
            # перестановка (как раньше) удаляла соседние строки: MuPDF
            # убирает любой текст, чей bbox пересекает красакт-прямоугольник.
            ix, iy = _story_inset(size)
            box = pymupdf.Rect(x - ix, y - iy, page.rect.x1, y - iy + size * 2)
            rc = page.insert_htmlbox(box, html, scale_low=1.0)
            # rc[0]<0 = дефицит высоты: расширяем бокс вниз и пробуем снова;
            # scale_low=0 (беззвучная усадка) — только если упёрлись в низ
            # страницы.
            if isinstance(rc, tuple) and rc and rc[0] < 0:
                box.y1 = min(box.y1 - rc[0] + 2, page.rect.y1)
                rc = page.insert_htmlbox(box, html, scale_low=1.0)
                if isinstance(rc, tuple) and rc and rc[0] < 0:
                    page.insert_htmlbox(box, html, scale_low=0.0)

        # shortcut: redaction применён до вставки; если вставка упадёт, блок
        # уже пуст. Вызывающие (GUI) обязаны сохранить снимок до вызова;
        # библиотечные вызовы без снимка теряют текст при сбое.
        # shortcut: одна строка шире страницы переносится htmlbox-ом при
        # рендере (запрет переноса на ширину страницы дал бы усадку шрифта
        # целиком); апгрейд: пост-проверка ширины спана.
        for i, ln in enumerate(lines):
            if not ln:
                continue
            s = src_style(i)
            x, y = pts[i]
            size = font_size if font_size else s["size"]
            color = s["color"]

            def put(font_name: str) -> bool:
                """Одна строка без переноса; True — вставили insert_text."""
                ref = emb_ref(font_name)
                if ref is not None and _insert_text_fit(
                        page, ref, ln, x, y, size, color):
                    return True
                cjk = _cjk_font(ln) if not ln.isascii() else None
                if cjk is not None:
                    _insert_text_fit(page, cjk, ln, x, y, size, color)
                    return True
                if not ln.isascii():
                    return False  # кириллица/греческий — htmlbox (unicode)
                return _insert_text_fit(page, _base14_key(font_name),
                                        ln, x, y, size, color)

            # Явно выбранный пользователем шрифт — override на весь блок.
            if font_choice and font_choice not in ("auto",):
                if font_choice.startswith("emb:"):
                    ref = emb_ref(font_choice[4:])
                    if ref is not None and _insert_text_fit(
                            page, ref, ln, x, y, size, color):
                        continue
                elif not ln.isascii() and font_choice not in (
                        "china-s", "china-ss", "japan", "korea"):
                    html_line(ln, size, color, x, y)
                    continue
                elif _insert_text_fit(page, _base14_key(font_choice),
                                      ln, x, y, size, color):
                    continue
                html_line(ln, size, color, x, y)
                continue

            # Авто: строка наследует шрифт исходной строки i.
            if put(s["font"]):
                continue
            html_line(ln, size, color, x, y)

    def save_as(self, path: str) -> None:
        if path == self.path:
            self.doc.save(self.path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        else:
            self.doc.save(path)