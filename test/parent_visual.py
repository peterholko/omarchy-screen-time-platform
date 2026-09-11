"""Run the real Qt parent window against the real core with fixture PAM.

QT_QPA_PLATFORM=offscreen python test/parent_visual.py
Screenshots are test data, not captures from an enrolled laptop.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
from test_core import PlatformTest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("parent_app", ROOT / "parent/app.py")
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
fixture = PlatformTest(); fixture.setUp()


class Backend:
    def request(self, payload):
        time.sleep(0.15)
        return fixture.core.request(fixture.uid, payload)

    def status(self):
        path = fixture.core.run / str(fixture.uid) / "status.json"
        return json.loads(path.read_text()) if path.exists() else {"ok": True, "pin_enabled": False}


def wait(window):
    deadline = time.monotonic() + 5
    while window.busy and time.monotonic() < deadline:
        QTest.qWait(20)
    assert not window.busy, "parent request hung"
    QTest.qWait(50)


def click(window, name):
    QTest.mouseClick(window.findChild(QPushButton, name), Qt.LeftButton)


def main():
    app = QApplication([]); app.setStyle("Fusion")
    window = module.ParentWindow(Backend()); window.show(); QTest.qWait(100)
    output = ROOT / "docs/images"; output.mkdir(parents=True, exist_ok=True)
    assert not window.auth_method.model().item(1).isEnabled()
    window.password.setText("wrong password"); click(window, "signIn")
    assert "Checking" in window.feedback.text() and not window.pages.isEnabled()
    window.grab().save(str(output / "checking-password.png")); wait(window)
    assert "not accepted" in window.feedback.text() and not window.credential
    window.password.setText(" correct parent password "); QTest.keyClick(window.password, Qt.Key_Return); wait(window)
    assert window.pages.currentIndex() == 1
    window.tabs.setCurrentIndex(1); QTest.qWait(100)
    window.grab().save(str(output / "parent-controls.png"))
    window.resize(700, 600); QTest.qWait(100)
    window.grab().save(str(output / "parent-controls-compact.png"))
    window.budgets["thu"].setValue(35); click(window, "saveSettings"); wait(window)
    assert fixture.core.config()["profiles"]["default"]["budget_minutes"]["thu"] == 35
    window.tabs.setCurrentIndex(5)
    assert not window.respect_school.isChecked()
    fixture.school_status()
    window.respect_school.setChecked(True); click(window, "saveSettings"); wait(window)
    assert fixture.core.config()["profiles"]["default"]["respect_school_mode"]
    assert "School Mode is active" in window.school_status.text()
    window.resize(820, 740); QTest.qWait(100)
    window.grab().save(str(output / "school-mode.png"))
    window.resize(700, 600); QTest.qWait(100)
    window.grab().save(str(output / "school-mode-compact.png"))
    fixture.school_status("free")
    fixture.send("config.patch", patch={"respect_school_mode": True}); window.refresh_status()
    assert "Free Time is active" in window.school_status.text()
    fixture.school_status(updatedAt=fixture.now - 31)
    fixture.send("config.patch", patch={"respect_school_mode": True}); window.refresh_status()
    assert "unavailable" in window.school_status.text()
    window.respect_school.setChecked(False); click(window, "saveSettings"); wait(window)
    assert window.school_status.text() == "Connection is off."
    assert not fixture.core.config()["profiles"]["default"]["respect_school_mode"]
    window.tabs.setCurrentIndex(0); click(window, "pauseTracking"); wait(window)
    assert window.pause.text() == "Resume tracking"
    click(window, "pauseTracking"); wait(window)
    assert window.pause.text() == "Pause tracking"
    window.tabs.setCurrentIndex(3); window.resize(820, 740); QTest.qWait(100)
    window.grab().save(str(output / "connected-games.png"))
    window.credits_enabled.setChecked(True); window.credit_cap.setValue(5)
    window.game_fields["test.garden"][0].setChecked(True)
    click(window, "saveSettings"); wait(window)
    assert fixture.core.config()["profiles"]["default"]["credits"]["enabled"]
    window.tabs.setCurrentIndex(4)
    window.pin_enabled.setChecked(True); window.new_pin.setText("2468"); window.confirm_pin.setText("2468")
    window.grab().save(str(output / "parent-pin.png")); click(window, "savePin"); wait(window)
    assert window.pages.currentIndex() == 0 and not window.credential
    window.auth_method.setCurrentIndex(1); window.password.setText("2468"); click(window, "signIn"); wait(window)
    assert window.pages.currentIndex() == 1
    window.tabs.setCurrentIndex(4); window.pin_enabled.setChecked(False); click(window, "savePin"); wait(window)
    assert not fixture.core.config()["authentication"]["pin_enabled"]
    assert not window.auth_method.model().item(1).isEnabled()
    window.close(); fixture.doCleanups()
    print("Parent UI: password feedback/failure, settings, School Mode connection and statuses, pause/resume, game caps, PIN lifecycle and compact layout passed.")


if __name__ == "__main__":
    main()
