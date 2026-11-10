"""Shared core of the SQL generators for Comarch Optima.

Every new record is a clone of an existing row entered by hand in the application, with only the variable
fields overridden (INSERT ... SELECT). That keeps the ~160 constant VatNag fields identical to documents
created in the GUI. Record graph and rules: docs/schema-reverse-engineering.md.
"""
import datetime as dt
import json
import os
import re
import sys
import uuid
from decimal import Decimal, ROUND_HALF_UP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get('OPTIMA_CONFIG_DIR') or os.path.join(ROOT, 'config')


_WARNED = set()


def load_json(name):
    """config/<name>, falling back to config/<name>.example.json (sample data) when there is no local file."""
    p = os.path.join(CONFIG_DIR, name)
    if not os.path.exists(p):
        p = os.path.join(CONFIG_DIR, name[:-5] + '.example.json')
        if not os.environ.get('OPTIMA_QUIET_EXAMPLE') and name not in _WARNED:
            _WARNED.add(name)
            print(f'-- WARNING: no {name} in {CONFIG_DIR}, using {os.path.basename(p)}', file=sys.stderr)
    with open(p, encoding='utf-8') as f:
        return json.load(f)


CFG = load_json('config.json')
DB_PROD = CFG['db_prod']
DB_CLONE = CFG['db_clone']
MONTHS_DIR = os.environ.get('OPTIMA_DATA') or os.path.join(ROOT, CFG.get('months_dir', 'months'))


def month_dir(ym, *sub):
    return os.path.join(MONTHS_DIR, ym, *sub)


def db_name(target):
    return {'clone': DB_CLONE, 'prod': DB_PROD}.get(target, target)


NS = uuid.UUID(CFG['guid_namespace'])  # never change after the first import: different GUIDs = duplicates instead of SKIP
APP = CFG['batch_tags']
assert all(0 < len(v) <= 5 for v in APP.values()), 'batch_tags: 1-5 characters'
APP_SQL = ','.join(f"'{v}'" for v in APP.values())
KV7 = {'IMP': 27, 'BFK': 37}  # CDN.KodyJPKV7
TS0 = 'DATEADD(ms,-DATEPART(ms,GETDATE()),GETDATE())'  # the application stores timestamps in whole seconds
OPE_FROM_CARD = load_json('templates.json')['operator_card']  # card created in the GUI: source of the operator fields
OPE_FIELDS = ['OpeZalID', 'StaZalId', 'OpeModID', 'StaModId', 'OpeZalKod', 'OpeModKod', 'OpeZalNazwisko', 'OpeModNazwisko']
EU_ISO = {'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU',
          'MT', 'NL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE'}


_COLS = {}
_LEN = {}


def col_len(table, col):
    """Max length of a text column in characters (schema_cols.tsv stores max_length in bytes), None if unknown."""
    if not _LEN:
        with open(os.path.join(PKG, 'schema_cols.tsv'), encoding='utf-8') as fh:
            rows = [line.rstrip('\n').split('|') for line in fh]
        for f in rows:
            if len(f) > 4 and 'char' in f[3] and f[4].lstrip('-').isdigit():
                n = int(f[4])
                _LEN[(f[0], f[2])] = n // 2 if f[3] in ('nvarchar', 'nchar') and n > 0 else n
    n = _LEN.get((table, col))
    return n if n and n > 0 else None


def cols(table):
    """Writable columns of a table (no identity, no computed) in column_id order, from schema_cols.tsv."""
    if not _COLS:
        with open(os.path.join(PKG, 'schema_cols.tsv'), encoding='utf-8') as fh:
            rows = [line.rstrip('\n').split('|') for line in fh]
        for f in rows:
            if len(f) > 10 and f[1].isdigit():
                _COLS.setdefault(f[0], []).append((int(f[1]), f[2], f[8] == '0' and f[9] == '0'))
    return [c for _, c, ok in sorted(_COLS[table]) if ok]


def q(s):
    return "N'" + str(s).replace("'", "''") + "'"


def d(x):
    return "'" + x.isoformat() + "'"


def money(x):
    return str(Decimal(str(x)).quantize(Decimal('0.01'), ROUND_HALF_UP))


def guid(*parts):
    return str(uuid.uuid5(NS, '|'.join(parts))).upper()


def guard(db):
    return (f"DECLARE @db sysname=DB_NAME(); IF @db<>'{db}' "
            f"BEGIN RAISERROR('WRONG DATABASE: %s',20,1,@db) WITH LOG; END")


