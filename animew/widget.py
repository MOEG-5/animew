"""M2: the desktop widget UI (PRD F7).

A frameless, translucent, always-on-bottom dark panel pinned to the bottom
right of the primary screen. Shows a 2-column scrollable grid of cards
(vertical card image + title + episode), latest confirmed episode first,
the currently-playing episode on top with a pulsing indicator. Clicking a
card opens its MAL page; right-click offers "Re-pick from search results".
"""

from __future__ import annotations

import queue
import subprocess
import sys
import textwrap
import threading

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import config
from .auth import TokenStore
from .checker import NewContentChecker
from .mal import MALClient
from .store import (
    COLUMNS_KEY,
    IMAGE_WIDTH_KEY,
    ROWS_KEY,
    TAGS_KEY,
    THRESHOLD_KEY,
    Store,
    get_card_image_width,
    get_grid_columns,
    get_grid_rows,
    get_release_tags,
    get_watch_threshold,
    set_release_tags,
)

CARD_W = 156
IMG_W = 140
IMG_H = 200
GRID_MARGIN = 10
GRID_SPACING = 10
SCROLLBAR_W = 12
ROW_W = 2 * CARD_W + GRID_SPACING  # two cards per row

APP_CSS = """
QWidget { font-family: 'DejaVu Sans', sans-serif; font-size: 12px; color: #e8e8f0; }
QFrame#Panel { background-color: rgba(16,17,23,235); border-radius: 14px; }
QLabel#Title { font-size: 12px; font-weight: 600; color: #e8e8f0; }
QLabel#Ep { font-size: 11px; color: #9aa0b0; }
QFrame#Card { background-color: #22242e; border-radius: 10px; }
QFrame#Card:hover { background-color: #2a2d3a; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #33363f; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QDialog { background-color: #1a1c24; }
QListWidget { background-color: #22242e; border: 1px solid #33363f; border-radius: 8px; }
QPushButton { background-color: #33363f; border: none; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background-color: #3d4150; }
QToolTip { background-color: #16171d; color: #d4d6e0; border: 1px solid #33363f; padding: 6px; }
"""


def wrap_tooltip(text: str, width: int = 72, max_chars: int = 500) -> str:
    """Normalize whitespace and hard-wrap tooltip text to a readable column
    width. Qt tooltips otherwise wrap only at screen width, so short synopses
    render as a couple of giant lines."""
    text = " ".join(text.split())
    if len(text) > max_chars:
        cut = text[:max_chars]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        text = cut.rstrip(" ,.;:") + " …"
    lines = textwrap.wrap(text, width=width, break_long_words=False,
                          break_on_hyphens=False)
    return "\n".join(lines) if lines else ""


def crop_fill(pix: QPixmap, w: int, h: int) -> QPixmap:
    """Scale to cover and center-crop into w x h."""
    if pix.isNull() or pix.width() <= 0 or pix.height() <= 0:
        return pix
    scale = max(w / pix.width(), h / pix.height())
    sw, sh = int(pix.width() * scale + 0.5), int(pix.height() * scale + 0.5)
    scaled = pix.scaled(sw, sh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    x, y = (sw - w) // 2, (sh - h) // 2
    return scaled.copy(x, y, w, h)


def placeholder(w: int, h: int, text: str) -> QPixmap:
    pix = QPixmap(w, h)
    pix.fill(QColor("#22242e"))
    p = QPainter(pix)
    p.setPen(QColor("#5a5f6e"))
    f = QFont()
    f.setPointSize(28)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignCenter, (text or "?").strip()[:1].upper())
    p.end()
    return pix


