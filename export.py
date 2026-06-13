import csv
import io
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                 Paragraph, Spacer, HRFlowable)
from reportlab.lib.enums import TA_CENTER
import database
from datetime import date

MONTH_NAMES = ["January","February","March","April","May","June",
               "July","August","September","October","November","December"]

def _get_export_data(cat_ids, year_months, include_transactions=True):
    """
    Returns a structured list of sections for export.
    year_months: list of (year, month) tuples sorted ascending.
    cat_ids: list/set of category ids to include.
    include_transactions: if True, include individual transaction rows.
    """
    currency   = database.get_setting("currency_symbol") or "$"
    cat_id_set = set(cat_ids)
    sections   = []

    for year, month in year_months:
        month_label    = date(year, month, 1).strftime("%B %Y")
        month_totals   = database.get_monthly_totals(year, month)
        month_income   = month_totals["total_income"]   if month_totals else 0
        month_expenses = month_totals["total_expenses"] if month_totals else 0
        month_net      = month_income - month_expenses
        month_sections = []

        for acct in database.get_accounts():
            is_debt  = acct["type"] == "Debt"
            cats     = database.get_categories(acct["id"])
            cat_rows = []

            for cat in cats:
                if cat["id"] not in cat_id_set:
                    continue

                ctype       = cat["category_type"]
                cat_summary = database.get_category_summary(cat["id"], year, month)
                spent       = cat_summary["total_spent"]  if cat_summary else 0
                income      = cat_summary["total_income"] if cat_summary else 0

                if ctype == "transfer":
                    txn_rows = []
                    if include_transactions:
                        transfers = database.get_transfers_for_category(cat["id"], year, month)
                        txn_rows  = [["Date","From","To","Amount","Direction"]]
                        for tr in transfers:
                            direction = "Out" if tr["from_account_id"] == acct["id"] else "In"
                            txn_rows.append([
                                tr["date"],
                                tr["from_account_name"],
                                tr["to_account_name"],
                                f"{currency}{tr['amount']:.2f}",
                                direction
                            ])
                    cat_rows.append({
                        "name": cat["name"], "type": "transfer",
                        "transactions": txn_rows
                    })

                elif ctype in ("loan","credit_card"):
                    base     = cat["budget_limit"] or 0
                    starting = database.get_debt_category_starting_balance(
                        cat["id"], base, year, month
                    )
                    remaining = starting + spent - income
                    txn_rows  = []
                    if include_transactions:
                        txns = database.get_transactions_with_running_balance(
                            cat["id"], base, year, month
                        )
                        txns_reversed = list(reversed(txns))
                        txn_rows = [["Date","Amount","Type","Remaining Balance"]]
                        for t in txns_reversed:
                            if t.get('is_pending'):
                                continue
                            txn_type = ("Payment" if t["type"]=="income" else
                                        ("Interest" if ctype=="loan" else "Charge"))
                            txn_rows.append([
                                t["date"],
                                f"{currency}{t['amount']:.2f}",
                                txn_type,
                                f"{currency}{t['running_balance']:.2f}"
                            ])
                    cat_rows.append({
                        "name":      cat["name"],
                        "type":      ctype,
                        "starting":  f"{currency}{starting:.2f}",
                        "spent":     f"{currency}{spent:.2f}",
                        "income":    f"{currency}{income:.2f}",
                        "remaining": f"{currency}{remaining:.2f}",
                        "pay_by":    cat["pay_by_date"] or "—",
                        "min_due":   f"{currency}{cat['minimum_due']:.2f}",
                        "transactions": txn_rows
                    })

                elif ctype == "income":
                    txn_rows = []
                    if include_transactions:
                        txns     = database.get_transactions(cat["id"], year, month)
                        txn_rows = [["Date","Description","Payer","Payment Method","Amount"]]
                        for t in txns:
                            if t.get('is_pending'):
                                continue
                            txn_rows.append([
                                t["date"], t["description"] or "",
                                t["payee"] or "", t["payment_method"] or "",
                                f"{currency}{t['amount']:.2f}"
                            ])
                    cat_rows.append({
                        "name": cat["name"], "type": "income",
                        "total_income": f"{currency}{income:.2f}",
                        "transactions": txn_rows
                    })

                else:
                    limit     = cat["budget_limit"] or 0
                    remaining = limit - spent
                    txn_rows  = []
                    if include_transactions:
                        txns     = database.get_transactions(cat["id"], year, month)
                        txn_rows = [["Date","Date Type","Description","Payee","Payment Method","Type","Amount"]]
                        for t in txns:
                            if t.get('is_pending'):
                                continue
                            txn_rows.append([
                                t["date"], t["date_type"], t["description"] or "",
                                t["payee"] or "", t["payment_method"] or "",
                                t["type"].capitalize(),
                                f"{currency}{t['amount']:.2f}"
                            ])
                    cat_rows.append({
                        "name":      cat["name"],
                        "type":      "budget",
                        "budgeted":  f"{currency}{limit:.2f}",
                        "spent":     f"{currency}{spent:.2f}",
                        "remaining": f"{currency}{remaining:.2f}",
                        "transactions": txn_rows
                    })

            if not cat_rows:
                continue  # skip account entirely if no selected categories

            acct_summary    = database.get_account_summary(acct["id"], year, month)
            acct_budgeted   = acct_summary["total_budgeted"] if acct_summary else 0
            acct_spent      = acct_summary["total_spent"]    if acct_summary else 0
            acct_income     = acct_summary["total_income"]   if acct_summary else 0
            month_sections.append({
                "account_name":     acct["name"],
                "account_type":     acct["type"],
                "is_debt":          is_debt,
                "acct_budgeted":    f"{currency}{acct_budgeted:.2f}",
                "acct_spent":       f"{currency}{acct_spent:.2f}",
                "acct_income":      f"{currency}{acct_income:.2f}",
                "categories":       cat_rows,
                "currency":         currency,
            })

        sections.append({
            "month_label":     month_label,
            "month_income":    f"{currency}{month_income:.2f}",
            "month_expenses":  f"{currency}{month_expenses:.2f}",
            "month_net":       f"{currency}{month_net:.2f}",
            "accounts":        month_sections
        })

    return sections, currency