class Month:
    def __init__(self, ym):
        y, m = map(int, ym.split('-'))
        self.y, self.m = y, m
        self.rm = y * 100 + m
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        self.prev_rm = py * 100 + pm
        self.q = (m - 1) // 3 + 1
        self.q_end = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}[self.q]
        nm = dt.date(y + (m == 12), m % 12 + 1, 1)
        self.last = nm - dt.timedelta(days=1)
        self.first = dt.date(y, m, 1)
        self.ym = ym


def due_dates(date, days=7):
    due = date + dt.timedelta(days=days)
    pay = due - dt.timedelta(days=max(0, due.weekday() - 4))  # Sat/Sun -> Friday
    return due, pay


def clone(table, pk, tpl, over, into_var=None):
    """INSERT INTO table (...) SELECT <override or column> FROM table WHERE pk=tpl. tpl is a number or an @variable."""
    cs = cols(table)
    unknown = set(over) - set(cs)
    assert not unknown, (table, unknown)
    for k, v in over.items():  # too long a literal = 'String or binary data would be truncated' halfway through an import
        m = re.fullmatch(r"N?'((?:[^']|'')*)'", str(v))
        n = col_len(table, k)
        if m and n and len(m.group(1).replace("''", "'")) > n:
            raise SystemExit(f"{table}.{k}={v} is longer than {n} characters")
    sel = ',\n    '.join(over.get(c, c) for c in cs)
    s = f"INSERT INTO {table} ({','.join(cs)})\n  SELECT {sel}\n  FROM {table} WHERE {pk}={tpl};\n"
    s += f"IF @@ROWCOUNT<>1 RAISERROR('template {table} {tpl}',16,1);\n"
    if into_var:
        s += f"SET {into_var}=SCOPE_IDENTITY();\n"
    return s


KNT_FIELDS = ['Nazwa1', 'Nazwa2', 'Nazwa3', 'Kraj', 'Wojewodztwo', 'Powiat', 'Gmina', 'Ulica', 'NrDomu',
              'NrLokalu', 'Miasto', 'KodPocztowy', 'Poczta', 'Adres2', 'NipKraj', 'NipE']


def knt_snapshot(prefix, table, var='@knt'):
    """Counterparty data copied from the card into the document (VaN_Knt*, VaN_Platnik*, KPR_Knt*)."""
    have = set(cols(table))
    return {f'{prefix}{f}': f'(SELECT Knt_{f} FROM CDN.Kontrahenci WHERE Knt_KntId={var})'
            for f in KNT_FIELDS if f'{prefix}{f}' in have}


def operator_fields(prefix, table):
    have = set(cols(table))
    out = {}
    for f in OPE_FIELDS:
        for cand in (f'{prefix}{f}', f'{prefix}{f.replace("ID", "Id")}', f'{prefix}{f.replace("Id", "ID")}'):
            if cand in have:
                out[cand] = f'(SELECT Knt_{f} FROM CDN.Kontrahenci WHERE Knt_KntId={OPE_FROM_CARD})'
                break
    return out


def jpk_codes(parent_var, codes):
    return '\n'.join(f"INSERT CDN.DokKodyJPKV7 (DKV7_ParentTyp,DKV7_ParentID,DKV7_KV7ID,DKV7_Status) VALUES (2,{parent_var},{KV7[c]},0);"
                     for c in codes)


def closed_month_guard(date_sql):
    return (f"IF CONVERT(char(10),{date_sql},120) <= ISNULL((SELECT Fir_Wartosc FROM CDN.Firma WHERE Fir_Numer=1300),'') "
            f"RAISERROR('month closed',16,1);")


def renumber(mo):
    """As the application does: the month's buffer renumbered by date.
    VaN_Lp per register (ZAKUP, INNE; type 1), DW = Lp of the original; KPR_Lp continues from max(Lp) of the previous month."""
    return f"""
  ;WITH o AS (SELECT VaN_VaNID, n=ROW_NUMBER() OVER (PARTITION BY VaN_Rejestr ORDER BY VaN_DataZap, VaN_VaNID) FROM CDN.VatNag WITH (UPDLOCK,HOLDLOCK)
              WHERE VaN_Rejestr IN ('ZAKUP','INNE') AND VaN_Typ=1 AND VaN_RokMies={mo.rm})
  UPDATE v SET VaN_Lp=o.n FROM CDN.VatNag v JOIN o ON o.VaN_VaNID=v.VaN_VaNID WHERE v.VaN_Lp<>o.n;
  UPDATE z SET VaN_Lp=p.VaN_Lp FROM CDN.VatNag z JOIN CDN.VatNag p ON p.VaN_VaNID=z.VaN_VanOrgId
    WHERE z.VaN_RokMies={mo.rm} AND z.VaN_Rejestr='WEW' AND z.VaN_Rodzaj=1 AND z.VaN_Lp<>p.VaN_Lp;
  UPDATE z SET VaN_Lp=p.VaN_Lp FROM CDN.VatNag z JOIN CDN.VatNag p ON p.VaN_VaNID=z.VaN_VanOrgId
    WHERE z.VaN_RokMies={mo.rm} AND z.VaN_Rejestr='WEW' AND z.VaN_Rodzaj=4 AND z.VaN_Lp<>p.VaN_Lp;
  DECLARE @base int=(SELECT ISNULL(MAX(KPR_Lp),0) FROM CDN.ZapisyKPR WHERE KPR_Bufor=1 AND KPR_RokMies={mo.prev_rm});
  ;WITH k AS (SELECT KPR_KPRID, n=@base+ROW_NUMBER() OVER (ORDER BY KPR_DataOpe, KPR_KPRID) FROM CDN.ZapisyKPR WITH (UPDLOCK,HOLDLOCK)
              WHERE KPR_Bufor=1 AND KPR_RokMies={mo.rm})
  UPDATE z SET KPR_Lp=k.n FROM CDN.ZapisyKPR z JOIN k ON k.KPR_KPRID=z.KPR_KPRID WHERE z.KPR_Lp<>k.n;"""


