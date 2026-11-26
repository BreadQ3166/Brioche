"""Modern dual-pane local/remote file manager with unattended folder transfer."""

from datetime import datetime
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from .file_browser import (
    RemoteFileBrowserClient,
    delete_entry,
    list_directory,
    local_roots,
    make_directory,
    rename_entry,
)
from .file_transfer import RATE_LIMIT, RATE_PRESETS, TransferJob
from .transfer_ui import TransferBoard, amount
from .ui_components import RoundedComboBox


def _fmt_time(ts):
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, TypeError):
        return "—"


class _FileTree(QTreeWidget):
    files_dropped = Signal(list)

    def __init__(self, accept_external_drops=False, parent=None):
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderLabels(["名称", "大小", "修改时间"])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSortingEnabled(False)
        self.setUniformRowHeights(True)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._accept_drops = bool(accept_external_drops)
        self.setAcceptDrops(self._accept_drops)

    def dragEnterEvent(self, event):
        if self._accept_drops and event.mimeData().hasUrls():
            if any(u.isLocalFile() for u in event.mimeData().urls()):
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._accept_drops and event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self._accept_drops and event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile() and u.toLocalFile()]
            if paths:
                event.acceptProposedAction()
                self.files_dropped.emit(paths)
                return
        super().dropEvent(event)


class _Bridge(QObject):
    remote_ready = Signal(dict)
    remote_error = Signal(str)
    remote_listed = Signal(dict)
    remote_op_done = Signal(str, str)
    transfer_update = Signal(dict)


