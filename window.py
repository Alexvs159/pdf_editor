"""Главное окно: миниатюры слева, страница с блоками в центре.

Правка текста — прямо на странице: двойной клик по рамке блока создаёт
in-place редактор (QGraphicsTextItem). Панель шрифта/размера/Применить
видна всегда; без выбранного блока её поля отключены.
"""
import os

from PyQt6.QtCore import (
    QRectF, QSettings, QSize, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QGraphicsRectItem, QGraphicsScene,
    QGraphicsTextItem, QGraphicsView, QSplitter, QVBoxLayout, QWidget,
)
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter

import pymupdf

import ocr
from document import PdfDocument, _base14_key, _clean_font_name

THUMB_W = 120
VIEW_ZOOM = 2.0


def parse_page_ranges(spec: str, total: int) -> list[int]:
    """'1,3,5-7' → [0,2,4,5,6]; пусто → все страницы. ValueError при выходе
    за пределы или нечисловом вводе."""
    spec = spec.strip()
    if not spec:
        return list(range(total))
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            first, last = int(lo), int(hi)
            if first > last:
                first, last = last, first
            pages.extend(range(first - 1, last))
        else:
            pages.append(int(part) - 1)
    bad = next((p for p in pages if not 0 <= p < total), None)
    if bad is not None:
        raise ValueError(f"Нет страницы {bad + 1} (всего страниц: {total})")
    return pages


def pixmap_to_qimage(pix: pymupdf.Pixmap) -> QImage:
    return QImage(pix.samples, pix.width, pix.height, pix.stride,
                  QImage.Format.Format_RGB888)


_OCR_SETTINGS = ("pdf_editor", "pdf_editor-ocr")


def load_ocr_config() -> ocr.OcrConfig:
    """Настройки OCR: env-дефолты, перекрытые сохранёнными в QSettings."""
    env = ocr.config_from_env()
    s = QSettings(*_OCR_SETTINGS)

    def pick(key: str, default: str) -> str:
        val = s.value(key, "")
        return str(val) if val else default

    return ocr.OcrConfig(pick("base_url", env.base_url),
                         pick("model", env.model),
                         pick("api_key", env.api_key),
                         pick("font_path", env.font_path))


class BlockItem(QGraphicsRectItem):
    """Рамка блока: выделение/перетаскивание, двойной клик — правка in-place."""

    def __init__(self, rect: QRectF, win: "MainWindow"):
        super().__init__(rect)
        self.win = win
        self._press = None

    def mouseDoubleClickEvent(self, event) -> None:
        self.win.start_edit(self)

    def mousePressEvent(self, event) -> None:
        self._press = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        p, self._press = self._press, None
        if p is None:
            return
        if abs(self.pos().x() - p.x()) + abs(self.pos().y() - p.y()) < 1:
            return  # клик, а не перемещение
        # Перемещение фиксируется сразу, без кнопки «Применить». Позицию
        # считаем сейчас: отложенный вызов пересоздаст сцену и этот item.
        idx = self.data(0)
        rect = self.win.scene_rect(self)
        QTimer.singleShot(0, lambda: self.win.commit_move(idx, rect))


