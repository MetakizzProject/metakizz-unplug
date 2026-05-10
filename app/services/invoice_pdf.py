"""Invoice PDF generator using ReportLab.

Reads issuer info from env vars (so Álvaro can change without redeploy):
  INVOICE_BUSINESS_NAME           → "Virtual Flow LLC"
  INVOICE_BUSINESS_ADDRESS_LINE1  → street + suite
  INVOICE_BUSINESS_ADDRESS_LINE2  → optional second line
  INVOICE_BUSINESS_CITY_STATE_ZIP → "Albuquerque, NM 87110"
  INVOICE_BUSINESS_COUNTRY        → "United States"
  INVOICE_BUSINESS_EIN            → "33-1929195"
  INVOICE_BUSINESS_EMAIL          → "info@metakizzproject.com"
  INVOICE_BUSINESS_WEBSITE        → "metakizzproject.com" (optional)
  INVOICE_BUSINESS_PHONE          → optional
  INVOICE_LOGO_PATH               → optional, server-side path or URL
  INVOICE_PAYMENT_TERMS           → default "Due on Receipt"
  INVOICE_FOOTER_NOTE             → default "Thank you for your business."
"""

import io
import os
import logging
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, KeepTogether
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logger = logging.getLogger(__name__)

# Brand colors (Matrix green from the rest of the app for accents).
COLOR_BRAND = colors.HexColor("#2EDB99")
COLOR_DARK = colors.HexColor("#0A0A0A")
COLOR_GRAY = colors.HexColor("#6B7280")
COLOR_LIGHT_GRAY = colors.HexColor("#E5E7EB")
COLOR_BG = colors.HexColor("#FAFAFA")


def _env(name, default=""):
    return (os.getenv(name, default) or "").strip()


def _format_money(amount_cents, currency="USD"):
    """Format like $1,234.56 USD."""
    amount = (amount_cents or 0) / 100
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get((currency or "usd").lower(), "")
    if symbol:
        return f"{symbol}{amount:,.2f}"
    return f"{amount:,.2f} {currency.upper()}"


def _format_date(dt):
    """May 10, 2026"""
    if not dt:
        return ""
    return dt.strftime("%B %-d, %Y")


def _customer_is_outside_us(customer_country):
    """Lightweight check — if explicit country is set and isn't US/USA,
    add the foreign-customer tax note. If unknown, omit (safer)."""
    if not customer_country:
        return False
    c = customer_country.strip().upper()
    return c not in ("US", "USA", "UNITED STATES", "U.S.", "U.S.A.")


