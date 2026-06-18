@echo off
call venv\Scripts\activate
venv\Scripts\pyinstaller --clean "Finance Tracker.spec"
