@echo off
call venv\Scripts\activate
pyinstaller --onefile --noconsole --add-data "templates;templates" --add-data "static;static" --add-data "databases.json;." --add-data "example.db;." --add-data "icon.ico;." --hidden-import "pystray._win32" --icon "icon.ico" --name "Finance Tracker" app.py
