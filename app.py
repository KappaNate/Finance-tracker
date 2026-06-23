from flask import (Flask, render_template, request,
                   redirect, url_for, jsonify, Response, send_file)
import database
import db_manager
import export as exp
from datetime import datetime, date
import sqlite3
import os
import sys
import shutil
import threading
import calendar

APP_VERSION = "1.1.4"

def resource_path(relative_path):
    """Get absolute path — works for dev and PyInstaller bundles."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative_path)

app = Flask(
    __name__,
    template_folder=resource_path('templates'),
    static_folder=resource_path('static'),
)
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.errorhandler(Exception)
def _handle_exception(e):
    import traceback as _tb_mod
    _log = os.path.join(_PREFS_DIR, 'error.log')
    try:
        with open(_log, 'a') as _f:
            _f.write(_tb_mod.format_exc() + '\n')
    except Exception:
        pass
    return "An unexpected error occurred. Check error.log in ~/.budget_app for details.", 500

_PREFS_DIR = os.path.join(os.path.expanduser('~'), '.budget_app')
os.makedirs(_PREFS_DIR, exist_ok=True)
_db_keys = {}   # db filename → passphrase (in-memory only, cleared on restart)
_GUIDE_FLAG = os.path.join(_PREFS_DIR, 'guide_shown')

def _activate_db():
    active_file = db_manager.get_active_file()
    db_path     = db_manager.get_db_path(active_file)
    database.set_active_db(db_path)
    database.set_active_key(_db_keys.get(active_file))
    database.set_active_kdf_iter(db_manager.get_kdf_iter(active_file))
    # Skip init_db if the DB is encrypted but not yet unlocked this session
    if not db_manager.get_encrypted(active_file) or active_file in _db_keys:
        database.init_db()

_activate_db()

def _pay_by_display(day_str, year, month):
    """Convert a stored day-of-month value to a full YYYY-MM-DD for the viewed month."""
    if not day_str:
        return ""
    try:
        day = int(day_str)
        last_day = calendar.monthrange(year, month)[1]
        day = min(max(day, 1), last_day)
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (ValueError, TypeError):
        return str(day_str)

def get_viewed_month(req):
    now = datetime.now()
    try:
        year  = int(req.args.get("year",  now.year))
        month = int(req.args.get("month", now.month))
        if month < 1:  year -= 1; month = 12
        if month > 12: year += 1; month = 1
    except (ValueError, TypeError):
        year, month = now.year, now.month
    return year, month

@app.route("/favicon.ico")
def favicon():
    return send_file(resource_path('icon.ico'), mimetype='image/x-icon')

@app.route("/")
def index():
    return _index_inner()

def _index_inner():
    now   = datetime.now()
    year, month = get_viewed_month(request)

    # If the active DB is encrypted but not yet unlocked, go straight to unlock
    active_file = db_manager.get_active_file()
    if db_manager.get_encrypted(active_file) and active_file not in _db_keys:
        unlock_file  = request.args.get("unlock", active_file)
        unlock_error = request.args.get("unlock_error", "")
        db_registry  = db_manager.get_all()
        return render_template("index.html",
            year=year, month=month,
            month_label=date(year, month, 1).strftime("%B %Y"),
            month_name=date(year, month, 1).strftime("%B"),
            year_options=[], current_year_months=[], prev_year=year, prev_month=month,
            next_year=year, next_month=month, month_options=[],
            account_data=[], all_categories=[], total_income=0, total_expenses=0,
            overall_balance=0, total_debt_remaining=0,
            open_panels="", scroll_to="", currency="$", all_accounts=[],
            db_registry=db_registry, active_db_name=db_manager.get_active_name(),
            active_db_file=active_file, active_db_path=db_manager.get_db_path(active_file),
            calendar_events=[], app_version=APP_VERSION, show_guide=False,
            unlock_file=unlock_file, unlock_error=unlock_error,
            pw_error_file="", pw_error="", pw_error_op="",
        )

    database.add_active_month(now.year, now.month)
    database.ensure_debt_accounts_in_month(year, month)

    prev_month = month - 1 if month > 1 else 12
    prev_year  = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year if month < 12 else year + 1

    raw_months   = database.get_active_months()
    viewed_found = any(m["year"] == year and m["month"] == month for m in raw_months)
    if not viewed_found:
        raw_months.append({"year": year, "month": month})
        raw_months.sort(key=lambda x: (x["year"], x["month"]), reverse=True)

    month_options = [{
        "year":       m["year"],
        "month":      m["month"],
        "label":      date(m["year"], m["month"], 1).strftime("%B %Y"),
        "month_name": date(m["year"], m["month"], 1).strftime("%B"),
        "selected":   (m["year"] == year and m["month"] == month)
    } for m in raw_months]

    # Two-dropdown nav: unique years (desc) and months for the current year
    year_months_map = {}
    for m in raw_months:
        year_months_map.setdefault(m["year"], []).append(m["month"])
    year_options = []
    for y in sorted(year_months_map.keys(), reverse=True):
        months_in_yr = sorted(year_months_map[y], reverse=True)
        nav_month = month if month in months_in_yr else months_in_yr[0]
        year_options.append({"year": y, "nav_month": nav_month, "selected": (y == year)})
    current_year_months = [m for m in month_options if m["year"] == year]

    currency     = database.get_setting("currency_symbol") or "$"
    accounts     = database.get_accounts()
    all_cats     = database.get_categories()
    account_data = []

    for account in accounts:
        # Skip account if viewing outside its active range
        if account["start_year"] and account["start_month"]:
            if (year, month) < (account["start_year"], account["start_month"]):
                continue
        if account["end_year"] and account["end_month"]:
            if (year, month) > (account["end_year"], account["end_month"]):
                continue

        categories   = database.get_categories(account["id"])
        folders      = database.get_folders(account["id"])
        acct_summary = database.get_account_summary(account["id"], year, month)
        is_debt      = account["type"] == "Debt"
        is_invest    = account["type"] == "Investment"
        cat_data     = []
        annual_reserved = 0

        if is_debt:
            debt_starting  = database.get_debt_account_starting_balance(account["id"], year, month)
            total_expenses = 0.0
            total_payments = 0.0
            debt_remaining = 0.0

            for cat in categories:
                # Skip category if viewing outside its active range
                if cat["start_year"] and cat["start_month"]:
                    if (year, month) < (cat["start_year"], cat["start_month"]):
                        continue
                if cat["end_year"] and cat["end_month"]:
                    if (year, month) > (cat["end_year"], cat["end_month"]):
                        continue
                cat_summary  = database.get_category_summary(cat["id"], year, month)
                ctype        = cat["category_type"]
                total_spent  = cat_summary["total_spent"]  if cat_summary else 0
                total_income = cat_summary["total_income"] if cat_summary else 0
                base_limit   = cat["budget_limit"] or 0

                if ctype == "transfer":
                    raw_tr = database.get_transfers_for_category(cat["id"], year, month)
                    transfer_list = []
                    for tr in raw_tr:
                        td = dict(tr)
                        is_out = (td["from_account_id"] == account["id"])
                        txn_id = td.get("from_transaction_id") if is_out else td.get("to_transaction_id")
                        td["note"]            = database.get_note("transaction", txn_id, year, month) if txn_id else ""
                        td["relevant_txn_id"] = txn_id
                        transfer_list.append(td)
                    total_in  = sum(td["amount"] for td in transfer_list if td["from_account_id"] != account["id"])
                    total_out = sum(td["amount"] for td in transfer_list if td["from_account_id"] == account["id"])
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "transfer",
                        "transfers": transfer_list,
                        "account_id": account["id"],
                        "total_in": total_in, "total_out": total_out,
                        "budget_limit":0,"total_spent":0,"total_income":0,"remaining":0,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype in ("loan","credit_card"):
                    starting  = database.get_debt_category_starting_balance(cat["id"], base_limit, year, month)
                    remaining = starting + total_spent - total_income
                    total_expenses += total_spent
                    total_payments += total_income
                    debt_remaining += remaining
                    txns = database.get_transactions_with_running_balance(cat["id"], base_limit, year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": ctype,
                        "base_limit": base_limit, "starting": starting,
                        "total_spent": total_spent, "total_income": total_income,
                        "remaining": remaining,
                        "pay_by_date": cat["pay_by_date"] or "",
                        "pay_by_date_display": _pay_by_display(cat["pay_by_date"] or "", year, month),
                        "minimum_due": database.get_effective_minimum_due(cat["id"], year, month),
                        "transactions": txns, "budget_limit": base_limit,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                else:
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": ctype,
                        "budget_limit":0,"total_spent":0,"total_income":0,"remaining":0,
                        "transactions":[],
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })

            for c in cat_data:
                items = c.get("transfers", []) if c.get("category_type") == "transfer" else c.get("transactions", [])
                c["has_pending"] = any(t.get("is_pending") for t in items)
            folder_list = [dict(f) for f in folders]
            for fdict in folder_list:
                fdict["has_pending"] = any(c["has_pending"] for c in cat_data if c.get("folder_id") == fdict["id"])
            account_data.append({
                "id": account["id"], "name": account["name"],
                "type": account["type"], "is_debt": True, "is_invest": False,
                "debt_starting": debt_starting,
                "total_expenses": total_expenses,
                "total_payments": total_payments,
                "debt_remaining": debt_remaining,
                "has_pending":    any(c["has_pending"] for c in cat_data),
                "folders":        folder_list,
                "categories": cat_data,
                "annual_reserved": 0,
                "starting_balance_manual": account["starting_balance"],
                "note": database.get_note("account", account["id"], year, month),
                "start_year":  account["start_year"],
                "start_month": account["start_month"],
            })

        elif is_invest:
            total_income = 0.0
            total_spent  = 0.0

            for cat in categories:
                # Skip category if viewing outside its active range
                if cat["start_year"] and cat["start_month"]:
                    if (year, month) < (cat["start_year"], cat["start_month"]):
                        continue
                if cat["end_year"] and cat["end_month"]:
                    if (year, month) > (cat["end_year"], cat["end_month"]):
                        continue
                cat_summary  = database.get_category_summary(cat["id"], year, month)
                ctype        = cat["category_type"]
                t_spent      = cat_summary["total_spent"]  if cat_summary else 0
                t_income     = cat_summary["total_income"] if cat_summary else 0
                base_limit   = cat["budget_limit"] or 0

                if ctype == "transfer":
                    raw_tr = database.get_transfers_for_category(cat["id"], year, month)
                    transfer_list = []
                    for tr in raw_tr:
                        td = dict(tr)
                        is_out = (td["from_account_id"] == account["id"])
                        txn_id = td.get("from_transaction_id") if is_out else td.get("to_transaction_id")
                        td["note"]            = database.get_note("transaction", txn_id, year, month) if txn_id else ""
                        td["relevant_txn_id"] = txn_id
                        transfer_list.append(td)
                    total_in  = sum(td["amount"] for td in transfer_list if td["from_account_id"] != account["id"])
                    total_out = sum(td["amount"] for td in transfer_list if td["from_account_id"] == account["id"])
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "transfer",
                        "transfers": transfer_list,
                        "account_id": account["id"],
                        "total_in": total_in, "total_out": total_out,
                        "budget_limit":0,"total_spent":0,"total_income":0,"remaining":0,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype == "investment":
                    starting  = database.get_investment_category_starting_balance(cat["id"], base_limit, year, month)
                    end_bal   = starting + t_income - t_spent
                    gain_loss = t_income - t_spent
                    total_income += t_income
                    total_spent  += t_spent
                    txns = database.get_transactions_with_investment_balance(cat["id"], base_limit, year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "investment",
                        "base_limit": base_limit, "starting": starting,
                        "total_income": t_income, "total_spent": t_spent,
                        "gain_loss": gain_loss, "end_balance": end_bal,
                        "transactions": txns, "budget_limit": base_limit,
                        "remaining": end_bal,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype == "interest":
                    interest_rec = database.get_interest(account["id"], year, month)
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    total_income += t_income
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "interest",
                        "total_income": t_income,
                        "interest_rec": dict(interest_rec) if interest_rec else None,
                        "transactions": txns,
                        "budget_limit":0,"total_spent":0,"remaining":0,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                else:
                    remaining = base_limit - t_spent
                    total_income += t_income
                    total_spent  += t_spent
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "budget",
                        "budget_limit": base_limit, "total_spent": t_spent,
                        "total_income": t_income, "remaining": remaining,
                        "transactions": txns,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })

            for cat in cat_data:
                cat.setdefault("budget_limit", 0)
                cat.setdefault("total_spent",  0)
                cat.setdefault("total_income", 0)
                cat.setdefault("remaining",    0)

            carryover   = database.get_account_carryover(account["id"], year, month)
            net_balance = total_income - total_spent + carryover
            annual_reserved = database.get_annual_reserved(account["id"], year, month)

            for c in cat_data:
                items = c.get("transfers", []) if c.get("category_type") == "transfer" else c.get("transactions", [])
                c["has_pending"] = any(t.get("is_pending") for t in items)
            folder_list = [dict(f) for f in folders]
            for fdict in folder_list:
                fdict["has_pending"] = any(c["has_pending"] for c in cat_data if c.get("folder_id") == fdict["id"])
            account_data.append({
                "id": account["id"], "name": account["name"],
                "type": account["type"], "is_debt": False, "is_invest": True,
                "total_income": total_income, "total_spent": total_spent,
                "total_budgeted": acct_summary["total_budgeted"],
                "carryover": carryover, "net_balance": net_balance,
                "annual_reserved": 0,
                "has_pending":    any(c["has_pending"] for c in cat_data),
                "folders":        folder_list,
                "categories": cat_data,
                "note": database.get_note("account", account["id"], year, month),
                "start_year":  account["start_year"],
                "start_month": account["start_month"],
            })

        else:
            for cat in categories:
                # Skip category if viewing outside its active range
                if cat["start_year"] and cat["start_month"]:
                    if (year, month) < (cat["start_year"], cat["start_month"]):
                        continue
                if cat["end_year"] and cat["end_month"]:
                    if (year, month) > (cat["end_year"], cat["end_month"]):
                        continue
                cat_summary  = database.get_category_summary(cat["id"], year, month)
                total_spent  = cat_summary["total_spent"]  if cat_summary else 0
                total_income = cat_summary["total_income"] if cat_summary else 0
                budget_limit = cat["budget_limit"] or 0
                ctype        = cat["category_type"]
                annual_reserved = database.get_annual_reserved(account["id"], year, month)

                if ctype == "transfer":
                    raw_tr = database.get_transfers_for_category(cat["id"], year, month)
                    transfer_list = []
                    for tr in raw_tr:
                        td = dict(tr)
                        is_out = (td["from_account_id"] == account["id"])
                        txn_id = td.get("from_transaction_id") if is_out else td.get("to_transaction_id")
                        td["note"]            = database.get_note("transaction", txn_id, year, month) if txn_id else ""
                        td["relevant_txn_id"] = txn_id
                        transfer_list.append(td)
                    total_in  = sum(td["amount"] for td in transfer_list if td["from_account_id"] != account["id"])
                    total_out = sum(td["amount"] for td in transfer_list if td["from_account_id"] == account["id"])
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "transfer",
                        "transfers": transfer_list,
                        "account_id": account["id"],
                        "total_in": total_in, "total_out": total_out,
                        "budget_limit":0,"total_spent":0,"total_income":0,"remaining":0,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype == "income":
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "income",
                        "total_income": total_income,
                        "budget_limit":0,"total_spent":0,"remaining":0,
                        "transactions": txns,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype == "interest":
                    interest_rec = database.get_interest(account["id"], year, month)
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "interest",
                        "total_income": total_income,
                        "interest_rec": dict(interest_rec) if interest_rec else None,
                        "transactions": txns,
                        "budget_limit":0,"total_spent":0,"remaining":0,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                elif ctype == "reserve":
                    base_limit    = cat["budget_limit"] or 0
                    reserve_start = database.get_investment_category_starting_balance(
                        cat["id"], base_limit, year, month
                    )
                    reserve_balance = reserve_start + total_income - total_spent
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type":   "reserve",
                        "base_limit":      base_limit,
                        "reserve_start":   reserve_start,
                        "total_income":    total_income,
                        "total_spent":     total_spent,
                        "reserve_balance": reserve_balance,
                        "budget_limit": 0,
                        "remaining":    0,
                        "transactions": txns,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })
                else:
                    budget_limit = database.get_effective_budget_limit(cat["id"], year, month)
                    pay_by_day   = database.get_effective_pay_by_date(cat["id"], year, month)
                    remaining = budget_limit - total_spent
                    txns = database.get_transactions(cat["id"], year, month)
                    for t in txns:
                        t["note"] = database.get_note("transaction", t["id"], year, month)
                    cat_data.append({
                        "id": cat["id"], "name": cat["name"],
                        "category_type": "budget",
                        "budget_limit": budget_limit,
                        "total_spent": total_spent,
                        "total_income": total_income,
                        "remaining": remaining,
                        "is_annual": cat["is_annual"],
                        "pay_by_date": pay_by_day,
                        "pay_by_date_display": _pay_by_display(pay_by_day, year, month),
                        "transactions": txns,
                        "note": database.get_note("category", cat["id"], year, month),
                        "folder_id":          cat["folder_id"],
                        "annual_pay_by_date": cat["annual_pay_by_date"] if "annual_pay_by_date" in cat.keys() else "",
                        "cat_start_year":  cat["start_year"],
                        "cat_start_month": cat["start_month"],
                        "cat_end_year":    cat["end_year"],
                        "cat_end_month":   cat["end_month"],
                        "start_year":  account["start_year"],
                        "start_month": account["start_month"],
                    })

            for cat in cat_data:
                cat.setdefault("budget_limit", 0)
                cat.setdefault("total_spent",  0)
                cat.setdefault("total_income", 0)
                cat.setdefault("remaining",    0)

            carryover   = database.get_account_carryover(account["id"], year, month)
            net_balance = acct_summary["total_income"] - acct_summary["total_spent"] + carryover
            annual_reserved = database.get_annual_reserved(account["id"], year, month)
            total_reserved  = sum(c.get("reserve_balance", 0) for c in cat_data if c.get("category_type") == "reserve")
            unreserved      = net_balance - total_reserved

            for c in cat_data:
                items = c.get("transfers", []) if c.get("category_type") == "transfer" else c.get("transactions", [])
                c["has_pending"] = any(t.get("is_pending") for t in items)
            folder_list = [dict(f) for f in folders]
            for fdict in folder_list:
                fdict["has_pending"] = any(c["has_pending"] for c in cat_data if c.get("folder_id") == fdict["id"])
            account_data.append({
                "id": account["id"], "name": account["name"],
                "type": account["type"], "is_debt": False, "is_invest": False,
                "total_income":   acct_summary["total_income"],
                "total_budgeted": acct_summary["total_budgeted"],
                "total_spent":    acct_summary["total_spent"],
                "carryover":      carryover,
                "net_balance":      net_balance,
                "annual_reserved":  annual_reserved,
                "total_reserved":   total_reserved,
                "unreserved":       unreserved,
                "has_pending":    any(c["has_pending"] for c in cat_data),
                "folders":        folder_list,
                "categories":       cat_data,
                "note": database.get_note("account", account["id"], year, month),
                "start_year":  account["start_year"],
                "start_month": account["start_month"],
            })

    overall_balance      = database.get_running_balance(account_data, year, month)
    total_debt_remaining = sum(a["debt_remaining"] for a in account_data if a.get("is_debt"))
    totals               = database.get_monthly_totals(year, month)

    # Build calendar events (pay-by dates + pending transactions)
    calendar_events = []
    seen_cal_tr = set()
    for _acct in account_data:
        for _cat in _acct["categories"]:
            _ctype = _cat.get("category_type", "")
            _pbd   = _cat.get("pay_by_date_display", "")
            if _pbd:
                _ev = {"date": _pbd, "kind": "payby",
                       "account": _acct["name"], "category": _cat["name"],
                       "category_type": _ctype, "detail": ""}
                if _ctype in ("loan", "credit_card"):
                    _min = _cat.get("minimum_due", 0) or 0
                    _ev["detail"] = f"Min due: {currency}{_min:.2f}"
                    _ev["amount"] = _min
                calendar_events.append(_ev)
            if _ctype == "transfer":
                for _tr in _cat.get("transfers", []):
                    if _tr.get("is_pending") and _tr.get("id") not in seen_cal_tr:
                        seen_cal_tr.add(_tr.get("id"))
                        calendar_events.append({
                            "date": _tr.get("date") or None,
                            "kind": "pending",
                            "account": _acct["name"], "category": _cat["name"],
                            "category_type": "transfer",
                            "detail": f"{_tr.get('from_account_name','')} → {_tr.get('to_account_name','')}",
                            "description": _tr.get("description") or "",
                            "amount": _tr.get("amount", 0),
                        })
            else:
                for _t in _cat.get("transactions", []):
                    if _t.get("is_pending"):
                        _tdate = _t.get("transaction_date") or _t.get("pay_by_date") or None
                        calendar_events.append({
                            "date": _tdate or None,
                            "kind": "pending",
                            "account": _acct["name"], "category": _cat["name"],
                            "category_type": _ctype,
                            "detail": _t.get("description") or "",
                            "payee": _t.get("payee") or "",
                            "amount": _t.get("amount", 0),
                        })
    open_panels     = request.args.get("open", "")
    scroll_to       = request.args.get("scroll_to", "")
    unlock_file     = request.args.get("unlock", "")
    unlock_error    = request.args.get("unlock_error", "")
    pw_error_file   = request.args.get("pw_file", "")
    pw_error        = request.args.get("pw_error", "")
    pw_error_op     = request.args.get("pw_op", "")

    db_registry    = db_manager.get_all()
    active_db_name = db_manager.get_active_name()
    active_db_file = db_manager.get_active_file()
    active_db_path = db_manager.get_db_path(active_db_file)

    try:
        shown_version = open(_GUIDE_FLAG).read().strip()
    except OSError:
        shown_version = ''
    show_guide = shown_version != APP_VERSION
    if show_guide:
        try:
            open(_GUIDE_FLAG, 'w').write(APP_VERSION)
        except OSError:
            pass

    return render_template("index.html",
        year=year, month=month,
        month_label=date(year, month, 1).strftime("%B %Y"),
        month_name=date(year, month, 1).strftime("%B"),
        year_options=year_options,
        current_year_months=current_year_months,
        prev_year=prev_year,   prev_month=prev_month,
        next_year=next_year,   next_month=next_month,
        month_options=month_options,
        account_data=account_data,
        all_categories=all_cats,
        total_income=totals["total_income"],
        total_expenses=totals["total_expenses"],
        overall_balance=overall_balance,
        total_debt_remaining=total_debt_remaining,
        open_panels=open_panels,
        scroll_to=scroll_to,
        currency=currency,
        all_accounts=accounts,
        db_registry=db_registry,
        active_db_name=active_db_name,
        active_db_file=active_db_file,
        active_db_path=active_db_path,
        calendar_events=calendar_events,
        app_version=APP_VERSION,
        show_guide=show_guide,
        unlock_file=unlock_file,
        unlock_error=unlock_error,
        pw_error_file=pw_error_file,
        pw_error=pw_error,
        pw_error_op=pw_error_op,
    )

# ── Notes API ────────────────────────────────────────────────────────────────
@app.route("/save_note", methods=["POST"])
def save_note():
    data = request.json
    database.save_note(
        data["entity_type"], int(data["entity_id"]),
        int(data["year"]), int(data["month"]),
        data.get("content", "")
    )
    return jsonify({"status": "ok"})

# ── Settings ─────────────────────────────────────────────────────────────────
@app.route("/save_settings", methods=["POST"])
def save_settings():
    database.set_setting("currency_symbol", request.form.get("currency_symbol", "$"))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

# ── Database management ───────────────────────────────────────────────────────
@app.route("/switch_db", methods=["POST"])
def switch_db():
    file = request.form.get("file")
    if not file:
        return redirect(url_for("index"))
    if db_manager.get_encrypted(file) and file not in _db_keys:
        return redirect(url_for("index", unlock=file))
    if db_manager.switch(file):
        _activate_db()
    return redirect(url_for("index"))

@app.route("/create_db", methods=["POST"])
def create_db():
    name     = request.form.get("db_name", "").strip()
    uploaded = request.files.get("db_file")
    if not name:
        return redirect(url_for("index"))
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
    filename  = safe_name.replace(" ", "_") + ".db"
    db_path   = db_manager.get_db_path(filename)
    if uploaded and uploaded.filename:
        uploaded.save(db_path)
    db_manager.create(name, filename)
    database.set_active_db(db_path)
    database.init_db()
    return redirect(url_for("index"))

@app.route("/rename_db", methods=["POST"])
def rename_db():
    file     = request.form.get("file")
    new_name = request.form.get("new_name", "").strip()
    if file and new_name:
        db_manager.rename(file, new_name)
    return redirect(url_for("index"))

@app.route("/delete_db", methods=["POST"])
def delete_db():
    file    = request.form.get("file")
    db_data = db_manager.get_all()
    if file and len(db_data["databases"]) > 1:
        db_path = db_manager.get_db_path(file)
        db_manager.delete(file)
        if os.path.exists(db_path):
            os.remove(db_path)
        _activate_db()
    return redirect(url_for("index"))

@app.route("/download_db", methods=["GET"])
def download_db():
    file = request.args.get("file", "")
    if not file:
        return redirect(url_for("index"))
    db_path = db_manager.get_db_path(file)
    if not os.path.exists(db_path):
        return redirect(url_for("index"))
    return send_file(db_path, as_attachment=True, download_name=file)

@app.route("/import_db", methods=["POST"])
def import_db():
    uploaded    = request.files.get("db_file")
    custom_name = request.form.get("db_name", "").strip()
    if not uploaded or not uploaded.filename:
        return redirect(url_for("index"))
    base = os.path.splitext(uploaded.filename)[0]
    name = custom_name if custom_name else base.replace("_", " ").replace("-", " ").title()
    safe = "".join(c for c in name if c.isalnum() or c in " _-").strip()
    filename = safe.replace(" ", "_") + ".db"
    db_path  = db_manager.get_db_path(filename)
    uploaded.save(db_path)
    enc = database.is_encrypted(db_path)
    db_manager.create(name, filename, encrypted=enc)
    if enc:
        # Switch to it but prompt for unlock before loading
        db_manager.switch(filename)
        return redirect(url_for("index", unlock=filename))
    database.set_active_db(db_path)
    database.init_db()
    return redirect(url_for("index"))

@app.route("/db_unlock", methods=["POST"])
def db_unlock():
    file     = request.form.get("file")
    password = request.form.get("password", "")
    if not file or not password:
        return redirect(url_for("index", unlock=file, unlock_error="1"))
    db_path     = db_manager.get_db_path(file)
    stored_iter = db_manager.get_kdf_iter(file)
    probe = [stored_iter] + [n for n in (None, 4000, 64000) if n != stored_iter]
    _UNSET = object()
    matched_iter = _UNSET
    for try_iter in probe:
        if database.verify_password(db_path, password, kdf_iter=try_iter):
            matched_iter = try_iter
            break
    if matched_iter is _UNSET:
        return redirect(url_for("index", unlock=file, unlock_error="1"))
    if matched_iter != stored_iter:
        db_manager.set_kdf_iter(file, matched_iter)
    _db_keys[file] = password
    db_manager.switch(file)
    _activate_db()
    return redirect(url_for("index"))

@app.route("/db_set_password", methods=["POST"])
def db_set_password():
    file         = request.form.get("file")
    current_pw   = request.form.get("current_password", "")
    new_pw       = request.form.get("new_password", "")
    is_encrypted = db_manager.get_encrypted(file)
    if not file or not new_pw:
        return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
    db_path           = db_manager.get_db_path(file)
    existing_kdf_iter = db_manager.get_kdf_iter(file)
    if is_encrypted:
        if file in _db_keys:
            # Already unlocked this session — compare in memory, no KDF re-run needed
            if current_pw != _db_keys[file]:
                return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
        else:
            # Probe multiple kdf_iter values in case the stored value is stale
            stored_iter = existing_kdf_iter
            probe = [stored_iter] + [n for n in (None, 4000) if n != stored_iter]
            matched = False
            for try_iter in probe:
                if database.verify_password(db_path, current_pw, kdf_iter=try_iter):
                    existing_kdf_iter = try_iter
                    matched = True
                    if try_iter != stored_iter:
                        db_manager.set_kdf_iter(file, try_iter)
                    break
            if not matched:
                return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
    # Password changes keep existing kdf_iter; new encryptions use SQLCipher default
    new_kdf_iter = existing_kdf_iter if is_encrypted else database.DEFAULT_KDF_ITER
    database.release_connection()
    ok = database.change_db_password(
        db_path, current_pw if is_encrypted else None, new_pw,
        kdf_iter=existing_kdf_iter, new_kdf_iter=new_kdf_iter
    )
    if not ok:
        return redirect(url_for("index", open="modal-manage-dbs", pw_error="2", pw_file=file, pw_op="set"))
    _db_keys[file] = new_pw
    db_manager.set_encrypted(file, True)
    db_manager.set_kdf_iter(file, new_kdf_iter)
    if file == db_manager.get_active_file():
        database.set_active_key(new_pw)
        database.set_active_kdf_iter(new_kdf_iter)
    return redirect(url_for("index"))

@app.route("/db_remove_password", methods=["POST"])
def db_remove_password():
    file       = request.form.get("file")
    current_pw = request.form.get("current_password", "")
    if not file or not current_pw:
        return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
    db_path = db_manager.get_db_path(file)
    if file in _db_keys:
        # Already unlocked this session — compare in memory, no KDF re-run needed
        if current_pw != _db_keys[file]:
            return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
        kdf_iter = db_manager.get_kdf_iter(file)
    else:
        # Probe multiple kdf_iter values in case the stored value is stale
        stored_iter = db_manager.get_kdf_iter(file)
        probe = [stored_iter] + [n for n in (None, 4000) if n != stored_iter]
        kdf_iter = None
        matched = False
        for try_iter in probe:
            if database.verify_password(db_path, current_pw, kdf_iter=try_iter):
                kdf_iter = try_iter
                matched = True
                if try_iter != stored_iter:
                    db_manager.set_kdf_iter(file, try_iter)
                break
        if not matched:
            return redirect(url_for("index", open="modal-manage-dbs", pw_error="1", pw_file=file))
    database.release_connection()
    ok = database.change_db_password(db_path, current_pw, None, kdf_iter=kdf_iter)
    if not ok:
        return redirect(url_for("index", open="modal-manage-dbs", pw_error="2", pw_file=file, pw_op="remove"))
    _db_keys.pop(file, None)
    db_manager.set_encrypted(file, False)
    db_manager.set_kdf_iter(file, None)
    if file == db_manager.get_active_file():
        database.set_active_key(None)
        database.set_active_kdf_iter(None)
    return redirect(url_for("index"))

# ── Export ────────────────────────────────────────────────────────────────────
@app.route("/export", methods=["POST"])
def export():
    fmt          = request.form.get("format", "csv")
    cat_ids      = [int(i) for i in request.form.getlist("cat_ids")]
    months_raw   = request.form.getlist("export_months")
    include_txns = request.form.get("include_transactions") == "1"
    year_months  = []
    for ym in months_raw:
        y, m = ym.split("-")
        year_months.append((int(y), int(m)))
    year_months.sort()
    if not cat_ids or not year_months:
        return redirect(url_for("index"))
    if fmt == "csv":
        data     = exp.generate_csv(cat_ids, year_months, include_txns)
        filename = "budget_export.csv"
        mimetype = "text/csv"
    else:
        data     = exp.generate_pdf(cat_ids, year_months, include_txns)
        filename = "budget_export.pdf"
        mimetype = "application/pdf"
    return Response(data, mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"})

# ── Active months ─────────────────────────────────────────────────────────────
@app.route("/add_month", methods=["POST"])
def add_month():
    y = int(request.form["year"])
    m = int(request.form["month"])
    database.add_active_month(y, m)
    return {"status": "ok", "year": y, "month": m}

# ── Reorder ───────────────────────────────────────────────────────────────────
@app.route("/reorder_accounts", methods=["POST"])
def reorder_accounts():
    database.reorder_accounts([int(i) for i in request.json.get("ids", [])])
    return {"status": "ok"}

@app.route("/reorder_categories", methods=["POST"])
def reorder_categories():
    database.reorder_categories([int(i) for i in request.json.get("ids", [])])
    return {"status": "ok"}

# ── Account routes ────────────────────────────────────────────────────────────
@app.route("/add_account", methods=["POST"])
def add_account():
    try:
        new_id = database.add_account(request.form["name"], request.form["type"])
        return redirect(url_for("index",
            year=request.form.get("year"), month=request.form.get("month"),
            open=request.form.get("open", ""),
            scroll_to=f"account-panel-{new_id}"
        ))
    except sqlite3.IntegrityError:
        return jsonify({"error": f"An account named '{request.form['name']}' already exists."}), 400

@app.route("/edit_account", methods=["POST"])
def edit_account():
    sb = float(request.form.get("starting_balance") or 0)
    database.edit_account(
        int(request.form["account_id"]),
        request.form["name"], request.form["type"], sb
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/delete_account", methods=["POST"])
def delete_account():
    database.delete_account(int(request.form["account_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

# ── Category routes ───────────────────────────────────────────────────────────
@app.route("/add_category", methods=["POST"])
def add_category():
    account_id         = int(request.form["account_id"])
    category_type      = request.form.get("category_type", "budget")
    pay_by_date        = request.form.get("pay_by_date", "") or request.form.get("budget_pay_by_date", "")
    minimum_due        = float(request.form.get("minimum_due") or 0)
    is_annual          = 1 if request.form.get("is_annual") else 0
    annual_pay_by_date = request.form.get("annual_pay_by_date", "")
    folder_id_raw      = request.form.get("folder_id", "")
    folder_id          = int(folder_id_raw) if folder_id_raw else None
    new_id = database.add_category(
        request.form["name"], account_id, category_type,
        pay_by_date, minimum_due, is_annual,
        annual_pay_by_date=annual_pay_by_date,
        folder_id=folder_id
    )
    if category_type in ('loan', 'credit_card') and request.form.get("starting_balance"):
        database.update_budget_limit(new_id, float(request.form.get("starting_balance") or 0))
    elif request.form.get("limit"):
        database.update_budget_limit(new_id, float(request.form.get("limit") or 0))
    if request.form.get("has_first_month") == "1":
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        database.set_category_start_month(new_id, fm_year, fm_month)
    open_set = set(filter(None, request.form.get("open", "").split(",")))
    open_set.add(f"account-{account_id}")
    if folder_id:
        open_set.add(f"folder-{folder_id}")
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=",".join(open_set),
        scroll_to=f"cat-block-{new_id}"
    ))

@app.route("/edit_category", methods=["POST"])
def edit_category():
    pay_by_date = request.form.get("pay_by_date", "")
    minimum_due = float(request.form.get("minimum_due") or 0)
    database.edit_category(
        int(request.form["category_id"]),
        request.form["name"], pay_by_date, minimum_due
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/delete_category", methods=["POST"])
def delete_category():
    database.delete_category(int(request.form["category_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/set_limit", methods=["POST"])
def set_limit():
    database.update_budget_limit(
        int(request.form["category_id"]),
        float(request.form.get("limit") or 0)
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

# ── Folder routes ─────────────────────────────────────────────────────────────
@app.route("/add_folder", methods=["POST"])
def add_folder():
    account_id = int(request.form["account_id"])
    new_id     = database.add_folder(request.form["name"], account_id)
    open_set   = set(filter(None, request.form.get("open","").split(",")))
    open_set.add(f"account-{account_id}")
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=",".join(open_set),
        scroll_to=f"folder-block-{new_id}"
    ))

@app.route("/edit_folder", methods=["POST"])
def edit_folder():
    database.edit_folder(int(request.form["folder_id"]), request.form["name"])
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open","")
    ))

@app.route("/delete_folder", methods=["POST"])
def delete_folder():
    database.delete_folder(int(request.form["folder_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open","")
    ))

@app.route("/reorder_folders", methods=["POST"])
def reorder_folders():
    database.reorder_folders([int(i) for i in request.json.get("ids",[])])
    return {"status": "ok"}

@app.route("/set_category_folder", methods=["POST"])
def set_category_folder():
    category_id = int(request.form["category_id"])
    folder_id   = request.form.get("folder_id")
    database.set_category_folder(
        category_id,
        int(folder_id) if folder_id else None
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open","")
    ))

# ── Start month routes ────────────────────────────────────────────────────────
@app.route("/set_account_start", methods=["POST"])
def set_account_start():
    database.set_account_start_month(
        int(request.form["account_id"]),
        int(request.form["year"]),
        int(request.form["month"])
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/clear_account_start", methods=["POST"])
def clear_account_start():
    database.clear_account_start_month(int(request.form["account_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/set_category_start", methods=["POST"])
def set_category_start():
    database.set_category_start_month(
        int(request.form["category_id"]),
        int(request.form["year"]),
        int(request.form["month"])
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/clear_category_start", methods=["POST"])
def clear_category_start():
    database.clear_category_start_month(int(request.form["category_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

# ── Interest routes ───────────────────────────────────────────────────────────
@app.route("/save_interest", methods=["POST"])
def save_interest():
    account_id  = int(request.form["account_id"])
    category_id = int(request.form["category_id"])
    year        = int(request.form["year"])
    month       = int(request.form["month"])
    mode        = request.form.get("interest_mode", "flat")
    amount      = float(request.form.get("interest_amount") or 0)
    rate        = float(request.form.get("interest_rate")   or 0)
    int_date    = request.form.get("interest_date", "")
    if not int_date:
        return redirect(url_for("index", year=year, month=month,
                                open=request.form.get("open", "")))
    database.upsert_interest(account_id, category_id, year, month,
                             mode, amount, rate, int_date)
    database.apply_interest_transaction(account_id, category_id, year, month,
                                        mode, amount, rate, int_date)
    open_set = set(filter(None, request.form.get("open", "").split(",")))
    open_set.add(f"account-{account_id}")
    open_set.add(f"cat-{category_id}")
    return redirect(url_for("index", year=year, month=month,
                            open=",".join(open_set)))

@app.route("/delete_interest", methods=["POST"])
def delete_interest():
    account_id  = int(request.form["account_id"])
    category_id = int(request.form["category_id"])
    year        = int(request.form["year"])
    month       = int(request.form["month"])
    database.delete_interest(account_id, year, month)
    conn = sqlite3.connect(database.get_active_db())
    conn.execute("""
        DELETE FROM transactions
        WHERE category_id=? AND transaction_date LIKE ? AND description='Auto Interest'
    """, (category_id, f"{year}-{month:02d}%"))
    conn.commit()
    conn.close()
    return redirect(url_for("index", year=year, month=month,
                            open=request.form.get("open", "")))

# ── Transfer routes ───────────────────────────────────────────────────────────
@app.route("/add_transfer", methods=["POST"])
def add_transfer():
    from_account_id = int(request.form["from_account_id"])
    to_account_id   = int(request.form["to_account_id"])
    transfer_date   = request.form["date"]
    amount          = float(request.form["amount"])
    description     = request.form.get("description", "")
    from_cat_id = database.ensure_transfer_category(from_account_id)
    to_cat_id   = database.ensure_transfer_category(to_account_id)
    is_pending = 1 if request.form.get("is_pending") else 0
    database.create_transfer(transfer_date, amount,
                             from_account_id, to_account_id,
                             from_cat_id, to_cat_id, description, is_pending)
    open_set = set(filter(None, request.form.get("open", "").split(",")))
    open_set.add(f"account-{from_account_id}")
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=",".join(open_set)
    ))

@app.route("/edit_transfer", methods=["POST"])
def edit_transfer():
    is_pending = 1 if request.form.get("is_pending") else 0
    database.edit_transfer(
        int(request.form["transfer_id"]),
        request.form["date"],
        float(request.form["amount"]),
        request.form.get("description", ""),
        is_pending
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/delete_transfer", methods=["POST"])
def delete_transfer():
    database.delete_transfer(int(request.form["transfer_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

# ── Transaction routes ────────────────────────────────────────────────────────
@app.route("/add_transaction", methods=["POST"])
def add_transaction():
    category_id = int(request.form["category_id"])
    txn_date    = request.form.get("transaction_date", "")
    pay_date    = request.form.get("pay_by_date", "")
    # If no date at all, store a month-bucket sentinel (YYYY-MM-99) so the
    # transaction is still visible when browsing that month while displaying
    # as blank in the UI (the -99 suffix is stripped on read).
    if not txn_date and not pay_date:
        yr  = int(request.form.get("year",  datetime.now().year))
        mo  = int(request.form.get("month", datetime.now().month))
        txn_date = f"{yr:04d}-{mo:02d}-99"
    is_pending = 1 if request.form.get("is_pending") else 0
    database.add_transaction(
        txn_date,
        pay_date,
        request.form.get("description", ""),
        request.form.get("payee", ""),
        request.form.get("payment_method", ""),
        float(request.form["amount"]),
        request.form["type"],
        category_id,
        is_pending
    )
    conn   = sqlite3.connect(database.get_active_db())
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    open_set = set(filter(None, request.form.get("open", "").split(",")))
    for cat in database.get_categories():
        if cat["id"] == category_id:
            open_set.add(f"account-{cat['account_id']}")
            break
    open_set.add(f"cat-{category_id}")
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=",".join(open_set), scroll_to=f"txn-{new_id}"
    ))

@app.route("/edit_transaction", methods=["POST"])
def edit_transaction():
    is_annual  = 1 if request.form.get("is_annual")  else 0
    is_pending = 1 if request.form.get("is_pending") else 0
    txn_date   = request.form.get("transaction_date", "")
    pay_date   = request.form.get("pay_by_date", "")
    if not txn_date and not pay_date:
        yr  = int(request.form.get("year",  datetime.now().year))
        mo  = int(request.form.get("month", datetime.now().month))
        txn_date = f"{yr:04d}-{mo:02d}-99"
    database.edit_transaction(
        int(request.form["transaction_id"]),
        txn_date,
        pay_date,
        request.form.get("description", ""),
        request.form.get("payee", ""),
        request.form.get("payment_method", ""),
        float(request.form["amount"]),
        request.form["type"],
        int(request.form["category_id"]),
        is_annual,
        is_pending
    )
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/delete_transaction", methods=["POST"])
def delete_transaction():
    database.delete_transaction(int(request.form["transaction_id"]))
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/edit_account_full", methods=["POST"])
def edit_account_full():
    account_id = int(request.form["account_id"])
    database.edit_account(
        account_id,
        request.form["name"],
        request.form["type"],
        float(request.form.get("starting_balance") or 0)
    )
    if request.form.get("first_month_sentinel"):
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        if request.form.get("has_first_month") == "1":
            database.set_account_start_month(account_id, fm_year, fm_month)
        else:
            database.clear_account_start_month(account_id)
    if request.form.get("last_month_sentinel"):
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        if request.form.get("has_last_month") == "1":
            database.set_account_end_month(account_id, fm_year, fm_month)
        else:
            database.clear_account_end_month(account_id)
    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))

@app.route("/edit_category_full", methods=["POST"])
def edit_category_full():
    category_id        = int(request.form["category_id"])
    is_annual          = 1 if request.form.get("is_annual") else 0
    annual_pay_by_date = request.form.get("annual_pay_by_date", "")

    # Handle pay_by_date with optional per-month history (same pattern as budget limit)
    pay_by_date_val   = request.form.get("pay_by_date", "")
    pay_by_apply_from = request.form.get("pay_by_apply_from", "").strip()
    if pay_by_apply_from and pay_by_date_val:
        # Write to history; preserve the current base value unchanged
        pay_by_for_edit = database.get_category_base_pay_by_date(category_id)
        parts = pay_by_apply_from.split("-")
        if len(parts) == 2:
            database.set_pay_by_date_from_month(
                category_id, int(parts[0]), int(parts[1]), pay_by_date_val
            )
    else:
        pay_by_for_edit = pay_by_date_val  # Update the base normally

    minimum_due_val        = float(request.form.get("minimum_due") or 0)
    minimum_due_apply_from = request.form.get("minimum_due_apply_from", "").strip()
    if minimum_due_apply_from:
        parts = minimum_due_apply_from.split("-")
        if len(parts) == 2:
            database.set_minimum_due_from_month(
                category_id, int(parts[0]), int(parts[1]), minimum_due_val
            )
        minimum_due_for_edit = database.get_category_base_minimum_due(category_id)
    else:
        minimum_due_for_edit = minimum_due_val

    database.edit_category(
        category_id,
        request.form["name"],
        pay_by_for_edit,
        minimum_due_for_edit,
        is_annual,
        annual_pay_by_date
    )

    # Debt first month + starting balance (loan/credit_card)
    # Skip entirely if the JS determined the user didn't touch the first-month checkbox
    if request.form.get("debt_edit_sentinel") and not request.form.get("preserve_first_month"):
        has_fm   = request.form.get("has_first_month") == "1"
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        if has_fm:
            database.update_budget_limit(category_id, float(request.form.get("starting_balance_debt") or 0))
            database.set_category_start_month(category_id, fm_year, fm_month)
        else:
            database.clear_category_start_month(category_id)
            database.update_budget_limit(category_id, 0)

    # Budget limit (budget categories only; not sent by debt form)
    elif request.form.get("limit") is not None and request.form.get("limit") != "":
        limit = float(request.form.get("limit") or 0)
        apply_from = request.form.get("apply_from", "").strip()
        if apply_from:
            parts = apply_from.split("-")
            if len(parts) == 2:
                af_year, af_month = int(parts[0]), int(parts[1])
                database.set_budget_limit_from_month(category_id, af_year, af_month, limit)
        else:
            database.update_budget_limit(category_id, limit)

    # First month checkbox (non-debt categories)
    if request.form.get("first_month_sentinel"):
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        if request.form.get("has_first_month") == "1":
            database.set_category_start_month(category_id, fm_year, fm_month)
        else:
            database.clear_category_start_month(category_id)

    # Last month checkbox (all non-transfer categories)
    if request.form.get("last_month_sentinel"):
        fm_year  = int(request.form.get("year",  datetime.now().year))
        fm_month = int(request.form.get("month", datetime.now().month))
        if request.form.get("has_last_month") == "1":
            database.set_category_end_month(category_id, fm_year, fm_month)
        else:
            database.clear_category_end_month(category_id)

    return redirect(url_for("index",
        year=request.form.get("year"), month=request.form.get("month"),
        open=request.form.get("open", "")
    ))


if __name__ == "__main__":
    import threading
    import webview

    port = int(os.environ.get("PORT", 5000))

    class WindowAPI:
        def __init__(self):
            self._win = None
            self._maximized = False

        def set_window(self, win):
            self._win = win

        def minimize(self):
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, 'Finance Tracker')
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0112, 0xF020, 0)  # WM_SYSCOMMAND SC_MINIMIZE

        def toggle_maximize(self):
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, 'Finance Tracker')
            if not hwnd:
                return
            if self._maximized:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0112, 0xF120, 0)  # SC_RESTORE
                self._maximized = False
            else:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0112, 0xF030, 0)  # SC_MAXIMIZE
                self._maximized = True

        def close(self):
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, 'Finance Tracker')
            if hwnd:
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE

        def download_db(self, file):
            if not self._win or not file:
                return {'ok': False}
            db_path = db_manager.get_db_path(file)
            if not os.path.exists(db_path):
                return {'ok': False}
            dest = self._win.create_file_dialog(
                webview.FileDialog.SAVE, directory='', save_filename=file,
                file_types=('Database Files (*.db)', 'All files (*.*)')
            )
            if isinstance(dest, (list, tuple)):
                dest = dest[0] if dest else None
            if not dest:
                return {'ok': False}
            shutil.copy2(db_path, dest)
            return {'ok': True}

        def start_drag(self):
            import ctypes, ctypes.wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, 'Finance Tracker')
            if hwnd:
                pt = ctypes.wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                lp = (pt.y << 16) | (pt.x & 0xFFFF)
                user32.ReleaseCapture()
                user32.PostMessageW(hwnd, 0x00A1, 2, lp)  # WM_NCLBUTTONDOWN HTCAPTION

    api = WindowAPI()

    threading.Thread(
        target=lambda: app.run(debug=False, port=port, use_reloader=False, threaded=False),
        daemon=True
    ).start()

    import time
    time.sleep(1.0)

    def set_window_icon():
        import ctypes, time
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('FinanceTracker.App')
        time.sleep(1.5)
        icon_path = resource_path('icon.ico')
        if not os.path.exists(icon_path):
            return
        try:
            user32 = ctypes.windll.user32
            hIconBig   = user32.LoadImageW(None, icon_path, 1, 32, 32, 0x10)
            hIconSmall = user32.LoadImageW(None, icon_path, 1, 16, 16, 0x10)
            hwnd = user32.FindWindowW(None, 'Finance Tracker')
            if hwnd:
                user32.SendMessageW(hwnd, 0x0080, 0, hIconSmall)
                user32.SendMessageW(hwnd, 0x0080, 1, hIconBig)
                user32.SetClassLongPtrW(hwnd, -14, hIconBig)
                user32.SetClassLongPtrW(hwnd, -34, hIconSmall)
                # Re-enable resize/maximize/caption since frameless removes them.
                # WS_CAPTION (0xC00000) is required for Aero Snap detection by DWM;
                # it has no visual effect because WM_NCCALCSIZE keeps NC area at 0.
                GWL_STYLE = -16
                cur_style = user32.GetWindowLongW(hwnd, GWL_STYLE)
                user32.SetWindowLongW(hwnd, GWL_STYLE, cur_style | 0x00C70000)
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)
                # Tell DWM this window has a caption after all style changes are done.
                # A 1px top margin signals Aero Snap; invisible against dark background.
                class _MARGINS(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int),
                                 ("top",  ctypes.c_int), ("bottom", ctypes.c_int)]
                ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(
                    hwnd, ctypes.byref(_MARGINS(0, 0, 1, 0)))
        except Exception:
            pass

    def fix_border():
        if getattr(api, '_wndproc', None):
            return  # already installed — don't reinstall on every page reload
        import ctypes, ctypes.wintypes, time
        from ctypes import wintypes
        time.sleep(0.2)
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, 'Finance Tracker')
            if not hwnd:
                return

            WM_ERASEBKGND  = 0x0014
            WM_NCCALCSIZE  = 0x0083
            WM_NCHITTEST   = 0x0084
            WM_NCLBUTTONDOWN = 0x00A1
            WM_SIZE        = 0x0005
            SIZE_MINIMIZED = 1
            GWL_WNDPROC   = -4
            RESIZE_BORDER = 8
            HTCAPTION = 2
            HTCLIENT  = 1
            HTLEFT, HTRIGHT = 10, 11
            HTTOP, HTTOPLEFT, HTTOPRIGHT = 12, 13, 14
            HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
            _was_minimized = [False]

            # DPI-aware title bar hit-test metrics
            user32.GetDpiForWindow.restype = ctypes.c_uint
            dpi = user32.GetDpiForWindow(hwnd) or 96
            dpi_scale = dpi / 96.0
            TITLEBAR_H_PX    = int(28 * dpi_scale)   # matches CSS height: 28px
            BUTTON_AREA_W_PX = int(80 * dpi_scale)   # ~76px controls + 4px margin

            # Maximize clipping fix: clamp client rect to the work area
            WM_NCLBUTTONDOWN = 0x00A1
            WS_MAXIMIZE = 0x01000000

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize",    wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork",    wintypes.RECT),
                    ("dwFlags",   wintypes.DWORD),
                ]
            class WINDOWPLACEMENT(ctypes.Structure):
                _fields_ = [
                    ("length",           ctypes.c_uint),
                    ("flags",            ctypes.c_uint),
                    ("showCmd",          ctypes.c_uint),
                    ("ptMinPosition",    wintypes.POINT),
                    ("ptMaxPosition",    wintypes.POINT),
                    ("rcNormalPosition", wintypes.RECT),
                ]
            user32.MonitorFromWindow.restype  = ctypes.c_void_p
            # argtypes required so HMONITOR isn't truncated to 32 bits on 64-bit Windows
            user32.GetMonitorInfoW.argtypes   = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
            user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
            user32.DefWindowProcW.restype     = ctypes.c_ssize_t
            user32.GetCursorPos.argtypes      = [ctypes.POINTER(wintypes.POINT)]

            WNDPROCTYPE = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM,
            )

            # 64-bit safe: preserve full pointer width when reading/writing WndProc
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.restype   = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes  = [
                ctypes.c_ssize_t, wintypes.HWND,
                wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            ]

            old_proc = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)

            def _notify_js(script):
                try:
                    ctrl = getattr(api, '_webview2_ctrl', None)
                    if ctrl:
                        ctrl.ExecuteScriptAsync(script)
                except Exception:
                    pass

            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == WM_ERASEBKGND:
                    # Fill with app background color to prevent white flash on minimize/maximize
                    rc = wintypes.RECT()
                    user32.GetClientRect(hwnd, ctypes.byref(rc))
                    brush = ctypes.windll.gdi32.CreateSolidBrush(0x00252525)  # --bg-page
                    user32.FillRect(ctypes.c_void_p(wparam), ctypes.byref(rc), brush)
                    ctypes.windll.gdi32.DeleteObject(brush)
                    return 1
                if msg == WM_SIZE:
                    prev_min = _was_minimized[0]
                    _was_minimized[0] = (wparam == SIZE_MINIMIZED)
                    result = user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)
                    if prev_min and wparam != SIZE_MINIMIZED:
                        # Restoring from minimized — tell Windows to recompute the frame
                        # and flush pending paints.  JS-based repaints won't work here
                        # because WebView2's renderer is still resuming; the HTML-side
                        # visibilitychange handler fires once it's actually ready.
                        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)  # SWP_FRAMECHANGED|NOZORDER|NOMOVE|NOSIZE
                        user32.RedrawWindow(hwnd, None, None, 0x0085)  # RDW_INVALIDATE|RDW_ERASE|RDW_ALLCHILDREN
                    return result
                if msg == WM_NCCALCSIZE and wparam:
                    # When maximized, constrain client rect to work area so content
                    # doesn't extend into the off-screen shadow/border region.
                    if user32.GetWindowLongW(hwnd, -16) & WS_MAXIMIZE:
                        hMon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
                        if hMon:
                            mi = MONITORINFO()
                            mi.cbSize = ctypes.sizeof(MONITORINFO)
                            user32.GetMonitorInfoW(hMon, ctypes.byref(mi))
                            rc = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT))
                            rc[0].left   = mi.rcWork.left
                            rc[0].top    = mi.rcWork.top
                            rc[0].right  = mi.rcWork.right
                            rc[0].bottom = mi.rcWork.bottom
                    return 0  # no non-client area → no DWM accent border
                if msg == WM_NCHITTEST:
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    rc = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rc))
                    l = x - rc.left   < RESIZE_BORDER
                    r = rc.right  - x < RESIZE_BORDER
                    t = y - rc.top    < RESIZE_BORDER
                    b = rc.bottom - y < RESIZE_BORDER
                    if t and l: return HTTOPLEFT
                    if t and r: return HTTOPRIGHT
                    if b and l: return HTBOTTOMLEFT
                    if b and r: return HTBOTTOMRIGHT
                    if l: return HTLEFT
                    if r: return HTRIGHT
                    if t: return HTTOP
                    if b: return HTBOTTOM
                    # Title bar non-button area → HTCAPTION enables native drag and Aero Snap
                    if (y - rc.top) < TITLEBAR_H_PX:
                        if (x - rc.left) < (rc.right - rc.left) - BUTTON_AREA_W_PX:
                            return HTCAPTION
                    return HTCLIENT
                # -webkit-app-region: drag on #titlebar makes WebView2 emit
                # WM_NCLBUTTONDOWN HTCAPTION for title bar clicks.  DefWindowProc
                # enters a native SC_MOVE drag loop, which lets DWM show Snap Layouts.
                # If the window is maximized when the drag starts, restore it first
                # (no-flash: clear WS_MAXIMIZE + SetWindowPos atomically) so the
                # native move loop anchors at the correct position under the cursor.
                if msg == WM_NCLBUTTONDOWN and wparam == HTCAPTION:
                    if user32.GetWindowLongW(hwnd, -16) & WS_MAXIMIZE:
                        _pt = wintypes.POINT()
                        user32.GetCursorPos(ctypes.byref(_pt))
                        _wp = WINDOWPLACEMENT()
                        _wp.length = ctypes.sizeof(WINDOWPLACEMENT)
                        user32.GetWindowPlacement(hwnd, ctypes.byref(_wp))
                        _ww = _wp.rcNormalPosition.right  - _wp.rcNormalPosition.left
                        _wh = _wp.rcNormalPosition.bottom - _wp.rcNormalPosition.top
                        _hm = user32.MonitorFromWindow(hwnd, 2)
                        if _hm:
                            _mi = MONITORINFO()
                            _mi.cbSize = ctypes.sizeof(MONITORINFO)
                            user32.GetMonitorInfoW(_hm, ctypes.byref(_mi))
                            _wl = _mi.rcWork.left;  _wt = _mi.rcWork.top
                            _wr = _mi.rcWork.right; _wb = _mi.rcWork.bottom
                        else:
                            _wl = _wt = 0; _wr = 1920; _wb = 1080
                        _gr = (_pt.x - _wl) / max(1, _wr - _wl)
                        _nx = max(_wl, min(int(_pt.x - _gr * _ww), _wr - _ww))
                        _ny = max(_wt, min(_pt.y - TITLEBAR_H_PX // 2, _wb - _wh))
                        _sty = user32.GetWindowLongW(hwnd, -16)
                        user32.SetWindowLongW(hwnd, -16, _sty & ~WS_MAXIMIZE)
                        user32.SetWindowPos(hwnd, None, _nx, _ny, _ww, _wh,
                                            0x0004 | 0x0020)
                        api._maximized = False
                        _notify_js('tbSetMaximized(false)')
                    return user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)
                if msg == 0x00A5 and wparam == HTCAPTION:  # WM_NCRBUTTONUP — suppress system menu
                    return 0
                if msg == 0x0112:  # WM_SYSCOMMAND
                    _sc = wparam & 0xFFF0
                    if _sc == 0xF010:  # SC_MOVE safety net (window still maximized somehow)
                        if user32.GetWindowLongW(hwnd, -16) & WS_MAXIMIZE:
                            _pt = wintypes.POINT()
                            user32.GetCursorPos(ctypes.byref(_pt))
                            _wp = WINDOWPLACEMENT()
                            _wp.length = ctypes.sizeof(WINDOWPLACEMENT)
                            user32.GetWindowPlacement(hwnd, ctypes.byref(_wp))
                            _ww = _wp.rcNormalPosition.right  - _wp.rcNormalPosition.left
                            _wh = _wp.rcNormalPosition.bottom - _wp.rcNormalPosition.top
                            _hm = user32.MonitorFromWindow(hwnd, 2)
                            if _hm:
                                _mi = MONITORINFO()
                                _mi.cbSize = ctypes.sizeof(MONITORINFO)
                                user32.GetMonitorInfoW(_hm, ctypes.byref(_mi))
                                _wl = _mi.rcWork.left;  _wt = _mi.rcWork.top
                                _wr = _mi.rcWork.right; _wb = _mi.rcWork.bottom
                            else:
                                _wl = _wt = 0; _wr = 1920; _wb = 1080
                            _gr = (_pt.x - _wl) / max(1, _wr - _wl)
                            _nx = max(_wl, min(int(_pt.x - _gr * _ww), _wr - _ww))
                            _ny = max(_wt, min(_pt.y - TITLEBAR_H_PX // 2, _wb - _wh))
                            _sty = user32.GetWindowLongW(hwnd, -16)
                            user32.SetWindowLongW(hwnd, -16, _sty & ~WS_MAXIMIZE)
                            user32.SetWindowPos(hwnd, None, _nx, _ny, _ww, _wh,
                                                0x0004 | 0x0020)
                            api._maximized = False
                            _notify_js('tbSetMaximized(false)')
                            _new_lp = ((_pt.y & 0xFFFF) << 16) | (_pt.x & 0xFFFF)
                            return user32.CallWindowProcW(old_proc, hwnd, msg, wparam,
                                                          _new_lp)
                    elif _sc == 0xF030:  # SC_MAXIMIZE (snap, double-click, etc.)
                        result = user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)
                        api._maximized = True
                        _notify_js('tbSetMaximized(true)')
                        return result
                    elif _sc == 0xF120:  # SC_RESTORE
                        result = user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)
                        api._maximized = False
                        _notify_js('tbSetMaximized(false)')
                        return result
                if msg == 0x0232:  # WM_EXITSIZEMOVE — sync state after any native drag/snap
                    _is_max = bool(user32.GetWindowLongW(hwnd, -16) & WS_MAXIMIZE)
                    if _is_max != api._maximized:
                        api._maximized = _is_max
                        _notify_js('tbSetMaximized(true)' if _is_max else 'tbSetMaximized(false)')
                    return user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)
                return user32.CallWindowProcW(old_proc, hwnd, msg, wparam, lparam)

            proc = WNDPROCTYPE(wnd_proc)
            user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, proc)
            api._wndproc = proc  # prevent garbage collection
            user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0027)  # SWP_FRAMECHANGED

            # Cache WebView2 control for fire-and-forget ExecuteScriptAsync calls
            # from within wnd_proc (can't use evaluate_js there — it blocks the UI
            # thread on a semaphore waiting for a WebView2 callback that needs the
            # same UI thread to deliver it → 5-second deadlock).
            try:
                from webview.platforms.winforms import BrowserView as _BVClass
                for _bv in _BVClass.instances.values():
                    api._webview2_ctrl = _bv.browser.webview
                    break
            except Exception:
                pass

        except Exception:
            pass

    threading.Thread(target=set_window_icon, daemon=True).start()

    # Enable -webkit-app-region: drag CSS support in WebView2.
    # pywebview never sets IsNonClientRegionSupportEnabled; without it the
    # CSS property is silently ignored and no WM_NCLBUTTONDOWN is emitted.
    # Patch before create_window so the hook is installed before WebView2 init.
    try:
        from webview.platforms.edgechromium import EdgeChrome as _EC
        _orig_wv_ready = _EC.on_webview_ready
        def _patched_wv_ready(self, sender, args):
            try:
                sender.CoreWebView2.Settings.IsNonClientRegionSupportEnabled = True
            except Exception:
                pass
            _orig_wv_ready(self, sender, args)
        _EC.on_webview_ready = _patched_wv_ready
    except Exception:
        pass

    win = webview.create_window(
        'Finance Tracker',
        f'http://127.0.0.1:{port}',
        width=1200,
        height=800,
        min_size=(800, 600),
        frameless=True,
        easy_drag=False,  # disable pywebview's JS mousedown drag — we use WndProc HTCAPTION instead
        js_api=api,
        background_color='#252525',
    )
    api.set_window(win)
    # Apply DWM border fix after page load, when WinForms is fully settled
    win.events.loaded += lambda: threading.Thread(target=fix_border, daemon=True).start()
    webview.start()
    os._exit(0)