import sqlcipher3
import os
import threading

def _replace_file(src, dst):
    """Atomically replace dst with src, falling back to copy+delete on Windows when the
    destination is held by a handle opened without FILE_SHARE_DELETE."""
    try:
        os.replace(src, dst)
    except PermissionError:
        import shutil as _shutil
        _shutil.copy2(src, dst)
        try: os.remove(src)
        except OSError: pass

DB_NAME          = "budget.db"
DEFAULT_KDF_ITER = None   # None = SQLCipher default (PBKDF2-HMAC-SHA512, 256k iterations)

_active_db       = DB_NAME
_active_key      = None   # None = unencrypted; str = passphrase for current DB
_active_kdf_iter = None   # None = let SQLCipher use its default (256k)

# Per-thread connection cache: KDF runs once per thread rather than once per query.
# A global generation counter lets all threads detect a stale connection after a db/key switch.
_conn_local      = threading.local()
_cache_generation = 0

class _ConnWrapper:
    """Proxy for the cached connection; .close() is a no-op to preserve the connection across queries."""
    __slots__ = ('_c',)
    def __init__(self, conn):
        object.__setattr__(self, '_c', conn)
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_c'), name)
    def close(self):
        pass  # intentional no-op; connection lives until the cache is invalidated

def _invalidate_connection():
    """Increment the generation counter (invalidates all threads' caches) and close this thread's connection."""
    global _cache_generation
    _cache_generation += 1
    entry = getattr(_conn_local, 'entry', None)
    if entry is not None:
        try:
            entry[0].close()
        except Exception:
            pass
        _conn_local.entry = None

def release_connection():
    """Close this thread's cached connection. Call before any operation that replaces the DB file on disk."""
    _invalidate_connection()

def set_active_db(path):
    global _active_db
    _active_db = path
    _invalidate_connection()

def get_active_db():
    return _active_db

def set_active_key(key):
    global _active_key
    _active_key = key or None
    _invalidate_connection()

def get_active_key():
    return _active_key

def set_active_kdf_iter(n):
    global _active_kdf_iter
    _active_kdf_iter = int(n) if n else None
    _invalidate_connection()

def get_active_kdf_iter():
    return _active_kdf_iter

def _escape_key(key):
    return key.replace("'", "''")

def _apply_key(conn, key, kdf_iter=None):
    """Set kdf_iter (if specified) then PRAGMA key on conn."""
    if kdf_iter:
        conn.execute(f"PRAGMA kdf_iter = {int(kdf_iter)}")
    conn.execute(f"PRAGMA key='{_escape_key(key)}'")

def get_connection():
    """Return the cached authenticated connection for this thread, creating it (and running KDF) if needed."""
    entry = getattr(_conn_local, 'entry', None)
    # entry is (conn, generation); a generation mismatch means db/key/kdf_iter changed
    if entry is None or entry[1] != _cache_generation:
        if entry is not None:
            try:
                entry[0].close()
            except Exception:
                pass
        conn = sqlcipher3.connect(_active_db)
        conn.row_factory = sqlcipher3.Row
        if _active_key:
            _apply_key(conn, _active_key, _active_kdf_iter)
        entry = (conn, _cache_generation)
        _conn_local.entry = entry
    return _ConnWrapper(entry[0])

