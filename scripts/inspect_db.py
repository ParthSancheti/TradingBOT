import sqlite3, os, json
from pathlib import Path

p = Path(os.environ['LOCALAPPDATA']) / 'JarvisTrade/workspace.db'
conn = sqlite3.connect(str(p))
row = conn.execute("SELECT data_json FROM records WHERE id = '8836e7a5ba5ef06c29adf246123d18434efea3547597acbbffbecc4d3d4076c2'").fetchone()
if row:
    print(json.dumps(json.loads(row[0]), indent=2))


