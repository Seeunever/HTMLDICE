import openpyxl
from pathlib import Path
import json
ws = openpyxl.load_workbook(Path(r'e:/trpg/coc7/莉莉娅.xlsx'), data_only=True)['人物卡']
grid = {}
for r in range(2, 12):
    for c in range(19, 31):
        v = ws.cell(r,c).value
        if v not in (None, ''):
            grid.setdefault(r, []).append([c, str(v)[:50]])
Path(r'e:/trpg/HTMLDICE/scripts/attr_grid.json').write_text(json.dumps(grid, ensure_ascii=False, indent=2), encoding='utf-8')