def generate_invoice_pdf(
    invoice_number,
    customer_email,
    customer_name=None,
    customer_address=None,  # multi-line string
    customer_country=None,
    line_items=None,        # list of dicts: {"description": str, "qty": int, "unit_price_cents": int}
    issue_date=None,
    due_date=None,
    currency="USD",
    stripe_charge_id=None,
    notes=None,
):
    """Render an invoice to PDF and return the bytes.

    line_items defaults to a single line built from the kwargs:
      [{"description": notes or "Digital Services", "qty": 1, "unit_price_cents": amount}]
    """
    issue_date = issue_date or datetime.now(timezone.utc)
    payment_terms = _env("INVOICE_PAYMENT_TERMS", "Due on Receipt")
    footer_note = _env(
        "INVOICE_FOOTER_NOTE",
        "Welcome to the community. So glad you're with us.",
    )

    line_items = line_items or []
    if not line_items:
        raise ValueError("generate_invoice_pdf requires at least one line item")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        rightMargin=0.5 * inch,
        leftMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        title=f"Invoice {invoice_number}",
        author=_env("INVOICE_BUSINESS_NAME", "Virtual Flow LLC"),
    )

    styles = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=22, leading=26, textColor=COLOR_DARK, spaceAfter=2)
    H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=11, leading=14, textColor=COLOR_GRAY, spaceAfter=6, fontName="Helvetica-Bold")
    BODY = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14, textColor=COLOR_DARK)
    SMALL = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, leading=11, textColor=COLOR_GRAY)
    LABEL = ParagraphStyle("Label", parent=styles["BodyText"], fontSize=9, leading=11, textColor=COLOR_GRAY, fontName="Helvetica-Bold")
    INVOICE_TITLE = ParagraphStyle("InvoiceTitle", parent=styles["Heading1"], fontSize=28, leading=32, textColor=COLOR_BRAND, alignment=2, fontName="Helvetica-Bold")

    story = []

    # ---------- HEADER (issuer block + INVOICE block) ----------
    biz_name = _env("INVOICE_BUSINESS_NAME", "Virtual Flow LLC")
    addr1 = _env("INVOICE_BUSINESS_ADDRESS_LINE1")
    addr2 = _env("INVOICE_BUSINESS_ADDRESS_LINE2")
    citystatezip = _env("INVOICE_BUSINESS_CITY_STATE_ZIP")
    country = _env("INVOICE_BUSINESS_COUNTRY", "United States")
    ein = _env("INVOICE_BUSINESS_EIN")
    biz_email = _env("INVOICE_BUSINESS_EMAIL")
    biz_phone = _env("INVOICE_BUSINESS_PHONE")
    biz_web = _env("INVOICE_BUSINESS_WEBSITE")

    # Optional logo (env-overridable). Default: bundled green Metakizz logo.
    logo_path = _env("INVOICE_LOGO_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "brand", "organized", "logo-green.png",
    )
    logo_flowable = None
    if os.path.exists(logo_path):
        try:
            logo_flowable = Image(logo_path, width=1.0 * inch, height=1.0 * inch, kind="proportional")
        except Exception:
            logger.exception("invoice: failed to load logo from %s", logo_path)
            logo_flowable = None

    issuer_lines = [biz_name]
    if addr1:
        issuer_lines.append(addr1)
    if addr2:
        issuer_lines.append(addr2)
    if citystatezip:
        issuer_lines.append(citystatezip)
    if country:
        issuer_lines.append(country)
    if ein:
        issuer_lines.append(f"EIN {ein}")
    if biz_email:
        issuer_lines.append(biz_email)
    if biz_phone:
        issuer_lines.append(biz_phone)
    if biz_web:
        issuer_lines.append(biz_web)

    issuer_para = Paragraph(
        f"<font size=12 color='#0A0A0A'><b>{biz_name}</b></font><br/>"
        + "<br/>".join(issuer_lines[1:]),
        BODY,
    )

    # If we have a logo, stack it above the issuer text in the left column.
    if logo_flowable is not None:
        from reportlab.platypus import KeepInFrame
        left_col = [logo_flowable, Spacer(1, 0.1 * inch), issuer_para]
    else:
        left_col = issuer_para

    invoice_block_html = (
        "INVOICE<br/>"
        f"<font size=10 color='#6B7280'><b>Number:</b> {invoice_number}</font><br/>"
        f"<font size=10 color='#6B7280'><b>Issue date:</b> {_format_date(issue_date)}</font>"
    )
    if due_date:
        invoice_block_html += f"<br/><font size=10 color='#6B7280'><b>Due date:</b> {_format_date(due_date)}</font>"
    invoice_block_html += f"<br/><font size=10 color='#6B7280'><b>Terms:</b> {payment_terms}</font>"
    invoice_para = Paragraph(invoice_block_html, INVOICE_TITLE)

    header_table = Table(
        [[left_col, invoice_para]],
        colWidths=[3.5 * inch, 4.0 * inch],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 0.3 * inch))

    # ---------- BILL TO ----------
    customer_block = [Paragraph("BILL TO", LABEL)]
    if customer_name:
        customer_block.append(Paragraph(f"<b>{customer_name}</b>", BODY))
    customer_block.append(Paragraph(customer_email or "", BODY))
    if customer_address:
        customer_block.append(Paragraph(customer_address.replace("\n", "<br/>"), BODY))
    if customer_country and customer_country.strip():
        customer_block.append(Paragraph(customer_country, BODY))

    story.append(Table(
        [[customer_block]],
        colWidths=[7.5 * inch],
        style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_LIGHT_GRAY),
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]),
    ))
    story.append(Spacer(1, 0.25 * inch))

    # ---------- LINE ITEMS TABLE ----------
    table_rows = [
        ["DESCRIPTION", "QTY", "UNIT PRICE", "AMOUNT"],
    ]
    subtotal_cents = 0
    for item in line_items:
        qty = int(item.get("qty") or 1)
        unit = int(item.get("unit_price_cents") or 0)
        amount = qty * unit
        subtotal_cents += amount
        desc_para = Paragraph(item.get("description") or "Service", BODY)
        table_rows.append([
            desc_para,
            str(qty),
            _format_money(unit, currency),
            _format_money(amount, currency),
        ])

    items_table = Table(
        table_rows,
        colWidths=[4.0 * inch, 0.7 * inch, 1.3 * inch, 1.5 * inch],
    )
    items_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        # Body rows
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, COLOR_BG]),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, COLOR_DARK),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 0.15 * inch))

    # ---------- TOTALS ----------
    total_cents = subtotal_cents  # no discount/tax for digital services US LLC
    totals_data = [
        ["Subtotal", _format_money(subtotal_cents, currency)],
        ["Tax", "—"],
        ["", ""],
        ["TOTAL", _format_money(total_cents, currency)],
    ]
    totals_table = Table(totals_data, colWidths=[1.5 * inch, 1.5 * inch], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        # Total row
        ("FONTNAME", (0, 3), (-1, 3), "Helvetica-Bold"),
        ("FONTSIZE", (0, 3), (-1, 3), 13),
        ("LINEABOVE", (0, 3), (-1, 3), 0.8, COLOR_DARK),
        ("TEXTCOLOR", (1, 3), (1, 3), COLOR_BRAND),
    ]))
    story.append(totals_table)
    story.append(Spacer(1, 0.3 * inch))

    # ---------- PAYMENT INFO ----------
    payment_block = [Paragraph("PAYMENT", LABEL)]
    if stripe_charge_id:
        payment_block.append(Paragraph(
            f"<b>Status: PAID</b><br/>Paid via Stripe<br/><font color='#6B7280' size=8>Charge ID: {stripe_charge_id}</font>",
            BODY,
        ))
    else:
        payment_block.append(Paragraph(f"<b>Status:</b> {payment_terms}", BODY))

    story.append(Table(
        [[payment_block]],
        colWidths=[7.5 * inch],
        style=TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_LIGHT_GRAY),
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]),
    ))
    story.append(Spacer(1, 0.3 * inch))

    # ---------- NOTES & LEGAL ----------
    if notes:
        story.append(Paragraph("NOTES", LABEL))
        story.append(Paragraph(notes, BODY))
        story.append(Spacer(1, 0.2 * inch))

    # Foreign-customer note (auto)
    if _customer_is_outside_us(customer_country):
        legal = (
            "Services rendered by a US LLC (Virtual Flow LLC, EIN "
            f"{ein or '33-1929195'}, New Mexico). Not subject to US sales tax. "
            "Recipient is responsible for any applicable taxes in their jurisdiction."
        )
        story.append(Paragraph(legal, SMALL))
        story.append(Spacer(1, 0.15 * inch))

    # Footer
    story.append(Paragraph(footer_note, ParagraphStyle(
        "Footer", parent=BODY, alignment=1, textColor=COLOR_GRAY, fontSize=10,
    )))

    doc.build(story)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes


def safe_pdf_filename(invoice_number, customer_name=None, customer_email=None):
    """Build a download-safe filename like INV-2026-0042_MariaLopez.pdf"""
    import re
    label = customer_name or (customer_email or "").split("@")[0] or "Customer"
    label = re.sub(r"[^A-Za-z0-9]", "", label)[:40] or "Customer"
    return f"{invoice_number}_{label}.pdf"
