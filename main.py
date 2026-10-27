"""Точка входа PDF-редактора: `python main.py [файл.pdf]`."""
import sys
from PyQt6.QtWidgets import QApplication, QFileDialog

from window import MainWindow


def _quiet_gtk_log() -> None:
    """Гасим косметический шум GTK3-платформенной темы Qt: Gtk-WARNING про
    иконки темы и GLib-GObject-CRITICAL g_object_unref (двойной unref в
    плагине). Другие домены и уровни не затрагиваются.
    shortcut: GTK-плагин всё ещё может не найти иконку — иконки в диалогах
    пропадут, апгрейдить при появлении видимых артефактов."""
    import ctypes
    glib = ctypes.CDLL("libglib-2.0.so.0")
    handler = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_void_p)
    # ссылка живёт весь процесс, иначе ctypes-колбэк освободится
    _quiet_gtk_log.cb = handler(lambda *a: None)
    levels = (1 << 3) | (1 << 4)  # G_LOG_LEVEL_CRITICAL | G_LOG_LEVEL_WARNING
    for domain in (b"Gtk", b"GLib-GObject"):
        glib.g_log_set_handler(domain, levels, _quiet_gtk_log.cb, None)


def main() -> None:
    _quiet_gtk_log()
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