class Card(QFrame):
    """One anime card: image, title, episode line. Click = MAL page,
    right-click = re-pick menu, "New!" badge = open + dismiss new content."""

    def __init__(self, info: dict, on_repick, on_new=None, on_dismiss=None,
                 img_w: int = IMG_W, img_h: int = IMG_H, card_w: int = CARD_W):
        super().__init__()
        self.info = info
        self.on_repick = on_repick
        self.on_new = on_new
        self.on_dismiss = on_dismiss
        self.img_w = img_w
        self.img_h = img_h
        self.setObjectName("Card")
        self.setFixedWidth(card_w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        self.img = QLabel()
        self.img.setFixedSize(img_w, img_h)
        self.img.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.img)

        self.title = QLabel()
        self.title.setObjectName("Title")
        self.title.setWordWrap(True)
        self.title.setFixedHeight(36)
        lay.addWidget(self.title)

        self.ep = QLabel()
        self.ep.setObjectName("Ep")
        lay.addWidget(self.ep)

        self._pulse_on = False
        self._pulse_timer: QTimer | None = None
        self._build()

    def _build(self) -> None:
        pix = QPixmap(self.info.get("image_path") or "")
        if pix.isNull():
            pix = placeholder(self.img_w, self.img_h, self.info.get("title", ""))
        self.img.setPixmap(crop_fill(pix, self.img_w, self.img_h))

        title = self.info.get("title") or ""
        self.title.setText(title)
        self.title.setToolTip(title)

        ep = self.info.get("episode")
        if (self.info.get("media_type") or "") == "movie" or (ep is None and not self.info.get("mal_status")):
            ep_text = "Movie"
        elif ep:
            ep_text = f"EP {ep:02d}"
        else:
            ep_text = "Not started"
        if self.info.get("playing"):
            ep_text = "▶ " + ep_text
        self.ep.setText(ep_text)

        synopsis = self.info.get("synopsis")
        if synopsis:
            self.setToolTip(wrap_tooltip(synopsis))

        if self.info.get("has_new"):
            self._add_badge()

        if self.info.get("playing"):
            self._pulse_timer = QTimer(self)
            self._pulse_timer.timeout.connect(self._pulse)
            self._pulse_timer.start(700)
            self._pulse()

    def _add_badge(self) -> None:
        badge = QLabel("New!", self.img)
        badge.setStyleSheet(
            "background-color: #e67e22; color: #16171d; font-weight: 700;"
            "font-size: 10px; border-radius: 8px; padding: 2px 8px;")
        badge.adjustSize()
        badge.move(self.img.width() - badge.width() - 4, 4)
        badge.mousePressEvent = self._badge_clicked
        self._badge = badge

    def _badge_clicked(self, event) -> None:
        event.accept()
        if self.on_new:
            self.on_new(self.info)

    def _pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        color = "#4ade80" if self._pulse_on else "#1f8a4d"
        self.ep.setStyleSheet(f"color: {color};")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            url = self.info.get("mal_url")
            if url:
                QDesktopServices.openUrl(QUrl(url))
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        action = menu.addAction("Re-pick from search results…")
        dismiss = None
        if self.info.get("has_new") and self.on_dismiss:
            dismiss = menu.addAction("Dismiss 'New!' badge")
        chosen = menu.exec(event.globalPos())
        if chosen is action:
            self.on_repick(self.info)
        elif chosen is dismiss and self.on_dismiss:
            self.on_dismiss(self.info)


