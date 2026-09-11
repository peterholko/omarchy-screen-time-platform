"""Parent application in its own process, outside the shell's plugin tree."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout,
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QStackedWidget, QTableWidget, QTabWidget, QTimeEdit, QVBoxLayout, QWidget)


class Backend:
    def request(self, payload):
        try:
            result = subprocess.run(["/usr/bin/omarchy-peterholko-screen-time", "control"],
                input=json.dumps(payload), capture_output=True, text=True, timeout=32)
            return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"ok": False, "error": "unavailable"}

    def status(self):
        try:
            return json.loads((Path("/run/peterholko-screen-time") / str(os.getuid()) / "status.json").read_text())
        except (OSError, ValueError):
            return {"ok": False}


def label(text, name=None):
    item = QLabel(text)
    item.setWordWrap(True)
    item.setTextFormat(Qt.PlainText)
    if name:
        item.setObjectName(name)
    return item


def number(low, high, value, name):
    item = QSpinBox()
    item.setRange(low, high); item.setValue(value); item.setObjectName(name)
    return item


class ParentWindow(QMainWindow):
    def __init__(self, backend=None, page="controls"):
        super().__init__()
        self.backend = backend or Backend()
        self.page = page
        self.credential = ""
        self.method = "password"
        self.snapshot = {}
        self.busy = False
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.setWindowTitle("Screen Time — Parent controls")
        self.resize(820, 740)
        self.setMinimumSize(700, 600)
        self.setStyleSheet("QGroupBox { margin-top: 16px; padding: 12px; } QGroupBox::title { top: 2px; } QLineEdit, QSpinBox, QComboBox, QPushButton { min-height: 28px; } QTabWidget::pane { padding: 12px; }")
        container = QWidget(); outer = QVBoxLayout(container)
        outer.setContentsMargins(24, 20, 24, 20); outer.setSpacing(14)
        heading = label("Screen Time")
        font = heading.font(); font.setPointSize(24); font.setBold(True); heading.setFont(font)
        outer.addWidget(heading)
        outer.addWidget(label("Daily limits, family agreements and time from connected games."))
        self.pages = QStackedWidget(); outer.addWidget(self.pages, 1)
        self.feedback = label("", "feedback"); self.feedback.setMinimumHeight(36)
        outer.addWidget(self.feedback)
        self.setCentralWidget(container)
        self.make_auth()
        self.make_settings()
        self.poll = QTimer(self); self.poll.setInterval(40); self.poll.timeout.connect(self.collect)
        self.expiry = QTimer(self); self.expiry.setSingleShot(True); self.expiry.setInterval(300000)
        self.expiry.timeout.connect(lambda: self.sign_out("Parent settings locked. Enter your password to continue."))
        self.status_timer = QTimer(self); self.status_timer.setInterval(3000)
        self.status_timer.timeout.connect(self.refresh_status); self.status_timer.start()
        self.refresh_status()
        self.password.setFocus()

    def make_auth(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.addStretch()
        box = QGroupBox("Parent sign-in"); form = QFormLayout(box); form.setSpacing(16)
        self.auth_method = QComboBox(); self.auth_method.setObjectName("authMethod")
        self.auth_method.addItem("Parent password", "password"); self.auth_method.addItem("Parent PIN", "pin")
        self.auth_method.currentIndexChanged.connect(self.method_changed)
        self.password = QLineEdit(); self.password.setObjectName("parentPassword")
        self.password.setEchoMode(QLineEdit.Password); self.password.setMaxLength(1024)
        self.password.setPlaceholderText("Enter the parent password")
        self.password.returnPressed.connect(self.sign_in)
        self.sign_in_button = QPushButton("Open parent controls"); self.sign_in_button.setObjectName("signIn")
        self.sign_in_button.clicked.connect(self.sign_in)
        form.addRow("Use", self.auth_method); form.addRow("Password or PIN", self.password)
        form.addRow(self.sign_in_button)
        form.addRow(label("The parent password is the root/administrator password set for an Omarchy child installation. A PIN is optional and starts disabled."))
        layout.addWidget(box); layout.addStretch(); self.pages.addWidget(page)

    def method_changed(self):
        self.password.clear()
        self.password.setPlaceholderText("Enter the parent PIN" if self.auth_method.currentData() == "pin" else "Enter the parent password")

    def make_settings(self):
        page = QWidget(); layout = QVBoxLayout(page)
        top = QHBoxLayout(); top.addWidget(label("Mode"))
        self.mode = QComboBox(); self.mode.setObjectName("mode")
        self.mode.addItem("Limits — count down and enforce", "limits")
        self.mode.addItem("Agreement — record usage without locking", "together")
        top.addWidget(self.mode, 1)
        self.lock_settings = QPushButton("Lock settings"); self.lock_settings.clicked.connect(lambda: self.sign_out())
        top.addWidget(self.lock_settings); layout.addLayout(top)
        self.tabs = QTabWidget(); self.tabs.setObjectName("settingsTabs"); layout.addWidget(self.tabs, 1)
        self.make_today(); self.make_limits(); self.make_agreement(); self.make_games(); self.make_security(); self.make_school()
        bottom = QHBoxLayout()
        bottom.addWidget(label("Changes require your parent password or enabled PIN."), 1)
        self.save = QPushButton("Save settings"); self.save.setObjectName("saveSettings"); self.save.clicked.connect(self.save_settings)
        self.tabs.currentChanged.connect(lambda index: self.save.setVisible(index != 4))
        bottom.addWidget(self.save); layout.addLayout(bottom)
        self.pages.addWidget(page)

    def tab(self, title):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)
        return layout

    def make_today(self):
        layout = self.tab("Today")
        self.total = label("Loading today’s screen time…", "remaining"); layout.addWidget(self.total)
        form = QFormLayout()
        self.grant = number(-600, 600, 15, "grantMinutes")
        form.addRow("Add or remove minutes", self.grant); layout.addLayout(form)
        grant = QPushButton("Apply time change"); grant.setObjectName("grantTime")
        grant.clicked.connect(lambda: self.send({"cmd": "grant", "minutes": self.grant.value()}))
        layout.addWidget(grant)
        self.pause = QPushButton("Pause tracking"); self.pause.setObjectName("pauseTracking")
        self.pause.clicked.connect(lambda: self.send({"cmd": "resume" if self.backend.status().get("phase") == "paused" else "pause"}))
        layout.addWidget(self.pause)
        lock = QPushButton("Lock the screen"); lock.setObjectName("lockScreen")
        lock.clicked.connect(lambda: self.send({"cmd": "lock"}, message="Screen lock requested.")); layout.addWidget(lock)
        layout.addWidget(label("Limits mode follows the daily budget and blocked periods. Agreement mode records usage and notes without a countdown or budget lock."))
        layout.addStretch()

    def make_limits(self):
        layout = self.tab("Limits")
        layout.addWidget(label("Daily budget in minutes"))
        grid = QGridLayout(); self.budgets = {}
        for column, day in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun")):
            self.budgets[day] = number(0, 1440, 60, "budget-" + day)
            self.budgets[day].setMinimumWidth(60)
            grid.addWidget(label(day.title()), 0, column); grid.addWidget(self.budgets[day], 1, column)
        layout.addLayout(grid)
        layout.addWidget(label("Blocked periods — these apply in Limits mode"))
        self.periods = QTableWidget(0, 5); self.periods.setObjectName("periods")
        self.periods.setHorizontalHeaderLabels(["Label", "On", "From", "Until", ""])
        self.periods.horizontalHeader().setStretchLastSection(True); self.periods.setColumnWidth(0, 185)
        self.periods.setColumnWidth(1, 45); self.periods.setColumnWidth(2, 115); self.periods.setColumnWidth(3, 115)
        self.periods.verticalHeader().hide(); self.periods.setFixedHeight(140)
        layout.addWidget(self.periods, 1)
        add = QPushButton("Add blocked period"); add.setObjectName("addPeriod")
        add.clicked.connect(lambda: self.add_period({"label": "Break", "enabled": True, "start": "18:00", "end": "18:30"}))
        layout.addWidget(add)
        form = QFormLayout(); self.on_empty = QComboBox(); self.on_empty.setObjectName("onEmpty")
        self.on_empty.addItem("Lock the screen", "lock"); self.on_empty.addItem("Notify only", "notify")
        self.grace = number(0, 3600, 60, "graceSeconds")
        form.addRow("When time is used up", self.on_empty); form.addRow("Time to wrap up (seconds)", self.grace)
        layout.addLayout(form)

    def add_period(self, period):
        if self.periods.rowCount() >= 8:
            return
        row = self.periods.rowCount(); self.periods.insertRow(row); self.periods.setRowHeight(row, 42)
        name = QLineEdit(period["label"]); name.setMaxLength(40)
        active = QCheckBox(); active.setChecked(period["enabled"])
        self.periods.setCellWidget(row, 0, name); self.periods.setCellWidget(row, 1, active)
        for column, key in ((2, "start"), (3, "end")):
            field = QTimeEdit(QTime.fromString(period[key], "HH:mm")); field.setDisplayFormat("HH:mm")
            self.periods.setCellWidget(row, column, field)
        remove = QPushButton("Remove"); remove.clicked.connect(lambda: self.remove_period(remove))
        self.periods.setCellWidget(row, 4, remove)

    def remove_period(self, button):
        for row in range(self.periods.rowCount()):
            if self.periods.cellWidget(row, 4) is button:
                self.periods.removeRow(row); break

    def make_agreement(self):
        layout = self.tab("Agreement")
        layout.addWidget(label("Write the family agreement together. Only a parent can save changes to the agreement or its settings."))
        self.agreement = QPlainTextEdit(); self.agreement.setObjectName("agreementText")
        self.agreement.setPlaceholderText("Our family screen-time agreement (up to 500 characters)")
        layout.addWidget(self.agreement, 1)
        form = QFormLayout(); self.agreement_minutes = number(0, 1440, 0, "agreementMinutes")
        self.nudge = number(0, 480, 45, "breakNudge")
        form.addRow("Agreed minutes (0 means no target)", self.agreement_minutes)
        form.addRow("Break reminder after minutes (0 means off)", self.nudge); layout.addLayout(form)

    def make_games(self):
        layout = self.tab("Connected games")
        self.credits_enabled = QCheckBox("Allow connected games to add screen time"); self.credits_enabled.setObjectName("creditsEnabled")
        layout.addWidget(self.credits_enabled)
        form = QFormLayout(); self.credit_cap = number(0, 1440, 30, "creditDailyCap")
        form.addRow("Maximum minutes from all games per day", self.credit_cap); layout.addLayout(form)
        layout.addWidget(label("Each game verifies its own completed activities. These limits apply together with the overall daily maximum. Credits pause in Agreement mode, during blocked periods or connected School Mode, and while tracking is paused or the session is locked."))
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        self.games_body = QWidget(); self.games_layout = QVBoxLayout(self.games_body)
        scroll.setWidget(self.games_body); layout.addWidget(scroll, 1)
        self.game_fields = {}

    def make_security(self):
        layout = self.tab("Parent access")
        layout.addWidget(label("The parent password is always available. You can optionally enable a separate PIN for day-to-day screen-time settings and time grants. It does not replace the computer’s login or administrator password."))
        self.pin_enabled = QCheckBox("Enable a parent PIN"); self.pin_enabled.setObjectName("enablePin")
        layout.addWidget(self.pin_enabled)
        form = QFormLayout(); self.new_pin = QLineEdit(); self.confirm_pin = QLineEdit()
        for field, name in ((self.new_pin, "newPin"), (self.confirm_pin, "confirmPin")):
            field.setEchoMode(QLineEdit.Password); field.setMaxLength(12); field.setObjectName(name)
        form.addRow("New PIN (4–12 digits)", self.new_pin); form.addRow("Confirm PIN", self.confirm_pin)
        layout.addLayout(form)
        self.pin_save = QPushButton("Save parent PIN settings"); self.pin_save.setObjectName("savePin")
        self.pin_save.clicked.connect(self.save_pin); layout.addWidget(self.pin_save)
        layout.addWidget(label("Enabling, changing or disabling the PIN requires your current parent password or enabled PIN. Saving locks this window and asks you to sign in again."))
        layout.addStretch()

    def make_school(self):
        layout = self.tab("School Mode")
        self.respect_school = QCheckBox("Connect to the separate School Mode plugin")
        self.respect_school.setObjectName("respectSchoolMode"); layout.addWidget(self.respect_school)
        layout.addWidget(label("During School Mode, pause the free-time budget and game rewards. Free Time resumes the remaining budget. School hours also pause usage tracking and break reminders in Agreement mode."))
        layout.addWidget(label("Bedtime and other blocked periods still apply in Limits mode. Explicit parent locks still apply in both modes."))
        self.school_status = label("Connection is off.", "schoolStatus"); layout.addWidget(self.school_status)
        layout.addWidget(label("Use the School / Free Time plugin to manage the school schedule, application whitelist and password-protected Free Time. Install it separately from github.com/peterholko/omarchy-school-mode."))
        layout.addWidget(label("This connection starts off. If School Mode is unavailable or its status stops updating, normal screen-time rules apply. Saving this setting requires your parent password or enabled PIN."))
        layout.addStretch()

    def refresh_status(self):
        status = self.backend.status()
        school = status.get("school_mode", {})
        if not status.get("ok"):
            school_text = "Screen Time status is unavailable."
        elif not school.get("linked"):
            school_text = "Connection is off."
        elif not school.get("available"):
            school_text = "School Mode is unavailable for this account. Normal screen-time rules apply."
        elif school.get("active"):
            school_text = "School Mode is active. The free-time budget and game rewards are paused."
        else:
            school_text = "Free Time is active. Normal screen-time rules apply."
        self.school_status.setText(school_text)
        enabled = status.get("pin_enabled", False)
        self.auth_method.model().item(1).setEnabled(enabled)
        if not enabled and self.auth_method.currentData() == "pin":
            self.auth_method.setCurrentIndex(0)
        if status.get("ok"):
            self.total.setText(f"{status.get('remaining_seconds', 0) // 60} minutes left today\n"
                f"{status.get('spent_seconds', 0) // 60} minutes used · {status.get('credited_seconds', 0) // 60} minutes from games · {status.get('granted_seconds', 0) // 60} minutes from a parent")
            self.pause.setText("Resume tracking" if status.get("phase") == "paused" else "Pause tracking")

    def sign_in(self):
        if self.busy or not self.password.text():
            return
        self.credential = self.password.text()  # Spaces can be part of a password.
        self.method = self.auth_method.currentData(); self.password.clear()
        self.send({"cmd": "config.get"}, self.signed_in)

    def signed_in(self, result):
        self.load(result); self.pages.setCurrentIndex(1)
        self.tabs.setCurrentIndex(1 if self.page == "settings" else 0)
        self.expiry.start(); self.feedback.setText("")

    def load(self, result):
        self.snapshot = result; profile = result["profile"]
        self.mode.setCurrentIndex(1 if profile["philosophy"] == "together" else 0)
        self.respect_school.setChecked(profile.get("respect_school_mode", False))
        for day, field in self.budgets.items():
            field.setValue(profile["budget_minutes"][day])
        self.periods.setRowCount(0)
        for period in profile["blocked_periods"]:
            self.add_period(period)
        self.on_empty.setCurrentIndex(0 if profile["on_empty"] == "lock" else 1)
        self.grace.setValue(profile["grace_seconds"])
        self.agreement.setPlainText(profile["agreement_text"])
        self.agreement_minutes.setValue(profile["agreement_minutes"]); self.nudge.setValue(profile["break_nudge_minutes"])
        self.credits_enabled.setChecked(profile["credits"]["enabled"]); self.credit_cap.setValue(profile["credits"]["daily_cap_minutes"])
        self.pin_enabled.setChecked(result["pin_enabled"]); self.new_pin.clear(); self.confirm_pin.clear()
        while self.games_layout.count():
            item = self.games_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.game_fields = {}
        for key, provider in result["providers"].items():
            box = QGroupBox(provider["name"]); form = QFormLayout(box)
            policy = profile["credits"]["providers"].get(key, {"enabled": False, "seconds_per_event": 60, "daily_cap_minutes": 10})
            enabled = QCheckBox("Allow credits"); enabled.setChecked(policy["enabled"]); enabled.setObjectName(key + "-enabled")
            seconds = number(1, 3600, policy["seconds_per_event"], key + "-seconds")
            cap = number(0, 1440, policy["daily_cap_minutes"], key + "-cap")
            form.addRow(enabled); form.addRow("Seconds per " + provider["unit"], seconds); form.addRow("Maximum minutes per day", cap)
            self.game_fields[key] = (enabled, seconds, cap); self.games_layout.addWidget(box)
        if not self.game_fields:
            self.games_layout.addWidget(label("No games connected yet. A compatible game registers its verifier when you install its optional screen-time integration."))
        self.games_layout.addStretch()

    def save_settings(self):
        periods = [{"label": self.periods.cellWidget(row, 0).text(), "enabled": self.periods.cellWidget(row, 1).isChecked(),
            "start": self.periods.cellWidget(row, 2).time().toString("HH:mm"), "end": self.periods.cellWidget(row, 3).time().toString("HH:mm")}
            for row in range(self.periods.rowCount())]
        patch = {"philosophy": self.mode.currentData(), "budget_minutes": {day: field.value() for day, field in self.budgets.items()},
            "respect_school_mode": self.respect_school.isChecked(),
            "blocked_periods": periods, "on_empty": self.on_empty.currentData(), "grace_seconds": self.grace.value(),
            "agreement_text": self.agreement.toPlainText(), "agreement_minutes": self.agreement_minutes.value(), "break_nudge_minutes": self.nudge.value(),
            "credits": {"enabled": self.credits_enabled.isChecked(), "daily_cap_minutes": self.credit_cap.value(), "providers": {
                key: {"enabled": fields[0].isChecked(), "seconds_per_event": fields[1].value(), "daily_cap_minutes": fields[2].value()} for key, fields in self.game_fields.items()}}}
        self.send({"cmd": "config.patch", "patch": patch}, lambda result: self.load(result))

    def save_pin(self):
        if self.pin_enabled.isChecked() and (self.new_pin.text() != self.confirm_pin.text() or not self.new_pin.text().isascii() or not self.new_pin.text().isdigit() or not 4 <= len(self.new_pin.text()) <= 12):
            self.feedback.setText("Enter the same 4–12 digit PIN in both fields."); return
        self.send({"cmd": "pin.set", "enabled": self.pin_enabled.isChecked(), "new_pin": self.new_pin.text()},
            lambda result: self.sign_out("Parent PIN settings saved. Sign in again to continue."))

    def send(self, payload, callback=None, message="Saved."):
        if self.busy:
            return
        self.busy = True; self.callback = callback; self.success_message = message
        self.feedback.setText("Checking parent password…" if self.method == "password" else "Checking parent PIN…")
        self.pages.setEnabled(False); self.expiry.stop()
        self.sign_in_button.setText("Checking…")
        self.future = self.pool.submit(self.backend.request, {**payload, "credential": self.credential, "auth_method": self.method})
        self.poll.start()

    def collect(self):
        if not self.future or not self.future.done():
            return
        self.poll.stop(); self.busy = False; self.pages.setEnabled(True)
        self.sign_in_button.setText("Open parent controls")
        try:
            result = self.future.result()
        except Exception:
            result = {"ok": False, "error": "unavailable"}
        self.future = None
        if result.get("ok"):
            self.feedback.setText(self.success_message)
            if self.callback:
                self.callback(result)
            if self.pages.currentIndex() == 1:
                self.expiry.start()
        else:
            error = result.get("error", "unavailable")
            messages = {"bad_password": "The parent password was not accepted.", "bad_pin": "The parent PIN was not accepted.",
                "password_locked_out": f"Too many attempts. Try again in {result.get('retry_in_seconds', 1)} seconds.",
                "password_checking": "Another parent password is being checked. Try again shortly.",
                "invalid_patch": "Check the settings: " + str(result.get("field", "invalid value")),
                "not_managed": "Set up the Screen Time service for this account first.",
                "authentication_unavailable": "Parent authentication is unavailable. Check the service installation."}
            message = messages.get(error, "Could not confirm the change. Reopen settings to check the saved values.")
            if error in ("bad_password", "bad_pin", "pin_disabled", "credentials_changed", "password_locked_out"):
                self.sign_out(message)
            else:
                self.feedback.setText(message)
        self.refresh_status()
        if self.pages.currentIndex() == 1:
            self.expiry.start()

    def sign_out(self, message="Enter the parent password to open settings."):
        if self.busy:
            return
        self.expiry.stop(); self.credential = ""; self.password.clear(); self.new_pin.clear(); self.confirm_pin.clear()
        self.auth_method.setCurrentIndex(0); self.method = "password"
        self.pages.setCurrentIndex(0); self.feedback.setText(message); self.password.setFocus()

    def closeEvent(self, event):
        if self.busy:
            self.feedback.setText("Finishing the current request…"); event.ignore(); return
        self.credential = ""; self.password.clear(); self.new_pin.clear(); self.confirm_pin.clear()
        self.pool.shutdown(wait=False); super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--page", choices=("controls", "settings"), default="controls")
    args = parser.parse_args()
    app = QApplication(sys.argv); app.setApplicationName("peterholko.screen-time-parent")
    window = ParentWindow(page=args.page); window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
