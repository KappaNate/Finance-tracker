"""
Manages the list of known databases and which one is active.
State is persisted to databases.json in the app folder.
"""

import json
import os

REGISTRY = os.path.join(os.path.dirname(__file__), "databases.json")

def _load():
    if not os.path.exists(REGISTRY):
        data = {"active": "budget.db",
                "databases": [{"name": "Default", "file": "budget.db"}]}
        _save(data)
        return data
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
    return os.path.abspath(os.path.join(os.path.dirname(__file__), file))