def generate_csv(cat_ids, year_months, include_transactions=True):
    sections, currency = _get_export_data(cat_ids, year_months, include_transactions)
    output = io.StringIO()
    writer = csv.writer(output)

    for section in sections:
        writer.writerow([section["month_label"]])
        writer.writerow(["  Monthly Summary"])
        writer.writerow(["  Total Income:",   section["month_income"]])
        writer.writerow(["  Total Expenses:", section["month_expenses"]])
        writer.writerow(["  Net:",            section["month_net"]])
        writer.writerow([])

        for acct in section["accounts"]:
            writer.writerow([f"Account: {acct['account_name']} ({acct['account_type']})"])
            writer.writerow(["  Budgeted:", acct["acct_budgeted"],
                             "  Spent:", acct["acct_spent"],
                             "  Income:", acct["acct_income"]])
            writer.writerow([])

            for cat in acct["categories"]:
                writer.writerow([f"  Category: {cat['name']} ({cat['type']})"])

                if cat["type"] == "income":
                    writer.writerow(["  Total Income:", cat["total_income"]])
                elif cat["type"] in ("loan","credit_card"):
                    writer.writerow(["  Starting Balance:", cat["starting"]])
                    writer.writerow(["  Charged/Interest:", cat["spent"]])
                    writer.writerow(["  Paid:",             cat["income"]])
                    writer.writerow(["  Remaining:",        cat["remaining"]])
                    writer.writerow(["  Pay By:",           cat["pay_by"]])
                    writer.writerow(["  Min Due:",          cat["min_due"]])
                elif cat["type"] == "budget":
                    writer.writerow(["  Budgeted:", cat["budgeted"]])
                    writer.writerow(["  Spent:",    cat["spent"]])
                    writer.writerow(["  Remaining:", cat["remaining"]])

                for row in cat["transactions"]:
                    writer.writerow(["  "] + row)

                writer.writerow([])
            writer.writerow([])
        writer.writerow([])

    return output.getvalue().encode("utf-8")


