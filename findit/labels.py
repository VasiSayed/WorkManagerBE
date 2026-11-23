"""Printable QR labels: A4 portrait, 3 x 7 labels of 50 x 40 mm, QR 30 mm + reference number.

Geometry (mm): 3 x 50 + 2 x 10 gap = 170 wide -> 20 side margins; 7 x 40 = 280 tall -> 8.5 top/bottom.
Most home printers leave ~4-5 mm unprintable, so nothing is clipped. Print at 100% / "Actual size".
The QR is vector (sharp at any printer resolution), black on white, error correction level H,
with at least the 4-module quiet zone around it: a label URL is a version-8 QR (49 modules), so at
30 mm one module is ~0.6 mm and the quiet zone ~2.5 mm.
Each label has a light dashed cutting border. Vertically (mm from the label's top): border 0,
QR 3-33, reference number cap height ~35.8-38.5, border 40. So the border is 3 mm from the QR
(top) and 10 mm (sides), the text ~2.8 mm below the QR, and the next row's QR 3 mm past the border.
"""

import io

from django.conf import settings
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

COLS, ROWS = 3, 7
LABEL_W, LABEL_H = 50 * mm, 40 * mm
GAP_X = 10 * mm
QR_SIZE = 30 * mm
QR_TOP = 3 * mm
TEXT_BASELINE = 5.5 * mm  # below the QR
TEXT_SIZE = 11  # pt, bold
CUT_GREY = 0.6  # light grey: visible to cut along, too light to be read as part of the code
CUT_WIDTH = 0.5  # pt
CUT_DASH = (3, 2)
PER_PAGE = COLS * ROWS


def scan_url(asset_uuid):
    """What every label encodes. Changing FINDIT_SCAN_BASE_URL after printing breaks printed labels."""
    base = getattr(settings, "FINDIT_SCAN_BASE_URL", "").rstrip("/")
    return f"{base}/q/{asset_uuid.hex}"


def _qr_drawing(data):
    widget = QrCodeWidget(data, barLevel="H", barBorder=0)
    x1, y1, x2, y2 = widget.getBounds()
    w, h = x2 - x1, y2 - y1
    drawing = Drawing(QR_SIZE, QR_SIZE, transform=[QR_SIZE / w, 0, 0, QR_SIZE / h, 0, 0])
    drawing.add(widget)
    return drawing


def labels_pdf(labels, title="FindIt labels"):
    """labels: list of (uuid, reference) in print order (duplicates allowed). Returns PDF bytes."""
    page_w, page_h = A4
    margin_x = (page_w - (COLS * LABEL_W + (COLS - 1) * GAP_X)) / 2
    margin_y = (page_h - ROWS * LABEL_H) / 2
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setTitle(title)
    pdf.setAuthor("FindIt")
    for index, (asset_uuid, reference) in enumerate(labels):
        slot = index % PER_PAGE
        if index and slot == 0:
            pdf.showPage()
        col, row = slot % COLS, slot // COLS
        left = margin_x + col * (LABEL_W + GAP_X)
        top = page_h - margin_y - row * LABEL_H
        pdf.setStrokeGray(CUT_GREY)
        pdf.setLineWidth(CUT_WIDTH)
        pdf.setDash(*CUT_DASH)
        pdf.rect(left, top - LABEL_H, LABEL_W, LABEL_H, stroke=1, fill=0)
        pdf.setDash()
        renderPDF.draw(_qr_drawing(scan_url(asset_uuid)), pdf, left + (LABEL_W - QR_SIZE) / 2, top - QR_TOP - QR_SIZE)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.setFont("Helvetica-Bold", TEXT_SIZE)
        pdf.drawCentredString(left + LABEL_W / 2, top - QR_TOP - QR_SIZE - TEXT_BASELINE, reference)
    pdf.save()
    return buf.getvalue()
