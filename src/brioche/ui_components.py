"""Small vector-painted controls. Animations run only during interaction."""

import time
from PySide6.QtCore import Qt, QRectF, QSize, QVariantAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QFont, QLinearGradient
from PySide6.QtWidgets import (
    QComboBox,
    QListView,
    QListWidget,
    QStyledItemDelegate,
    QAbstractItemView,
    QFrame,
    QGraphicsOpacityEffect,
)
from PySide6.QtWidgets import QStyle


class RoundedComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        view = QListView()
        view.setObjectName("comboPopup")
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setView(view)
        view.setItemDelegate(ComboDelegate(view))
        view.setUniformItemSizes(True)
        popup = view.window()
        popup.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        popup.setStyleSheet(
            "QFrame { background: transparent; border: none; } QListView { background: #fffdf9; border: 1px solid #e4ded4; border-radius: 12px; padding: 6px; outline: none; } QListView::item { padding: 9px 12px; border-radius: 7px; } QListView::item:selected { background: #f0dfcd; color: #8c4d2c; }"
        )


class ComboDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(super().sizeHint(option,index).width(),36)


class DeviceDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        live = bool(data.get("online") or data.get("reachable"))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        key = data.get("key")
        hover = self.parent().hover if key == self.parent().hover_key else 0
        fresh = self.parent().entries.get(key)
        opacity = min(1.0, (time.monotonic() - fresh) / 0.25) if fresh else 1.0
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(opacity)
        rect = QRectF(option.rect).adjusted(3, 5 - hover * 3, -3, -5 - hover * 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(72, 47, 22, int(9 + hover * 7)))
        painter.drawRoundedRect(rect.translated(0, 3), 17, 17)
        painter.setBrush(QColor("#fffdf9" if live else "#f5f2ec"))
        painter.setPen(
            QPen(QColor("#be8559" if selected or hover > 0.2 else "#e8e1d7"), 1)
        )
        painter.drawRoundedRect(rect, 17, 17)
        screen = QRectF(rect.left() + 22, rect.top() + 20, rect.width() - 44, 94)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#363d42" if live else "#92948f"))
        painter.drawRoundedRect(screen, 8, 8)
        inside = screen.adjusted(5, 5, -5, -9)
        gradient = QLinearGradient(inside.topLeft(), inside.bottomRight())
        gradient.setColorAt(0, QColor("#e6c9a8" if live else "#d9d8d0"))
        gradient.setColorAt(1, QColor("#98b2b3" if live else "#c9cbc5"))
        painter.setBrush(gradient)
        painter.drawRoundedRect(inside, 4, 4)
        painter.setBrush(QColor(255, 255, 255, 165))
        window = inside.adjusted(17, 13, -17, -14)
        painter.drawRoundedRect(window, 5, 5)
        painter.setBrush(QColor("#c5946d" if live else "#b8b6aa"))
        painter.drawRoundedRect(
            QRectF(window.left() + 7, window.top() + 7, 26, 4), 2, 2
        )
        painter.setBrush(QColor("#d7dbd6"))
        painter.drawRoundedRect(
            QRectF(
                window.left() + 7, window.top() + 17, max(10, window.width() - 20), 3
            ),
            1,
            1,
        )
        painter.setBrush(QColor("#515959" if live else "#9c9e97"))
        painter.drawRoundedRect(
            QRectF(screen.center().x() - 5, screen.bottom() - 1, 10, 9), 2, 2
        )
        painter.drawRoundedRect(
            QRectF(screen.center().x() - 25, screen.bottom() + 7, 50, 4), 2, 2
        )
        painter.setBrush(QColor("#4f9d7d" if live else "#a2a19b"))
        painter.drawEllipse(QRectF(rect.right() - 29, rect.top() + 17, 9, 9))
        painter.setPen(QColor("#303632" if live else "#85877f"))
        font = QFont(option.font)
        font.setPointSize(11)
        font.setBold(True)
        painter.setFont(font)
        label = data.get("display_name", data.get("name", "电脑"))
        painter.drawText(
            QRectF(rect.left() + 18, rect.top() + 133, rect.width() - 36, 25),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideRight, int(rect.width() - 36)
            ),
        )
        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#568c71" if data.get("connectable") else "#898b82"))
        status = data.get("status_text", "")
        painter.drawText(
            QRectF(rect.left() + 18, rect.top() + 160, rect.width() - 36, 20),
            Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(
                status, Qt.TextElideMode.ElideRight, int(rect.width() - 36)
            ),
        )
        painter.setPen(QColor("#9a9d94"))
        painter.drawText(
            QRectF(rect.left() + 18, rect.top() + 182, rect.width() - 36, 18),
            Qt.AlignmentFlag.AlignVCenter,
            str(data.get("ip", "")),
        )
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(260, 220)


class DeviceList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("deviceList")
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setSpacing(8)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(DeviceDelegate(self))
        self.hover, self.hover_key, self.entries = 0.0, None, {}
        self.animations = True
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(160)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._motion)
        self.entered.connect(self._entered)
        self.fade_timer = QTimer(self)
        self.fade_timer.setInterval(16)
        self.fade_timer.timeout.connect(self._fade)

    def _motion(self, value):
        self.hover = float(value)
        self.viewport().update()

    def _entered(self, index):
        key = (index.data(Qt.ItemDataRole.UserRole) or {}).get("key")
        if key != self.hover_key:
            self.hover_key = key
            self.hover = 0
            self.motion.stop()
            if self.animations:
                self.motion.setStartValue(0.0)
                self.motion.setEndValue(1.0)
                self.motion.start()
            else:
                self.hover = 1.0
                self.viewport().update()

    def leaveEvent(self, event):
        self.motion.stop()
        if self.animations:
            self.motion.setStartValue(self.hover)
            self.motion.setEndValue(0.0)
            self.motion.start()
        else:
            self.hover = 0.0
            self.viewport().update()
        super().leaveEvent(event)

    def new_entry(self, key):
        if self.animations:
            self.entries[key] = time.monotonic()
            self.fade_timer.start()

    def _fade(self):
        now = time.monotonic()
        self.entries = {
            key: stamp for key, stamp in self.entries.items() if now - stamp < 0.3
        }
        self.viewport().update()
        if not self.entries:
            self.fade_timer.stop()

    def set_animations(self, enabled):
        self.animations = enabled
        if not enabled:
            self.motion.stop()
            self.fade_timer.stop()
            self.entries.clear()
            self.hover = 0.0
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(250, self.viewport().width() - 18)
        columns = max(1, min(3, width // 260))
        self.setGridSize(QSize(max(240, (width - 16 * (columns - 1)) // columns), 220))
        for index in range(self.count()):
            self.item(index).setSizeHint(self.gridSize() - QSize(4, 4))


class PageTransition:
    def __init__(self, stack):
        self.stack = stack
        self.animation = QVariantAnimation(stack)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.effect = None
        self.animation.valueChanged.connect(self._value)
        self.animation.finished.connect(self._finish)

    def show(self, index, animate=True):
        self.animation.stop()
        self._finish()
        if self.stack.currentIndex() == index:
            return
        self.stack.setCurrentIndex(index)
        if animate and self.stack.isVisible():
            self.effect = QGraphicsOpacityEffect(self.stack)
            self.stack.setGraphicsEffect(self.effect)
            self.animation.setStartValue(0.35)
            self.animation.setEndValue(1.0)
            self.animation.start()

    def _value(self, value):
        if self.effect:
            self.effect.setOpacity(float(value))

    def _finish(self):
        if self.effect:
            self.stack.setGraphicsEffect(None)
            self.effect = None
