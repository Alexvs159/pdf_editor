"""Главное окно: миниатюры слева, страница с блоками в центре, редактор справа."""
import os

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QSplitter, QWidget,
)

import pymupdf

from document import PdfDocument, _base14_key, _clean_font_name

THUMB_W = 120
VIEW_ZOOM = 2.0


def pixmap_to_qimage(pix: pymupdf.Pixmap) -> QImage:
    return QImage(pix.samples, pix.width, pix.height, pix.stride,
                  QImage.Format.Format_RGB888)


class MainWindow(QMainWindow):
    def __init__(self, pdf_path: str | None = None):
        super().__init__()
        self.doc: PdfDocument | None = None
        self.page_idx = -1
        self.blocks = []
        self.selected = -1
        self._undo_stack = []

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

        # Правая панель: редактор блока.
        right = QWidget()
        grid = QGridLayout(right)
        self.info = QLabel("Блок не выбран")
        self.info.setWordWrap(True)
        self.font_combo = QComboBox()
        self.font_combo.addItem("Авто (сохранить исходный)", "auto")
        self.editor = QPlainTextEdit()
        apply_btn = QPushButton("Применить (Ctrl+Enter)")
        apply_btn.clicked.connect(self.apply_edit)
        undo_btn = QPushButton("Отменить (Ctrl+Z)")
        undo_btn.clicked.connect(self.undo)
        grid.addWidget(self.info, 0, 0)
        grid.addWidget(self.font_combo, 1, 0)
        grid.addWidget(self.editor, 2, 0)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(undo_btn)
        btn_row.addWidget(apply_btn)
        grid.addLayout(btn_row, 3, 0)
        right.setFixedWidth(360)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.thumbs)
        split.addWidget(self.view)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        QShortcut(QKeySequence("Ctrl+Return"), self, self.apply_edit)
        QShortcut(QKeySequence("Ctrl+Z"), self, self.undo)
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
            self.font_combo.addItem(val, _key)
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
        self.editor.setPlainText("")
        self.info.setText("Блок не выбран")
        self.scene.clear()
        pix = self.doc.page_pixmap(self.page_idx, zoom=VIEW_ZOOM)
        self.scene.addPixmap(QPixmap.fromImage(pixmap_to_qimage(pix)))
        self.blocks = self.doc.blocks(self.page_idx)
        for n, b in enumerate(self.blocks):
            r = QGraphicsRectItem(QRectF(b.rect.x0 * VIEW_ZOOM, b.rect.y0 * VIEW_ZOOM,
                                         b.rect.width * VIEW_ZOOM, b.rect.height * VIEW_ZOOM))
            r.setPen(QPen(QColor(30, 90, 220, 160), 1))
            r.setBrush(QColor(30, 90, 220, 30))
            r.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsSelectable)
            r.setData(0, n)
            self.scene.addItem(r)
        if not self.blocks:
            self.info.setText("Текста на странице нет")

    def on_block_selected(self) -> None:
        items = [it for it in self.scene.selectedItems() if isinstance(it, QGraphicsRectItem)]
        if not items:
            return
        b = self.blocks[items[0].data(0)]
        self.selected = items[0].data(0)
        self.info.setText(f"Шрифт: {b.font} · {b.size:g} pt")
        self.editor.setPlainText(b.text)
        clean = _clean_font_name(b.font)
        i = self.font_combo.findData(f"emb:{clean}")
        if i < 0:
            i = self.font_combo.findData(_base14_key(b.font))
        if i < 0:
            i = self.font_combo.findData("auto")
        self.font_combo.setCurrentIndex(i if i >= 0 else 0)

    def apply_edit(self) -> None:
        if not self.doc or self.selected < 0:
            return
        b = self.blocks[self.selected]
        self._undo_stack.append(self.doc.doc.tobytes())
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)
        choice = self.font_combo.currentData()
        try:
            self.doc.set_text(self.page_idx, b, self.editor.toPlainText(), font_choice=choice)
        except Exception as e:
            self._undo_stack.pop()  # не оставляем фиктивный снимок
            QMessageBox.critical(self, "PDF Editor", f"Ошибка редактирования:\n{e}")
            return
        self.select_page(self.page_idx)
        self.thumbs.currentItem().setIcon(
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
        # shortcut: автосохранения нет, несохранённые правки теряются без
        # предупреждения; добавить вопрос при закрытии, когда появится undo.
        event.accept()
