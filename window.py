"""Главное окно: миниатюры слева, страница с блоками в центре.

Правка текста — прямо на странице: двойной клик по рамке блока создаёт
in-place редактор (QGraphicsTextItem). Панель шрифта/размера/Применить
появляется сверху при выделении блока.
"""
import os

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import (
    QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPen, QPixmap, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QGraphicsRectItem, QGraphicsScene, QGraphicsTextItem, QGraphicsView,
    QSplitter, QVBoxLayout, QWidget,
)

import pymupdf

from document import PdfDocument, _base14_key, _clean_font_name

THUMB_W = 120
VIEW_ZOOM = 2.0


def pixmap_to_qimage(pix: pymupdf.Pixmap) -> QImage:
    return QImage(pix.samples, pix.width, pix.height, pix.stride,
                  QImage.Format.Format_RGB888)


class BlockItem(QGraphicsRectItem):
    """Рамка блока: выделение/перетаскивание, двойной клик — правка in-place."""

    def __init__(self, rect: QRectF, win: "MainWindow"):
        super().__init__(rect)
        self.win = win

    def mouseDoubleClickEvent(self, event) -> None:
        self.win.start_edit(self)


class MainWindow(QMainWindow):
    def __init__(self, pdf_path: str | None = None):
        super().__init__()
        self.doc: PdfDocument | None = None
        self.page_idx = -1
        self.blocks = []
        self.selected = -1
        self._undo_stack = []
        self.overlay: QGraphicsTextItem | None = None  # in-place редактор
        self.overlay_bg = None                         # белая подложка под ним

        self.setWindowTitle("PDF Editor")
        self.resize(1280, 860)

        # Левая панель: миниатюры страниц.
        self.thumbs = QListWidget()
        self.thumbs.setFixedWidth(THUMB_W + 30)
        self.thumbs.setIconSize(QSize(THUMB_W, THUMB_W))
        self.thumbs.currentRowChanged.connect(self.select_page)

        # Центр: страница с рамками блоков.
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.scene.selectionChanged.connect(self.on_block_selected)

        # Верхняя панель: параметры шрифта + Применить/Отмена.
        self.top = QWidget()
        top_row = QHBoxLayout(self.top)
        self.info = QLabel("Блок не выбран")
        self.info.setWordWrap(True)
        self.font_combo = QComboBox()
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(4.0, 72.0)
        self.size_spin.setSingleStep(0.5)
        self.size_spin.setDecimals(1)
        self.size_spin.setSuffix(" pt")
        self.size_spin.setValue(12.0)
        self.size_auto = QCheckBox("Авто")
        self.size_auto.setChecked(True)
        self.size_auto.setToolTip("Размер по строкам исходного блока")
        apply_btn = QPushButton("Применить (Ctrl+Enter)")
        apply_btn.clicked.connect(self.apply_edit)
        cancel_btn = QPushButton("Отмена (Esc)")
        cancel_btn.clicked.connect(self.cancel_edit)
        undo_btn = QPushButton("Отменить (Ctrl+Z)")
        undo_btn.clicked.connect(self.undo)
        top_row.addWidget(self.info, 1)
        top_row.addWidget(self.font_combo)
        top_row.addWidget(self.size_auto)
        top_row.addWidget(self.size_spin)
        top_row.addWidget(apply_btn)
        top_row.addWidget(cancel_btn)
        top_row.addWidget(undo_btn)
        self.top.hide()

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.top)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.thumbs)
        split.addWidget(self.view)
        split.setStretchFactor(1, 1)
        lay.addWidget(split, 1)
        self.setCentralWidget(central)

        QShortcut(QKeySequence("Ctrl+Return"), self, self.apply_edit)
        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo)
        QShortcut(QKeySequence("Escape"), self, self.cancel_edit)
        QShortcut(QKeySequence("Ctrl+S"), self, self.prompt_save)

        menu = self.menuBar().addMenu("&Файл")
        act_open = QAction("&Открыть…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.open_file_dialog)
        act_save = QAction("Сохранить &как…", self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(self.prompt_save)
        menu.addAction(act_open)
        menu.addAction(act_save)

        if pdf_path:
            self.open_pdf(pdf_path)

    # -- файлы ------------------------------------------------------------
    def open_pdf(self, path: str) -> None:
        try:
            self.doc = PdfDocument(path)
        except Exception as e:
            QMessageBox.critical(self, "PDF Editor", f"Не удалось открыть файл:\n{e}")
            self.doc = None
            return
        self.setWindowTitle(f"PDF Editor — {os.path.basename(path)}")
        self._undo_stack.clear()
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for i in range(self.doc.page_count):
            pix = self.doc.thumbnail_pixmap(i)
            item = QListWidgetItem(QIcon(QPixmap.fromImage(pixmap_to_qimage(pix))), f"Стр. {i + 1}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.thumbs.addItem(item)
        self.thumbs.blockSignals(False)
        self.font_combo.blockSignals(True)
        self.font_combo.clear()
        self.font_combo.addItem("Авто (сохранить исходный)", "auto")
        for name in self.doc.embedded_font_names():
            self.font_combo.addItem(f"Встроенный: {name}", f"emb:{name}")
        seen = set()
        for _key, val in pymupdf.Base14_fontdict.items():
            if val in seen:
                continue
            seen.add(val)
            # данные — короткое зарезервированное имя (метрики корректны)
            self.font_combo.addItem(val, _base14_key(val))
        for cjk in ("china-s", "china-ss", "japan", "korea"):
            self.font_combo.addItem(f"CJK: {cjk}", cjk)
        self.font_combo.blockSignals(False)
        self.thumbs.setCurrentRow(0)

    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть PDF", "", "PDF (*.pdf)")
        if path:
            self.open_pdf(path)

    def prompt_save(self) -> None:
        if not self.doc:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", self.doc.path, "PDF (*.pdf)")
        if path:
            self.doc.save_as(path)
            QMessageBox.information(self, "PDF Editor", f"Сохранено: {path}")

    # -- страницы и блоки -------------------------------------------------
    def select_page(self, row: int) -> None:
        if not self.doc or row < 0:
            return
        self.page_idx = row
        self.selected = -1
        self.scene.clear()
        self.overlay = None
        self.overlay_bg = None
        pix = self.doc.page_pixmap(self.page_idx, zoom=VIEW_ZOOM)
        self.scene.addPixmap(QPixmap.fromImage(pixmap_to_qimage(pix)))
        self.blocks = self.doc.blocks(self.page_idx)
        for n, b in enumerate(self.blocks):
            r = BlockItem(QRectF(b.rect.x0 * VIEW_ZOOM, b.rect.y0 * VIEW_ZOOM,
                                 b.rect.width * VIEW_ZOOM, b.rect.height * VIEW_ZOOM), self)
            r.setPen(QPen(QColor(30, 90, 220, 160), 1))
            r.setBrush(QColor(30, 90, 220, 30))
            r.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable)
            r.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsMovable)
            r.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemSendsGeometryChanges)
            r.setData(0, n)
            self.scene.addItem(r)

    def on_block_selected(self) -> None:
        items = [it for it in self.scene.selectedItems() if isinstance(it, BlockItem)]
        if not items:
            if self.overlay is not None:
                # Клик мимо блоков при открытом редакторе — выход без правки.
                self.cancel_edit()
            self.top.hide()
            return
        b = self.blocks[items[0].data(0)]
        self.selected = items[0].data(0)
        self.info.setText(f"Шрифт: {b.font} · {b.size:g} pt (двойной клик — правка)")
        self.size_spin.setValue(b.size)
        self.size_auto.setChecked(True)
        clean = _clean_font_name(b.font)
        i = self.font_combo.findData(f"emb:{clean}")
        if i < 0:
            i = self.font_combo.findData(_base14_key(b.font))
        if i < 0:
            i = self.font_combo.findData("auto")
        self.font_combo.setCurrentIndex(i if i >= 0 else 0)
        self.top.show()

    # -- правка in-place --------------------------------------------------
    def start_edit(self, item: BlockItem) -> None:
        if not self.doc or not self.blocks:
            return
        self.cancel_edit()
        item.setSelected(True)
        b = self.blocks[item.data(0)]
        rect = item.sceneBoundingRect()
        self.overlay_bg = self.scene.addRect(
            rect, pen=QPen(Qt.PenStyle.NoPen), brush=QColor(255, 255, 255))
        t = QGraphicsTextItem()
        # shortcut: гарнитура оверлея приблизительная (QFont по очищенному
        # имени, встроенный TTF в Qt не загружается, кегль — эмпирический
        # пересчёт); точный шрифт/размер применяет модель при вставке.
        f = QFont(_clean_font_name(b.font))
        f.setPointSizeF(max(4.0, b.size * VIEW_ZOOM * 0.75))
        t.setFont(f)
        t.setDefaultTextColor(QColor(0, 0, 0))
        t.setPlainText(b.text)
        t.setTextWidth(rect.width())
        t.setPos(rect.topLeft())
        t.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        t.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsFocusable, True)
        t.setZValue(1)
        self.overlay = t
        self.scene.addItem(t)
        t.setFocus()

    def _overlay_text(self) -> str:
        # toPlainText() может дать U+2029 как разделитель абзацев —
        # приводим к "\n" (модель делит текст по "\n").
        return self.overlay.toPlainText().replace("\u2029", "\n")

    def cancel_edit(self) -> None:
        t, self.overlay = self.overlay, None
        bg, self.overlay_bg = self.overlay_bg, None
        if bg is not None:
            self.scene.removeItem(bg)
        if t is not None:
            self.scene.removeItem(t)

    def apply_edit(self) -> None:
        if not self.doc or self.overlay is None or self.selected < 0:
            return
        text = self._overlay_text()
        b = self.blocks[self.selected]
        self._undo_stack.append(self.doc.doc.tobytes())
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)
        choice = self.font_combo.currentData()
        font_size = None if self.size_auto.isChecked() else self.size_spin.value()
        # Если блок перемещали мышью — вставляем в новое место.
        target = None
        for it in self.scene.items():
            if isinstance(it, BlockItem) and it.data(0) == self.selected:
                sr = it.sceneBoundingRect()
                target = pymupdf.Rect(sr.x() / VIEW_ZOOM, sr.y() / VIEW_ZOOM,
                                      (sr.x() + sr.width()) / VIEW_ZOOM,
                                      (sr.y() + sr.height()) / VIEW_ZOOM)
                break
        try:
            self.doc.set_text(self.page_idx, b, text, font_choice=choice,
                              font_size=font_size, insert_rect=target)
        except Exception as e:
            QMessageBox.critical(self, "PDF Editor", f"Ошибка редактирования:\n{e}")
            return
        # Оверлей больше не нужен: снимок уже в undo, страницу перерисуем.
        self.overlay = None
        self.overlay_bg = None
        self.select_page(self.page_idx)
        item = self.thumbs.currentItem()
        if item is not None:
            item.setIcon(
                QIcon(QPixmap.fromImage(pixmap_to_qimage(self.doc.thumbnail_pixmap(self.page_idx))))
            )
        self.info.setText("Изменено (Ctrl+Z — отменить)")

    def undo(self) -> None:
        if not self.doc or not self._undo_stack:
            return
        data = self._undo_stack.pop()
        try:
            self.doc.doc.close()
            self.doc.doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            QMessageBox.critical(self, "PDF Editor", f"Не удалось отменить:\n{e}")
            return
        self.select_page(self.page_idx)
        self.thumbs.blockSignals(True)
        for i in range(self.doc.page_count):
            item = self.thumbs.item(i)
            if item is not None:
                item.setIcon(QIcon(QPixmap.fromImage(pixmap_to_qimage(self.doc.thumbnail_pixmap(i)))))
        self.thumbs.blockSignals(False)
        self.info.setText("Отменено (Ctrl+Z)")

    def closeEvent(self, event):
        if self._undo_stack:
            reply = QMessageBox.question(
                self, "PDF Editor", "Есть несохранённые правки. Закрыть без сохранения?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