class RePickDialog(QDialog):
    """Searches MAL in a background thread, lists candidates, returns the pick."""

    def __init__(self, mal: MALClient, title: str, parent=None):
        super().__init__(parent)
        self.mal = mal
        self.results: list[dict] = []
        self._q: queue.Queue = queue.Queue()
        self.setWindowTitle("Re-pick anime")
        lay = QVBoxLayout(self)
        self.label = QLabel(f'Searching MAL for "{title}" …')
        self.list = QListWidget()
        lay.addWidget(self.label)
        lay.addWidget(self.list)
        btns = QHBoxLayout()
        self.ok = QPushButton("Use selected")
        self.ok.setEnabled(False)
        cancel = QPushButton("Cancel")
        btns.addWidget(cancel)
        btns.addWidget(self.ok)
        lay.addLayout(btns)
        self.ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())

        threading.Thread(target=self._search, args=(title,), daemon=True).start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(100)

    def _search(self, title: str) -> None:
        try:
            self._q.put(self.mal.search(title, limit=5))
        except Exception as exc:
            self._q.put(exc)

    def _poll(self) -> None:
        try:
            result = self._q.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        if isinstance(result, Exception):
            self.label.setText(f"Search failed: {result}")
            return
        self.results = result
        if not result:
            self.label.setText("No results found.")
            return
        self.label.setText("Select the correct entry:")
        for cand in result:
            item = QListWidgetItem(f"{cand['title']}  ({cand['media_type'] or '?'})")
            item.setData(Qt.UserRole, cand)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.ok.setEnabled(True)

    def selected(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None


class AuthDialog(QDialog):
    """Runs the MAL OAuth flow: opens the browser, waits for the callback,
    returns the tokens dict via ``tokens`` when accepted."""

    def __init__(self, client_id: str, client_secret: str, callback_url: str, parent=None):
        super().__init__(parent)
        self.tokens: dict | None = None
        self._q: queue.Queue = queue.Queue()
        self.setWindowTitle("MyAnimeList authorization")
        lay = QVBoxLayout(self)
        self.label = QLabel(
            "AnimeW Widget needs permission to read and update your\n"
            "MyAnimeList list.\n\n"
            "A browser tab has opened — click Allow, then return here."
        )
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.cancel = QPushButton("Cancel")
        lay.addWidget(self.cancel)
        self.cancel.clicked.connect(self.reject)
        threading.Thread(
            target=self._authorize, args=(client_id, client_secret, callback_url),
            daemon=True,
        ).start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(150)

    def _authorize(self, client_id: str, client_secret: str, callback_url: str) -> None:
        try:
            from .auth import login
            self._q.put(login(client_id, client_secret, callback_url))
        except Exception as exc:
            self._q.put(exc)

    def _poll(self) -> None:
        try:
            result = self._q.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        if isinstance(result, Exception):
            self.label.setText(f"Authorization failed: {result}")
            self.cancel.setText("Close")
            return
        self.tokens = result
        self.accept()


class SettingsDialog(QDialog):
    """F14: release tags, watch threshold, grid columns/rows, card image size."""

    def __init__(self, parent=None, tags=None, threshold_min=10,
                 columns=2, rows=3, image_width=140):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("Release tags (empty = track nothing):"))
        self._tags = QListWidget()
        for t in tags or []:
            self._tags.addItem(t)
        lay.addWidget(self._tags)
        tag_row = QHBoxLayout()
        self._tag_input = QLineEdit()
        self._tag_input.setPlaceholderText("e.g. MyGroup")
        add_btn = QPushButton("Add")
        remove_btn = QPushButton("Remove")
        tag_row.addWidget(self._tag_input)
        tag_row.addWidget(add_btn)
        tag_row.addWidget(remove_btn)
        lay.addLayout(tag_row)
        add_btn.clicked.connect(self._add_tag)
        remove_btn.clicked.connect(self._remove_tag)
        self._tag_input.returnPressed.connect(self._add_tag)

        th_row = QHBoxLayout()
        th_row.addWidget(QLabel("Watched after (minutes):"))
        self._threshold = QSpinBox()
        self._threshold.setRange(1, 180)
        self._threshold.setValue(threshold_min)
        th_row.addWidget(self._threshold)
        th_row.addStretch()
        lay.addLayout(th_row)

        grid_row = QHBoxLayout()
        grid_row.addWidget(QLabel("Columns:"))
        self._columns = QSpinBox()
        self._columns.setRange(1, 4)
        self._columns.setValue(columns)
        grid_row.addWidget(self._columns)
        grid_row.addSpacing(14)
        grid_row.addWidget(QLabel("Visible rows:"))
        self._rows = QSpinBox()
        self._rows.setRange(1, 8)
        self._rows.setValue(rows)
        grid_row.addWidget(self._rows)
        grid_row.addStretch()
        lay.addLayout(grid_row)

        img_row = QHBoxLayout()
        img_row.addWidget(QLabel("Card image size:"))
        self._img_w = QSlider(Qt.Horizontal)
        self._img_w.setRange(100, 450)
        self._img_w.setValue(image_width)
        img_row.addWidget(self._img_w, 1)
        self._img_label = QLabel(f"{image_width} px")
        self._img_w.valueChanged.connect(lambda v: self._img_label.setText(f"{v} px"))
        img_row.addWidget(self._img_label)
        lay.addLayout(img_row)

        btns = QHBoxLayout()
        cancel = QPushButton("Cancel")
        ok = QPushButton("Save")
        btns.addWidget(cancel)
        btns.addWidget(ok)
        lay.addLayout(btns)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)

    def _add_tag(self) -> None:
        text = self._tag_input.text().strip()
        if text:
            self._tags.addItem(text)
            self._tag_input.clear()

    def _remove_tag(self) -> None:
        for item in self._tags.selectedItems():
            self._tags.takeItem(self._tags.row(item))

    def values(self) -> dict:
        tags = [self._tags.item(i).text().strip() for i in range(self._tags.count())]
        return {
            "tags": [t for t in tags if t],
            "threshold_minutes": self._threshold.value(),
            "columns": self._columns.value(),
            "rows": self._rows.value(),
            "image_width": self._img_w.value(),
        }