def is_encrypted(path):
    """Return True if path is an SQLCipher-encrypted database."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        conn = sqlcipher3.connect(path)
        conn.execute("SELECT count(*) FROM sqlite_master")
        conn.close()
        return False
    except Exception:
        return True

def verify_password(path, key, kdf_iter=None):
    """Return True if key correctly decrypts the database at path."""
    try:
        conn = sqlcipher3.connect(path)
        _apply_key(conn, key, kdf_iter)
        conn.execute("SELECT count(*) FROM sqlite_master")
        conn.close()
        return True
    except Exception:
        return False

def change_db_password(path, current_key, new_key, kdf_iter=None, new_kdf_iter=None):
    """
    Add, change, or remove encryption on a database file.
    current_key:  None/'' for unencrypted, passphrase for encrypted.
    new_key:      None/'' to remove encryption, passphrase to set/change.
    kdf_iter:     KDF iterations used to open the current encrypted file (None = SQLCipher default).
    Returns True on success, False on failure.
    """
    tmp = path + '._cipher_tmp'
    if os.path.exists(tmp):
        try: os.remove(tmp)
        except OSError: pass
    try:
        if current_key and new_key:
            # Encrypted → new key: PRAGMA rekey (in-place, no file replacement needed)
            conn = sqlcipher3.connect(path)
            _apply_key(conn, current_key, kdf_iter)
            conn.execute("SELECT count(*) FROM sqlite_master")
            conn.execute(f"PRAGMA rekey='{_escape_key(new_key)}'")
            conn.close()
        elif current_key and not new_key:
            # Encrypted → plaintext: ATTACH + sqlcipher_export (SQLCipher-recommended approach)
            escaped_tmp = tmp.replace("'", "''")
            conn = sqlcipher3.connect(path, isolation_level=None)
            _apply_key(conn, current_key, kdf_iter)
            conn.execute("SELECT count(*) FROM sqlite_master")
            conn.execute(f"ATTACH DATABASE '{escaped_tmp}' AS plaintext KEY ''")
            conn.execute("SELECT sqlcipher_export('plaintext')").fetchone()
            conn.execute("DETACH DATABASE plaintext")
            conn.close()
            del conn
            import gc as _gc; _gc.collect()
            _replace_file(tmp, path)
        else:
            # Plaintext → encrypted: ATTACH + sqlcipher_export.
            # isolation_level=None (autocommit) prevents Python's sqlite3 from wrapping
            # the export in an implicit BEGIN, which keeps the source file locked on Windows.
            escaped_tmp = tmp.replace("'", "''")
            conn = sqlcipher3.connect(path, isolation_level=None)
            conn.execute(f"ATTACH DATABASE '{escaped_tmp}' AS encrypted KEY '{_escape_key(new_key)}'")
            conn.execute("SELECT sqlcipher_export('encrypted')").fetchone()
            conn.execute("DETACH DATABASE encrypted")
            conn.close()
            del conn
            import gc as _gc; _gc.collect()
            _replace_file(tmp, path)
        return True
    except Exception:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        return False

def init_db():
    conn   = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('currency_symbol','$')")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL UNIQUE,
            type             TEXT NOT NULL,
            sort_order       INTEGER DEFAULT 0,
            starting_balance REAL DEFAULT 0,
            start_year       INTEGER DEFAULT NULL,
            start_month      INTEGER DEFAULT NULL,
            end_year         INTEGER DEFAULT NULL,
            end_month        INTEGER DEFAULT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT NOT NULL,
            budget_limit       REAL DEFAULT 0,
            account_id         INTEGER NOT NULL,
            sort_order         INTEGER DEFAULT 0,
            category_type      TEXT DEFAULT 'budget',
            pay_by_date        TEXT DEFAULT '',
            minimum_due        REAL DEFAULT 0,
            is_annual          INTEGER DEFAULT 0,
            annual_pay_by_date TEXT DEFAULT '',
            start_year         INTEGER DEFAULT NULL,
            start_month        INTEGER DEFAULT NULL,
            end_year           INTEGER DEFAULT NULL,
            end_month          INTEGER DEFAULT NULL,
            folder_id          INTEGER DEFAULT NULL,
            FOREIGN KEY (account_id) REFERENCES accounts(id),
            FOREIGN KEY (folder_id)  REFERENCES folders(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_date TEXT NOT NULL DEFAULT '',
            pay_by_date      TEXT DEFAULT '',
            description      TEXT DEFAULT '',
            payee            TEXT DEFAULT '',
            payment_method   TEXT DEFAULT '',
            amount           REAL NOT NULL,
            type             TEXT NOT NULL,
            category_id      INTEGER,
            is_annual        INTEGER DEFAULT 0,
            is_pending       INTEGER DEFAULT 0,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT NOT NULL,
            amount              REAL NOT NULL,
            from_account_id     INTEGER NOT NULL,
            to_account_id       INTEGER NOT NULL,
            from_transaction_id INTEGER,
            to_transaction_id   INTEGER,
            FOREIGN KEY (from_account_id)     REFERENCES accounts(id),
            FOREIGN KEY (to_account_id)       REFERENCES accounts(id),
            FOREIGN KEY (from_transaction_id) REFERENCES transactions(id),
            FOREIGN KEY (to_transaction_id)   REFERENCES transactions(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_months (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            year  INTEGER NOT NULL,
            month INTEGER NOT NULL,
            UNIQUE(year, month)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_interest (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id  INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            mode        TEXT NOT NULL DEFAULT 'flat',
            amount      REAL DEFAULT 0,
            rate        REAL DEFAULT 0,
            date        TEXT NOT NULL,
            UNIQUE(account_id, year, month),
            FOREIGN KEY (account_id)  REFERENCES accounts(id),
            FOREIGN KEY (category_id) REFERENCES categories(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id   INTEGER NOT NULL,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            content     TEXT DEFAULT '',
            UNIQUE(entity_type, entity_id, year, month)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS category_budget_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id  INTEGER NOT NULL,
            year         INTEGER NOT NULL,
            month        INTEGER NOT NULL,
            budget_limit REAL NOT NULL DEFAULT 0,
            UNIQUE(category_id, year, month),
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS category_pay_by_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id  INTEGER NOT NULL,
            year         INTEGER NOT NULL,
            month        INTEGER NOT NULL,
            pay_by_date  TEXT NOT NULL DEFAULT '',
            UNIQUE(category_id, year, month),
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS category_minimum_due_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            year        INTEGER NOT NULL,
            month       INTEGER NOT NULL,
            minimum_due REAL NOT NULL DEFAULT 0,
            UNIQUE(category_id, year, month),
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
    """)

    # Migrate existing databases to add end_year / end_month columns
    for tbl in ("accounts", "categories"):
        for col in ("end_year", "end_month"):
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER DEFAULT NULL")
            except Exception:
                pass  # column already exists

    # Migrate existing databases to add is_pending column to transactions
    try:
        cursor.execute("ALTER TABLE transactions ADD COLUMN is_pending INTEGER DEFAULT 0")
    except Exception:
        pass  # column already exists

    # Migrate per-month account/category notes to the persistent format (year=0, month=0).
    # For each entity, the most recent non-empty note wins.
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO notes (entity_type, entity_id, year, month, content)
            SELECT entity_type, entity_id, 0, 0, content
            FROM notes
            WHERE entity_type IN ('account', 'category')
              AND year  != 0
              AND content != ''
            ORDER BY year DESC, month DESC
        """)
    except Exception:
        pass

    conn.commit()
    conn.close()

# ---------- Settings ----------

def get_setting(key):
    conn = get_connection()
    row  = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def set_setting(key, value):
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

# ---------- Notes ----------

# Account and category notes are persistent (not tied to a specific month).
# Transaction notes remain month-specific.
_PERSISTENT_NOTE_TYPES = frozenset({'account', 'category'})

def get_note(entity_type, entity_id, year, month):
    if entity_type in _PERSISTENT_NOTE_TYPES:
        year, month = 0, 0
    conn = get_connection()
    row  = conn.execute("""
        SELECT content FROM notes
        WHERE entity_type=? AND entity_id=? AND year=? AND month=?
    """, (entity_type, entity_id, year, month)).fetchone()
    conn.close()
    return row["content"] if row else ""

def save_note(entity_type, entity_id, year, month, content):
    if entity_type in _PERSISTENT_NOTE_TYPES:
        year, month = 0, 0
    conn = get_connection()
    conn.execute("""
        INSERT INTO notes (entity_type, entity_id, year, month, content)
        VALUES (?,?,?,?,?)
        ON CONFLICT(entity_type, entity_id, year, month)
        DO UPDATE SET content=excluded.content
    """, (entity_type, entity_id, year, month, content))
    conn.commit()
    conn.close()

# ---------- Active Months ----------

def get_active_months():
    conn      = get_connection()
    explicit  = conn.execute("SELECT year,month FROM active_months").fetchall()
    with_data = conn.execute("""
        SELECT DISTINCT
            CAST(substr(transaction_date,1,4) AS INTEGER) as year,
            CAST(substr(transaction_date,6,2) AS INTEGER) as month
        FROM transactions
        WHERE transaction_date != ''
    """).fetchall()
    conn.close()
    seen, months = set(), []
    for row in list(explicit) + list(with_data):
        key = (row["year"], row["month"])
        if key not in seen:
            seen.add(key)
            months.append({"year": row["year"], "month": row["month"]})
    months.sort(key=lambda x: (x["year"], x["month"]), reverse=True)
    return months

def add_active_month(year, month):
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO active_months (year,month) VALUES (?,?)", (year, month))
    conn.commit()
    conn.close()

def ensure_debt_accounts_in_month(year, month):
    conn     = get_connection()
    has_debt = conn.execute(
        "SELECT COUNT(*) as n FROM accounts WHERE type='Debt'"
    ).fetchone()["n"]
    conn.close()
    if has_debt:
        add_active_month(year, month)

# ---------- Accounts ----------

def get_accounts():
    conn = get_connection()
    rows = conn.execute("SELECT * FROM accounts ORDER BY sort_order,id").fetchall()
    conn.close()
    return rows

def add_account(name, acct_type):
    conn      = get_connection()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM accounts"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO accounts (name,type,sort_order) VALUES (?,?,?)",
        (name, acct_type, max_order + 1)
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return new_id

def edit_account(account_id, name, acct_type, starting_balance=0):
    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET name=?,type=?,starting_balance=? WHERE id=?",
        (name, acct_type, starting_balance, account_id)
    )
    conn.commit()
    conn.close()

def delete_account(account_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM transfers WHERE from_account_id=? OR to_account_id=?",
        (account_id, account_id)
    )
    conn.execute(
        "DELETE FROM transactions WHERE category_id IN "
        "(SELECT id FROM categories WHERE account_id=?)", (account_id,)
    )
    conn.execute("DELETE FROM categories WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM account_interest WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
    conn.execute(
        "DELETE FROM notes WHERE entity_type='account' AND entity_id=?",
        (account_id,)
    )
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()
    conn.close()

def reorder_accounts(ordered_ids):
    conn = get_connection()
    for i, aid in enumerate(ordered_ids):
        conn.execute("UPDATE accounts SET sort_order=? WHERE id=?", (i, aid))
    conn.commit()
    conn.close()

def set_account_start_month(account_id, year, month):
    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET start_year=?, start_month=? WHERE id=?",
        (year, month, account_id)
    )
    conn.commit()
    conn.close()

def clear_account_start_month(account_id):
    conn = get_connection()
    conn.execute(
        "UPDATE accounts SET start_year=NULL, start_month=NULL WHERE id=?",
        (account_id,)
    )
    conn.commit()
    conn.close()

def set_account_end_month(account_id, year, month):
    conn = get_connection()
    conn.execute("UPDATE accounts SET end_year=?, end_month=? WHERE id=?",
                 (year, month, account_id))
    conn.commit()
    conn.close()

def clear_account_end_month(account_id):
    conn = get_connection()
    conn.execute("UPDATE accounts SET end_year=NULL, end_month=NULL WHERE id=?",
                 (account_id,))
    conn.commit()
    conn.close()

def get_account_summary(account_id, year, month):
    conn = get_connection()
    ms   = f"{year}-{month:02d}"
    result = conn.execute("""
        SELECT
            COALESCE((
                SELECT SUM(budget_limit)
                FROM categories
                WHERE account_id=? AND category_type='budget'
            ), 0) as total_budgeted,
            COALESCE(SUM(CASE WHEN t.type='expense' AND t.is_pending=0
                AND (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
                THEN t.amount ELSE 0 END),0) as total_spent,
            COALESCE(SUM(CASE WHEN t.type='income' AND t.is_pending=0
                AND (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
                THEN t.amount ELSE 0 END),0) as total_income
        FROM categories c
        LEFT JOIN transactions t ON t.category_id=c.id
        WHERE c.account_id=?
          AND c.category_type != 'reserve'
    """, (account_id, f"{ms}%", f"{ms}%", f"{ms}%", f"{ms}%", account_id)).fetchone()
    conn.close()
    return result

def get_account_carryover(account_id, year, month):
    """Cumulative net of ALL transactions before this month (income − expenses)."""
    cutoff = f"{year}-{month:02d}-01"
    conn   = get_connection()
    result = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN t.type='income'  THEN t.amount ELSE 0 END),0) -
            COALESCE(SUM(CASE WHEN t.type='expense' THEN t.amount ELSE 0 END),0) as carryover
        FROM transactions t
        JOIN categories c ON t.category_id=c.id
        WHERE c.account_id=?
          AND c.category_type != 'reserve'
          AND t.is_pending = 0
          AND (
            (t.transaction_date != '' AND t.transaction_date < ?) OR
            (t.transaction_date  = '' AND t.pay_by_date != '' AND t.pay_by_date < ?)
          )
    """, (account_id, cutoff, cutoff)).fetchone()
    conn.close()
    return result["carryover"] if result else 0.0

# ---------- Debt balance helpers ----------

def _get_cat_transactions_before_month(category_id, year, month):
    cutoff = f"{year}-{month:02d}-01"
    conn   = get_connection()
    rows   = conn.execute("""
        SELECT * FROM transactions
        WHERE category_id=?
          AND is_pending = 0
          AND (
            (transaction_date != '' AND transaction_date < ?) OR
            (transaction_date  = '' AND pay_by_date != '' AND pay_by_date < ?)
          )
        ORDER BY COALESCE(NULLIF(transaction_date,''), pay_by_date) ASC
    """, (category_id, cutoff, cutoff)).fetchall()
    conn.close()
    return rows

def get_debt_category_starting_balance(category_id, base_balance, year, month):
    base_balance = base_balance or 0.0
    prior        = _get_cat_transactions_before_month(category_id, year, month)
    balance      = base_balance
    for t in prior:
        if t["type"] == "expense":
            balance += t["amount"]
        else:
            balance -= t["amount"]
    return balance

def get_debt_account_starting_balance(account_id, year, month):
    conn = get_connection()
    cats = conn.execute(
        "SELECT * FROM categories WHERE account_id=? AND category_type IN ('loan','credit_card')",
        (account_id,)
    ).fetchall()
    conn.close()
    total = 0.0
    for cat in cats:
        base   = cat["budget_limit"] or 0.0
        total += get_debt_category_starting_balance(cat["id"], base, year, month)
    return total

# ---------- Investment balance helpers ----------

def get_investment_category_starting_balance(category_id, base_balance, year, month):
    base_balance = base_balance or 0.0
    prior        = _get_cat_transactions_before_month(category_id, year, month)
    balance      = base_balance
    for t in prior:
        if t["type"] == "income":
            balance += t["amount"]
        else:
            balance -= t["amount"]
    return balance

def get_transactions_with_investment_balance(category_id, base_balance, year, month):
    ms   = f"{year}-{month:02d}"
    conn = get_connection()
    txns = conn.execute("""
        SELECT * FROM transactions
        WHERE category_id=?
          AND (transaction_date LIKE ? OR pay_by_date LIKE ?)
        ORDER BY COALESCE(NULLIF(transaction_date,''), pay_by_date) ASC, id ASC
    """, (category_id, f"{ms}%", f"{ms}%")).fetchall()
    conn.close()
    starting = get_investment_category_starting_balance(
        category_id, base_balance, year, month
    )
    balance  = starting
    result   = []
    for t in txns:
        row = dict(t)
        if row.get('transaction_date', '').endswith('-99'):
            row['transaction_date'] = ''
        if row.get('is_pending'):
            row["start_bal"]   = balance
            row["end_balance"] = balance
        else:
            start_bal = balance
            if t["type"] == "income":
                balance += t["amount"]
            else:
                balance -= t["amount"]
            row["start_bal"]   = start_bal
            row["end_balance"] = balance
        result.append(row)
    result.reverse()
    return result

# ---------- Categories ----------

def get_categories(account_id=None):
    conn = get_connection()
    if account_id:
        rows = conn.execute(
            "SELECT * FROM categories WHERE account_id=? ORDER BY sort_order,id",
            (account_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM categories ORDER BY sort_order,id"
        ).fetchall()
    conn.close()
    return rows

def get_transfer_category_for_account(account_id):
    conn = get_connection()
    cat  = conn.execute(
        "SELECT * FROM categories WHERE account_id=? AND category_type='transfer' LIMIT 1",
        (account_id,)
    ).fetchone()
    conn.close()
    return cat

def ensure_transfer_category(account_id):
    """Return existing transfer category id, or create one and return new id."""
    cat = get_transfer_category_for_account(account_id)
    if cat:
        return cat["id"]
    return add_category("Transfers", account_id, "transfer")

def add_category(name, account_id, category_type='budget',
                 pay_by_date='', minimum_due=0, is_annual=0,
                 annual_pay_by_date='', folder_id=None):
    conn      = get_connection()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM categories WHERE account_id=?",
        (account_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO categories "
        "(name,account_id,sort_order,category_type,pay_by_date,minimum_due,"
        "is_annual,annual_pay_by_date,folder_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (name, account_id, max_order+1, category_type,
         pay_by_date, minimum_due, is_annual, annual_pay_by_date, folder_id)
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return new_id

def edit_category(category_id, name, pay_by_date='', minimum_due=0,
                  is_annual=0, annual_pay_by_date=''):
    conn = get_connection()
    conn.execute(
        "UPDATE categories SET name=?,pay_by_date=?,minimum_due=?,"
        "is_annual=?,annual_pay_by_date=? WHERE id=?",
        (name, pay_by_date, minimum_due, is_annual,
         annual_pay_by_date, category_id)
    )
    conn.commit()
    conn.close()

def delete_category(category_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM transfers WHERE from_transaction_id IN "
        "(SELECT id FROM transactions WHERE category_id=?) OR "
        "to_transaction_id IN "
        "(SELECT id FROM transactions WHERE category_id=?)",
        (category_id, category_id)
    )
    conn.execute("DELETE FROM account_interest WHERE category_id=?", (category_id,))
    conn.execute(
        "DELETE FROM notes WHERE entity_type='category' AND entity_id=?",
        (category_id,)
    )
    conn.execute("DELETE FROM category_budget_history WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM category_pay_by_history WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM category_minimum_due_history WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM transactions WHERE category_id=?", (category_id,))
    conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
    conn.commit()
    conn.close()

def update_budget_limit(category_id, limit):
    conn = get_connection()
    conn.execute("UPDATE categories SET budget_limit=? WHERE id=?", (limit, category_id))
    conn.commit()
    conn.close()

def get_effective_budget_limit(category_id, year, month):
    """Return the budget limit in effect for the given month.
    Looks for the most recent history entry on or before year/month;
    falls back to categories.budget_limit."""
    conn = get_connection()
    row = conn.execute("""
        SELECT budget_limit FROM category_budget_history
        WHERE category_id = ?
          AND (year < ? OR (year = ? AND month <= ?))
        ORDER BY year DESC, month DESC
        LIMIT 1
    """, (category_id, year, year, month)).fetchone()
    if row is not None:
        conn.close()
        return row["budget_limit"]
    row = conn.execute(
        "SELECT budget_limit FROM categories WHERE id=?", (category_id,)
    ).fetchone()
    conn.close()
    return row["budget_limit"] if row else 0

def set_budget_limit_from_month(category_id, year, month, limit):
    """Upsert a history entry: from year/month forward, the budget limit is `limit`."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO category_budget_history (category_id, year, month, budget_limit)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(category_id, year, month) DO UPDATE SET budget_limit=excluded.budget_limit
    """, (category_id, year, month, limit))
    conn.commit()
    conn.close()

def get_category_base_pay_by_date(category_id):
    """Return the base pay_by_date stored directly on the category row."""
    conn = get_connection()
    row  = conn.execute("SELECT pay_by_date FROM categories WHERE id=?", (category_id,)).fetchone()
    conn.close()
    return row["pay_by_date"] if row else ""

def get_effective_pay_by_date(category_id, year, month):
    """Return the pay_by_date in effect for the given month.
    Checks history first; falls back to categories.pay_by_date."""
    conn = get_connection()
    row = conn.execute("""
        SELECT pay_by_date FROM category_pay_by_history
        WHERE category_id = ?
          AND (year < ? OR (year = ? AND month <= ?))
        ORDER BY year DESC, month DESC
        LIMIT 1
    """, (category_id, year, year, month)).fetchone()
    if row is not None:
        conn.close()
        return row["pay_by_date"]
    row = conn.execute(
        "SELECT pay_by_date FROM categories WHERE id=?", (category_id,)
    ).fetchone()
    conn.close()
    return row["pay_by_date"] if row else ""

def set_pay_by_date_from_month(category_id, year, month, pay_by_date):
    """Upsert a history entry: from year/month forward, the pay_by_date is `pay_by_date`."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO category_pay_by_history (category_id, year, month, pay_by_date)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(category_id, year, month) DO UPDATE SET pay_by_date=excluded.pay_by_date
    """, (category_id, year, month, pay_by_date))
    conn.commit()
    conn.close()