def generate_pdf(cat_ids, year_months, include_transactions=True):
    sections, currency = _get_export_data(cat_ids, year_months, include_transactions)
    buffer = io.BytesIO()
    doc    = SimpleDocTemplate(buffer, pagesize=landscape(letter),
                               leftMargin=0.5*inch, rightMargin=0.5*inch,
                               topMargin=0.5*inch,  bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()
    story  = []

    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=14,
                         spaceBefore=0, spaceAfter=3, textColor=colors.HexColor("#111111"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12,
                         spaceBefore=0, spaceAfter=2, textColor=colors.HexColor("#333333"))
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=10,
                         spaceBefore=0, spaceAfter=2, textColor=colors.HexColor("#1a5a96"))
    normal = ParagraphStyle("n", parent=styles["Normal"], fontSize=9,
                             textColor=colors.HexColor("#111111"))

    tbl_header  = colors.HexColor("#e8e8e8")
    tbl_odd     = colors.HexColor("#f5f5f5")
    tbl_even    = colors.HexColor("#ffffff")
    tbl_text    = colors.HexColor("#111111")
    tbl_muted   = colors.HexColor("#555555")
    bg_page     = colors.HexColor("#ffffff")

    def make_table(rows):
        if len(rows) <= 1:
            return None
        col_count = len(rows[0])
        col_width  = (landscape(letter)[0] - inch) / col_count
        t = Table(rows, colWidths=[col_width] * col_count, repeatRows=1)
        style = [
            ("BACKGROUND",  (0,0), (-1,0),  tbl_header),
            ("TEXTCOLOR",   (0,0), (-1,0),  tbl_muted),
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("TEXTCOLOR",   (0,1), (-1,-1), tbl_text),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [tbl_odd, tbl_even]),
            ("GRID",        (0,0), (-1,-1), 0.25, colors.HexColor("#cccccc")),
            ("TOPPADDING",  (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",(0,0),(-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
        ]
        t.setStyle(TableStyle(style))
        return t

    for section in sections:
        story.append(Paragraph(section["month_label"], h1))
        story.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#cccccc")))
        story.append(Spacer(1, 3))

        # Monthly summary
        monthly_rows = [
            ["Total Income",   section["month_income"]],
            ["Total Expenses", section["month_expenses"]],
            ["Net",            section["month_net"]],
        ]
        mt = Table(monthly_rows, colWidths=[2*inch, 1.5*inch])
        mt.setStyle(TableStyle([
            ("FONTSIZE",       (0,0),(-1,-1), 9),
            ("TEXTCOLOR",      (0,0),(0,-1),  tbl_muted),
            ("TEXTCOLOR",      (1,0),(1,-1),  tbl_text),
            ("TOPPADDING",     (0,0),(-1,-1), 2),
            ("BOTTOMPADDING",  (0,0),(-1,-1), 2),
        ]))
        story.append(mt)
        story.append(Spacer(1, 6))

        for acct in section["accounts"]:
            story.append(Paragraph(
                f"{acct['account_name']} <font size='9' color='#555555'>({acct['account_type']})</font>", h2
            ))
            # Account subtotals
            acct_rows = [
                ["Budgeted", acct["acct_budgeted"],
                 "Spent", acct["acct_spent"],
                 "Income", acct["acct_income"]],
            ]
            at = Table(acct_rows, colWidths=[0.9*inch, 1.1*inch, 0.7*inch, 1.1*inch, 0.7*inch, 1.1*inch])
            at.setStyle(TableStyle([
                ("FONTSIZE",      (0,0),(-1,-1), 8),
                ("TEXTCOLOR",     (0,0),(0,-1),  tbl_muted),
                ("TEXTCOLOR",     (1,0),(1,-1),  tbl_text),
                ("TEXTCOLOR",     (2,0),(2,-1),  tbl_muted),
                ("TEXTCOLOR",     (3,0),(3,-1),  tbl_text),
                ("TEXTCOLOR",     (4,0),(4,-1),  tbl_muted),
                ("TEXTCOLOR",     (5,0),(5,-1),  tbl_text),
                ("TOPPADDING",    (0,0),(-1,-1), 1),
                ("BOTTOMPADDING", (0,0),(-1,-1), 2),
            ]))
            story.append(at)

            for cat in acct["categories"]:
                story.append(Paragraph(cat["name"], h3))

                summary_rows = []
                summary_cols = []
                if cat["type"] == "income":
                    summary_rows = [["Total Income", cat["total_income"]]]
                    summary_cols = [1.1*inch, 1.0*inch]
                elif cat["type"] in ("loan","credit_card"):
                    summary_rows = [
                        ["Starting Balance", cat["starting"],  "Charged/Interest", cat["spent"],  "Paid",    cat["income"]],
                        ["Remaining",        cat["remaining"], "Pay By",           cat["pay_by"], "Min Due", cat["min_due"]],
                    ]
                    summary_cols = [1.3*inch, 1.0*inch, 1.3*inch, 1.0*inch, 0.7*inch, 1.0*inch]
                elif cat["type"] == "budget":
                    summary_rows = [["Budgeted", cat["budgeted"], "Spent", cat["spent"], "Remaining", cat["remaining"]]]
                    summary_cols = [0.8*inch, 1.0*inch, 0.6*inch, 1.0*inch, 0.9*inch, 1.0*inch]

                if summary_rows:
                    t = Table(summary_rows, colWidths=summary_cols)
                    t.setStyle(TableStyle([
                        ("FONTSIZE",      (0,0),(-1,-1), 8),
                        ("TEXTCOLOR",     (0,0),(0,-1),  tbl_muted),
                        ("TEXTCOLOR",     (1,0),(1,-1),  tbl_text),
                        ("TEXTCOLOR",     (2,0),(2,-1),  tbl_muted),
                        ("TEXTCOLOR",     (3,0),(3,-1),  tbl_text),
                        ("TEXTCOLOR",     (4,0),(4,-1),  tbl_muted),
                        ("TEXTCOLOR",     (5,0),(5,-1),  tbl_text),
                        ("TOPPADDING",    (0,0),(-1,-1), 1),
                        ("BOTTOMPADDING", (0,0),(-1,-1), 1),
                    ]))
                    story.append(t)
                    story.append(Spacer(1, 3))

                tbl = make_table(cat["transactions"])
                if tbl:
                    story.append(tbl)
                story.append(Spacer(1, 5))

            story.append(Spacer(1, 4))
        story.append(Spacer(1, 8))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()
