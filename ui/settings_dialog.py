"""Password-protected settings: labels, access control, network/relay, address book."""
from __future__ import annotations

import asyncio
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
                             QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                             QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                             QWidget)

from core import security, settingslock
from core.relay_client import RelaySettings, connect_via_relay
from core.relay_proto import RelayError
from utils.addressbook import AddressBook, Entry
from utils.config import Config

_throttle = settingslock.Throttle()


def ask_settings_access(parent, cfg: Config) -> bool:
    """Gate: asks for (or, on first use, creates) the settings password."""
    if not cfg.settings_lock:
        pw, ok = QInputDialog.getText(
            parent, "رمز تنظیمات",
            "برای محافظت از تنظیمات یک رمز بسازید.\n"
            "(بدون این رمز کسی نمی‌تواند تنظیمات را تغییر دهد)",
            QLineEdit.EchoMode.Password)
        if not ok or not pw:
            return False
        again, ok = QInputDialog.getText(parent, "رمز تنظیمات", "رمز را دوباره وارد کنید:",
                                         QLineEdit.EchoMode.Password)
        if not ok or again != pw:
            QMessageBox.warning(parent, "ViunaLink", "دو رمز یکسان نبودند.")
            return False
        cfg.settings_lock = settingslock.make_hash(pw)
        cfg.save()
        return True
    while True:
        wait = _throttle.wait_seconds()
        if wait > 0:
            QMessageBox.warning(parent, "ViunaLink",
                                f"تلاش‌های ناموفق زیاد بود. {wait:.0f} ثانیه صبر کنید.")
            return False
        pw, ok = QInputDialog.getText(parent, "ورود به تنظیمات", "رمز تنظیمات:",
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return False
        if settingslock.verify(cfg.settings_lock, pw):
            _throttle.ok()
            return True
        _throttle.failed()


class SettingsDialog(QDialog):
    relay_test_done = pyqtSignal(str)

    def __init__(self, cfg: Config, book: AddressBook, parent=None):
        super().__init__(parent)
        self.cfg, self.book = cfg, book
        self.setWindowTitle("تنظیمات ViunaLink")
        self.resize(640, 560)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._general(), "عمومی")
        tabs.addTab(self._security(), "امنیت و دسترسی")
        tabs.addTab(self._network(), "شبکه و اینترنت")
        tabs.addTab(self._book_tab(), "دفترچه آدرس")
        lay.addWidget(tabs)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                               QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.relay_test_done.connect(self._relay_result)

    # ---------------------------------------------------------------- tabs
    def _general(self) -> QWidget:
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        c = self.cfg
        self.label = QLineEdit(c.device_name)
        self.label.setPlaceholderText("برچسبی که دیگران برای این دستگاه می‌بینند")
        self.dl = QLineEdit(c.download_dir)
        self.dl.setPlaceholderText("پیش‌فرض")
        browse = QPushButton("انتخاب…")
        browse.clicked.connect(lambda: self.dl.setText(
            QFileDialog.getExistingDirectory(self, "پوشه‌ی فایل‌های دریافتی") or self.dl.text()))
        row = QHBoxLayout()
        row.addWidget(self.dl, 1)
        row.addWidget(browse)
        self.maxmb = self._spin(1, 100000, c.max_file_mb)
        self.quality = self._spin(10, 95, c.jpeg_quality)
        self.fps = self._spin(1, 60, c.fps)
        self.codec = QComboBox()
        self.codec.addItems(["jpeg", "webp"])
        self.codec.setCurrentText(c.codec)
        f.addRow("برچسب این دستگاه:", self.label)
        f.addRow("پوشه‌ی دریافت فایل:", row)
        f.addRow("حداکثر اندازه‌ی فایل (MB):", self.maxmb)
        f.addRow("کیفیت تصویر:", self.quality)
        f.addRow("فریم بر ثانیه:", self.fps)
        f.addRow("کدک:", self.codec)
        return w

    def _security(self) -> QWidget:
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        c = self.cfg
        self.perm_on = QCheckBox("رمز دائمی فعال باشد (دسترسی بدون حضور)")
        self.perm_on.setChecked(c.permanent_enabled)
        self.perm = QLineEdit(c.password)
        gen = QPushButton("ساخت رمز تصادفی")
        gen.clicked.connect(lambda: self.perm.setText(security.generate_password(12)))
        row = QHBoxLayout()
        row.addWidget(self.perm, 1)
        row.addWidget(gen)
        self.ttl = self._spin(1, 1440, c.otp_ttl_minutes)
        self.otp_len = self._spin(6, 16, c.otp_length)
        self.confirm = QCheckBox("برای هر اتصال از من تأیید بگیر")
        self.confirm.setChecked(c.confirm_connections)
        self.control = QCheckBox("اجازه‌ی کنترل ماوس و کیبورد (خاموش = فقط تماشا)")
        self.control.setChecked(c.allow_control)
        self.files = QCheckBox("اجازه‌ی انتقال فایل")
        self.files.setChecked(c.allow_files)
        self.clip = QCheckBox("اشتراک کلیپ‌بورد")
        self.clip.setChecked(c.share_clipboard)
        chg = QPushButton("تغییر رمز تنظیمات…")
        chg.clicked.connect(self._change_lock)
        f.addRow(self.perm_on)
        f.addRow("رمز دائمی:", row)
        f.addRow("اعتبار رمز یکبارمصرف (دقیقه):", self.ttl)
        f.addRow("طول رمز یکبارمصرف:", self.otp_len)
        f.addRow(self.confirm)
        f.addRow(self.control)
        f.addRow(self.files)
        f.addRow(self.clip)
        f.addRow(chg)
        note = QLabel("رمز یکبارمصرف فقط برای یک ورود موفق معتبر است و پس از مدت تعیین‌شده منقضی می‌شود.")
        note.setWordWrap(True)
        f.addRow(note)
        return w

    def _network(self) -> QWidget:
        w, f = QWidget(), QFormLayout()
        w.setLayout(f)
        c = self.cfg
        self.port = self._spin(1, 65535, c.port)
        self.disc = QCheckBox("در شبکه‌ی محلی قابل کشف باشد (پیدا شدن با شناسه)")
        self.disc.setChecked(c.discoverable)
        self.relay = QLineEdit(c.relay_server)
        self.relay.setPlaceholderText("مثلاً relay.example.com — خالی = فقط شبکه‌ی محلی")
        self.relay_port = self._spin(1, 65535, c.relay_port)
        self.token = QLineEdit(c.relay_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tls = QCheckBox("اتصال امن TLS به ریلی")
        self.tls.setChecked(c.relay_tls)
        test = QPushButton("آزمایش اتصال به ریلی")
        test.clicked.connect(self._test_relay)
        self.relay_result = QLabel("")
        f.addRow("پورت TCP:", self.port)
        f.addRow(self.disc)
        f.addRow("سرور ریلی:", self.relay)
        f.addRow("پورت ریلی:", self.relay_port)
        f.addRow("توکن ریلی:", self.token)
        f.addRow(self.tls)
        f.addRow(test)
        f.addRow(self.relay_result)
        note = QLabel("برای اتصال از اینترنت، هر دو طرف باید به یک سرور ریلی (روی یک VPS) وصل باشند. "
                      "ترافیک بین دو دستگاه سرتاسری رمز می‌شود و ریلی آن را نمی‌بیند.")
        note.setWordWrap(True)
        f.addRow(note)
        return w

    def _book_tab(self) -> QWidget:
        w, lay = QWidget(), QVBoxLayout()
        w.setLayout(lay)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["شناسه / میزبان", "برچسب", "تگ‌ها (با ، جدا کنید)", "یادداشت", "پورت"])
        self.table.horizontalHeader().setStretchLastSection(True)
        for e in self.book.sorted():
            self._add_row(e)
        add = QPushButton("افزودن")
        add.clicked.connect(lambda: self._add_row(Entry("")))
        rem = QPushButton("حذف انتخاب‌شده")
        rem.clicked.connect(lambda: [self.table.removeRow(i) for i in sorted(
            {x.row() for x in self.table.selectedIndexes()}, reverse=True)])
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        lay.addWidget(self.table)
        lay.addLayout(row)
        return w

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _spin(lo, hi, val) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(val))
        return s

    def _add_row(self, e: Entry) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for col, text in enumerate((e.key, e.label, "، ".join(e.tags), e.note, str(e.port or ""))):
            self.table.setItem(r, col, QTableWidgetItem(text))

    def _change_lock(self) -> None:
        pw, ok = QInputDialog.getText(self, "رمز جدید تنظیمات", "رمز جدید:",
                                      QLineEdit.EchoMode.Password)
        if ok and pw:
            self.cfg.settings_lock = settingslock.make_hash(pw)
            self.cfg.save()
            QMessageBox.information(self, "ViunaLink", "رمز تنظیمات تغییر کرد.")

    def _test_relay(self) -> None:
        rs = RelaySettings(self.relay.text().strip(), self.relay_port.value(),
                           self.token.text(), self.tls.isChecked())
        if not rs.enabled:
            self.relay_result.setText("آدرس ریلی را وارد کنید.")
            return
        self.relay_result.setText("در حال آزمایش…")

        def work():
            async def probe() -> str:
                try:
                    await connect_via_relay(rs, "000000000", timeout=8)
                    return "ok"
                except RelayError as e:
                    return {"offline": "ok", "bad_token": "توکن اشتباه است"}.get(
                        e.code, f"خطا: {e.code}")
                except Exception as e:                    # noqa: BLE001
                    return f"اتصال ممکن نیست: {e}"
            self.relay_test_done.emit(asyncio.run(probe()))
        threading.Thread(target=work, daemon=True).start()

    def _relay_result(self, r: str) -> None:
        self.relay_result.setText("✔ ریلی در دسترس است و توکن درست است." if r == "ok" else "✘ " + r)

    # ----------------------------------------------------------------- save
    def _save(self) -> None:
        if self.perm_on.isChecked() and len(self.perm.text()) < 6:
            QMessageBox.warning(self, "ViunaLink", "رمز دائمی باید حداقل ۶ کاراکتر باشد.")
            return
        c = self.cfg
        c.device_name = self.label.text().strip() or c.device_name
        c.download_dir, c.max_file_mb = self.dl.text().strip(), self.maxmb.value()
        c.jpeg_quality, c.fps, c.codec = self.quality.value(), self.fps.value(), self.codec.currentText()
        c.permanent_enabled, c.password = self.perm_on.isChecked(), self.perm.text() or c.password
        c.otp_ttl_minutes, c.otp_length = self.ttl.value(), self.otp_len.value()
        c.confirm_connections, c.allow_control = self.confirm.isChecked(), self.control.isChecked()
        c.allow_files, c.share_clipboard = self.files.isChecked(), self.clip.isChecked()
        c.port, c.discoverable = self.port.value(), self.disc.isChecked()
        c.relay_server, c.relay_port = self.relay.text().strip(), self.relay_port.value()
        c.relay_token, c.relay_tls = self.token.text(), self.tls.isChecked()
        c.save()
        self.book.entries.clear()
        for r in range(self.table.rowCount()):
            cell = lambda i: (self.table.item(r, i).text().strip() if self.table.item(r, i) else "")
            key = cell(0).replace(" ", "")
            if key:
                tags = [t.strip() for t in cell(2).replace("،", ",").split(",") if t.strip()]
                port = int(cell(4)) if cell(4).isdigit() else 0
                self.book.entries[key] = Entry(key, cell(1), tags, cell(3), port)
        self.book.save()
        self.accept()