def get_effective_minimum_due(category_id, year, month):
    """Return the minimum due in effect for the given month.
    Checks history first; falls back to categories.minimum_due."""
    conn = get_connection()
    row = conn.execute("""
        SELECT minimum_due FROM category_minimum_due_history
        WHERE category_id = ?
          AND (year < ? OR (year = ? AND month <= ?))
        ORDER BY year DESC, month DESC
        LIMIT 1
    """, (category_id, year, year, month)).fetchone()
    if row is not None:
        conn.close()
        return row["minimum_due"]
    row = conn.execute(
        "SELECT minimum_due FROM categories WHERE id=?", (category_id,)
    ).fetchone()
    conn.close()
    return row["minimum_due"] if row else 0

def set_minimum_due_from_month(category_id, year, month, minimum_due):
    """Upsert a history entry: from year/month forward, the minimum_due is `minimum_due`."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO category_minimum_due_history (category_id, year, month, minimum_due)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(category_id, year, month) DO UPDATE SET minimum_due=excluded.minimum_due
    """, (category_id, year, month, minimum_due))
    conn.commit()
    conn.close()

def get_category_base_minimum_due(category_id):
    """Return the base minimum_due stored directly on the category row."""
    conn = get_connection()
    row = conn.execute("SELECT minimum_due FROM categories WHERE id=?", (category_id,)).fetchone()
    conn.close()
    return row["minimum_due"] if row else 0