class OcrSettingsDialog(QDialog):
    """base_url/модель/ключ/шрифт. Дефолты — env, хранение — QSettings."""

    def __init__(self, cfg: ocr.OcrConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки OCR")
        lay = QVBoxLayout(self)
        self.base_url = QLineEdit(cfg.base_url)
        self.base_url.setPlaceholderText("https://api.openai.com/v1")
        self.model = QLineEdit(cfg.model)
        self.model.setPlaceholderText("vision-модель, например gpt-4o-mini")
        self.key = QLineEdit(cfg.api_key)
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setToolTip(
            "Ключ хранится в QSettings в открытом виде (локальный инструмент).")
        self.font_path = QLineEdit(cfg.font_path)
        self.font_path.setPlaceholderText(
            "TTF с кириллицей для текстового слоя (пусто — системный поиск)")
        for label, w in (("Base URL", self.base_url), ("Модель", self.model),
                         ("Ключ API", self.key),
                         ("TTF для кириллицы", self.font_path)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(w, 1)
            lay.addLayout(row)
        btns = QHBoxLayout()
        ok = QPushButton("Сохранить")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        lay.addLayout(btns)

    def config(self) -> ocr.OcrConfig:
        return ocr.OcrConfig(self.base_url.text().strip().rstrip("/"),
                             self.model.text().strip(),
                             self.key.text().strip(),
                             self.font_path.text().strip())


class OcrWorker(QThread):
    """Транскрипция подготовленных растров страниц вне главного потока.
    Результат применяется только целиком: сбой → failed, вставок нет."""

    progress = pyqtSignal(int, int)  # готово страниц, всего
    ok = pyqtSignal(list)            # [(индекс страницы, [OcrBlock])]
    failed = pyqtSignal(str)

    def __init__(self, cfg: ocr.OcrConfig, pages: list[tuple[int, pymupdf.Pixmap]]):
        super().__init__()
        self.cfg = cfg
        self.pages = pages

    def run(self) -> None:
        try:
            out = []
            for n, (idx, pix) in enumerate(self.pages):
                blocks = ocr.run_page_ocr(self.cfg, pix, VIEW_ZOOM)
                if blocks:
                    out.append((idx, blocks))
                self.progress.emit(n + 1, len(self.pages))
            self.ok.emit(out)
        except Exception as e:
            self.failed.emit(str(e))


class MainWindow(QMainWindow):
    def __init__(self, pdf_path: str | None = None):
        super().__init__()
        self.doc: PdfDocument | None = None
        self.page_idx = -1
        self.blocks = []
        self.selected = -1
        self._undo_stack = []
        self._dirty = False                            # есть несохранённые правки
        self.ocr_worker: OcrWorker | None = None       # OCR в фоне
        self._ocr_cfg: ocr.OcrConfig | None = None     # настройки запуска
        self.overlay: QGraphicsTextItem | None = None  # in-place редактор
        self.overlay_bg = None                         # белая подложка под ним
        self.overlay_idx = None                        # блок, который правим
        self.edit_rect = None                          # исходный rect блока

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
        self.apply_btn = QPushButton("Применить (Ctrl+Enter)")
        self.apply_btn.clicked.connect(self.apply_edit)
        self.cancel_btn = QPushButton("Отмена (Esc)")
        self.cancel_btn.clicked.connect(self.cancel_edit)
        undo_btn = QPushButton("Отменить (Ctrl+Z)")
        undo_btn.clicked.connect(self.undo)
        top_row.addWidget(self.info, 1)
        top_row.addWidget(self.font_combo)
        top_row.addWidget(self.size_auto)
        top_row.addWidget(self.size_spin)
        top_row.addWidget(self.apply_btn)
        top_row.addWidget(self.cancel_btn)
        top_row.addWidget(undo_btn)
        # Панель видна всегда: иначе при выборе блока страница прыгает вниз
        # на её высоту. Без выбора блока параметры недоступны.
        self.set_block_controls_enabled(False)

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
        QShortcut(QKeySequence("Ctrl+P"), self, self.print_pages)

        menu = self.menuBar().addMenu("&Файл")
        act_open = QAction("&Открыть…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.open_file_dialog)
        act_save = QAction("Сохранить &как…", self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(self.prompt_save)
        act_print = QAction("&Печать…", self)
        act_print.setShortcut(QKeySequence("Ctrl+P"))
        act_print.triggered.connect(self.print_pages)
        menu.addAction(act_open)
        menu.addAction(act_save)
        menu.addAction(act_print)

        menu_ocr = self.menuBar().addMenu("&OCR")
        self.act_ocr_page = QAction("OCR &страницы…", self)
        self.act_ocr_page.triggered.connect(self.ocr_page)
        self.act_ocr_doc = QAction("OCR весь &документ…", self)
        self.act_ocr_doc.triggered.connect(self.ocr_document)
        self.act_ocr_settings = QAction("&Настройки OCR…", self)
        self.act_ocr_settings.triggered.connect(self.edit_ocr_settings)
        self._ocr_actions = [self.act_ocr_page, self.act_ocr_doc,
                             self.act_ocr_settings]
        menu_ocr.addAction(self.act_ocr_page)
        menu_ocr.addAction(self.act_ocr_doc)
        menu_ocr.addSeparator()
        menu_ocr.addAction(self.act_ocr_settings)

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
        self._dirty = False
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
        if not path:
            return
        try:
            self.doc.save_as(path)
        except Exception as e:
            # Без catch исключение в слоте Qt роняет приложение вместе
            # с несохранёнными правками.
            QMessageBox.critical(self, "PDF Editor", f"Не удалось сохранить:\n{e}")
            return
        self._dirty = False
        QMessageBox.information(self, "PDF Editor", f"Сохранено: {path}")

    def print_pages(self) -> None:
        """Печать через системный диалог: выбор страниц — номера/диапазоны."""
        if not self.doc:
            return
        spec, ok = QInputDialog.getText(
            self, "Печать",
            f"Страницы: номера через запятую или диапазоны (например 1,3,5-7).\n"
            f"Пусто — весь документ. Всего страниц: {self.doc.page_count}.")
        if not ok:
            return
        try:
            pages = parse_page_ranges(spec, self.doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self, "PDF Editor", str(e))
            return
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(os.path.basename(self.doc.path))
        if QPrintDialog(printer, self).exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.print_pages_to(printer, pages)
        except Exception as e:
            # Исключение в слоте Qt роняет приложение — как при сохранении.
            QMessageBox.critical(self, "PDF Editor", f"Не удалось напечатать:\n{e}")

    def print_pages_to(self, printer: QPrinter, pages: list[int]) -> None:
        """Печать выбранных страниц: рендер модели постранично как изображение."""
        painter = QPainter()
        if not painter.begin(printer):
            raise RuntimeError("Принтер недоступен")
        try:
            for pos, i in enumerate(pages):
                if pos:
                    printer.newPage()
                img = pixmap_to_qimage(self.doc.page_pixmap(i, zoom=2.5))
                # Логическая система координат — страница принтера в пунктах.
                page = printer.pageRect(QPrinter.Unit.Point)
                if not page.isValid():
                    page = printer.paperRect(QPrinter.Unit.Point)
                painter.setWindow(page.toRect())
                size = img.size().scaled(page.size().toSize(),
                                         Qt.AspectRatioMode.KeepAspectRatio)
                target = QRectF(
                    page.x() + (page.width() - size.width()) / 2,
                    page.y() + (page.height() - size.height()) / 2,
                    size.width(), size.height())
                painter.drawImage(target, img)
        finally:
            painter.end()

    def set_block_controls_enabled(self, on: bool) -> None:
        """Параметры шрифта и Применить/Отмена — только при выбранном блоке."""
        for w in (self.font_combo, self.size_auto, self.size_spin,
                  self.apply_btn, self.cancel_btn):
            w.setEnabled(on)

    # -- OCR --------------------------------------------------------------
    def edit_ocr_settings(self) -> None:
        dlg = OcrSettingsDialog(load_ocr_config(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dlg.config()
        s = QSettings(*_OCR_SETTINGS)
        for key, val in (("base_url", cfg.base_url), ("model", cfg.model),
                         ("api_key", cfg.api_key),
                         ("font_path", cfg.font_path)):
            s.setValue(key, val)
        s.sync()

    def ocr_page(self) -> None:
        if self.doc and self.page_idx >= 0:
            self.run_ocr([self.page_idx])

    def ocr_document(self) -> None:
        if self.doc:
            self.run_ocr(list(range(self.doc.page_count)))

    def run_ocr(self, pages: list[int]) -> None:
        """Растры выбранных страниц → фоновый воркер; вставка только при
        успехе ВСЕХ страниц (политика «прервать всё»)."""
        if not self.doc or self.ocr_worker is not None or not pages:
            return
        cfg = load_ocr_config()
        if not cfg.configured():
            QMessageBox.warning(self, "OCR",
                                "Не заданы base_url, модель или ключ API.\n"
                                "Открою настройки OCR.")
            self.edit_ocr_settings()
            return
        # shortcut: повёрнутые страницы пропускаем — текст вставился бы
        # вместе с поворотом страницы; апгрейд — пересчёт через матрицу.
        rotated = [i for i in pages if self.doc.doc[i].rotation % 360]
        pages = [i for i in pages if i not in rotated]
        if rotated:
            QMessageBox.warning(self, "OCR",
                                "Пропущены повёрнутые страницы: "
                                + ", ".join(str(i + 1) for i in rotated))
        if not pages:
            return
        with_text = [i for i in pages
                     if self.doc.doc[i].get_text().strip()]
        if with_text:
            reply = QMessageBox.question(
                self, "OCR",
                "На страницах уже есть текстовый слой: "
                + ", ".join(str(i + 1) for i in with_text)
                + ". Добавить ещё один?")
            if reply != QMessageBox.StandardButton.Yes:
                return
        pixmaps = [(i, self.doc.page_pixmap(i, zoom=VIEW_ZOOM))
                   for i in pages]
        self._ocr_cfg = cfg
        self.ocr_worker = OcrWorker(cfg, pixmaps)
        self.ocr_worker.progress.connect(self._ocr_progress)
        self.ocr_worker.ok.connect(self._ocr_done)
        self.ocr_worker.failed.connect(self._ocr_failed)
        for a in self._ocr_actions:
            a.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self.statusBar().showMessage(f"OCR: 0/{len(pixmaps)}")
        self.ocr_worker.start()

    def _ocr_progress(self, done: int, total: int) -> None:
        self.statusBar().showMessage(f"OCR: {done}/{total}")

    def _ocr_done(self, results: list) -> None:
        self._ocr_finish()
        if not results:
            QMessageBox.information(self, "OCR",
                                    "Текст на страницах не найден.")
            return
        self._snapshot()
        try:
            for idx, blocks in results:
                self.doc.apply_ocr(idx, blocks,
                                   font_path=self._ocr_cfg.font_path)
        except Exception as e:
            self.undo()  # откат к снимку до вставки
            QMessageBox.critical(self, "OCR",
                                 f"Не удалось вставить текст:\n{e}")
            return
        self._dirty = True
        self.select_page(self.page_idx)
        self.thumbs.blockSignals(True)
        for idx, _blocks in results:
            item = self.thumbs.item(idx)
            if item is not None:
                item.setIcon(QIcon(QPixmap.fromImage(pixmap_to_qimage(
                    self.doc.thumbnail_pixmap(idx)))))
        self.thumbs.blockSignals(False)
        total = sum(len(b) for _i, b in results)
        self.info.setText(f"OCR: добавлено блоков {total} (Ctrl+Z — отменить)")

    def _ocr_failed(self, msg: str) -> None:
        self._ocr_finish()
        QMessageBox.critical(self, "OCR",
                             f"OCR прерван, изменений нет:\n{msg}")

    def _ocr_finish(self) -> None:
        w, self.ocr_worker = self.ocr_worker, None
        if w is not None:
            w.wait()
        for a in self._ocr_actions:
            a.setEnabled(True)
        QApplication.restoreOverrideCursor()
        self.statusBar().clearMessage()

    # -- страницы и блоки -------------------------------------------------
    def select_page(self, row: int) -> None:
        if not self.doc or row < 0:
            return
        self.page_idx = row
        self.selected = -1
        self.scene.clear()
        self.overlay = None
        self.overlay_bg = None
        self.overlay_idx = None
        self.info.setText("Блок не выбран")
        self.set_block_controls_enabled(False)
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
            self.info.setText("Блок не выбран")
            self.set_block_controls_enabled(False)
            return
        b = self.blocks[items[0].data(0)]
        self.selected = items[0].data(0)
        self.info.setText(f"Шрифт: {b.font} · {b.size:g} pt (двойной клик — правка)")
        self.size_spin.setValue(b.size)
        self.size_auto.setChecked(True)
        # «Авто»: шрифт каждого прогона берётся из модели. Выставлять здесь
        # шрифт первого спана нельзя — это override на весь блок, и блок со
        # смешанными начертаниями становился целиком жирным.
        self.font_combo.setCurrentIndex(0)
        self.set_block_controls_enabled(True)

    # -- правка in-place --------------------------------------------------
    def start_edit(self, item: BlockItem) -> None:
        if not self.doc or not self.blocks:
            return
        self.cancel_edit()
        item.setSelected(True)
        self.overlay_idx = item.data(0)
        b = self.blocks[self.overlay_idx]
        rect = item.sceneBoundingRect()
        self.edit_rect = rect
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
        # Без ограничения ширины: текст не переносится сам, только по Enter.
        t.setTextWidth(-1)
        t.setPos(rect.topLeft())
        t.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        t.setFlag(QGraphicsRectItem.GraphicsItemFlag.ItemIsFocusable, True)
        t.setZValue(1)
        self.overlay = t
        self.scene.addItem(t)
        t.document().contentsChanged.connect(self._fit_overlay_bg)
        self._fit_overlay_bg()
        t.setFocus()

    def _fit_overlay_bg(self) -> None:
        """Подложка растёт вместе с текстом по горизонтали и вертикали."""
        if self.overlay is None or self.overlay_bg is None:
            return
        r = self.overlay.mapRectToScene(self.overlay.boundingRect())
        self.overlay_bg.setRect(r.united(self.edit_rect))

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

    def scene_rect(self, item: BlockItem) -> pymupdf.Rect:
        """Rect рамки в координатах PDF (сцена масштабирована VIEW_ZOOM)."""
        sr = item.sceneBoundingRect()
        return pymupdf.Rect(sr.x() / VIEW_ZOOM, sr.y() / VIEW_ZOOM,
                            (sr.x() + sr.width()) / VIEW_ZOOM,
                            (sr.y() + sr.height()) / VIEW_ZOOM)

    def _snapshot(self) -> None:
        """Снимок документа для undo: не больше 20 шагов и 64 МБ."""
        self._undo_stack.append(self.doc.doc.tobytes())
        total = sum(len(d) for d in self._undo_stack)

        while len(self._undo_stack) > 1 and (
                len(self._undo_stack) > 20 or total > 64 * 1024 * 1024):
            total -= len(self._undo_stack.pop(0))

    def _submit(self, b, text: str, target: pymupdf.Rect | None,
                choice: str | None = None, font_size: float | None = None) -> None:
        """Общая точка применения: undo-снимок, правка модели, перерисовка."""
        self._snapshot()
        try:
            self.doc.set_text(self.page_idx, b, text, font_choice=choice,
                              font_size=font_size, insert_rect=target)
        except Exception as e:
            QMessageBox.critical(self, "PDF Editor", f"Ошибка редактирования:\n{e}")
            return
        self._dirty = True
        # Оверлей больше не нужен: снимок уже в undo, страницу перерисуем.
        self.overlay = None
        self.overlay_bg = None
        self.overlay_idx = None
        self.select_page(self.page_idx)
        item = self.thumbs.currentItem()
        if item is not None:
            item.setIcon(
                QIcon(QPixmap.fromImage(pixmap_to_qimage(self.doc.thumbnail_pixmap(self.page_idx))))
            )
        self.info.setText("Изменено (Ctrl+Z — отменить)")

    def apply_edit(self) -> None:
        """Применить текст из редактора и/или шрифт с размером к блоку."""
        if not self.doc:
            return
        idx = self.overlay_idx if self.overlay_idx is not None else self.selected
        if idx is None or not 0 <= idx < len(self.blocks):
            return
        b = self.blocks[idx]
        target = None
        for it in self.scene.items():
            if isinstance(it, BlockItem) and it.data(0) == idx:
                target = self.scene_rect(it)
                break
        text = self._overlay_text() if self.overlay is not None else b.text
        self._submit(b, text, target, self.font_combo.currentData(),
                     None if self.size_auto.isChecked() else self.size_spin.value())

    def commit_move(self, idx: int, target: pymupdf.Rect) -> None:
        """Перемещение мышью: фиксируется сразу, текст встаёт на новое место."""
        if not self.doc or idx < 0 or idx >= len(self.blocks):
            return
        old = self.blocks[idx]
        if self.overlay is not None:
            # Набранный текст не выбрасываем: применяем его до перемещения,
            # затем ищем сдвинутый блок заново (страница перерисована).
            self.apply_edit()
            idx = next((i for i, b in enumerate(self.blocks)
                        if abs(b.rect.x0 - old.rect.x0) < 1
                        and abs(b.rect.y0 - old.rect.y0) < 1), None)
            if idx is None:
                return
        self._submit(self.blocks[idx], self.blocks[idx].text, target)

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
        self._dirty = True
        self.select_page(self.page_idx)
        self.thumbs.blockSignals(True)
        for i in range(self.doc.page_count):
            item = self.thumbs.item(i)
            if item is not None:
                item.setIcon(QIcon(QPixmap.fromImage(pixmap_to_qimage(self.doc.thumbnail_pixmap(i)))))
        self.thumbs.blockSignals(False)
        self.info.setText("Отменено (Ctrl+Z)")

    def closeEvent(self, event):
        if self._dirty:
            reply = QMessageBox.question(
                self, "PDF Editor", "Есть несохранённые правки. Закрыть без сохранения?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
