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
    runs = _consecutive_rows(np.flatnonzero((rows >= 2) & (rows < full)))
    boxes = _line_boxes(runs, dark)
    if not boxes:
        return []
    med_h = float(np.median([y1 - y0 for _x, y0, _x2, y1 in boxes]))
    # Крапинки/артефакты: линия сильно мельче остального текста.
    # shortcut: крупный рисунок (высота > 4×медианы) не отбрасывается —
    # апгрейд при проблемах с колонтитулами-логотипами.
    boxes = [b for b in boxes if b[3] - b[1] >= max(0.35 * med_h, 6)]
    return _group_blocks(sorted(boxes), zoom)


def _consecutive_rows(idx: np.ndarray) -> list[tuple[int, int]]:
    """Индексы рядов → отрезки [start, end), слияние зазора < 3 рядов."""
    idx = [int(i) for i in idx]
    if not idx:
        return []
    out, start, prev = [], idx[0], idx[0]
    for r in idx[1:]:
        if r - prev > 3:
            out.append((start, prev + 1))
            start = r
        prev = r
    out.append((start, prev + 1))
    return out


def _line_boxes(runs: list[tuple[int, int]], dark: np.ndarray
                ) -> list[tuple[int, int, int, int]]:
    """(y0,y1) → (x0,y0,x1,y1): обрезка пустых полей по колонкам."""
    out = []
    for y0, y1 in runs:
        xs = np.flatnonzero(dark[y0:y1].sum(axis=0) >= 1)
        if len(xs) == 0 or xs[-1] + 1 - xs[0] < 8:
            continue
        out.append((int(xs[0]), y0, int(xs[-1]) + 1, y1))
    return out


def _group_blocks(lines: list[tuple[int, int, int, int]], zoom: float
                  ) -> list[OcrBlock]:
    """Строки → абзацы: вертикальный зазор < 0.8 высоты строки И
    горизонтальное перекрытие ≥ 50% меньшей ширины (колонки не сцепляются).
    Координаты переводятся из пикселей зума в пункты PDF."""
    med_h = float(np.median([y1 - y0 for _x, y0, _x2, y1 in lines]))
    groups: list[list[tuple[int, int, int, int]]] = []
    for ln in lines:
        if not groups or not _same_para(groups[-1][-1], ln, med_h):
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
        size = float(np.median([(l[3] - l[1]) / 0.9 for l in g])) * z
        out.append(OcrBlock(rect=rect, text="", size=size, lines=line_rects))
    return out


def _same_para(prev: tuple, ln: tuple, med_h: float) -> bool:
    gap = ln[1] - prev[3]
    overlap = min(prev[2], ln[2]) - max(prev[0], ln[0])
    return (gap < 0.8 * med_h
            and overlap >= 0.5 * min(prev[2] - prev[0], ln[2] - ln[0]))


# --- LLM-клиент ---------------------------------------------------------------

_PROMPT = ("Транскрибируй дословно весь текст с изображения. "
           "Сохраняй переносы строк. Не добавляй пояснений, markdown "
           "или кавычек. Если текста нет, верни пустую строку.")


class OcrClient:
    def __init__(self, cfg: OcrConfig):
        self.cfg = cfg

    def transcribe_crop(self, png: bytes) -> str:
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
        return _clean_text(content)


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

    def one(b: OcrBlock, png: bytes) -> str:
        c = OcrClient(cfg)
        try:
            return c.transcribe_crop(png)
        except OcrError:
            return c.transcribe_crop(png)  # один ретрай на кроп

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(one, b, png) for b, png in zip(blocks, crops)]
        for b, fut in zip(blocks, futures):
            b.text = fut.result()
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