def reorder_categories(ordered_ids):
    conn = get_connection()
    for i, cid in enumerate(ordered_ids):
        conn.execute("UPDATE categories SET sort_order=? WHERE id=?", (i, cid))
    conn.commit()
    conn.close()

# ---------- Folders ----------

def get_folders(account_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM folders WHERE account_id=? ORDER BY sort_order,id",
        (account_id,)
    ).fetchall()
    conn.close()
    return rows

def add_folder(name, account_id):
    conn      = get_connection()
    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order),0) FROM folders WHERE account_id=?",
        (account_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO folders (name,account_id,sort_order) VALUES (?,?,?)",
        (name, account_id, max_order+1)
    )
    conn.commit()
    new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return new_id

def edit_folder(folder_id, name):
    conn = get_connection()
    conn.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    conn.commit()
    conn.close()

def delete_folder(folder_id):
    """Unassign categories from this folder then delete it."""
    conn = get_connection()
    conn.execute(
        "UPDATE categories SET folder_id=NULL WHERE folder_id=?",
        (folder_id,)
    )
    conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()
    conn.close()

def reorder_folders(ordered_ids):
    conn = get_connection()
    for i, fid in enumerate(ordered_ids):
        conn.execute("UPDATE folders SET sort_order=? WHERE id=?", (i, fid))
    conn.commit()
    conn.close()

