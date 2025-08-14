from __future__ import annotations
from PySide6.QtCore import QSize, QPoint, QRect
from PySide6.QtWidgets import QLayout, QWidgetItem, QWidget

class FlowLayout(QLayout):
    """Simple flow layout for responsive wrapping cards."""
    def __init__(self, parent: QWidget | None = None, margin: int = 0, hspacing: int = 12, vspacing: int = 12):
        super().__init__(parent)
        self._h = hspacing
        self._v = vspacing
        self.setContentsMargins(margin, margin, margin, margin)
        self._items: list[QWidgetItem] = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return 0

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for item in self._items:
            s = s.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        s += QSize(m.left() + m.right(), m.top() + m.bottom())
        return s

    def _do_layout(self, rect: QRect, test_only: bool):
        x = rect.x()
        y = rect.y()
        line_height = 0
        m = self.contentsMargins()
        effective_width = rect.width() - (m.left() + m.right())
        x = rect.x() + m.left()
        y = rect.y() + m.top()
        for item in self._items:
            hint = item.sizeHint()
            w = hint.width()
            h = hint.height()
            if x + w > rect.x() + m.left() + effective_width and line_height > 0:
                x = rect.x() + m.left()
                y += line_height + self._v
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += w + self._h
            line_height = max(line_height, h)
        y += line_height
        y += m.bottom()
        return y - rect.y()
