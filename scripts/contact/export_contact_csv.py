"""从 contact_persons.json 生成「完全版」人员表 CSV（保留全部字段）。

输出 UTF-8 with BOM，Excel 双击打开中文不乱码。
"""
import json, csv, pathlib, sys

BASE = pathlib.Path(__file__).resolve().parent
SRC = BASE / 'data' / 'contact_persons.json'
DST = BASE / 'data' / 'contact_persons.csv'

persons = json.loads(SRC.read_text(encoding='utf-8'))
if not persons:
    print('源数据为空，请先运行 fetch_contact.py 拉取')
    sys.exit(1)

# 字段取所有人员的并集（保持首次出现顺序），确保不丢列
fields = []
for p in persons:
    for k in p.keys():
        if k not in fields:
            fields.append(k)

with DST.open('w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
    w.writeheader()
    for p in persons:
        w.writerow(p)

print('rows:', len(persons), 'cols:', len(fields))
print('fields:', fields)
print('saved ->', DST)
