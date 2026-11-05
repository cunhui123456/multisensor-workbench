"""Review deleted records and restore selections; no filesystem work in Qt slots."""
from PySide6 import QtCore, QtWidgets


def record_name(row):
    name = row.get('sample_name') or row.get('label') or ''
    return str(name if name and name != '—' else row.get('key', '未命名记录'))


class ArchiveDeleteDialog(QtWidgets.QDialog):
    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle('删除所选实验记录')
        self.resize(620, 420)
        layout = QtWidgets.QVBoxLayout(self)
        explanation = QtWidgets.QLabel(f'将删除 {len(rows)} 条记录，并移入“已删除记录”。完整记录保留，可随时恢复。')
        explanation.setWordWrap(True)
        explanation.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(explanation)
        self.records = QtWidgets.QListWidget()
        for row in rows:
            conditions = str(row.get('condition_summary') or '').strip()
            summary = f"\n{conditions}" if conditions and conditions != '—' else ''
            self.records.addItem(f"{record_name(row)}{summary}\n{row['key']}")
        layout.addWidget(self.records, 1)
        buttons = QtWidgets.QDialogButtonBox()
        cancel = buttons.addButton('取消', QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setDefault(True)
        buttons.addButton('移入已删除记录', QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ArchiveTrashDialog(QtWidgets.QDialog):
    refresh_requested = QtCore.Signal()
    restore_requested = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('已删除记录')
        self.resize(880, 460)
        self._busy = False
        self._blocked = False
        self.records = []
        layout = QtWidgets.QVBoxLayout(self)
        self.status = QtWidgets.QLabel('记录保留在本机；勾选后可恢复到原实验档案。')
        self.status.setWordWrap(True)
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['选择', '名称', '记录 ID', '类别', '删除时间（本机时区）'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self._update_actions)
        layout.addWidget(self.table, 1)
        actions = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton('刷新')
        self.refresh_button.clicked.connect(self.refresh_requested)
        self.restore_button = QtWidgets.QPushButton('恢复所选记录')
        self.restore_button.clicked.connect(lambda: self.restore_requested.emit(self.selected_ids()))
        close = QtWidgets.QPushButton('关闭')
        close.clicked.connect(self.close)
        actions.addWidget(self.refresh_button)
        actions.addStretch()
        actions.addWidget(self.restore_button)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._update_actions()

    def selected_ids(self):
        return [self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
                for row in range(self.table.rowCount())
                if self.table.item(row, 0).checkState() == QtCore.Qt.CheckState.Checked]

    def set_records(self, rows):
        self.records = list(rows)
        with QtCore.QSignalBlocker(self.table):
            self.table.setRowCount(len(rows))
            for index, row in enumerate(rows):
                checked = QtWidgets.QTableWidgetItem()
                checked.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
                                 | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                checked.setCheckState(QtCore.Qt.CheckState.Unchecked)
                checked.setData(QtCore.Qt.ItemDataRole.UserRole, row['trash_id'])
                self.table.setItem(index, 0, checked)
                for column, value in enumerate((record_name(row), row['key'], row.get('category_label', row.get('category', '')),
                                                row.get('deleted_local', row.get('deleted_utc', ''))), 1):
                    self.table.setItem(index, column, QtWidgets.QTableWidgetItem(str(value)))
        self.status.setText(f'共 {len(rows)} 条已删除记录，可勾选恢复。' if rows else '没有已删除记录。')
        self._update_actions()

    def set_busy(self, busy, *, blocked=False):
        self._busy, self._blocked = bool(busy), bool(blocked)
        self._update_actions()

    def _update_actions(self, *_):
        idle = not self._busy and not self._blocked
        self.table.setEnabled(idle)
        self.refresh_button.setEnabled(idle)
        self.restore_button.setEnabled(idle and bool(self.selected_ids()))
