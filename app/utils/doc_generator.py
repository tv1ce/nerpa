"""Заполнение .docx шаблонов данными и конвертация в PDF."""
import os
import shutil
from datetime import date
from docx import Document


def _fmt_date(d) -> str:
    if not d:
        return ""
    if isinstance(d, date):
        return d.strftime("%d.%m.%Y")
    return str(d)


def _fmt_money(v) -> str:
    if v is None:
        return "0,00"
    return f"{v:,.2f}".replace(",", " ").replace(".", ",")


def fill_contract_template(template_path: str, output_path: str, contract, company) -> str:
    """Заполняет .docx шаблон и сохраняет результат. Возвращает путь к файлу."""
    doc = Document(template_path)
    cp = contract.counterparty

    placeholders = {
        "{{contract_number}}": contract.number or "",
        "{{contract_date}}": _fmt_date(contract.date),
        "{{contract_subject}}": contract.subject or "",
        "{{contract_amount}}": _fmt_money(contract.amount),
        "{{start_date}}": _fmt_date(contract.start_date),
        "{{end_date}}": _fmt_date(contract.end_date),
        # Наша компания
        "{{company_name}}": company.name or "",
        "{{company_short_name}}": company.short_name or company.name or "",
        "{{company_inn}}": company.inn or "",
        "{{company_kpp}}": company.kpp or "",
        "{{company_ogrn}}": company.ogrn or "",
        "{{company_address}}": company.legal_address or "",
        "{{company_phone}}": company.phone or "",
        "{{company_director}}": company.director or "",
        "{{company_director_basis}}": company.director_basis or "Устава",
        "{{company_bank_name}}": company.bank_name or "",
        "{{company_bank_account}}": company.bank_account or "",
        "{{company_bik}}": company.bank_bik or "",
        "{{company_corr_account}}": company.bank_corr_account or "",
        # Контрагент
        "{{client_name}}": cp.name or "",
        "{{client_short_name}}": cp.short_name or cp.name or "",
        "{{client_inn}}": cp.inn or "",
        "{{client_kpp}}": cp.kpp or "",
        "{{client_ogrn}}": cp.ogrn or "",
        "{{client_address}}": cp.legal_address or "",
        "{{client_phone}}": cp.phone or "",
        "{{client_contact}}": cp.contact_person or "",
        "{{client_bank_name}}": cp.bank_name or "",
        "{{client_bank_account}}": cp.bank_account or "",
        "{{client_bik}}": cp.bank_bik or "",
        "{{client_corr_account}}": cp.bank_corr_account or "",
    }

    for para in doc.paragraphs:
        _replace_in_para(para, placeholders)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_para(para, placeholders)

    doc.save(output_path)
    return output_path


def _replace_in_para(para, replacements: dict):
    for key, value in replacements.items():
        if key in para.text:
            for run in para.runs:
                if key in run.text:
                    run.text = run.text.replace(key, value)
            # Если замена не сработала через run — заменяем весь параграф
            if key in para.text:
                full_text = para.text
                new_text = full_text.replace(key, value)
                if para.runs:
                    para.runs[0].text = new_text
                    for run in para.runs[1:]:
                        run.text = ""


def convert_docx_to_pdf(docx_path: str, output_dir: str) -> str | None:
    """Конвертирует .docx в PDF используя LibreOffice или MS Word (Windows)."""
    try:
        import subprocess
        # Попытка через LibreOffice
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", output_dir, docx_path],
            capture_output=True, timeout=30
        )
        if result.returncode == 0:
            base = os.path.splitext(os.path.basename(docx_path))[0]
            return os.path.join(output_dir, base + ".pdf")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        import subprocess
        # Попытка через MS Word на Windows
        script = (
            f"$word = New-Object -ComObject Word.Application;"
            f"$doc = $word.Documents.Open('{docx_path.replace(chr(92), chr(92)*2)}');"
            f"$doc.SaveAs('{os.path.join(output_dir, os.path.splitext(os.path.basename(docx_path))[0] + '.pdf').replace(chr(92), chr(92)*2)}', 17);"
            f"$doc.Close(); $word.Quit()"
        )
        result = subprocess.run(["powershell", "-Command", script], capture_output=True, timeout=60)
        if result.returncode == 0:
            base = os.path.splitext(os.path.basename(docx_path))[0]
            return os.path.join(output_dir, base + ".pdf")
    except Exception:
        pass

    return None
