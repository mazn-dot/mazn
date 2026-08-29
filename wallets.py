import json, os, re
from config import DEFAULT_WALLETS
FILE = os.path.join(os.path.dirname(__file__), "wallets.json")
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
def load():
  try:
      with open(FILE, encoding="utf-8") as f: data = json.load(f)
      return {str(k): str(v) for k, v in data.items() if ADDRESS.match(str(v))}
  except (OSError, ValueError): return dict(DEFAULT_WALLETS)
def get_all(): return load()
def add(label, address):
  label, address = label.strip(), address.strip()
  if not label: return "اسم المحفظة مطلوب"
  if not ADDRESS.match(address): return "عنوان BSC غير صحيح"
  data = load(); data[label] = address
  with open(FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
  return None
def remove(label):
  data = load()
  if label not in data or len(data) <= 1: return False
  del data[label]
  with open(FILE, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
  return True