class FileManagerWindow(QDialog):
    """Dual-pane file manager: browse local and remote folders side-by-side and transfer directly."""

    started = Signal(str, str, object)
    updated = Signal(dict)

    def __init__(self, uri, remote_name="远端电脑", parent=None):
        super().__init__(parent)
        self.uri = uri
        self.remote_name = remote_name or "远端电脑"
        self.setWindowTitle(f"Brioche · 文件管理器 · {self.remote_name}")
        self.resize(1120, 720)
        self.setMinimumSize(840, 540)

        self._closed = False
        self._client = None
        self._job = None
        self._last_direction = None
        self._local_path = str(Path.home())
        self._local_parent = ""
        self._remote_path = ""
        self._remote_parent = ""
        self._remote_connected = False

        self.bridge = _Bridge(self)
        self.bridge.remote_ready.connect(self._on_remote_ready)
        self.bridge.remote_error.connect(self._on_remote_error)
        self.bridge.remote_listed.connect(self._on_remote_listed)
        self.bridge.remote_op_done.connect(self._on_remote_op_done)
        self.bridge.transfer_update.connect(self._on_transfer_update)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        top.setSpacing(10)
        title = QLabel(f"文件管理器 · {self.remote_name}")
        title.setObjectName("subheading")
        top.addWidget(title)
        self.status_label = QLabel("正在连接远端文件夹…")
        self.status_label.setObjectName("muted")
        top.addWidget(self.status_label, 1)

        rate_lbl = QLabel("传输速率")
        rate_lbl.setObjectName("muted")
        self.rate_combo = RoundedComboBox()
        for label, value in RATE_PRESETS:
            self.rate_combo.addItem(label, value)
        idx = self.rate_combo.findData(RATE_LIMIT)
        if idx >= 0:
            self.rate_combo.setCurrentIndex(idx)
        top.addWidget(rate_lbl)
        top.addWidget(self.rate_combo)

        self.refresh_all_btn = QPushButton("刷新两端")
        self.refresh_all_btn.clicked.connect(self.refresh_both)
        top.addWidget(self.refresh_all_btn)
        root.addLayout(top)

        # Dual panes
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_local_pane())
        splitter.addWidget(self._build_remote_pane())
        splitter.setSizes([540, 540])
        root.addWidget(splitter, 3)

        # Bottom transfer queue
        queue_card = QFrame()
        queue_card.setObjectName("surfaceCard")
        q_layout = QVBoxLayout(queue_card)
        q_layout.setContentsMargins(14, 10, 14, 10)
        q_layout.setSpacing(6)
        q_head = QLabel("传输队列（支持多文件与文件夹递归 · SHA-256 校验 · 支持直接拖入右侧远端目录）")
        q_head.setObjectName("sectionLabel")
        q_layout.addWidget(q_head)
        self.board = TransferBoard()
        self.board.setMaximumHeight(165)
        q_layout.addWidget(self.board)
        root.addWidget(queue_card, 1)

        self._load_local(self._local_path)
        self._connect_remote()

    # ------------------------------------------------------------------
    # Pane builders
    # ------------------------------------------------------------------
    def _build_local_pane(self):
        card = QFrame()
        card.setObjectName("surfaceCard")
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)

        head = QHBoxLayout()
        lbl = QLabel("本机电脑")
        lbl.setObjectName("subheading")
        head.addWidget(lbl)
        self.local_root_combo = RoundedComboBox()
        for item in local_roots():
            self.local_root_combo.addItem(item["name"], item["path"])
        self.local_root_combo.activated.connect(
            lambda i: self._load_local(self.local_root_combo.itemData(i))
        )
        head.addWidget(self.local_root_combo, 1)
        up_btn = QPushButton("↑ 上级")
        up_btn.clicked.connect(lambda: self._local_parent and self._load_local(self._local_parent))
        head.addWidget(up_btn)
        col.addLayout(head)

        path_row = QHBoxLayout()
        self.local_path_edit = QLineEdit(self._local_path)
        self.local_path_edit.returnPressed.connect(lambda: self._load_local(self.local_path_edit.text().strip()))
        go_btn = QPushButton("前往")
        go_btn.clicked.connect(lambda: self._load_local(self.local_path_edit.text().strip()))
        path_row.addWidget(self.local_path_edit, 1)
        path_row.addWidget(go_btn)
        col.addLayout(path_row)

        self.local_tree = _FileTree(accept_external_drops=False)
        self.local_tree.itemDoubleClicked.connect(self._on_local_double_click)
        col.addWidget(self.local_tree, 1)

        actions = QHBoxLayout()
        mkdir_btn = QPushButton("新建文件夹")
        mkdir_btn.clicked.connect(self._local_mkdir)
        rename_btn = QPushButton("重命名")
        rename_btn.clicked.connect(self._local_rename)
        self.upload_btn = QPushButton("发送到远端 →")
        self.upload_btn.setObjectName("primary")
        self.upload_btn.setEnabled(False)
        self.upload_btn.clicked.connect(self.upload_selected)
        actions.addWidget(mkdir_btn)
        actions.addWidget(rename_btn)
        actions.addStretch()
        actions.addWidget(self.upload_btn)
        col.addLayout(actions)
        return card

    def _build_remote_pane(self):
        card = QFrame()
        card.setObjectName("surfaceCard")
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 12, 14, 12)
        col.setSpacing(8)

        head = QHBoxLayout()
        lbl = QLabel(f"远端：{self.remote_name}")
        lbl.setObjectName("subheading")
        head.addWidget(lbl)
        self.remote_root_combo = RoundedComboBox()
        self.remote_root_combo.setEnabled(False)
        self.remote_root_combo.activated.connect(
            lambda i: self._load_remote(self.remote_root_combo.itemData(i))
        )
        head.addWidget(self.remote_root_combo, 1)
        self.remote_up_btn = QPushButton("↑ 上级")
        self.remote_up_btn.setEnabled(False)
        self.remote_up_btn.clicked.connect(
            lambda: self._remote_parent and self._load_remote(self._remote_parent)
        )
        head.addWidget(self.remote_up_btn)
        col.addLayout(head)

        path_row = QHBoxLayout()
        self.remote_path_edit = QLineEdit()
        self.remote_path_edit.setPlaceholderText("正在读取远端目录…")
        self.remote_path_edit.setEnabled(False)
        self.remote_path_edit.returnPressed.connect(
            lambda: self._load_remote(self.remote_path_edit.text().strip())
        )
        self.remote_go_btn = QPushButton("前往")
        self.remote_go_btn.setEnabled(False)
        self.remote_go_btn.clicked.connect(
            lambda: self._load_remote(self.remote_path_edit.text().strip())
        )
        path_row.addWidget(self.remote_path_edit, 1)
        path_row.addWidget(self.remote_go_btn)
        col.addLayout(path_row)

        self.remote_tree = _FileTree(accept_external_drops=True)
        self.remote_tree.itemDoubleClicked.connect(self._on_remote_double_click)
        self.remote_tree.files_dropped.connect(self.upload_paths)
        col.addWidget(self.remote_tree, 1)

        actions = QHBoxLayout()
        self.download_btn = QPushButton("← 下载到本机")
        self.download_btn.setObjectName("primary")
        self.download_btn.setEnabled(False)
        self.download_btn.clicked.connect(self.download_selected)
        self.remote_mkdir_btn = QPushButton("新建文件夹")
        self.remote_mkdir_btn.setEnabled(False)
        self.remote_mkdir_btn.clicked.connect(self._remote_mkdir)
        self.remote_rename_btn = QPushButton("重命名")
        self.remote_rename_btn.setEnabled(False)
        self.remote_rename_btn.clicked.connect(self._remote_rename)
        self.remote_delete_btn = QPushButton("删除")
        self.remote_delete_btn.setObjectName("danger")
        self.remote_delete_btn.setEnabled(False)
        self.remote_delete_btn.clicked.connect(self._remote_delete)
        actions.addWidget(self.download_btn)
        actions.addStretch()
        actions.addWidget(self.remote_mkdir_btn)
        actions.addWidget(self.remote_rename_btn)
        actions.addWidget(self.remote_delete_btn)
        col.addLayout(actions)
        return card

    # ------------------------------------------------------------------
    # Local directory operations
    # ------------------------------------------------------------------
    def _populate_tree(self, tree, entries):
        tree.clear()
        for entry in entries:
            prefix = "📁 " if entry["is_dir"] else "📄 "
            size_str = "—" if entry["is_dir"] else amount(entry["size"])
            item = QTreeWidgetItem([prefix + entry["name"], size_str, _fmt_time(entry.get("mtime"))])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            tree.addTopLevelItem(item)

    def _load_local(self, path):
        try:
            data = list_directory(path)
        except Exception as exc:
            self.status_label.setText(str(exc))
            return
        self._local_path = data["path"]
        self._local_parent = data["parent"]
        self.local_path_edit.setText(self._local_path)
        self._populate_tree(self.local_tree, data["entries"])

    def _on_local_double_click(self, item, _col):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry and entry.get("is_dir"):
            self._load_local(entry["path"])

    def _local_mkdir(self):
        name, ok = QInputDialog.getText(self, "新建本机文件夹", "文件夹名称：")
        if ok and name.strip():
            try:
                make_directory(self._local_path, name.strip())
                self._load_local(self._local_path)
            except Exception as exc:
                self.status_label.setText(str(exc))

    def _local_rename(self):
        items = self.local_tree.selectedItems()
        if len(items) != 1:
            self.status_label.setText("请选择单个本机文件或文件夹进行重命名")
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        new_name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=entry["name"])
        if ok and new_name.strip() and new_name.strip() != entry["name"]:
            try:
                rename_entry(entry["path"], new_name.strip())
                self._load_local(self._local_path)
            except Exception as exc:
                self.status_label.setText(str(exc))

    # ------------------------------------------------------------------
    # Remote directory operations (async worker thread)
    # ------------------------------------------------------------------
    def _connect_remote(self):
        def run():
            client = RemoteFileBrowserClient(self.uri)
            try:
                info = client.start()
                self._client = client
                if not self._closed:
                    self.bridge.remote_ready.emit(info)
            except Exception as exc:
                client.close()
                if not self._closed:
                    self.bridge.remote_error.emit(str(exc)[:220])

        threading.Thread(target=run, name="brioche-fm-connect", daemon=True).start()

    def _on_remote_ready(self, info):
        self._remote_connected = True
        self.remote_root_combo.clear()
        for item in info.get("roots", []):
            self.remote_root_combo.addItem(item["name"], item["path"])
        for w in (
            self.remote_root_combo,
            self.remote_up_btn,
            self.remote_path_edit,
            self.remote_go_btn,
            self.upload_btn,
            self.download_btn,
            self.remote_mkdir_btn,
            self.remote_rename_btn,
            self.remote_delete_btn,
        ):
            w.setEnabled(True)
        self.status_label.setText(f"已连接远端文件系统（{info.get('platform', '桌面')}）")
        self._load_remote(info.get("home", ""))

    def _on_remote_error(self, message):
        self.status_label.setText(message)
        self.remote_path_edit.setPlaceholderText(message)

    def _load_remote(self, path):
        if not self._client or self._closed:
            return
        self.status_label.setText("正在读取远端目录…")

        def run():
            try:
                data = self._client.list_dir(path)
                if not self._closed:
                    self.bridge.remote_listed.emit(data)
            except Exception as exc:
                if not self._closed:
                    self.bridge.remote_error.emit(str(exc)[:220])

        threading.Thread(target=run, name="brioche-fm-list", daemon=True).start()

    def _on_remote_listed(self, data):
        self._remote_path = data["path"]
        self._remote_parent = data.get("parent", "")
        self.remote_path_edit.setText(self._remote_path)
        self._populate_tree(self.remote_tree, data.get("entries", []))
        self.status_label.setText(f"远端目录已就绪 · 共 {len(data.get('entries', []))} 项")

    def _on_remote_double_click(self, item, _col):
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry and entry.get("is_dir"):
            self._load_remote(entry["path"])

    def _run_remote_op(self, label, fn):
        if not self._client or self._closed:
            return

        def run():
            try:
                fn()
                if not self._closed:
                    self.bridge.remote_op_done.emit(label, "")
            except Exception as exc:
                if not self._closed:
                    self.bridge.remote_op_done.emit(label, str(exc)[:220])

        threading.Thread(target=run, name="brioche-fm-op", daemon=True).start()

    def _on_remote_op_done(self, label, error):
        if error:
            self.status_label.setText(f"{label}失败：{error}")
        else:
            self.status_label.setText(f"{label}完成")
            self._load_remote(self._remote_path)

    def _remote_mkdir(self):
        name, ok = QInputDialog.getText(self, "新建远端文件夹", "文件夹名称：")
        if ok and name.strip():
            self._run_remote_op(
                "新建远端文件夹", lambda: self._client.mkdir(self._remote_path, name.strip())
            )

    def _remote_rename(self):
        items = self.remote_tree.selectedItems()
        if len(items) != 1:
            self.status_label.setText("请选择单个远端文件或文件夹进行重命名")
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        new_name, ok = QInputDialog.getText(self, "重命名远端项目", "新名称：", text=entry["name"])
        if ok and new_name.strip() and new_name.strip() != entry["name"]:
            self._run_remote_op(
                "重命名", lambda: self._client.rename(entry["path"], new_name.strip())
            )

    def _remote_delete(self):
        items = self.remote_tree.selectedItems()
        if len(items) != 1:
            self.status_label.setText("每次仅支持删除单个远端文件或空文件夹")
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        answer = QMessageBox.question(
            self,
            "确认删除远端项目",
            f"确定删除远端 {entry['name']}？此操作不可撤销。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._run_remote_op("删除", lambda: self._client.delete(entry["path"]))

    def refresh_both(self):
        self._load_local(self._local_path)
        if self._remote_connected:
            self._load_remote(self._remote_path)

    # ------------------------------------------------------------------
    # Direct unattended bidirectional transfers
    # ------------------------------------------------------------------
    def _set_transfer_busy(self, busy):
        enabled = self._remote_connected and not busy
        self.upload_btn.setEnabled(enabled)
        self.download_btn.setEnabled(enabled)

    def upload_selected(self):
        items = self.local_tree.selectedItems()
        if not items:
            self.status_label.setText("请先在左侧选择要发送的本机文件或文件夹")
            return
        paths = [it.data(0, Qt.ItemDataRole.UserRole)["path"] for it in items]
        self.upload_paths(paths)

    def upload_paths(self, paths):
        clean = [str(p) for p in paths if p]
        if not clean:
            return False
        if self._job and not self._job.finished.is_set():
            self.status_label.setText("已有传输任务正在进行，请等待完成")
            return False
        first = Path(clean[0]).name
        title = (
            f"发送 {first} → {self._remote_path or '远端'}"
            if len(clean) == 1
            else f"发送 {first} 等 {len(clean)} 项 → {self._remote_path or '远端'}"
        )
        return self._start_job(
            "upload",
            clean,
            title,
            target_dir=self._remote_path if self._remote_connected and self._remote_path else None,
        )

    def download_selected(self):
        items = self.remote_tree.selectedItems()
        if not items:
            self.status_label.setText("请先在右侧选择要下载的远端文件或文件夹")
            return False
        if self._job and not self._job.finished.is_set():
            self.status_label.setText("已有传输任务正在进行，请等待完成")
            return False
        remote_paths = [it.data(0, Qt.ItemDataRole.UserRole)["path"] for it in items]
        first = items[0].data(0, Qt.ItemDataRole.UserRole)["name"]
        title = (
            f"下载 {first} → {self._local_path}"
            if len(remote_paths) == 1
            else f"下载 {first} 等 {len(remote_paths)} 项 → {self._local_path}"
        )
        return self._start_job(
            "download",
            self._local_path,
            title,
            remote_paths=remote_paths,
        )

    def _start_job(self, direction, selection, title, target_dir=None, remote_paths=None):
        def update(value):
            try:
                self.bridge.transfer_update.emit(value)
            except RuntimeError:
                pass

        rate = self.rate_combo.currentData()
        self._last_direction = direction
        self._job = TransferJob(
            self.uri,
            direction,
            selection,
            update,
            rate=RATE_LIMIT if rate is None else rate,
            target_dir=target_dir,
            remote_paths=remote_paths,
        )
        self.board.add(self._job.id, title, self._job.stop)
        self.started.emit(self._job.id, title, self._job.stop)
        self._set_transfer_busy(True)
        self._job.start()
        return True

    def _on_transfer_update(self, value):
        self.updated.emit(value)
        self.board.update_task(value)
        if value.get("finished"):
            self._set_transfer_busy(False)
            if self._last_direction == "upload" and self._remote_connected:
                self._load_remote(self._remote_path)
            elif self._last_direction == "download":
                self._load_local(self._local_path)

    def stop_jobs(self):
        self.board.stop_all()

    def closeEvent(self, event):
        self._closed = True
        self.stop_jobs()
        if self._client:
            self._client.close()
            self._client = None
        super().closeEvent(event)
