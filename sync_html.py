#!/usr/bin/env python3
"""Sync listings.csv data into the embedded CSV_DATA block in index.html.
Run this after any change to listings.csv."""
import re, sys
from pathlib import Path

base = Path(__file__).parent
csv_content = (base / 'listings.csv').read_text().strip()
html_path = base / 'index.html'
html = html_path.read_text()

pattern = r'(const CSV_DATA = `)[\s\S]*?(`;)'
new_html, count = re.subn(pattern, r'\g<1>' + csv_content + r'\2', html)

if count == 0:
    print("ERROR: CSV_DATA block not found in index.html", file=sys.stderr)
    sys.exit(1)

html_path.write_text(new_html)
rows = csv_content.count('\n')
print(f"Synced {rows} listings into index.html")
