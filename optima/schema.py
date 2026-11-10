"""Regenerates optima/schema_cols.tsv (column metadata of the CDN schema) from a live Optima database.

  python3 -m optima.schema query | bin/sql prod - | python3 -m optima.schema save

The generators read writable columns, their order and text lengths from this file, so it must match the
installed Optima version. `query` is read-only.
"""
import argparse, os, sys
from . import common as c

QUERY = """SET NOCOUNT ON;
SELECT s.name+'.'+t.name tbl, c.column_id, c.name col, ty.name typ, c.max_length, c.precision, c.scale,
       CAST(c.is_nullable AS int) is_nullable, CAST(c.is_identity AS int) is_identity, CAST(c.is_computed AS int) is_computed,
       ISNULL(dc.definition,'NULL') dflt
FROM sys.columns c
JOIN sys.tables t ON t.object_id=c.object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
LEFT JOIN sys.default_constraints dc ON dc.object_id=c.default_object_id
WHERE s.name='CDN'
ORDER BY t.name, c.column_id;"""


def save(src, out):
    rows = []
    for line in src:
        line = line.rstrip('\r\n').lstrip('\ufeff')
        if not line or line.startswith('tbl|') or set(line) <= set('-|') or line.endswith('rows affected)'):
            continue
        if line.count('|') != 10:
            raise SystemExit(f'unexpected line: {line[:120]}')
        rows.append(line)
    if not rows:
        raise SystemExit('no rows on stdin')
    with open(out, 'w', encoding='utf-8', newline='') as f:
        f.write(''.join(r + '\r\n' for r in rows))
    print(f'{len(rows)} columns, {len({r.split("|")[0] for r in rows})} tables -> {out}', file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['query', 'save'])
    ap.add_argument('--out', default=os.path.join(c.PKG, 'schema_cols.tsv'))
    a = ap.parse_args()
    if a.action == 'query':
        print(QUERY)
    else:
        save(sys.stdin, a.out)


if __name__ == '__main__':
    main()