def set_category_folder(category_id, folder_id):
    """Assign or unassign a category to a folder."""
    conn = get_connection()
    conn.execute(
        "UPDATE categories SET folder_id=? WHERE id=?",
        (folder_id, category_id)
    )
    conn.commit()
    conn.close()

def set_category_start_month(category_id, year, month):
    conn = get_connection()
    conn.execute(
        "UPDATE categories SET start_year=?, start_month=? WHERE id=?",
        (year, month, category_id)
    )
    conn.commit()
    conn.close()

def clear_category_start_month(category_id):
    conn = get_connection()
    conn.execute(
        "UPDATE categories SET start_year=NULL, start_month=NULL WHERE id=?",
        (category_id,)
    )
    conn.commit()
    conn.close()

def set_category_end_month(category_id, year, month):
    conn = get_connection()
    conn.execute("UPDATE categories SET end_year=?, end_month=? WHERE id=?",
                 (year, month, category_id))
    conn.commit()
    conn.close()

def clear_category_end_month(category_id):
    conn = get_connection()
    conn.execute("UPDATE categories SET end_year=NULL, end_month=NULL WHERE id=?",
                 (category_id,))
    conn.commit()
    conn.close()

def get_category_summary(category_id, year, month):
    ms   = f"{year}-{month:02d}"
    conn = get_connection()
    result = conn.execute("""
        SELECT
            c.budget_limit, c.category_type, c.pay_by_date, c.minimum_due,
            COALESCE(SUM(CASE WHEN t.type='expense' AND t.is_pending=0
                AND (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
                THEN t.amount ELSE 0 END),0) as total_spent,
            COALESCE(SUM(CASE WHEN t.type='income' AND t.is_pending=0
                AND (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
                THEN t.amount ELSE 0 END),0) as total_income
        FROM categories c
        LEFT JOIN transactions t ON t.category_id=c.id
        WHERE c.id=?
        GROUP BY c.id
    """, (f"{ms}%", f"{ms}%", f"{ms}%", f"{ms}%", category_id)).fetchone()
    conn.close()
    return result

