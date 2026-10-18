from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QUrl, Qt, QTimer
from PySide6.QtGui import QDesktopServices, QFontMetrics
from PySide6.QtPdfWidgets import QPdfView


class PdfViewerMixin:
    def _update_page_indicator(self):
        if not hasattr(self, "pdf_page_label"):
            return

        total = self.pdf_document.pageCount()
        if total <= 0:
            self.pdf_page_label.setText("0 / 0")
            self.btn_prev_page.setEnabled(False)
            self.btn_next_page.setEnabled(False)
            return

        current = self.pdf_view.pageNavigator().currentPage() + 1
        self.pdf_page_label.setText(f"{current} / {total}")
        self.btn_prev_page.setEnabled(current > 1)
        self.btn_next_page.setEnabled(current < total)

    def _on_pdf_status_changed(self):
        self._update_page_indicator()
        pending = getattr(self, "_pending_highlight_page", None)
        if pending is not None and self.pdf_document.pageCount() > pending:
            self._pending_highlight_page = None
            loc = getattr(self, "_pending_highlight_norm_pos", None)
            self._pending_highlight_norm_pos = None
            QTimer.singleShot(15, lambda page=pending, pos=loc: self._jump_to_pdf_page(page, pos))

    def _jump_to_pdf_page(self, page_index: int, norm_pos: tuple[float, float] | None = None):
        if page_index < 0 or page_index >= self.pdf_document.pageCount():
            return
        nav = self.pdf_view.pageNavigator()
        target_pt = QPointF(0, 0)
        if norm_pos is not None:
            try:
                page_size = self.pdf_document.pagePointSize(page_index)
                px = max(0.0, float(norm_pos[0]) * float(page_size.width()) - 24.0)
                py = max(0.0, float(norm_pos[1]) * float(page_size.height()) - 36.0)
                target_pt = QPointF(px, py)
            except Exception:
                target_pt = QPointF(0, 0)
        nav.jump(page_index, target_pt, nav.currentZoom())
        if norm_pos is not None:
            try:
                vbar = self.pdf_view.verticalScrollBar()
                if vbar and vbar.maximum() > 0:
                    target_val = int(vbar.maximum() * float(norm_pos[1]) - vbar.pageStep() * 0.15)
                    vbar.setValue(max(0, min(vbar.maximum(), target_val)))
            except Exception:
                pass

    def _parser_regions_for_pdf(self, pdf_path: Path):
        diagnostics = getattr(self, "parser_diagnostics", {}) or {}
        merged: dict[str, list[dict]] = {}
        if isinstance(diagnostics, dict):
            raw_regions = diagnostics.get("field_regions")
            if isinstance(raw_regions, dict):
                for k, v in raw_regions.items():
                    if isinstance(v, list) and v:
                        merged[k] = [x for x in v if isinstance(x, dict) and x.get("rect_norm")]
            field_results = diagnostics.get("field_results")
            if isinstance(field_results, dict):
                for k, payload in field_results.items():
                    if k not in merged and isinstance(payload, dict):
                        s_blocks = [
                            x for x in (payload.get("source_blocks") or [])
                            if isinstance(x, dict) and x.get("rect_norm")
                        ]
                        if s_blocks:
                            merged[k] = s_blocks
        if merged:
            return merged
        cache = getattr(self, "parser_cache", None)
        if cache is not None:
            try:
                return cache.load_regions(pdf_path)
            except OSError:
                pass
        return {}

    def _preview_path_with_parser_highlights(self, pdf_path: Path) -> Path:
        selected = getattr(self, "_highlight_field", None)
        if not selected:
            return pdf_path
        regions = self._parser_regions_for_pdf(pdf_path)
        preview = getattr(self, "pdf_highlight_preview", None)
        if not regions or preview is None:
            return pdf_path
        try:
            highlighted = preview.build(pdf_path, regions, active_field=selected)
        except TypeError:
            highlighted = preview.build(pdf_path, regions)
        if highlighted is None:
            return pdf_path
        target_items = regions.get(selected) or []
        valid_items = [item for item in target_items if isinstance(item, dict)]
        if valid_items:
            first_item = min(valid_items, key=lambda x: int(x.get("page_index", 0) or 0))
            self._pending_highlight_page = int(first_item.get("page_index", 0) or 0)
            rect_norm = first_item.get("rect_norm") or []
            if len(rect_norm) == 4 and selected:
                self._pending_highlight_norm_pos = (float(rect_norm[0]), float(rect_norm[1]))
        return highlighted

    def _on_pdf_page_changed(self, page_index: int):
        self._update_page_indicator()

    def prev_pdf_page(self):
        nav = self.pdf_view.pageNavigator()
        current_page = nav.currentPage()
        if current_page > 0:
            nav.jump(current_page - 1, QPointF(0, 0), nav.currentZoom())

    def next_pdf_page(self):
        nav = self.pdf_view.pageNavigator()
        current_page = nav.currentPage()
        if current_page < self.pdf_document.pageCount() - 1:
            nav.jump(current_page + 1, QPointF(0, 0), nav.currentZoom())

    def _update_pdf_actions_state(self):
        has_pdf = False

        if getattr(self, "selected_pdf", None):
            has_pdf = Path(self.selected_pdf).exists()
        elif (
            self.request
            and self.request.source_document
            and not getattr(self, "detach_pdf_requested", False)
        ):
            candidate = Path(self.request.source_document)
            has_pdf = candidate.exists() or bool(
                self.documents_dir and (
                    self.documents_dir / candidate.name).exists()
            )

        self.btn_view.setVisible(has_pdf)
        self.btn_detach.setVisible(has_pdf)

    def _center_on_screen(self, target_width: int, target_height: int):
        screen = self.screen()
        if not screen and self.parentWidget():
            screen = self.parentWidget().screen()

        if screen:
            available = screen.availableGeometry()
            width = min(target_width, available.width() - 40)
            height = min(target_height, available.height() - 40)
            x = available.left() + (available.width() - width) // 2
            y = available.top() + (available.height() - height) // 2
            self.setGeometry(x, y, width, height)
        else:
            self.resize(target_width, target_height)

    def zoom_in_pdf(self):
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
        new_factor = min(4.0, self.pdf_view.zoomFactor() * 1.2)
        self.pdf_view.setZoomFactor(new_factor)
        self._update_zoom_label()

    def zoom_out_pdf(self):
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.Custom)
        new_factor = max(0.2, self.pdf_view.zoomFactor() / 1.2)
        self.pdf_view.setZoomFactor(new_factor)
        self._update_zoom_label()

    def zoom_fit_width(self):
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self._update_zoom_label()

    def zoom_fit_page(self):
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitInView)
        self._update_zoom_label()

    def _update_zoom_label(self):
        if not hasattr(self, "zoom_label"):
            return

        mode = self.pdf_view.zoomMode()
        if mode == QPdfView.ZoomMode.FitInView:
            self.zoom_label.setText("Стр.")
        elif mode == QPdfView.ZoomMode.FitToWidth:
            self.zoom_label.setText("Шир.")
        else:
            percent = int(round(self.pdf_view.zoomFactor() * 100))
            self.zoom_label.setText(f"{percent}%")

    def eventFilter(self, watched, event):
        if hasattr(self, "pdf_view") and watched == self.pdf_view.viewport():
            if event.type() == QEvent.Type.Wheel:
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    if event.angleDelta().y() > 0:
                        self.zoom_in_pdf()
                    else:
                        self.zoom_out_pdf()
                    return True
        return super().eventFilter(watched, event)

    def toggle_pdf_viewer(self, show: bool, pdf_path: Path | None = None):
        if show and pdf_path and pdf_path.exists():
            # Close a previous temporary preview before rebuilding it. This is
            # important on Windows, where an open QPdfDocument keeps the file locked.
            try:
                self.pdf_document.close()
            except Exception:
                pass
            was_visible = self.pdf_container.isVisible()
            if not was_visible or self.pdf_view.zoomMode() == QPdfView.ZoomMode.FitInView:
                self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
                self._update_zoom_label()

            display_path = self._preview_path_with_parser_highlights(pdf_path)
            self.pdf_document.load(str(display_path))
            self.current_pdf_path = pdf_path

            metrics = QFontMetrics(self.pdf_preview_title.font())
            self.pdf_preview_title.setText(metrics.elidedText(
                pdf_path.name, Qt.TextElideMode.ElideMiddle, 220))
            self.pdf_preview_title.setToolTip(str(pdf_path))

            if not self.pdf_container.isVisible():
                self.pdf_container.show()
                self._center_on_screen(
                    self.width() + 540, max(760, self.height()))

            self.btn_view.setText("Скрыть PDF")
        else:
            if self.pdf_container.isVisible():
                self.pdf_container.hide()
                self._center_on_screen(
                    max(680, self.width() - 540), self.height())

            self.btn_view.setText("Открыть")
            self.current_pdf_path = None

    def open_external_pdf(self):
        if self.current_pdf_path and self.current_pdf_path.exists():
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.current_pdf_path)))
