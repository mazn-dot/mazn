import re

import trades_db
from config import DEFAULT_WALLETS

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
MAX_WALLETS = 40


def load():
    """Load wallets from the persistent DB (PostgreSQL or SQLite)."""
    data = trades_db.load_wallets()
    if data:
        return {str(k): str(v) for k, v in data.items() if ADDRESS.match(str(v))}
    # First run / empty table — seed defaults (init already seeds, but safe fallback)
    return dict(DEFAULT_WALLETS)


def get_all():
    return load()


def add(label, address):
    label, address = label.strip(), address.strip()
    if not label:
        return "اسم المحفظة مطلوب"
    if not ADDRESS.match(address):
        return "عنوان غير صحيح (لازم 0x + 40 حرف)"
    data = load()
    if len(data) >= MAX_WALLETS and label not in data:
        return f"وصلت للحد الأقصى ({MAX_WALLETS} محفظة). احذف محافظ قديمة أولاً."
    for existing_label, existing_addr in data.items():
        if existing_addr.lower() == address.lower() and existing_label != label:
            return "العنوان موجود مسبقاً باسم: " + existing_label
    trades_db.upsert_wallet(label, address)
    return None


def add_bulk(lines):
    """Accepts multiple lines. Returns (success_count, errors_list)"""
    success = 0
    errors = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            errors.append(f"صيغة غلط: {line[:40]}")
            continue
        if parts[0] in ("إضافة", "اضافه", "add"):
            parts = parts[1:]
        is_whale = False
        if parts and parts[0] in ("حوت", "whale"):
            is_whale = True
            parts = parts[1:]
        if len(parts) < 2:
            errors.append(f"صيغة غلط: {line[:40]}")
            continue
        address = parts[-1]
        label = " ".join(parts[:-1])
        if is_whale and not label.startswith("حوت"):
            label = "حوت " + label
        err = add(label, address)
        if err:
            errors.append(f"{label}: {err}")
        else:
            success += 1
    return success, errors


def remove(label):
    data = load()
    if label not in data:
        return False
    if len(data) <= 1:
        return False
    trades_db.delete_wallet(label)
    return True


def list_text():
    data = load()
    if not data:
        return "لا توجد محافظ."
    lines = []
    for i, (label, address) in enumerate(data.items(), 1):
        lines.append("%d. <b>%s</b>\n   <code>%s</code>" % (i, label, address))
    return "\n".join(lines)