def get_annual_reserved(account_id, year, month):
    """Sum of effective budget limits for annual-marked budget categories active in this month."""
    conn = get_connection()
    cats = conn.execute("""
        SELECT id, start_year, start_month, end_year, end_month
        FROM categories
        WHERE account_id=? AND is_annual=1 AND category_type='budget'
    """, (account_id,)).fetchall()
    conn.close()
    total = 0.0
    for cat in cats:
        if cat["start_year"] and cat["start_month"]:
            if (year, month) < (cat["start_year"], cat["start_month"]):
                continue
        if cat["end_year"] and cat["end_month"]:
            if (year, month) > (cat["end_year"], cat["end_month"]):
                continue
        total += get_effective_budget_limit(cat["id"], year, month)
    return total

# ---------- Transactions with running balance (debt) ----------

def get_transactions_with_running_balance(category_id, base_balance, year, month):
    ms   = f"{year}-{month:02d}"
    conn = get_connection()
    txns = conn.execute("""
        SELECT * FROM transactions
        WHERE category_id=?
          AND (transaction_date LIKE ? OR pay_by_date LIKE ?)
        ORDER BY COALESCE(NULLIF(transaction_date,''), pay_by_date) ASC, id ASC
    """, (category_id, f"{ms}%", f"{ms}%")).fetchall()
    conn.close()
    starting = get_debt_category_starting_balance(category_id, base_balance, year, month)
    balance  = starting
    result   = []
    for t in txns:
        row = dict(t)
        if row.get('transaction_date', '').endswith('-99'):
            row['transaction_date'] = ''
        if row.get('is_pending'):
            row["running_balance"] = None
        else:
            if t["type"] == "expense":
                balance += t["amount"]
            else:
                balance -= t["amount"]
            row["running_balance"] = balance
        result.append(row)
    result.reverse()
    return result

# ---------- Interest ----------

def get_interest(account_id, year, month):
    conn   = get_connection()
    result = conn.execute(
        "SELECT * FROM account_interest WHERE account_id=? AND year=? AND month=?",
        (account_id, year, month)
    ).fetchone()
    conn.close()
    return result

def upsert_interest(account_id, category_id, year, month,
                    mode, amount, rate, date):
    conn = get_connection()
    conn.execute("""
        INSERT INTO account_interest
            (account_id,category_id,year,month,mode,amount,rate,date)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id,year,month) DO UPDATE SET
            category_id=excluded.category_id,
            mode=excluded.mode, amount=excluded.amount,
            rate=excluded.rate, date=excluded.date
    """, (account_id, category_id, year, month, mode, amount, rate, date))
    conn.commit()
    conn.close()

def delete_interest(account_id, year, month):
    conn = get_connection()
    conn.execute(
        "DELETE FROM account_interest WHERE account_id=? AND year=? AND month=?",
        (account_id, year, month)
    )
    conn.commit()
    conn.close()

def apply_interest_transaction(account_id, category_id, year, month,
                               mode, amount, rate, interest_date):
    conn = get_connection()
    conn.execute("""
        DELETE FROM transactions
        WHERE category_id=?
          AND transaction_date LIKE ?
          AND description='Auto Interest'
    """, (category_id, f"{year}-{month:02d}%"))
    if mode == "percent":
        ms  = f"{year}-{month:02d}"
        row = conn.execute("""
            SELECT COALESCE(SUM(amount),0) as total_income
            FROM transactions
            WHERE category_id=?
              AND transaction_date LIKE ?
              AND type='income'
              AND description != 'Auto Interest'
        """, (category_id, f"{ms}%")).fetchone()
        base   = row["total_income"] if row else 0.0
        amount = round(base * (rate / 100.0), 2)
    if amount > 0:
        conn.execute("""
            INSERT INTO transactions
                (transaction_date, description, amount, type, category_id)
            VALUES (?, 'Auto Interest', ?, 'income', ?)
        """, (interest_date, amount, category_id))
    conn.commit()
    conn.close()
    return amount

# ---------- Transfers ----------

