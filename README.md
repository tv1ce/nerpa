# TMS — Transport Management System

A web-based TMS built with FastAPI for managing orders, invoices, counterparties, contracts, and warehouse operations.

## Stack

- **Backend:** Python 3, FastAPI, SQLAlchemy
- **Frontend:** Jinja2 templates, vanilla JS
- **Database:** SQLite (`tms.db`, created on first run)
- **Documents:** PDF invoices via ReportLab, Word docs via python-docx

## Modules

| Module | Path |
|---|---|
| Orders | `/orders` |
| Invoices | `/invoices` |
| Counterparties | `/counterparties` |
| Contracts | `/contracts` |
| Warehouse | `/warehouse` |
| Reports | `/reports` |
| Dashboard | `/` |

## Getting started

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server (initializes the DB automatically)
python run.py
```

The app runs at **http://localhost:8080**.

Default login is created on first run — see `app/auth.py` for credentials.
