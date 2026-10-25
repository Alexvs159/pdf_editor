"""GUI-смоки правки на канве. Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python test_gui_edit.py"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMessageBox

app = QApplication(sys.argv)

from window import BlockItem, MainWindow

WIN = MainWindow(pdf_path="Счет.pdf")
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


def blocks_by_text(substr):
    return {i: b for i, b in enumerate(WIN.blocks) if substr in b.text}


def item_text(item: BlockItem) -> str:
    return WIN.blocks[item.data(0)].text


# --- 1. открытие файла, выбор страницы
WIN.thumbs.setCurrentRow(0)
check("1. страница открыта", WIN.page_idx == 0 and WIN.blocks and not WIN.top.isVisible())

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

# --- 2b. одиночный клик — панель появляется при выделении
sel_item = find_block(t0.split("\n")[0][:10])
sprinkled = sel_item.data(0)
sel_item.setSelected(True)
check("2b. одиночный клик выбирает блок",
      WIN.selected == sprinkled and WIN.top.isVisible())

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
check("8. переноса нет (одна строка)", h2 < h1)
check("8. подложка закрывает текст", WIN.overlay_bg.rect().width() >= w2 - 1)
WIN.cancel_edit()
check("8. отмена убирает оверлей", WIN.overlay is None)

#OFFSCREEN: диалог подтверждения закрытия модален — подменяем на "Да".
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
WIN.close()  # closeEvent покажет подтверждение несохранённых правок

print()
print("Ошибка" if failures else "Все смоки пройдены", failures or "")
sys.exit(1 if failures else 0)