def get_transfers_for_category(category_id, year=None, month=None):
    conn = get_connection()
    if year and month:
        ms   = f"{year}-{month:02d}"
        rows = conn.execute("""
            SELECT tr.*,
                   fa.name as from_account_name,
                   ta.name as to_account_name,
                   t_from.category_id  as from_cat_id,
                   t_to.category_id    as to_cat_id,
                   t_from.description  as description,
                   COALESCE(t_from.is_pending, 0) as is_pending
            FROM transfers tr
            JOIN accounts fa ON tr.from_account_id=fa.id
            JOIN accounts ta ON tr.to_account_id  =ta.id
            LEFT JOIN transactions t_from ON tr.from_transaction_id=t_from.id
            LEFT JOIN transactions t_to   ON tr.to_transaction_id  =t_to.id
            WHERE (t_from.category_id=? OR t_to.category_id=?)
              AND tr.date LIKE ?
            ORDER BY tr.date DESC
        """, (category_id, category_id, f"{ms}%")).fetchall()
    else:
        rows = conn.execute("""
            SELECT tr.*,
                   fa.name as from_account_name,
                   ta.name as to_account_name,
                   t_from.description  as description,
                   COALESCE(t_from.is_pending, 0) as is_pending
            FROM transfers tr
            JOIN accounts fa ON tr.from_account_id=fa.id
            JOIN accounts ta ON tr.to_account_id  =ta.id
            LEFT JOIN transactions t_from ON tr.from_transaction_id=t_from.id
            WHERE tr.from_transaction_id IN
                  (SELECT id FROM transactions WHERE category_id=?)
               OR tr.to_transaction_id IN
                  (SELECT id FROM transactions WHERE category_id=?)
            ORDER BY tr.date DESC
        """, (category_id, category_id)).fetchall()
    conn.close()
    return rows

def create_transfer(date, amount, from_account_id, to_account_id,
                    from_cat_id, to_cat_id, description="", is_pending=0):
    conn = get_connection()
    desc = description.strip() if description else ""
    conn.execute("""
        INSERT INTO transactions
            (transaction_date, description, amount, type, category_id, is_pending)
        VALUES (?, ?, ?, 'expense', ?, ?)
    """, (date, desc, amount, from_cat_id, is_pending))
    from_txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("""
        INSERT INTO transactions
            (transaction_date, description, amount, type, category_id, is_pending)
        VALUES (?, ?, ?, 'income', ?, ?)
    """, (date, desc, amount, to_cat_id, is_pending))
    to_txn_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("""
        INSERT INTO transfers
            (date,amount,from_account_id,to_account_id,
             from_transaction_id,to_transaction_id)
        VALUES (?,?,?,?,?,?)
    """, (date, amount, from_account_id, to_account_id, from_txn_id, to_txn_id))
    conn.commit()
    conn.close()
    return from_txn_id, to_txn_id

def edit_transfer(transfer_id, date, amount, description="", is_pending=0):
    conn = get_connection()
    tr   = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
    if tr:
        desc = description.strip() if description else ""
        conn.execute("UPDATE transfers SET date=?, amount=? WHERE id=?",
                     (date, amount, transfer_id))
        for txn_id in (tr["from_transaction_id"], tr["to_transaction_id"]):
            conn.execute("""UPDATE transactions
                            SET transaction_date=?, amount=?, description=?, is_pending=?
                            WHERE id=?""", (date, amount, desc, is_pending, txn_id))
    conn.commit()
    conn.close()

def delete_transfer(transfer_id):
    conn = get_connection()
    tr   = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
    if tr:
        conn.execute("DELETE FROM transactions WHERE id=?", (tr["from_transaction_id"],))
        conn.execute("DELETE FROM transactions WHERE id=?", (tr["to_transaction_id"],))
        conn.execute("DELETE FROM transfers WHERE id=?", (transfer_id,))
    conn.commit()
    conn.close()

# ---------- Transactions ----------

