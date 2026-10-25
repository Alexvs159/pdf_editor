"""Точка входа PDF-редактора: `python main.py [файл.pdf]`."""
import sys
from PyQt6.QtWidgets import QApplication, QFileDialog

from window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        path, _ = QFileDialog.getOpenFileName(None, "Открыть PDF", "", "PDF (*.pdf)")
    if path:
        win = MainWindow(pdf_path=path)
        win.show()
        sys.exit(app.exec())


if __name__ == "__main__":
    main()
