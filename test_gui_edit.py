"""GUI-смоки правки на канве. Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python test_gui_edit.py"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox

import pymupdf

app = QApplication(sys.argv)

from window import BlockItem, MainWindow

# Пример PDF создаём сами: в репозитории нет тестовых файлов (*.pdf в gitignore).
SAMPLE = os.path.join(tempfile.mkdtemp(), "sample.pdf")
_doc = pymupdf.open()
_page = _doc.new_page()
_page.insert_textbox(pymupdf.Rect(72, 72, 400, 120), "Hello world test",
                     fontname="helv", fontsize=12)
_page.insert_htmlbox(
    pymupdf.Rect(72, 200, 400, 260),
    '<span style="font-size:12pt"><b>Bold start</b> regular tail here</span>')
_page.insert_textbox(pymupdf.Rect(72, 320, 400, 380), "Second block for move",
                     fontname="helv", fontsize=12)
_doc.save(SAMPLE)
_doc.close()


def is_bold(name: str) -> bool:
    return "bold" in name.lower()


WIN = MainWindow(pdf_path=SAMPLE)
WIN.resize(1280, 860)
WIN.show()

failures = []


def check(name, cond):
    print(("OK " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)


def find_block(substr):
    for it in WIN.scene.items():
        if isinstance(it, BlockItem) and substr in WIN.blocks[it.data(0)].text:
            return it
    return None


def item_text(item: BlockItem) -> str:
    return WIN.blocks[item.data(0)].text


# --- 1. открытие файла, выбор страницы
WIN.thumbs.setCurrentRow(0)
check("1. страница открыта", WIN.page_idx == 0 and WIN.blocks and WIN.top.isVisible())
check("1. без выбора блока параметры недоступны",
      not WIN.font_combo.isEnabled() and not WIN.size_spin.isEnabled()
      and not WIN.apply_btn.isEnabled())
geo_before = WIN.view.geometry()

# --- 2. двойной клик -> оверлей
item = find_block("")
check("2. есть блоки на странице", item is not None)
orig_idx = item.data(0)
orig = WIN.blocks[orig_idx]
t0 = orig.text
WIN.start_edit(item)
ov = WIN.overlay
check("2. оверлей создан", ov is not None and WIN.overlay_bg is not None)
check("2. текст оверлея = block.text",
      ov is not None and ov.toPlainText().replace("\u2029", "\n") == t0)
pos_ok = abs(ov.pos().x() - orig.rect.x0 * 2.0) < 1 and abs(ov.pos().y() - orig.rect.y0 * 2.0) < 1
check("2. оверлей на месте блока", pos_ok)
check("2. фокус в оверлее", WIN.scene.focusItem() is ov)
# Кегль оверлея = кегль блока в px сцены (сцена × VIEW_ZOOM): иначе
# редактор рисует текст мельче/крупнее страницы.
fpx = ov.font().pixelSize()
check("2. кегль оверлея = кеглю блока",
      fpx == max(1, round(orig.size * 2.0)))

# --- 2b. одиночный клик — панель появляется при выделении
sel_item = find_block(t0.split("\n")[0][:10])
sprinkled = sel_item.data(0)
sel_item.setSelected(True)
check("2b. одиночный клик выбирает блок",
      WIN.selected == sprinkled and WIN.top.isVisible())
check("2b. параметры стали доступны",
      WIN.font_combo.isEnabled() and WIN.size_spin.isEnabled()
      and WIN.apply_btn.isEnabled())
check("2b. выбор блока не сдвигает страницу", WIN.view.geometry() == geo_before)

# --- 3. правка с явным "\n" + Применить
ov.setPlainText(t0 + "\nНовая строка проверки")
raw = ov.toPlainText()
print(f"(эмпирика) raw toPlainText после '\\n': {raw[-10:]!r}")
check("3. явный '\\n' дошёл до модели текстом '\\n'",
      WIN._overlay_text() == t0 + "\nНовая строка проверки")
WIN.font_combo.setCurrentIndex(0)  # Авто
WIN.size_auto.setChecked(True)
WIN.apply_edit()
got = {i: b for i, b in enumerate(WIN.blocks) if "Новая строка проверки" in b.text}
check("3. новая строка применена и есть на странице", bool(got))
check("3. оверлей закрыт", WIN.overlay is None)
new_idx = next(iter(got))
check("3. переносы '\\n' сохранились", len(got[new_idx].text.split("\n")) == len(t0.split("\n")) + 1)
new_sizes = [ls["size"] for ls in got[new_idx].line_styles]
orig_sizes = [ls["size"] for ls in orig.line_styles]
check("3. Авто: построчные размеры исходных строк сохранены",
      all(abs(s - o) < 0.3 for s, o in zip(new_sizes, orig_sizes)))
applied = got[new_idx]

# --- 5. undo возвращает старый текст
check("5. undo-снимок есть", bool(WIN._undo_stack))
WIN.undo()
check("5. старый текст вернулся", "Новая строка проверки" not in WIN.doc.blocks(0) and
      not any("Новая строка проверки" in b.text for b in WIN.blocks))

# --- 4. Esc/Отмена не применяет правки
item = find_block(t0[:15])
WIN.start_edit(item)
lv = WIN.overlay
check("4. панель шрифта видима при правке", WIN.top.isVisible())
lv.setPlainText("ZZZ ESC TEST")
WIN.cancel_edit()
WIN.undo()  # убрать возможный висящий снимок 5-го пункта не нужен: правки не было
check("4. после Отмена/Esc текст не изменился",
      not any("ZZZ ESC TEST" in b.text for b in WIN.blocks))

# --- 6. перетаскивание рамки + Применить -> текст в новом месте
item = find_block(t0[:15])
idx = item.data(0)
b0 = WIN.blocks[idx]
item.setSelected(True)
dx, dy = 30, 20  # в сценных координатах (= 15,10 pt)
item.moveBy(dx, dy)
WIN.start_edit(item)  # start_edit не сбрасывает геометрию
check("6. правка после перемещения по-прежнему работает", WIN.overlay is not None)
WIN.apply_edit()
moved = [b for b in WIN.blocks if b.text == b0.text]
check("6. блок найден на новом месте",
      bool(moved) and abs(moved[0].rect.x0 - (b0.rect.x0 + dx / 2.0)) < 2.0
      and abs(moved[0].rect.y0 - (b0.rect.y0 + dy / 2.0)) < 8.0)

# --- 7a. размер override: size_spin работает (блок с шага 6, текст t0)
item = find_block(t0[:15])
t_7a = item_text(item)
check("7a. единственный блок с текстом t0",
      sum(1 for b in WIN.blocks if b.text == t_7a) == 1)
WIN.start_edit(item)
WIN.size_auto.setChecked(False)
WIN.size_spin.setValue(8.0)
WIN.apply_edit()
i2 = next(i for i, b in enumerate(WIN.blocks) if b.text == t_7a)
sizes = [ls["size"] for ls in WIN.blocks[i2].line_styles]
check("7a. размер override 8pt применился ко всем строкам",
      sizes and all(abs(s - 8.0) < 0.6 for s in sizes))

# --- 7b. Авто: правка тем же текстом сохраняет текст/размеры
item = find_block(t0[:15])
idx = item.data(0)
before_t = WIN.blocks[idx].text
before_sizes = [ls["size"] for ls in WIN.blocks[idx].line_styles]
WIN.start_edit(item)
WIN.size_auto.setChecked(True)
WIN.font_combo.setCurrentIndex(0)
WIN.apply_edit()
i3 = next(i for i, b in enumerate(WIN.blocks) if b.text == before_t)
after_sizes = [ls["size"] for ls in WIN.blocks[i3].line_styles]
check("7b. Авто roundtrip: текст не изменился", WIN.blocks[i3].text == before_t)
check("7b. Авто roundtrip: размеры стабильны",
      len(before_sizes) == len(after_sizes)
      and all(abs(a - b) < 0.3 for a, b in zip(before_sizes, after_sizes)))

# --- 4b. клик мимо блоков закрывает редактор без изменений
item = find_block(WIN.blocks[0].text[:10])
WIN.start_edit(item)
WIN.overlay.setPlainText("QQQ MISS")
item.setSelected(False)
check("4b. клик мимо блоков закрывает редактор", WIN.overlay is None)
check("4b. текст не изменился", not any("QQQ MISS" in b.text for b in WIN.blocks))

# --- 8. оверлей растёт по ширине и не переносит строку сам
item = find_block(WIN.blocks[0].text[:10])
WIN.start_edit(item)
ov = WIN.overlay
w1, h1 = ov.boundingRect().width(), ov.boundingRect().height()
ov.setPlainText(ov.toPlainText().split("\n")[0] + " " + "LONGER TEXT " * 6)
w2, h2 = ov.boundingRect().width(), ov.boundingRect().height()
check("8. оверлей вырос по ширине", w2 > w1 * 1.4)
check("8. переноса нет (высота не выросла)", h2 <= h1 + 0.5)
check("8. подложка закрывает текст", WIN.overlay_bg.rect().width() >= w2 - 1)
WIN.cancel_edit()
check("8. отмена убирает оверлей", WIN.overlay is None)

# --- 9. блок со смешанными начертаниями не становится целиком жирным
mixed = next(i for i, b in enumerate(WIN.blocks) if b.text.startswith("Bold start"))
item = next(it for it in WIN.scene.items() if isinstance(it, BlockItem) and it.data(0) == mixed)
item.setSelected(True)
WIN.on_block_selected()
check("9. шрифт = Авто (нет override на весь блок)",
      WIN.font_combo.currentData() == "auto")
WIN.start_edit(item)
WIN.overlay.setPlainText(
    WIN.overlay.toPlainText().replace("regular", "regular (kept)"))
WIN.apply_edit()
b9 = next(x for x in WIN.blocks if x.text.startswith("Bold start"))
runs9 = [r for line in b9.line_runs for r in line]
check("9. начертания сохранены",
      len(runs9) >= 2 and is_bold(runs9[0]["font"])
      and not is_bold(runs9[1]["font"]))
check("9. размеры сохранены",
      all(abs(r["size"] - 12.0) < 0.6 for r in runs9))

# --- 10. перемещение мышью фиксируется сразу, текст едет вместе с рамкой
WIN.open_pdf(SAMPLE)  # чистый документ: порядок блоков стабилен
WIN.thumbs.setCurrentRow(0)
marker = "Second block for move"
idx = next(i for i, b in enumerate(WIN.blocks) if b.text.startswith(marker))
old = WIN.blocks[idx].rect
item = next(it for it in WIN.scene.items()
            if isinstance(it, BlockItem) and it.data(0) == idx)
item.moveBy(60, 40)  # перетаскивание рамки
WIN.commit_move(idx, WIN.scene_rect(item))  # то же, что делает mouseReleaseEvent
moved = [b for b in WIN.blocks if b.text.startswith(marker)]
check("10. перемещение применено без кнопки",
      len(moved) == 1 and abs(moved[0].rect.x0 - old.x0) > 3
      and abs(moved[0].rect.y0 - old.y0) > 3)
check("10. текст на новом месте",
      any(abs(wd[0] - moved[0].rect.x0) < 3
          for wd in WIN.doc.doc[0].get_text("words") if wd[4] == "Second"))
check("10. шрифт сохранён",
      not is_bold([r["font"] for runs in moved[0].line_runs for r in runs][0]))
WIN.undo()
WIN.select_page(0)
back = [b for b in WIN.blocks if b.text.startswith(marker)][0]
check("10. undo возвращает позицию", abs(back.rect.x0 - old.x0) < 1)

# --- 11. сбой сохранения перехвачен; успешное снимает флаг «грязный»
errors = []
_saved_info = QMessageBox.information
QMessageBox.critical = staticmethod(lambda *a, **k: errors.append(a[2] if len(a) > 2 else ""))
QMessageBox.information = staticmethod(lambda *a, **k: None)
QFileDialog.getSaveFileName = staticmethod(
    lambda *a, **k: ("/nonexistent-xyz/x.pdf", ""))
WIN.prompt_save()
check("11. сбой сохранения перехвачен (приложение живо)", bool(errors))
saved = os.path.join(os.path.dirname(SAMPLE), "saved.pdf")
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (saved, ""))
WIN.prompt_save()
check("11. файл сохранён", os.path.exists(saved))
check("11. флаг «грязный» снят", WIN._dirty is False)

# --- 12. «Применить» без редактора меняет параметры выбранного блока
idx = next(i for i, b in enumerate(WIN.blocks) if b.text.startswith("Second block"))
item = next(it for it in WIN.scene.items()
            if isinstance(it, BlockItem) and it.data(0) == idx)
item.setSelected(True)
WIN.on_block_selected()
WIN.size_auto.setChecked(False)
WIN.size_spin.setValue(20.0)
WIN.apply_edit()
b12 = next(x for x in WIN.blocks if x.text.startswith("Second block"))
sizes12 = [round(r["size"], 1) for line in b12.line_runs for r in line]
check("12. размер применён без редактора", sizes12 and all(s == 20.0 for s in sizes12))
check("12. правка помечена как несохранённая", WIN._dirty is True)
QMessageBox.information = _saved_info

# --- 13. печать: парсер диапазонов и постраничная печать в PDF-принтер
from window import parse_page_ranges
from PyQt6.QtPrintSupport import QPrinter

check("13. парсер 1,3,5-7", parse_page_ranges("1,3,5-7", 10) == [0, 2, 4, 5, 6])
check("13. парсер пусто = весь документ", parse_page_ranges("", 3) == [0, 1, 2])
check("13. парсер перевёрнутый диапазон (возрастание)", parse_page_ranges("5-3", 10) == [2, 3, 4])
check("13. парсер пробелы", parse_page_ranges(" 2 , 4-5 ", 10) == [1, 3, 4])
try:
    parse_page_ranges("0", 10); check("13. парсер 0 = ошибка", False)
except ValueError:
    check("13. парсер 0 = ошибка", True)
try:
    parse_page_ranges("11", 10); check("13. парсер за пределом = ошибка", False)
except ValueError:
    check("13. парсер за пределом = ошибка", True)
try:
    parse_page_ranges("abc", 10); check("13. парсер мусор = ошибка", False)
except ValueError:
    check("13. парсер мусор = ошибка", True)

out_pdf = os.path.join(os.path.dirname(SAMPLE), "printed.pdf")
printer = QPrinter(QPrinter.PrinterMode.HighResolution)
printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
printer.setOutputFileName(out_pdf)
WIN.print_pages_to(printer, [0, 0])  # две страницы — одна исходная дважды
printed = pymupdf.open(out_pdf)
check("13. напечатано 2 страницы", len(printed) == 2)
check("13. страница — отрисованный растр",
      all(len(printed[i].get_images()) == 1
          and any(px < 200 for px in printed[i].get_pixmap().samples)
          for i in (0, 1)))

# --- 15. OCR: диалог настроек и запуск с fake-провайдером
import ocr
import window as _window
from window import OcrSettingsDialog

cfg15 = ocr.OcrConfig("http://fake", "fake-model", "fake-key", "")
dlg15 = OcrSettingsDialog(cfg15)
check("15. диалог настроек заполнен из конфига", dlg15.config() == cfg15)


class _FakeOcr:
    def __init__(self, cfg):
        self.cfg = cfg

    def transcribe_crop(self, png: bytes) -> tuple[str, dict]:
        return "OCR fake text", {}


ocr.OcrClient = _FakeOcr
_orig_load = _window.load_ocr_config
_window.load_ocr_config = lambda: cfg15
_orig_question, _orig_info, _orig_crit = (
    QMessageBox.question, QMessageBox.information, QMessageBox.critical)
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Yes)
QMessageBox.information = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.critical = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Ok)

WIN.thumbs.setCurrentRow(0)
WIN.run_ocr([0])
w15 = WIN.ocr_worker
check("15. OCR-воркер запущен, действия disabled",
      w15 is not None and not WIN.act_ocr_page.isEnabled())
w15.wait()
QApplication.processEvents()
check("15. OCR завершён, воркер освобождён", WIN.ocr_worker is None)
check("15. блоки OCR вставлены",
      any("OCR fake text" in b.text for b in WIN.blocks))
check("15. страница помечена как изменённая", WIN._dirty is True)
check("15. действия OCR включены снова", WIN.act_ocr_page.isEnabled())
WIN.undo()
check("15. undo убирает OCR-слой",
      not any("OCR fake text" in b.text for b in WIN.blocks))

QMessageBox.question = _orig_question
QMessageBox.information = _orig_info
QMessageBox.critical = _orig_crit
_window.load_ocr_config = _orig_load

# --- 14. GTK-шум платформенной темы Qt подавлен (_quiet_gtk_log)
import ctypes
import main as _main
_glib = ctypes.CDLL("libglib-2.0.so.0")
_gobj = ctypes.CDLL("libgobject-2.0.so.0")
_glib.g_log.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
_glib.g_log.restype = None

def _gtk_noise():
    _gobj.g_object_unref(None)  # GLib-GObject-CRITICAL
    _glib.g_log(b"Gtk", 1 << 4, b"Could not load a pixbuf from icon theme.")

def _capture2(fn):
    import tempfile
    f = tempfile.TemporaryFile(); saved = os.dup(2); os.dup2(f.fileno(), 2)
    try:
        fn()
    finally:
        os.dup2(saved, 2); os.close(saved)
        f.seek(0); out = f.read().decode(errors="replace"); f.close()
    return out

noise_before = _capture2(_gtk_noise)
_main._quiet_gtk_log()
noise_after = _capture2(_gtk_noise)
check("14. шум GTK до глушителя виден", "GLib-GObject" in noise_before)
check("14. шум GTK после глушителя нет", noise_after == "")

# --- 16. таблица: блоки по ячейкам + «Копировать текст» правым кликом
_tbl = pymupdf.open()
_p = _tbl.new_page(width=300, height=140)
for _y in (30, 60, 90):
    _p.draw_line((30, _y), (270, _y), color=(0, 0, 0), width=1)
for _x in (30, 150, 270):
    _p.draw_line((_x, 30), (_x, 90), color=(0, 0, 0), width=1)
for _cx, _cy, _t in [(55, 50, "Cell A1"), (175, 50, "Cell A2"),
                     (55, 80, "Cell B1"), (175, 80, "Cell B2")]:
    _p.insert_text((_cx, _cy), _t, fontname="helv", fontsize=10)
_tpath = os.path.join(os.path.dirname(SAMPLE), "table.pdf")
_tbl.save(_tpath); _tbl.close()
WT = _window.MainWindow(pdf_path=_tpath)
WT.resize(1280, 860)
texts16 = {b.text for b in WT.blocks}
check("16. блоки таблицы разбиты по ячейкам", all(
    c in texts16 for c in ("Cell A1", "Cell A2", "Cell B1", "Cell B2")))
idx16 = next(i for i, b in enumerate(WT.blocks) if b.text == "Cell B2")
WT.copy_block_text(idx16)
check("16. «Копировать текст» кладёт текст ячейки в буфер",
      QApplication.clipboard().text() == "Cell B2")
WT.close()

# --- 18. дабл клик: оверлей реальной гарнитурой и кеглем 1:1
_ef = pymupdf.open()
_ep = _ef.new_page()
_ep.insert_font(fontname="dj", fontfile="/usr/share/fonts/TTF/DejaVuSans.ttf")
_ep.insert_text((72, 100), "Exact face preview", fontname="dj", fontsize=12)
_ef.save(FONTPDF := os.path.join(os.path.dirname(SAMPLE), "font.pdf"))
_ef.close()
WF = _window.MainWindow(pdf_path=FONTPDF)
WF.resize(1280, 860)
_fitem = next(it for it in WF.scene.items()
              if isinstance(it, BlockItem) and it.data(0) == 0)
WF.start_edit(_fitem)
check("18. оверлей встроенным TTF (точная гарнитура)",
      WF.overlay is not None and WF.overlay.font().family() == "DejaVu Sans")
check("18. кегль точно как в блоке (px сцены × VIEW_ZOOM)",
      WF.overlay.font().pixelSize() == round(WF.blocks[0].size * 2.0))
WF.cancel_edit()
# Base-14: буфера нет — остаётся подмена по имени, кегль всё равно точен
_hitem = next(it for it in WIN.scene.items() if isinstance(it, BlockItem)
              and it.data(0) == 0)
WIN.start_edit(_hitem)
check("18. кегль оверлея Base-14 тоже 1:1 со страницей",
      WIN.overlay is not None
      and WIN.overlay.font().pixelSize() == round(WIN.blocks[0].size * 2.0))
WIN.cancel_edit()

# --- 17. Ctrl+C на выделенном блоке; Копировать/Вырезать/Удалить
item17 = next(it for it in WIN.scene.items()
              if isinstance(it, BlockItem) and it.data(0) < len(WIN.blocks))
WIN.scene.clearSelection()  # после 18 остаётся выделение; selectedItems()[0]
item17.setSelected(True)    # недетерминирован при двух выбранных блоках
WIN.on_block_selected()
QApplication.clipboard().setText("")
WIN._copy_selected()
check("17. Ctrl+C копирует текст выделенного блока",
      QApplication.clipboard().text() == WIN.blocks[item17.data(0)].text)
check("17. Ctrl+C доступен без открытия редактора", WIN.copy_sc.isEnabled())

# Контекстное меню: exec подменяем (модален), вызываем само событие с
# заглушкой позиции — проверяем и состав пунктов, и корректный screenPos
# (сломанный globalPos в PyQt6 ронял программу SIGABRT).
from types import SimpleNamespace
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QMenu
_qmenu_exec, texts17 = QMenu.exec, []
QMenu.exec = lambda self, *a, **k: (
    texts17.extend(a.text() for a in self.actions()), None)[1]
item17.contextMenuEvent(SimpleNamespace(screenPos=lambda: QPoint(1, 1)))
QMenu.exec = _qmenu_exec
check("17. меню: Копировать/Вырезать/Удалить",
      texts17 == ["Копировать", "Вырезать", "Удалить"])

WIN.start_edit(item17)
check("17. при открытом редакторе Ctrl+C выключен", not WIN.copy_sc.isEnabled())
WIN.cancel_edit()
check("17. после отмены Ctrl+C включён", WIN.copy_sc.isEnabled())

t17 = WIN.blocks[item17.data(0)].text
idx17 = item17.data(0)
WIN.cut_block_text(idx17)
check("17. вырезать: текст в буфере", QApplication.clipboard().text() == t17)
check("17. вырезать: текст блока удалён",
      not any(b.text == t17 for b in WIN.blocks))
WIN.undo()
check("17. undo восстановил после вырезания",
      any(b.text == t17 for b in WIN.blocks))

WIN.delete_block_text(idx17)
check("17. удалить: текст блока исчез",
      not any(b.text == t17 for b in WIN.blocks))
WIN.undo()
check("17. undo восстановил после удаления",
      any(b.text == t17 for b in WIN.blocks))

#OFFSCREEN: диалог подтверждения закрытия модален — подменяем на "Да".
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
WIN.close()  # closeEvent покажет подтверждение несохранённых правок

print()
print("Ошибка" if failures else "Все смоки пройдены", failures or "")
sys.exit(1 if failures else 0)