def add_transaction(transaction_date, pay_by_date, description, payee,
                    payment_method, amount, txn_type, category_id, is_pending=0):
    conn = get_connection()
    conn.execute("""
        INSERT INTO transactions
            (transaction_date, pay_by_date, description, payee,
             payment_method, amount, type, category_id, is_pending)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (transaction_date or '', pay_by_date or '', description or '',
          payee or '', payment_method or '', amount, txn_type,
          category_id, is_pending))
    conn.commit()
    conn.close()

def edit_transaction(transaction_id, transaction_date, pay_by_date,
                     description, payee, payment_method,
                     amount, txn_type, category_id, is_annual=0, is_pending=0):
    conn = get_connection()
    conn.execute("""
        UPDATE transactions
        SET transaction_date=?, pay_by_date=?, description=?,
            payee=?, payment_method=?, amount=?, type=?,
            category_id=?, is_annual=?, is_pending=?
        WHERE id=?
    """, (transaction_date, pay_by_date, description, payee,
          payment_method, amount, txn_type, category_id,
          is_annual, is_pending, transaction_id))
    conn.commit()
    conn.close()

def delete_transaction(transaction_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM notes WHERE entity_type='transaction' AND entity_id=?",
        (transaction_id,)
    )
    conn.execute("DELETE FROM transactions WHERE id=?", (transaction_id,))
    conn.commit()
    conn.close()

def get_transactions(category_id=None, year=None, month=None):
    conn = get_connection()
    if category_id and year and month:
        ms   = f"{year}-{month:02d}"
        rows = conn.execute("""
            SELECT t.*, c.name as category_name, c.account_id, c.category_type
            FROM transactions t
            LEFT JOIN categories c ON t.category_id=c.id
            WHERE t.category_id=?
              AND (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
            ORDER BY COALESCE(NULLIF(t.transaction_date,''), t.pay_by_date) DESC
        """, (category_id, f"{ms}%", f"{ms}%")).fetchall()
    elif category_id:
        rows = conn.execute("""
            SELECT t.*, c.name as category_name, c.account_id, c.category_type
            FROM transactions t
            LEFT JOIN categories c ON t.category_id=c.id
            WHERE t.category_id=?
            ORDER BY COALESCE(NULLIF(t.transaction_date,''), t.pay_by_date) DESC
        """, (category_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.*, c.name as category_name, c.account_id, c.category_type
            FROM transactions t
            LEFT JOIN categories c ON t.category_id=c.id
            ORDER BY COALESCE(NULLIF(t.transaction_date,''), t.pay_by_date) DESC
        """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get('transaction_date', '').endswith('-99'):
            d['transaction_date'] = ''
        result.append(d)
    return result

def get_monthly_totals(year, month):
    ms   = f"{year}-{month:02d}"
    conn = get_connection()
    result = conn.execute("""
        SELECT
            COALESCE(SUM(
                CASE WHEN a.type != 'Debt' AND c.category_type != 'transfer' AND t.type='income'
                     THEN t.amount ELSE 0 END
            ), 0) AS total_income,
            COALESCE(SUM(
                CASE
                    WHEN a.type != 'Debt' AND c.category_type != 'transfer' AND t.type='expense'
                         THEN t.amount
                    -- CC/loan payments: counted as an expense here so this card
                    -- matches the cash outflow already reflected in the running balance
                    WHEN a.type = 'Debt' AND c.category_type IN ('loan','credit_card') AND t.type='income'
                         THEN t.amount
                    ELSE 0
                END
            ), 0) AS total_expenses
        FROM transactions t
        JOIN categories c ON t.category_id=c.id
        JOIN accounts   a ON c.account_id =a.id
        WHERE (t.transaction_date LIKE ? OR t.pay_by_date LIKE ?)
          AND t.is_pending = 0
          AND c.category_type != 'reserve'
    """, (f"{ms}%", f"{ms}%")).fetchone()
    conn.close()
    return result

def get_overall_balance(account_data):
    return sum(
        a["net_balance"] for a in account_data
        if a["type"] in ("Checking","Savings","Cash","Investment")
    )

def get_running_balance(account_data, year, month):
    """Cumulative running balance through the viewed month.

    Logic mirrors the income/expense summary cards (which exclude transfers):
      Non-debt accounts  →  income adds, expenses subtract, transfers ignored
      Debt accounts      →  loan/credit_card PAYMENTS (income) deducted as
                            outgoing cash; charges and transfers ignored

    CC payments flow:
      The transfer from checking to the CC account is excluded (transfer
      category).  The separately-recorded payment in the CC credit_card
      category is deducted here, so each payment is counted exactly once.
    """
    all_ids = [a["id"] for a in account_data]
    if not all_ids:
        return 0.0
    viewed_ym    = f"{year}-{month:02d}"
    placeholders = ",".join("?" * len(all_ids))
    conn   = get_connection()
    result = conn.execute(f"""
        SELECT COALESCE(SUM(
            CASE
                -- Non-debt transfers: excluded to match income/expense card logic
                WHEN a.type IN ('Checking','Savings','Cash','Investment')
                     AND c.category_type = 'transfer'
                THEN 0
                -- Non-debt income (salary, interest, investment gains, etc.)
                WHEN a.type IN ('Checking','Savings','Cash','Investment')
                     AND t.type = 'income'
                THEN t.amount
                -- Non-debt expense (bills, groceries, etc.)
                WHEN a.type IN ('Checking','Savings','Cash','Investment')
                     AND t.type = 'expense'
                THEN -t.amount
                -- CC/loan payments (Paid Toward Card/Loan): deduct as outgoing cash
                WHEN a.type = 'Debt'
                     AND c.category_type IN ('loan','credit_card')
                     AND t.type = 'income'
                THEN -t.amount
                -- Everything else (debt charges, debt transfers, interest): ignore
                ELSE 0
            END
        ), 0) AS balance
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN accounts   a ON c.account_id  = a.id
        WHERE c.account_id IN ({placeholders})
          AND t.is_pending = 0
          AND c.category_type != 'reserve'
          AND (
            (t.transaction_date != '' AND SUBSTR(t.transaction_date, 1, 7) <= ?) OR
            (t.transaction_date  = '' AND t.pay_by_date != ''
             AND SUBSTR(t.pay_by_date, 1, 7) <= ?)
          )
    """, (*all_ids, viewed_ym, viewed_ym)).fetchone()
    conn.close()
    return result["balance"] if result else 0.0

def get_cumulative_non_debt_balance(year, month):
    """Running total of income − expenses across all non-debt accounts up to
    and including the viewed month.  Transfers are intentionally included so
    that payments made from a non-debt account to a debt account (transfer
    expense side) correctly reduce this balance, mirroring the old per-account
    carryover approach.  Internal non-debt transfers cancel out (−A +A = 0).
    Debt account transactions are excluded entirely; their remaining balance is
    shown separately as 'Total Debt Owed'."""
    viewed_ym = f"{year}-{month:02d}"
    conn = get_connection()
    result = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN t.type = 'income'  THEN t.amount ELSE 0 END), 0) -
            COALESCE(SUM(CASE WHEN t.type = 'expense' THEN t.amount ELSE 0 END), 0) AS balance
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN accounts   a ON c.account_id  = a.id
        WHERE a.type NOT IN ('Debt')
          AND t.is_pending = 0
          AND (
            (t.transaction_date != '' AND SUBSTR(t.transaction_date, 1, 7) <= ?) OR
            (t.transaction_date  = '' AND t.pay_by_date != '' AND SUBSTR(t.pay_by_date, 1, 7) <= ?)
          )
    """, (viewed_ym, viewed_ym)).fetchone()
    conn.close()
    return result["balance"] if result else 0.0