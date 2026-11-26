"""User-selected file & folder exchange with speed display and drag-drop support."""

from pathlib import Path
from PySide6.QtCore import QObject, Signal, Qt, QStandardPaths
from PySide6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QFileDialog,
)
from .file_transfer import RATE_LIMIT, RATE_PRESETS, TransferJob
from .ui_components import RoundedComboBox


def downloads():
    path = (
        Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation
            )
            or str(Path.home())
        )
        / "Brioche"
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except OSError:
        return str(Path.home())


def amount(value):
    if value >= 1024**3:
        return f"{value/1024**3:.2f} GiB"
    if value >= 1024**2:
        return f"{value/1024**2:.1f} MiB"
    return f"{value/1024:.0f} KiB"


def speed_text(bps):
    if not bps or bps <= 0:
        return ""
    if bps >= 1024**2:
        return f"{bps/1024**2:.1f} MiB/s"
    return f"{bps/1024:.0f} KiB/s"


class TransferBoard(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.empty = QLabel("文件传输任务会显示在这里。")
        self.empty.setObjectName("muted")
        layout.addWidget(self.empty)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.tasks = QVBoxLayout(content)
        self.tasks.setContentsMargins(0, 0, 8, 0)
        self.tasks.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def add(self, task_id, title, cancel):
        if task_id in self.rows:
            return
        self.empty.hide()
        card = QWidget()
        card.setObjectName("transferCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setTextFormat(Qt.TextFormat.PlainText)
        title_label.setObjectName("transferTitle")
        layout.addWidget(title_label)
        progress = QProgressBar()
        progress.setRange(0, 1000)
        progress.setValue(0)
        progress.setTextVisible(False)
        layout.addWidget(progress)
        line = QHBoxLayout()
        status = QLabel("等待确认")
        status.setObjectName("muted")
        status.setWordWrap(True)
        status.setTextFormat(Qt.TextFormat.PlainText)
        button = QPushButton("取消")
        button.clicked.connect(cancel)
        line.addWidget(status, 1)
        line.addWidget(button)
        layout.addLayout(line)
        self.tasks.insertWidget(0, card)
        self.rows[task_id] = {
            "widget": card,
            "progress": progress,
            "status": status,
            "cancel": button,
            "stop": cancel,
            "state": {},
        }
        if len(self.rows) > 30:
            for old_id, row in list(self.rows.items()):
                if row["state"].get("finished"):
                    row["widget"].deleteLater()
                    del self.rows[old_id]
                    break

    def update_task(self, value):
        row = self.rows.get(value["id"])
        if not row:
            return
        row["state"].update(value)
        state = row["state"]
        completed, total = state.get("completed", 0), state.get("total", 0)
        if total:
            row["progress"].setValue(min(1000, int(completed * 1000 / total)))
        text = state.get("status", "传输中")
        if total:
            text += f"  ·  {amount(completed)} / {amount(total)}"
        spd = speed_text(state.get("speed_bps", 0))
        if spd and not state.get("finished"):
            text += f"  ·  {spd}"
        row["status"].setText(text)
        if state.get("finished"):
            row["cancel"].hide()
            if "校验通过" in text:
                row["progress"].setValue(1000)

    def stop_all(self):
        for row in self.rows.values():
            if not row["state"].get("finished"):
                row["stop"]()


class TransferSignals(QObject):
    update = Signal(dict)


class TransferDialog(QDialog):
    started = Signal(str, str, object)
    updated = Signal(dict)

    def __init__(self, uri, parent=None):
        super().__init__(parent)
        self.uri, self.job = uri, None
        self.setWindowTitle("Brioche · 文件互传")
        self.resize(650, 480)
        self.bridge = TransferSignals(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(10)

        heading = QLabel("文件与文件夹互传")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        note = QLabel(
            "接收方确认目录后开始传输，支持文件夹递归与拖拽到远程窗口直传，逐文件 SHA-256 校验。"
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        rate_row = QHBoxLayout()
        rate_lbl = QLabel("发送速率上限")
        rate_lbl.setObjectName("muted")
        self.rate_combo = RoundedComboBox()
        for label, value in RATE_PRESETS:
            self.rate_combo.addItem(label, value)
        idx = self.rate_combo.findData(RATE_LIMIT)
        if idx >= 0:
            self.rate_combo.setCurrentIndex(idx)
        rate_row.addWidget(rate_lbl)
        rate_row.addWidget(self.rate_combo, 1)
        layout.addLayout(rate_row)

        buttons = QHBoxLayout()
        self.send = QPushButton("发送本地文件")
        self.send.setObjectName("primary")
        self.send_folder = QPushButton("发送文件夹")
        self.receive = QPushButton("请对方选择文件")
        self.send.clicked.connect(self.upload)
        self.send_folder.clicked.connect(self.upload_folder)
        self.receive.clicked.connect(self.download)
        buttons.addWidget(self.send)
        buttons.addWidget(self.send_folder)
        buttons.addWidget(self.receive)
        layout.addLayout(buttons)

        self.board = TransferBoard()
        layout.addWidget(self.board, 1)
        self.bridge.update.connect(self.result)

    def upload(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要发送的文件")
        if paths:
            self.upload_paths(paths)

    def upload_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择要发送的文件夹")
        if folder:
            self.upload_paths([folder])

    def upload_paths(self, paths):
        clean = [str(p) for p in paths if p]
        if not clean:
            return False
        first = Path(clean[0]).name
        title = (
            f"发送 {first}"
            if len(clean) == 1
            else f"发送 {first} 等 {len(clean)} 个项目"
        )
        return self.start_job("upload", clean, title)

    def download(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择接收文件的保存目录", downloads()
        )
        if folder:
            self.start_job("download", folder, "等待对方选择文件")

    def start_job(self, direction, selection, title):
        if self.job and not self.job.finished.is_set():
            return False

        def update(value):
            try:
                self.bridge.update.emit(value)
            except RuntimeError:
                pass

        rate = self.rate_combo.currentData()
        self.job = TransferJob(
            self.uri,
            direction,
            selection,
            update,
            rate=RATE_LIMIT if rate is None else rate,
        )
        self.board.add(self.job.id, title, self.job.stop)
        self.started.emit(self.job.id, title, self.job.stop)
        self.send.setEnabled(False)
        self.send_folder.setEnabled(False)
        self.receive.setEnabled(False)
        self.job.start()
        return True

    def result(self, value):
        self.updated.emit(value)
        self.board.update_task(value)
        if value.get("finished"):
            self.send.setEnabled(True)
            self.send_folder.setEnabled(True)
            self.receive.setEnabled(True)

    def stop_jobs(self):
        self.board.stop_all()