class Widget(QWidget):
    def __init__(self, cfg: dict, store: Store, out_queue: queue.Queue,
                 cmd_queue: queue.Queue, mal: MALClient, sync=None,
                 checker: NewContentChecker | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.Tool)
        self.cfg = cfg
        self.store = store
        self.out = out_queue
        self.cmd = cmd_queue
        self.mal = mal
        self.sync = sync
        self.checker = checker
        self.current: dict | None = None

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("AnimeW Widget")
        self.setStyleSheet(APP_CSS)

        panel = QFrame()
        panel.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(GRID_MARGIN, GRID_MARGIN, GRID_MARGIN, GRID_MARGIN)

        header = QHBoxLayout()
        header.setContentsMargins(2, 0, 2, 0)
        self._hint = QLabel("add tags here!")
        self._hint.setStyleSheet("color: #e67e22; font-size: 11px; font-weight: 600;")
        self._hint.setCursor(Qt.PointingHandCursor)
        self._hint.mousePressEvent = lambda e: self._open_settings()
        self._cog = QToolButton()
        self._cog.setText("⚙")
        self._cog.setCursor(Qt.PointingHandCursor)
        self._cog.setToolTip("Settings")
        self._cog.setStyleSheet(
            "QToolButton { color: #9aa0b0; background: transparent; border: none; font-size: 15px; }"
            "QToolButton:hover { color: #e8e8f0; }"
        )
        self._cog.clicked.connect(self._open_settings)
        header.addWidget(self._hint)
        header.addStretch()
        header.addWidget(self._cog)
        pl.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background: transparent;")
        self.grid_host.setFixedWidth(ROW_W)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GRID_SPACING)
        self.grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.grid_host)
        pl.addWidget(self.scroll)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(250)

        if sync is not None:
            self._retry_timer = QTimer(self)
            self._retry_timer.timeout.connect(self._retry_pending)
            self._retry_timer.start(5 * 60 * 1000)

        if checker is not None:
            self._check_timer = QTimer(self)
            self._check_timer.timeout.connect(self._maybe_check_new_content)
            self._check_timer.start(60 * 60 * 1000)
            QTimer.singleShot(2000, self._maybe_check_new_content)

        self._threshold = get_watch_threshold(store)
        self._update_hint()
        self._apply_layout()

    def _position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 24)

    def _apply_layout(self) -> None:
        """Recompute grid geometry and window size from settings (F14)."""
        self.cols = get_grid_columns(self.store)
        self.rows = get_grid_rows(self.store)
        self.img_w = get_card_image_width(self.store)
        self.img_h = int(self.img_w * 10 / 7)  # 2:3 aspect
        self.card_w = self.img_w + 16
        grid_w = self.cols * self.card_w + (self.cols - 1) * GRID_SPACING
        self.grid_host.setFixedWidth(grid_w)
        self.refresh()
        screen = QApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() - 120 if screen else 500
        hdr = 34
        card_h = self.img_h + 78
        w = grid_w + GRID_MARGIN * 2 + SCROLLBAR_W
        h = min(self.rows * card_h + hdr + GRID_MARGIN * 2, avail_h)
        self.resize(w, h)
        self._position()

    def _update_hint(self) -> None:
        self._hint.setVisible(not get_release_tags(self.store))

    def _open_settings(self) -> None:
        dlg = SettingsDialog(
            self,
            tags=get_release_tags(self.store),
            threshold_min=max(1, int(self._threshold // 60)),
            columns=self.cols,
            rows=self.rows,
            image_width=self.img_w,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        set_release_tags(self.store, v["tags"])
        self.store.set_setting(THRESHOLD_KEY, str(v["threshold_minutes"]))
        self.store.set_setting(COLUMNS_KEY, str(v["columns"]))
        self.store.set_setting(ROWS_KEY, str(v["rows"]))
        self.store.set_setting(IMAGE_WIDTH_KEY, str(v["image_width"]))
        self._threshold = v["threshold_minutes"] * 60.0
        self.cmd.put({"cmd": "set_tags", "tags": v["tags"]})
        self.cmd.put({"cmd": "set_threshold", "threshold": self._threshold})
        self._update_hint()
        self._apply_layout()

    # -- event loop ----------------------------------------------------------

    def _drain(self) -> None:
        changed = False
        while True:
            try:
                msg = self.out.get_nowait()
            except queue.Empty:
                break
            kind = msg["type"]
            if kind == "resolved":
                self.current = msg
                self.current["confirmed"] = False
                self.current["playing"] = True
                changed = True
            elif kind == "confirmed":
                cur = self.current
                if (cur and cur.get("mal_id") == msg["mal_id"]
                        and cur.get("episode") == msg["episode"]):
                    cur["confirmed"] = True
                    cur["playing"] = False
                if not msg.get("first", True):
                    self._notify_rewatch(msg)
                changed = True
            elif kind == "repicked":
                if self.current:
                    for key in ("mal_id", "title", "media_type", "image_path",
                                "mal_url", "synopsis", "episode", "confirmed"):
                        if key in msg:
                            self.current[key] = msg[key]
                    self.current["playing"] = not bool(self.current.get("confirmed"))
                changed = True
            elif kind in ("connected", "disconnected"):
                print(f"[widget] {kind} via {msg.get('socket', '')}", file=sys.stderr)
            elif kind == "refresh":
                changed = True
            elif kind == "new_content":
                self._handle_new_content(msg)
                changed = True
        if changed:
            self.refresh()

    def refresh(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        entries: list[dict] = []
        badge = self.store.badge_map()
        cur = self.current
        if cur is not None and cur.get("mal_id") is not None:
            entry = dict(cur)
            targets = badge.get(entry["mal_id"], [])
            entry["has_new"] = bool(targets)
            entry["new_targets"] = targets
            entries.append(entry)

        for row in self.store.collection(60):
            if cur is not None and row["mal_id"] == cur.get("mal_id"):
                continue  # the current card already represents this anime
            row = dict(row)
            row["playing"] = False
            targets = badge.get(row["mal_id"], [])
            row["has_new"] = bool(targets)
            row["new_targets"] = targets
            entries.append(row)
        # stable sort: cards with new content to the top, recency within groups
        entries.sort(key=lambda e: not e.get("has_new"))

        if not entries:
            lbl = QLabel("Nothing here yet.\nPlay a tracked release in mpv,\nor connect your MAL list.")
            lbl.setStyleSheet("color: #5a5f6e; padding: 24px;")
            self.grid.addWidget(lbl, 0, 0)
            return

        for i, entry in enumerate(entries):
            card = Card(entry, self._on_repick, self._on_new_content, self._on_dismiss_new_content,
                        self.img_w, self.img_h, self.card_w)
            self.grid.addWidget(card, i // 2, i % 2)

    # -- interactions -----------------------------------------------------------

    def _on_repick(self, info: dict) -> None:
        dlg = RePickDialog(self.mal, info.get("title", ""), self)
        if dlg.exec() != QDialog.Accepted:
            return
        cand = dlg.selected()
        if not cand:
            return
        self.cmd.put({
            "cmd": "repick",
            "old_mal_id": info.get("mal_id"),
            "new_mal_id": cand["id"],
            "title": cand["title"],
        })

    def _on_new_content(self, info: dict) -> None:
        # Just open the page — the badge stays until the new content is
        # actually watched (dismiss is only a right-click option).
        targets = info.get("new_targets") or []
        if not targets:
            return
        QDesktopServices.openUrl(QUrl(f"https://myanimelist.net/anime/{targets[0]}"))

    def _on_dismiss_new_content(self, info: dict) -> None:
        self.store.dismiss_new_content(info["mal_id"])
        self.refresh()

    # -- new-content check (M4) --------------------------------------------------

    def _maybe_check_new_content(self) -> None:
        if self.checker is None or not self.checker.due():
            return
        checker = self.checker

        def _run():
            try:
                detected = checker.run_check()
                keys = {(d["source_mal_id"], d["target_mal_id"]) for d in detected}
                pending = [r for r in self.store.unnotified_new_content()
                           if (r["source_mal_id"], r["target_mal_id"]) not in keys]
                self.out.put({"type": "new_content", "new_items": detected, "pending": pending})
            except Exception as exc:
                print(f"[checker] failed: {exc}", file=sys.stderr)

        threading.Thread(target=_run, daemon=True, name="new-content-check").start()

    def _handle_new_content(self, msg: dict) -> None:
        for item in msg.get("new_items", []):
            self._notify("AnimeW Widget", f"New content detected: {item.get('target_title')}")
            self.store.set_new_content_notified(item["source_mal_id"], item["target_mal_id"])
        for row in msg.get("pending", []):
            if row.get("target_title"):
                self._notify("AnimeW Widget", f"New content detected: {row['target_title']}")
            self.store.set_new_content_notified(row["source_mal_id"], row["target_mal_id"])

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        if self.sync is not None:
            auth_action = QAction("Re-authorize MyAnimeList", self)
            auth_action.triggered.connect(self._reauthorize)
            menu.addAction(auth_action)
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings)
        menu.addAction(settings_action)
        quit_action = QAction("Quit", self)
        menu.addAction(quit_action)
        chosen = menu.exec(event.globalPos())
        if chosen is quit_action:
            QApplication.instance().quit()

    def _reauthorize(self) -> None:
        cfg = config.load_config()
        if not cfg.get("mal_client_id"):
            return
        dlg = AuthDialog(cfg["mal_client_id"], cfg.get("mal_client_secret", ""),
                         cfg["callback_url"], self)
        if dlg.exec() == QDialog.Accepted and dlg.tokens:
            TokenStore().save(dlg.tokens)

    def _retry_pending(self) -> None:
        sync = self.sync
        if sync is None:
            return

        def _retry():
            try:
                left = sync.retry_pending()
                if left:
                    print(f"[sync] {left} update(s) still pending", file=sys.stderr)
            except Exception as exc:
                print(f"[sync] retry failed: {exc}", file=sys.stderr)

        threading.Thread(target=_retry, daemon=True).start()

    # -- notifications ------------------------------------------------------------

    def _notify_rewatch(self, msg: dict) -> None:
        title = msg.get("title") or "anime"
        ep = msg.get("episode")
        body = f"Rewatched {title} EP {ep:02d} — already marked as watched." if ep is not None else f"Rewatched {title} — already marked."
        self._notify("AnimeW Widget", body)

    @staticmethod
    def _notify(summary: str, body: str) -> None:
        try:
            subprocess.run(
                ["notify-send", "-a", "AnimeW", summary, body],
                timeout=5, capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def main(argv: list[str] | None = None) -> int:
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName("AnimeW Widget")
    cfg = config.load_config()
    store = Store()
    out: queue.Queue = queue.Queue()
    cmd: queue.Queue = queue.Queue()

    sync = None
    if cfg.get("mal_client_id"):
        token_store = TokenStore()
        if not token_store.load():
            dlg = AuthDialog(cfg["mal_client_id"], cfg.get("mal_client_secret", ""),
                             cfg["callback_url"])
            if dlg.exec() == QDialog.Accepted and dlg.tokens:
                token_store.save(dlg.tokens)
        if token_store.load():
            from .sync import MALSync

            sync = MALSync(cfg["mal_client_id"], cfg.get("mal_client_secret", ""),
                           token_store, store)
            # Pull: refresh the list mirror from MAL on every startup so
            # manual edits / other-device watching show up in the widget.
            try:
                n = sync.import_list()
                print(f"[sync] refreshed {n} entries from MAL list", file=sys.stderr)
            except Exception as exc:
                print(f"[sync] list refresh failed: {exc}", file=sys.stderr)
            try:
                synced = sync.reconcile()
                if synced:
                    print(f"[sync] reconciled {synced} locally-watched show(s) with MAL", file=sys.stderr)
            except Exception as exc:
                print(f"[sync] reconcile failed: {exc}", file=sys.stderr)
            try:
                left = sync.retry_pending()
                if left:
                    print(f"[sync] {left} update(s) still pending", file=sys.stderr)
            except Exception as exc:
                print(f"[sync] retry failed: {exc}", file=sys.stderr)

    from .watcher import WatchWorker

    worker = WatchWorker(
        cfg["mpv_sockets"], out, cmd, store=store, sync=sync,
        tags=get_release_tags(store), threshold=get_watch_threshold(store),
    )
    threading.Thread(target=worker.run, daemon=True, name="mpv-watcher").start()

    mal = MALClient()

    def _backfill_details():
        """Fetch real details (synopsis, episode count, image) for rows the
        MAL list import created without them, replace "Second season of X."
        stubs with the first season's synopsis, and create cards for orphan
        ids (watched/mal_list rows with no anime row — e.g. prequels
        completed by franchise sync before this fix).

        API etiquette: one row per ~1.2s (≈50 req/min sustained, under MAL's
        limit), an unconditional pause even when a fetch fails (so an offline
        startup does not rapid-fire), and each attempted row gets a 7-day
        cooldown — a genuinely short synopsis with no prequel to fall back on
        is retried at most weekly, not on every launch."""
        from .images import ensure_image
        import time as _time

        updated = 0
        for row in store.rows_needing_details():
            try:
                d = mal.details_with_synopsis(row["mal_id"])
                pic = d.get("main_picture") or {}
                image_url = pic.get("large") or pic.get("medium")
                image_path = ensure_image(image_url, row["mal_id"]) if image_url else None
                store.upsert_anime(
                    row["mal_id"], d.get("title") or row["title"] or "",
                    d.get("media_type"), d.get("num_episodes"),
                    d.get("synopsis") or "", image_path,
                    f"https://myanimelist.net/anime/{row['mal_id']}",
                )
                store.set_details_attempted(row["mal_id"])
                updated += 1
            except Exception:
                pass  # transient failure: no cooldown, retried next startup
            _time.sleep(1.2)  # MAL v2 allows ~60 requests/min
        if updated:
            print(f"[widget] backfilled details for {updated} anime", file=sys.stderr)
            out.put({"type": "refresh"})

    threading.Thread(target=_backfill_details, daemon=True,
                     name="detail-backfill").start()

    checker = NewContentChecker(store, mal, out)
    widget = Widget(cfg, store, out, cmd, mal, sync, checker)
    widget.show()

    def _fix_images():
        from .images import ensure_image

        fixed = 0
        for row in store.remote_image_rows():
            path = ensure_image(row["image_path"], row["mal_id"])
            if path:
                store.set_anime_image(row["mal_id"], path)
                fixed += 1
        if fixed:
            print(f"[widget] downloaded {fixed} missing card image(s)", file=sys.stderr)
            out.put({"type": "refresh"})

    threading.Thread(target=_fix_images, daemon=True, name="image-fixer").start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
