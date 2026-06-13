"""
Manages the list of known databases and which one is active.
State is persisted to databases.json in the app folder.
"""

import json
import os
import shutil
import sys

def _app_dir():
    """Return AppData\Roaming\BudgetApp (frozen) or the project folder (dev)."""
    if getattr(sys, 'frozen', False):
        data_dir = os.path.join(os.environ.get('APPDATA', os.path.dirname(sys.executable)), 'Finance Tracker')
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))

REGISTRY = os.path.join(_app_dir(), "databases.json")

def _bundle_path(filename):
    """Return path to a file bundled by PyInstaller, or None if not frozen/missing."""
    base = getattr(sys, '_MEIPASS', None)
    if base is None:
        return None
    path = os.path.join(base, filename)
    return path if os.path.exists(path) else None

def _seed_from_bundle():
    """On first install, copy bundled databases.json and .db files to AppData."""
    bundled_registry = _bundle_path('databases.json')
    if not bundled_registry:
        return
    with open(bundled_registry, 'r') as f:
        data = json.load(f)
    _save(data)
    data_dir = _app_dir()
    for db in data.get('databases', []):
        src = _bundle_path(db['file'])
        dst = os.path.join(data_dir, db['file'])
        if src and not os.path.exists(dst):
            shutil.copy2(src, dst)

def _load():
    if not os.path.exists(REGISTRY):
        _seed_from_bundle()
    if not os.path.exists(REGISTRY):
        data = {"active": "budget.db",
                "databases": [{"name": "Default", "file": "budget.db"}]}
        _save(data)
    with open(REGISTRY, "r") as f:
        return json.load(f)

def _save(data):
    with open(REGISTRY, "w") as f:
        json.dump(data, f, indent=2)

def get_all():
    return _load()

def get_active_file():
    return _load()["active"]

def get_active_name():
    data = _load()
    for db in data["databases"]:
        if db["file"] == data["active"]:
            return db["name"]
    return "Unknown"

def switch(file):
    data = _load()
    if any(db["file"] == file for db in data["databases"]):
        data["active"] = file
        _save(data)
        return True
    return False

def create(name, file):
    data = _load()
    if any(db["file"] == file for db in data["databases"]):
        return False
    data["databases"].append({"name": name, "file": file})
    data["active"] = file
    _save(data)
    return True

def delete(file):
    data = _load()
    data["databases"] = [db for db in data["databases"] if db["file"] != file]
    if data["active"] == file:
        data["active"] = data["databases"][0]["file"] if data["databases"] else "budget.db"
    _save(data)

def rename(file, new_name):
    data = _load()
    for db in data["databases"]:
        if db["file"] == file:
            db["name"] = new_name
            break
    _save(data)

def get_db_path(file):
    return os.path.abspath(os.path.join(_app_dir(), file))