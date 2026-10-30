"""Модель PDF-редактора: рендер, блоки текста, правка блоков. Без GUI."""
import difflib
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
    # Спаны по строкам: [[{font, size, color, origin, text}, ...], ...] —
    # единственный источник стиля (в том числе смешения внутри строки).
    # None = не извлекалось (TextBlock создан вручную).
    line_runs: list[list[dict]] | None = None

    @property
    def line_styles(self) -> list[dict]:
        """Стиль каждой строки — первый спан (вычисляется, не хранится)."""
        out: list[dict] = []
        for runs in self.line_runs or []:
            if runs:
                out.append({k: runs[0][k] for k in
                            ("font", "size", "color", "origin")})
            else:  # пустая строка наследует стиль предыдущей
                out.append(dict(out[-1]) if out else {})
        return out


def _clean_font_name(name: str) -> str:
    return name.split("+")[-1].strip()


@dataclass
class OcrBlock:
    """Блок, найденный детекцией на растре страницы OCR (ocr.py).
    rect/lines — координаты PDF; lines — rect каждой строки абзаца;
    text заполняется после транскрипции."""
    rect: pymupdf.Rect
    text: str
    size: float
    lines: list[pymupdf.Rect]


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


def _map_runs(old_runs: list[dict], new_text: str, fallback: dict) -> list[dict]:
    """Прогоны для нового текста строки: посимвольно по difflib, совпавшие
    символы берут стиль исходного символа, вставленные/заменённые — стиль
    ближайшего предыдущего (в начале строки — первого спана). Соседние
    символы одного стиля склеиваются в один run."""
    src = old_runs or [dict(fallback, text=new_text)]
    chars = [r for r in src for _ in r["text"]]
    if not chars:
        one = src[0]
        return [{"text": new_text, "font": one["font"], "size": one["size"],
                 "color": one["color"]}]
    sm = difflib.SequenceMatcher(None, "".join(r["text"] for r in src),
                                 new_text, autojunk=False)
    styles = [None] * len(new_text)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            styles[j1:j2] = chars[i1:i2]
        else:
            prev = chars[i1 - 1] if i1 else chars[0]
            styles[j1:j2] = [prev] * (j2 - j1)
    runs: list[dict] = []
    for ch, s in zip(new_text, styles):
        if runs and (runs[-1]["font"], runs[-1]["size"], runs[-1]["color"]) == (
                s["font"], s["size"], s["color"]):
            runs[-1]["text"] += ch
        else:
            runs.append({"text": ch, "font": s["font"], "size": s["size"],
                         "color": s["color"]})
    return runs


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
            line_runs = []
            for line in b["lines"]:
                runs = []
                for s in line["spans"]:
                    c = s["color"]
                    runs.append({
                        "font": s["font"],
                        "size": s["size"],
                        "color": ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255,
                                  (c & 255) / 255),
                        "origin": tuple(s["origin"]),
                        # shortcut: TTF-буферы с LATIN-кодировкой извлекают
                        # пробел как \xa0 (артефакт MuPDF); нормализуем, иначе
                        # \xa0 попадает в редактор и в следующие вставки.
                        "text": s["text"].replace("\xa0", " "),
                    })
                line_runs.append(runs)
            text = "\n".join("".join(r["text"] for r in runs)
                             for runs in line_runs)
            first = next((r for runs in line_runs for r in runs), None)
            if not text or first is None:
                continue
            out.append(TextBlock(
                rect=pymupdf.Rect(b["bbox"]),
                text=text,
                font=first["font"],
                size=first["size"],
                color=first["color"],
                line_runs=line_runs,
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

        # Явно выбранный пользователем шрифт/размер — override на весь блок.
        override_font = None
        if font_choice and font_choice not in ("auto",):
            override_font = (font_choice[4:] if font_choice.startswith("emb:")
                             else font_choice)
        old_runs = block.line_runs or []

        font_memo: dict[str, str | None] = {}
        emb_bufs: dict[str, bytes] = {}
        font_objs: dict[str, pymupdf.Font] = {}

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
                    # Префиксный матч: имя спана короче /BaseFont ("CharisSIL"
                    # vs "Charis SIL Regular"), но начертание должно совпадать
                    # — иначе "CharisSIL" берёт буфер "Charis SIL Bold".
                    def face(s: str) -> tuple:
                        return ("bold" in s,
                                "italic" in s or "oblique" in s)
                    for k, b in font_buffers.items():
                        if (k.startswith(norm) or norm.startswith(k)) \
                                and face(k) == face(norm):
                            buf = b
                            break
                ref = None
                if buf:
                    ref = "EdF" + "".join(ch for ch in clean if ch.isalnum())[:32]
                    page.insert_font(fontname=ref, fontbuffer=buf)
                    emb_bufs[ref] = buf
                font_memo[key] = ref
            return font_memo[key]

        def plan_run(font_name: str, text: str) -> tuple[str, str | None]:
            """Как вставлять прогон: emb/cjk/base — insert_text, html —
            htmlbox (unicode без встроенного шрифта)."""
            name = override_font or font_name
            if name in ("china-s", "china-ss", "japan", "korea"):
                return "cjk", name  # явный CJK-выбор пользователя
            ref = emb_ref(name)
            if ref is not None:
                return "emb", ref
            if not text.isascii():
                cjk = _cjk_font(text)
                return ("cjk", cjk) if cjk else ("html", None)
            return "base", _base14_key(name)

        def run_width(kind: str, refname: str, text: str, size: float) -> float:
            """Ширина прогона тем же шрифтом, которым он вставлен."""
            if kind == "base":
                return pymupdf.get_text_length(text, fontname=refname,
                                               fontsize=size)
            if refname not in font_objs:
                buf = emb_bufs.get(refname)
                font_objs[refname] = (pymupdf.Font(fontbuffer=buf) if buf
                                      else pymupdf.Font(refname))
            return font_objs[refname].text_length(text, fontsize=size)

        def html_line(runs: list[dict], y: float) -> None:
            """Строка одним htmlbox: спаны с size/color/bold/italic — Story
            раскладывает их по x сам (смешение начертаний внутри строки)."""
            parts = []
            for r in runs:
                r_, g, b = r["color"]
                f = _clean_font_name(r["font"]).lower()
                extra = ("font-weight:bold;" if "bold" in f else
                         "font-style:italic;" if ("italic" in f or "oblique" in f)
                         else "")
                body = _html.escape(r["text"], quote=False)
                parts.append(
                    f'<span style="font-size:{r["size"]:.1f}pt;'
                    f'color:rgb({r_*255:.0f},{g*255:.0f},{b*255:.0f});{extra}">'
                    f'{body}</span>')
            # white-space:pre — иначе Story схлопывает/съедает пробелы и
            # переносит строку по ширине бокса.
            html = '<span style="white-space:pre">' + "".join(parts) + "</span>"
            # Story кладёт глифы со своим внутренним отступом от бокса,
            # зависящим только от size: измеряем его на отдельном документе
            # (с кэшем) и входим в baseline (x, y) с первого раза. Отступ
            # задаёт САМЫЙ крупный span строки (проверено: baseline смешанной
            # строки = _story_inset(max size)). Redact-перестановка (как
            # раньше) удаляла соседние строки: MuPDF убирает любой текст, чей
            # bbox пересекает красакт-прямоугольник.
            size = max(r["size"] for r in runs)
            ix, iy = _story_inset(size)
            x = runs[0]["x"]
            # Бокс не обрезаем по краю страницы: иначе Story усаживает
            # строку (scale_low) вместо роста вправо. Точную ширину строки
            # заранее не знаем (метрики Story-гарнитуры), поэтому при
            # неудаче расширяем бокс и пробуем снова. scale_low=0
            # (беззвучная усадка) — только последний рубеж.
            box = pymupdf.Rect(x - ix, y - iy, page.rect.x1, y - iy + size * 2)
            for _ in range(4):
                rc = page.insert_htmlbox(box, html, scale_low=1.0)
                if not (isinstance(rc, tuple) and rc and rc[0] < 0):
                    break
                box.x1 += 200  # вправо за край страницы — не перенос, а рост
                box.y1 = min(box.y1 + 20, page.rect.y1)
            else:
                page.insert_htmlbox(box, html, scale_low=0.0)

        # shortcut: redaction применён до вставки; если вставка упадёт, блок
        # уже пуст. Вызывающие (GUI) обязаны сохранить снимок до вызова;
        # библиотечные вызовы без снимка теряют текст при сбое.
        # shortcut: переноса по ширине нет ни на одном пути (только "\\n").
        # htmlbox-строку шире страницы Story не переносит, а усаживает
        # (scale_low); на insert_text-пути строка уходит за правый край.
        # Апгрейд: пост-проверка ширины строки.
        for i, ln in enumerate(lines):
            if not ln:
                continue
            s = src_style(i)
            x, y = pts[i]
            orig = old_runs[i] if i < len(old_runs) else []
            if (orig and not override_font and not font_size
                    and ln == "".join(r["text"] for r in orig)):
                # Текст строки не изменился: спаны на своих исходных x —
                # правка идемпотентна бит-в-бит (метрики не участвуют).
                runs = [dict(r, x=r["origin"][0] + dx) for r in orig]
            else:
                runs = _map_runs(orig, ln, s)

            plans = [(r, plan_run(r["font"], r["text"])) for r in runs]
            explicit = "x" in runs[0]
            if not explicit:
                runs[0]["x"] = x
            if any(kind == "html" for _r, (kind, _n) in plans):
                html_line(runs, y)
                continue
            for r, (kind, refname) in plans:
                size = font_size or r["size"]
                if not explicit:
                    r["x"] = x
                # insert_text не смог (низ страницы) — строка целиком через
                # htmlbox, без потери текста.
                if not _insert_text_fit(page, refname, r["text"], r["x"], y,
                                        size, r["color"]):
                    html_line([r], y)
                elif not explicit:
                    x = r["x"] + run_width(kind, refname, r["text"], size)

    def save_as(self, path: str) -> None:
        if path == self.path:
            try:
                self.doc.save(self.path, incremental=True,
                              encryption=pymupdf.PDF_ENCRYPT_KEEP)
            except Exception:
                # Инкрементальная запись требует исходный файл: после undo
                # документ открыт из памяти — сохраняем целиком.
                self.doc.save(path)
        else:
            self.doc.save(path)
        self.path = path

    def apply_ocr(self, idx: int, blocks: list[OcrBlock],
                  font_path: str | None = None) -> None:
        """Невидимый текстовый слой по блокам OCR.

        Блоки вставляются через render_mode=3 (текст есть для поиска/копи
        -рования, не виден поверх растра). Baseline: если строк LLM
        столько же, сколько строк детекции — по rect'ам строк; иначе
        равномерно внутри bbox абзаца.
        """
        page = self.doc[idx]
        for b in blocks:
            ln_texts = [ln for ln in b.text.split("\n") if ln]
            if not ln_texts:
                continue
            font_name = self._ocr_font(page, "".join(b.text),
                                       font_path=font_path)
            if len(ln_texts) == len(b.lines):
                baselines = [r.y1 - 0.2 * b.size for r in b.lines]
            elif len(ln_texts) == 1:
                baselines = [b.rect.y1 - 0.2 * b.size]
            else:
                y_first = b.rect.y0 + 0.75 * b.size
                y_last = b.rect.y1 - 0.2 * b.size
                step = (y_last - y_first) / (len(ln_texts) - 1)
                baselines = [y_first + i * step
                             for i in range(len(ln_texts))]
            for y, ln in zip(baselines, ln_texts):
                page.insert_text((b.rect.x0, y), ln, fontname=font_name,
                                 fontsize=b.size, render_mode=3)

    def _ocr_font(self, page, text: str,
                  font_path: str | None = None) -> str:
        """refname шрифта невидимого слоя по скрипту текста.

        htmlbox (кириллица не влезает в Base-14) здесь не вариант: он
        вставляется видимым. Не-ASCII скрипты — системный TTF со шрифа
        -товой поддержкой (буфер), CJK — reserved имена, иначе helv."""
        if any("\u0400" <= c <= "\u04FF" or "\u0370" <= c <= "\u03FF"
               for c in text):
            buf = _cyr_font_buffer(font_path)
            page.insert_font(fontname="OcrFSil", fontbuffer=buf)
            return "OcrFSil"
        cjk = _cjk_font(text)
        return cjk if cjk else "helv"


# Порядок кандидатов: Linux → Windows → macOS. Все — регулярные начертания
# со встроенной кириллицей.
_CYR_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
    "/usr/share/fonts/carlito/Carlito-Regular.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

_cyr_buf_cache: dict[str, bytes | None] = {}


def _cyr_font_buffer(font_path: str | None) -> bytes:
    """Буфер TTF с кириллицей: PDF_OCR_FONT/font_path → известные пути.
    Кэшируется: буфер читается один раз."""
    key = font_path or ""
    if key in _cyr_buf_cache:
        buf = _cyr_buf_cache[key]
    else:
        buf = None
        for cand in ([font_path] if font_path else
                     list(_CYR_FONT_CANDIDATES)):
            try:
                buf = open(cand, "rb").read()
                break
            except OSError:
                continue
        _cyr_buf_cache[key] = buf
    if not buf:
        raise ValueError(
            "Не найден системный TTF с кириллицей для невидимого "
            "текстового слоя. Укажите шрифт через PDF_OCR_FONT "
            "или меню OCR → Настройки.")
    return buf