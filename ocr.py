"""OCR сканов через vision-LLM: детекция абзацев локально (numpy),
транскрипция кропов через OpenAI-совместимый API.

Геометрия принадлежит локальному анализу растра (vision-LLM ненадёжно
отдаёт координаты); LLM только читает текст кропа. Модуль слоя модели:
без Qt, без обращения к бизнес-логике документа.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import pymupdf

from document import OcrBlock

_TIMEOUT = 60  # сек на запрос; кропы небольшие, но Ollama отвечает медленно


@dataclass(frozen=True)
class OcrConfig:
    base_url: str
    model: str
    api_key: str
    font_path: str

    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


def config_from_env(env=None) -> OcrConfig:
    env = env if env is not None else os.environ
    return OcrConfig(
        base_url=env.get("PDF_OCR_BASE_URL", "").rstrip("/"),
        model=env.get("PDF_OCR_MODEL", ""),
        api_key=env.get("PDF_OCR_API_KEY", ""),
        font_path=env.get("PDF_OCR_FONT", ""),
    )


class OcrError(Exception):
    pass


# --- Детекция ----------------------------------------------------------------

def detect_text_blocks(samples: bytes, width: int, height: int,
                       zoom: float) -> list[OcrBlock]:
    """Строки/абзацы растра страницы по проекционным профилям.

    Возвращает блоки в координатах PDF (rect — объединение строк,
    lines — rect каждой строки, size — медианный размер строк).
    """
    img = np.frombuffer(samples, dtype=np.uint8).reshape(height, width, -1)
    dark = img[..., :3].astype(np.float64).mean(axis=2) < 128

    rows = dark.sum(axis=1)
    # Почти сплошные тёмные ряды — линейки/разделители таблиц, не текст.
    full = 0.6 * width
    rule_rows = np.flatnonzero(rows >= full)
    # Низкий порог: высокие ascender-ряды тонкой строки дают всего пару
    # тёмных пикселей. От вертикальных линий рамок защищают: обрезка
    # rule-колонок в _line_boxes, cap высоты и сортировка по Y.
    runs = _consecutive_rows(np.flatnonzero((rows >= 2) & (rows < full)))
    runs = _split_at_valleys(runs, rows)
    boxes = _line_boxes(runs, dark)
    if not boxes:
        return []
    med_h = float(np.median([y1 - y0 for _x, y0, _x2, y1 in boxes]))
    # Крапинки/артефакты — ниже медианной строки; крупные сплошные блоки
    # (штрих-код, рисунки, логотипы) — намного выше текстовой строки.
    # shortcut: широкий заголовок > 3.5× медианы отбросится; апгрейд —
    # классификация по плотности/аспекту.
    boxes = [b for b in boxes
             if max(0.35 * med_h, 6) <= b[3] - b[1] <= 3.5 * med_h]
    # Порядок строк — чтения (сверху вниз), НЕ по x: иначе группировка
    # абзацев идёт вперемешку между колонками и склеивает дальние строки.
    return _group_blocks(sorted(boxes, key=lambda b: (b[1], b[0])),
                         zoom, dark, rule_rows)


def _consecutive_rows(idx: np.ndarray) -> list[tuple[int, int]]:
    """Индексы рядов → отрезки [start, end) по НЕПРЕРЫВНЫМ рядам.

    Склеивать через пустые ряды нельзя: зазор между строками текста на
    zoom=2 бывает 1–3 px, и соседние строки сливались в одну «мега-строку»
    (блоки во всю страницу, перекрытия)."""
    idx = [int(i) for i in idx]
    if not idx:
        return []
    out, start, prev = [], idx[0], idx[0]
    for r in idx[1:]:
        if r != prev + 1:
            out.append((start, prev + 1))
            start = r
        prev = r
    out.append((start, prev + 1))
    return out


def _split_at_valleys(runs: list[tuple[int, int]],
                      rows: np.ndarray) -> list[tuple[int, int]]:
    """Резать «слипшиеся» строки в долине профиля.

    Строки соседних ячеек таблицы стоят на разной высоте: полностью пустых
    рядов между ними нет, и две текстовые строки сливались в одну
    (высота ≈ 2× медианной). Режем только такие пробеги — по минимуму
    профиля в средней трети — не трогая аскендеры/десцендеры одной строки."""
    med = float(np.median([b - a for a, b in runs]))
    out: list[tuple[int, int]] = []
    stack = list(runs)
    while stack:
        y0, y1 = stack.pop(0)
        h = y1 - y0
        if h <= 1.6 * med:
            out.append((y0, y1))
            continue
        prof = rows[y0:y1]
        lo, hi = int(h * 0.3), max(int(h * 0.3) + 1, int(h * 0.7))
        i = lo + int(np.argmin(prof[lo:hi]))
        if prof[i] >= 0.6 * prof.max():
            out.append((y0, y1))
            continue
        stack[0:0] = [(y0, y0 + i + 1), (y0 + i + 1, y1)]
    return sorted(out)


def _line_boxes(runs: list[tuple[int, int]], dark: np.ndarray
                ) -> list[tuple[int, int, int, int]]:
    """(y0,y1) → (x0,y0,x1,y1): обрезка пустых полей и линий рамок.

    Вертикальная линия рамки — тонкая (≤2 px) колонка, тёмная по всей
    высоте строки и отделённая большим зазором от текста. Глифовый штрих
    ('l', 'i') тоже тонкий и высокий, но стоит вплотную к соседнему глифу,
    поэтому по зазору не режется (иначе bbox съезжал на 3pt)."""
    out = []
    for y0, y1 in runs:
        sub = dark[y0:y1]
        h = y1 - y0
        cols = sub.sum(axis=0)
        groups = _ink_groups(cols >= 1)
        if not groups:
            continue
        while len(groups) > 1 and _is_rule_group(
                groups[0], cols, h) and groups[1][0] - groups[0][1] >= 0.3 * h:
            groups.pop(0)
        while len(groups) > 1 and _is_rule_group(
                groups[-1], cols, h) and groups[-1][0] - groups[-2][1] >= 0.3 * h:
            groups.pop()
        x0, x1 = groups[0][0], groups[-1][1]
        if x1 - x0 < 8:
            continue
        out.append((int(x0), y0, int(x1), y1))
    return out


def _ink_groups(mask: np.ndarray) -> list[tuple[int, int]]:
    """Непрерывные группы колонок с чернилами → [(start, end), ...]."""
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def _is_rule_group(group: tuple[int, int], cols: np.ndarray,
                   h: int) -> bool:
    a, b = group
    return b - a <= 2 and bool(np.all(cols[a:b] >= 0.9 * h))


def _group_blocks(lines: list[tuple[int, int, int, int]], zoom: float,
                  dark: np.ndarray, rule_rows: np.ndarray) -> list[OcrBlock]:
    """Строки → абзацы: вертикальный зазор < 0.8 высоты строки И
    горизонтальное перекрытие ≥ 50% меньшей ширины (колонки не сцепляются),
    и между строками нет горизонтальной линейки (строки таблицы — отдельные
    блоки, а не гигантский абзац на всю таблицу).
    Координаты переводятся из пикселей зума в пункты PDF."""
    med_h = float(np.median([y1 - y0 for _x, y0, _x2, y1 in lines]))
    groups: list[list[tuple[int, int, int, int]]] = []
    for ln in lines:
        if not groups or not _same_para(groups[-1][-1], ln, med_h,
                                        rule_rows):
            groups.append([ln])
        else:
            groups[-1].append(ln)
    z = 1.0 / zoom
    out = []
    for g in groups:
        rect = pymupdf.Rect(min(l[0] for l in g) * z, min(l[1] for l in g) * z,
                            max(l[2] for l in g) * z, max(l[3] for l in g) * z)
        line_rects = [pymupdf.Rect(l[0] * z, l[1] * z, l[2] * z, l[3] * z)
                      for l in g]
        ink_px = float(np.median([(l[3] - l[1]) / 0.9 for l in g]))
        # Размер по межстрочному шагу (leading ~1.2): по высоте чернил он
        # занижен (x-height), и MuPDF из-за этого дробит абзац на строки.
        pitches = [g[i + 1][1] - g[i][1] for i in range(len(g) - 1)]
        pitch_px = float(np.median(pitches)) if pitches else 0.0
        size = max(pitch_px / 1.2, ink_px) * z
        out.append(OcrBlock(rect=rect, text="", size=size, lines=line_rects,
                            style=_estimate_style(dark, g)))
    return out


def _estimate_style(dark: np.ndarray,
                    lines: list[tuple[int, int, int, int]]) -> dict:
    """Начертание по растру (страховка, когда LLM не отдал теги):
    bold — медианная толщина штриха относительно высоты строки;
    italic — сдвиг центроида чернил от верха к низу строки."""
    bold_votes = italic_votes = 0
    for x0, y0, x1, y1 in lines:
        h = y1 - y0
        if h < 6:
            continue
        seg = dark[y0:y1, x0:x1]
        strokes = []
        for row in seg:
            edges = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
            starts = np.flatnonzero(edges == 1)
            ends = np.flatnonzero(edges == -1)
            strokes.extend((ends - starts).tolist())
        if strokes:
            bold_votes += np.median(strokes) / h > 0.13
        rr, cc = seg.nonzero()
        if len(rr) >= 8:
            top = cc[rr < h * 0.25]
            bottom = cc[rr > h * 0.75]
            if len(top) and len(bottom) and bottom.mean() - top.mean() \
                    > 0.18 * h:
                italic_votes += 1
    half = max(len(lines) / 2, 1)
    style = {}
    if bold_votes >= half:
        style["bold"] = True
    if italic_votes >= half:
        style["italic"] = True
    return style


def _same_para(prev: tuple, ln: tuple, med_h: float,
               rule_rows: np.ndarray) -> bool:
    gap = ln[1] - prev[3]
    overlap = min(prev[2], ln[2]) - max(prev[0], ln[0])
    if np.any((rule_rows >= prev[3]) & (rule_rows < ln[1])):
        return False  # между строками линейка — не абзац
    return (gap < 0.8 * med_h
            and overlap >= 0.5 * min(prev[2] - prev[0], ln[2] - ln[0]))


# --- LLM-клиент ---------------------------------------------------------------

_PROMPT = (
    "Транскрибируй дословно текст с изображения, сохраняя переносы строк. "
    "Верни строго JSON без пояснений и markdown: "
    '{"text": "<текст>", "bold": true|false, "italic": true|false, '
    '"serif": true|false, "mono": true|false} — теги начертания по виду '
    "шрифта на изображении. Если текста нет, text — пустая строка.")


class OcrClient:
    def __init__(self, cfg: OcrConfig):
        self.cfg = cfg

    def transcribe_crop(self, png: bytes) -> tuple[str, dict]:
        """Кроп → (текст, стиль). Стиль — только явные теги из JSON;
        отсутствующие заполняет эвристика детекции."""
        payload = {
            "model": self.cfg.model,
            "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,"
                                         + base64.b64encode(png).decode()}},
            ]}],
        }
        req = urllib.request.Request(
            self.cfg.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.cfg.api_key})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            raise OcrError(f"HTTP {e.code}: {_body_text(e)}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OcrError(f"нет ответа API: {e}") from None
        except json.JSONDecodeError as e:
            raise OcrError(f"ответ не JSON: {e}") from None
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise OcrError(f"неожиданный ответ API: {body}") from None
        return _parse_reply(content)


_STYLE_KEYS = ("bold", "italic", "serif", "mono")


def _parse_reply(content: str) -> tuple[str, dict]:
    """Ответ модели → (текст, теги стиля). Не-JSON ответ — просто текст
    (модель проигнорировала формат), стиль останется на эвристике."""
    raw = _clean_text(content)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw, {}
    if not isinstance(obj, dict) or not isinstance(obj.get("text"), str):
        return raw, {}
    style = {k: bool(obj[k]) for k in _STYLE_KEYS if k in obj}
    return obj["text"].strip(), style


def _body_text(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:300]
    except Exception:
        return ""


def _clean_text(content: str) -> str:
    """Ответ модели → текст: снимаем ```-фенсы и крайние whitespace."""
    text = content.strip()
    m = re.fullmatch(r"```[a-z]*\s*(.*?)\s*```", text, re.S)
    if m:
        text = m.group(1).strip()
    return text


# --- Оркестратор страницы ------------------------------------------------------

def run_page_ocr(cfg: OcrConfig, pixmap: pymupdf.Pixmap, zoom: float,
                 max_workers: int = 4) -> list[OcrBlock]:
    """Страница → блоки с текстом. Любая ошибка транскрипции поднимается
    наверх (политика «прервать всё»: вызывающий ничего не вставляет)."""
    blocks = detect_text_blocks(pixmap.samples, pixmap.width, pixmap.height,
                                zoom)
    if not blocks:
        return []
    crops = [_crop_png(pixmap, b, zoom) for b in blocks]

    def one(b: OcrBlock, png: bytes) -> tuple[str, dict]:
        c = OcrClient(cfg)
        try:
            return c.transcribe_crop(png)
        except OcrError:
            return c.transcribe_crop(png)  # один ретрай на кроп

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(one, b, png) for b, png in zip(blocks, crops)]
        for b, fut in zip(blocks, futures):
            text, style = fut.result()
            # Явные теги LLM перекрывают эвристику; недостающие — из неё.
            b.text, b.style = text, {**b.style, **style}
    return [b for b in blocks if b.text]


def _crop_png(pixmap: pymupdf.Pixmap, block: OcrBlock,
              zoom: float) -> bytes:
    """Кроп блока из растра страницы → PNG."""
    img = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, -1)[..., :3]
    x0 = max(int(block.rect.x0 * zoom), 0)
    y0 = max(int(block.rect.y0 * zoom), 0)
    x1 = min(int(np.ceil(block.rect.x1 * zoom)), pixmap.width)
    y1 = min(int(np.ceil(block.rect.y1 * zoom)), pixmap.height)
    sub = np.ascontiguousarray(img[y0:y1, x0:x1])
    pm = pymupdf.Pixmap(pymupdf.csRGB, sub.shape[1], sub.shape[0],
                        sub.tobytes(), 0)
    return pm.tobytes("png")
