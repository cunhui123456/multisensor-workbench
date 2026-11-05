"""Reusable non-modal experiment help, independent of the acquisition layout."""
from PySide6 import QtCore, QtWidgets


class ExperimentHelpDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setModal(False)
        self.resize(620, 340)
        self.setMinimumSize(440, 260)
        layout = QtWidgets.QVBoxLayout(self)
        self.heading = QtWidgets.QLabel()
        self.heading.setStyleSheet('font-size: 17px; font-weight: 600;')
        self.heading.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.heading)
        self.body = QtWidgets.QTextBrowser()
        self.body.setOpenExternalLinks(False)
        layout.addWidget(self.body, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setText('关闭')
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def set_content(self, name, text):
        self.setWindowTitle(name + ' · 采集说明')
        self.heading.setText(name + ' · 采集流程与说明')
        if self.body.toPlainText() != text:
            self.body.setPlainText(text)