def renumber_block(mo):
    return ("IF @fails=0 BEGIN TRY BEGIN TRAN;" + renumber(mo) +
            "\nCOMMIT; PRINT 'RENUMBERED';\nEND TRY BEGIN CATCH IF @@TRANCOUNT>0 ROLLBACK; "
            "PRINT 'FAIL renumbering: '+ERROR_MESSAGE(); SET @fails=@fails+1; END CATCH")


def kpr_lp_next(mo):
    """Temporary Lp (renumbered at the end anyway): max of the previous and current month + 1."""
    return (f"SELECT @kprlp=ISNULL(MAX(KPR_Lp),0)+1 FROM CDN.ZapisyKPR WITH (UPDLOCK,HOLDLOCK) "
            f"WHERE KPR_Bufor=1 AND KPR_RokMies BETWEEN {mo.prev_rm} AND {mo.rm};")


def new_card_sql(key, card):
    """New counterparty card = clone of a template card. card: tpl, code, name, country, iso, tax_id, [street, number, city, postal_code]."""
    g = guid('KNT', key)
    over = {'Knt_Kod': q(card['code']), 'Knt_Nazwa1': q(card['name']), 'Knt_Nazwa2': "''", 'Knt_Nazwa3': "''",
            'Knt_Kraj': q(card.get('country', '')), 'Knt_NipKraj': q(card.get('iso', '')), 'Knt_KrajISO': q(card.get('iso', '')),
            'Knt_NipE': q(card.get('tax_id', '')), 'Knt_Nip': q(card.get('tax_id', '')),
            'Knt_Miasto': q(card.get('city', '')), 'Knt_Ulica': q(card.get('street', '')), 'Knt_NrDomu': q(card.get('number', '')),
            'Knt_KodPocztowy': q(card.get('postal_code', '')), 'Knt_Adres2': "''", 'Knt_Poczta': "''",
            'Knt_GIDNumer': '0', 'Knt_GlID': 'NULL', 'Knt_ImportRowId': f"'{g}'",
            'Knt_TS_Zal': 'GETDATE()', 'Knt_TS_Mod': 'GETDATE()', 'Knt_LimitKredytuWykorzystany': '0',
            **{f'Knt_{f}': f'(SELECT Knt_{f} FROM CDN.Kontrahenci WHERE Knt_KntId={OPE_FROM_CARD})' for f in OPE_FIELDS}}
    if card.get('iso'):  # Export from the country, not the template: EU = 3 (intra-community), outside EU = 1, as on existing cards
        over['Knt_Export'] = '3' if card['iso'] in EU_ISO else '1'
    have = set(cols('CDN.Kontrahenci'))
    over = {k: v for k, v in over.items() if k in have}
    sql = (f"\n-- ===== new card {key}\n"
           f"IF NOT EXISTS (SELECT 1 FROM CDN.Kontrahenci WHERE Knt_ImportRowId='{g}') BEGIN\n"
           + clone('CDN.Kontrahenci', 'Knt_KntId', card['tpl'], over)
           + f"PRINT 'CARD {card['code']} id='+LTRIM(STR(SCOPE_IDENTITY()));\nEND")
    return sql, g


def card_expr(ref):
    """ref: int (Knt_KntId) or 'NEW:KEY' -> SQL expression returning the card id."""
    if isinstance(ref, int):
        return str(ref)
    return f"(SELECT Knt_KntId FROM CDN.Kontrahenci WHERE Knt_ImportRowId='{guid('KNT', ref[4:])}')"
