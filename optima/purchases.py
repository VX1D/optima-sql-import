"""Purchases from CSV (parsed_all_orders.csv) -> import / rollback T-SQL.

  python3 -m optima.purchases import   --month 2026-08 --db clone|prod [--csv ...] [--manifest ...] > file.sql
  python3 -m optima.purchases rollback --month 2026-08 --db clone|prod > file.sql

Graph per order (rules: docs/schema-reverse-engineering.md):
  ZAKUP: original (NP 0%) + DW 1/2 (output VAT 23%) + DW 4/1 (input VAT 23%), a VatTab for each,
         VatTab7 (original when Export<>0, DW 1/2 always), JPK codes, BnkZdarzenia (transfer +7 days), ZapisyKPR (Towary).
  INNE: original + VatTab (+VatTab7 when Export<>0) + codes + BnkZdarzenia + ZapisyKPR.
  Document Export = card's Knt_Export; codes: ZAKUP ex0 BFK / ex<>0 IMP; INNE ex1 BFK,IMP / otherwise BFK.
Idempotency: deterministic VaN_ImportRowId (UUIDv5 source|account|order_id|role), unique index VaNGUID.
After the first FAIL the remaining orders are skipped (Lp order); the month's buffer is renumbered at the end.
"""
import argparse, collections, csv, datetime as dt, os, re, sys
from decimal import Decimal
from . import common as c

APP = c.APP['purchases']


def load_orders(path):
    g = collections.OrderedDict()
    for r in csv.DictReader(open(path, encoding='utf-8-sig'), delimiter=';'):
        g.setdefault((r['source'], r['account'], r['order_id']), []).append(r)
    return g


def manifest_numbers(manifest, sources):
    """(source, order_id) -> document number from manifest lines '<source> / <account> / <order_id> | ... purchase <number>'."""
    if not manifest or not os.path.exists(manifest):
        return {}
    text = open(manifest, encoding='utf-8').read()
    return {(src, oid): num for src in sources
            for oid, num in re.findall(rf'{re.escape(src)} / [^/\n]* / ([^\s|]+) \|[^\n]*?purchase ([A-Za-z0-9_]+)', text)}


def order_card(src, r0, cards):
    p = cards['platforms'][src]
    ref = p['card'] if p['card'] is not None else cards['sellers'][src].get(r0['seller'])
    if ref is None:
        raise SystemExit(f"MISSING CARD: {src} seller={r0['seller']!r} - add it to config/cards.json")
    return ref


def order_sql(key, rows, cards, T, doc_numbers, mo):
    src, acc, oid = key
    r0 = rows[0]
    p = cards['platforms'][src]
    reg = p['register']
    ref = order_card(src, r0, cards)
    doc = doc_numbers[(src, oid)] if p.get('doc_number_from_manifest') else oid
    date = dt.date.fromisoformat(r0['date'])
    due, pay = c.due_dates(date)
    rm = date.year * 100 + date.month
    pln = c.money(r0['order_total_pln'])
    t = T['purchase'] if reg == 'ZAKUP' else T['other']
    if p['in_currency'] and r0['currency'] != 'PLN':
        cur, cur_amt, rate, rate_date = r0['currency'], c.money(r0['order_total']), r0['nbp_rate'], dt.date.fromisoformat(r0['nbp_date'])
    else:
        cur, cur_amt, rate, rate_date = '', pln, '1', date
    g = {role: c.guid(src, acc, oid, role) for role in ('ORG', 'DWZ', 'DWS', 'BZD')}
    s = [f"\n-- ===== {src} | {acc or '-'} | {oid} | {date} | {pln} PLN ({cur_amt} {cur or 'PLN'}) | card {ref}",
         f"IF @fails>0 PRINT 'STOP (earlier FAIL): {oid}';\n"
         f"ELSE IF EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_ImportRowId IN ('{g['ORG']}','{g['DWZ']}','{g['DWS']}'))\n"
         f"  PRINT 'SKIP already imported: {oid}';\nELSE BEGIN",
         "BEGIN TRY BEGIN TRAN;",
         f"SET @knt={c.card_expr(ref)}; IF NOT EXISTS (SELECT 1 FROM CDN.Kontrahenci WHERE Knt_KntId=@knt) RAISERROR('card missing',16,1);",
         "SELECT @kex=Knt_Export FROM CDN.Kontrahenci WHERE Knt_KntId=@knt;",
         c.closed_month_guard(c.d(date))]
    kind, typ = (1, 1) if reg == 'ZAKUP' else (0, 1)
    s.append(f"SELECT @lp=ISNULL(MAX(VaN_Lp),0)+1 FROM CDN.VatNag WITH (UPDLOCK,HOLDLOCK) WHERE VaN_Rejestr='{reg}' AND VaN_Rodzaj={kind} AND VaN_Typ={typ} AND VaN_RokMies={rm};")
    dates = {x: c.d(date) for x in ['VaN_DataObowiazkuPodatkowego', 'VaN_DataPrawaOdliczenia', 'VaN_DataZap', 'VaN_DataWys', 'VaN_DataOpe']}
    base = {**dates, **c.knt_snapshot('VaN_Knt', 'CDN.VatNag'), **c.knt_snapshot('VaN_Platnik', 'CDN.VatNag'),
            'VaN_RokMies': str(rm), 'VaN_DeklRokMies': str(rm), 'VaN_DeklRokMiesKasa': '0', 'VaN_Lp': '@lp',
            'VaN_IdentKsieg': "''", 'VaN_Dokument': c.q(doc), 'VaN_PodID': '@knt', 'VaN_PlatnikID': '@knt',
            'VaN_Termin': c.d(due), 'VaN_KpRID': 'NULL', 'VaN_ImportAppId': f"'{APP}'",
            'VaN_RokOSS': str(date.year), 'VaN_KwartalOSS': str((date.month - 1) // 3 + 1),
            'VaN_DataKurOSS': f"'{date.year}-{c.Month(f'{date.year}-{date.month:02d}').q_end}'",
            'VaN_Opis': c.q(f'{src} {acc} {oid}'.replace('  ', ' ')),
            'VaN_TS_Zal': 'GETDATE()', 'VaN_TS_Mod': c.TS0}
    org = {**base, 'VaN_Waluta': c.q(cur), 'VaN_WalutaDoVAT': c.q(cur), 'VaN_KursL': rate, 'VaN_KursLDoVAT': rate,
           'VaN_DataKur': c.d(rate_date), 'VaN_DataKurDoVAT': c.d(rate_date), 'VaN_VanOrgId': 'NULL',
           **{x: pln for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT', 'VaN_KwotaNKUP']},
           **{x: '0' for x in ['VaN_RazemVAT', 'VaN_RazemVATDoVAT', 'VaN_VATNKUP', 'VaN_RazemVATOSS']},
           'VaN_RazemBruttoWal': cur_amt, 'VaN_RazemBruttoOSS': cur_amt, 'VaN_RazemNettoOSS': cur_amt,
           'VaN_ImportRowId': f"'{g['ORG']}'", 'VaN_Export': '@kex'}
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', t['van'], org, '@org'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', t['vat'], {
        'VaT_VaNID': '@org', 'VaT_VaTOrgId': 'NULL', **{x: pln for x in ['VaT_Netto', 'VaT_NettoDoVAT', 'VaT_KwotaNKUP']},
        'VaT_NettoWal': cur_amt, 'VaT_NettoOSS': cur_amt}, '@orgvat'))
    s.append("IF @kex<>0 BEGIN\n" + c.clone('CDN.VatTab7', 'Va7_Va7ID', T['va7_original'], {
        'Va7_VaNID': '@org', 'Va7_RokMiesiac': str(rm), 'Va7_Netto': pln, 'Va7_VAT': '0'}) + "END")
    ins = "INSERT CDN.DokKodyJPKV7 (DKV7_ParentTyp,DKV7_ParentID,DKV7_KV7ID,DKV7_Status) VALUES (2,@org,{},0);"
    if reg == 'ZAKUP':
        s.append(f"IF @kex=0 {ins.format(c.KV7['BFK'])} ELSE {ins.format(c.KV7['IMP'])}")
    else:
        s.append(ins.format(c.KV7['BFK']))
        s.append(f"IF @kex=1 {ins.format(c.KV7['IMP'])}")
    s.append(c.clone('CDN.BnkZdarzenia', 'BZd_BZdID', t['bzd'], {
        'BZd_PrwID': '@org', 'BZd_DokumentID': '@org', 'BZd_PodmiotID': '@knt',
        'BZd_NumerPelny': c.q(doc), 'BZd_NumerObcy': c.q(doc), 'BZd_SplitPayNrDok': c.q(doc[:30]),
        'BZd_DataDok': c.d(date), 'BZd_Termin': c.d(due), 'BZd_DataReal': c.d(pay), 'BZd_DataWys': c.d(date),
        'BZd_KwotaSys': pln, 'BZd_Kwota': cur_amt, 'BZd_Waluta': c.q(cur), 'BZd_WalutaDok': c.q(cur), 'BZd_KursL': rate,
        'BZd_DataKur': c.d(rate_date), 'BZd_ElixirO1': c.q(('Zapłata za ' + doc)[:35]), 'BZd_ElixirO2': c.q(('Zapłata za ' + doc)[35:]),
        'BZd_Opis': c.q(f'Rejestr VAT: {doc}.') + "+CHAR(13)+CHAR(10)+(SELECT VaN_Kategoria FROM CDN.VatNag WHERE VaN_VaNID=@org)",
        'BZd_KwotaRoz': '0', 'BZd_KwotaRozSys': '0', 'BZd_DataRoz': 'NULL', 'BZd_Rozliczono': '1', 'BZd_Rozliczono2': '1', 'BZd_Stan': '1',
        'BZd_SplitPayNipE': "NULLIF((SELECT Knt_NipE FROM CDN.Kontrahenci WHERE Knt_KntId=@knt),'')",
        'BZd_ImportRowId': f"'{g['BZD']}'", 'BZd_ImportAppId': f"'{APP}'", 'BZd_TS_Zal': 'GETDATE()', 'BZd_TS_Mod': 'GETDATE()'}))
    if reg == 'ZAKUP':
        vat = c.money(Decimal(pln) * Decimal('0.23'))
        gross = c.money(Decimal(pln) + Decimal(vat))
        dw = {**base, 'VaN_Waluta': "''", 'VaN_WalutaDoVAT': "''", 'VaN_KursL': '1', 'VaN_KursLDoVAT': '1',
              'VaN_DataKur': c.d(date), 'VaN_DataKurDoVAT': c.d(date),
              **{x: pln for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemNettoOSS']},
              **{x: vat for x in ['VaN_RazemVAT', 'VaN_RazemVATDoVAT', 'VaN_VATNKUP', 'VaN_RazemVATOSS']},
              **{x: gross for x in ['VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT', 'VaN_RazemBruttoWal', 'VaN_KwotaNKUP', 'VaN_RazemBruttoOSS']},
              'VaN_ImportAppId': "''"}
        dwvat = {**{x: pln for x in ['VaT_Netto', 'VaT_NettoDoVAT', 'VaT_NettoWal', 'VaT_NettoOSS']},
                 **{x: vat for x in ['VaT_VAT', 'VaT_VATDoVAT', 'VaT_VATWal', 'VaT_VATOSS', 'VaT_VATNKUP']},
                 'VaT_KwotaNKUP': gross}
        s.append(c.clone('CDN.VatNag', 'VaN_VaNID', t['dwz'], {**dw, 'VaN_VanOrgId': '@org', 'VaN_ImportRowId': f"'{g['DWZ']}'"}, '@dwz'))
        s.append(c.clone('CDN.VatTab', 'VaT_VaTID', t['dwz_vat'], {**dwvat, 'VaT_VaNID': '@dwz', 'VaT_VaTOrgId': '@orgvat'}, '@dwzvat'))
        s.append(c.clone('CDN.VatTab7', 'Va7_Va7ID', t['dwz_va7'], {'Va7_VaNID': '@dwz', 'Va7_RokMiesiac': str(rm), 'Va7_Netto': pln, 'Va7_VAT': vat}))
        s.append(c.jpk_codes('@dwz', ['BFK']))
        s.append(c.clone('CDN.VatNag', 'VaN_VaNID', t['dws'], {**dw, 'VaN_VanOrgId': '@dwz', 'VaN_ImportRowId': f"'{g['DWS']}'"}, '@dws'))
        s.append(c.clone('CDN.VatTab', 'VaT_VaTID', t['dws_vat'], {**dwvat, 'VaT_VaNID': '@dws', 'VaT_VaTOrgId': '@dwzvat'}))
        s.append(c.jpk_codes('@dws', ['IMP', 'BFK']))
    s.append(c.kpr_lp_next(mo))
    s.append(c.clone('CDN.ZapisyKPR', 'KPR_KPRID', t['kpr'], {
        **c.knt_snapshot('KPR_Knt', 'CDN.ZapisyKPR'), 'KPR_Lp': '@kprlp', 'KPR_RokMies': str(rm), 'KPR_DataOpe': c.d(date), 'KPR_DataGosp': c.d(date),
        'KPR_Dokument': c.q(doc), 'KPR_PodID': '@knt', 'KPR_Towary': pln, 'KPR_Bufor': '1', 'KPR_Skreslony': '0',
        'KPR_TS_Zal': c.TS0, 'KPR_TS_Mod': c.TS0}, '@kpr'))
    s.append("UPDATE CDN.VatNag SET VaN_KpRID=@kpr WHERE VaN_VaNID=@org;")
    s.append(f"COMMIT; PRINT 'OK {oid} org='+LTRIM(@org)+' kpr='+LTRIM(@kpr);")
    s.append(f"END TRY BEGIN CATCH IF @@TRANCOUNT>0 ROLLBACK; PRINT 'FAIL {oid}: '+ERROR_MESSAGE(); SET @fails=@fails+1; END CATCH")
    s.append("END")
    return '\n'.join(s)


def gen_import(a, mo):
    cards, T = c.load_json('cards.json'), c.load_json('templates.json')
    orders = load_orders(a.csv)
    from_manifest = [s for s, p in cards['platforms'].items() if p.get('doc_number_from_manifest')]
    doc_numbers = manifest_numbers(a.manifest, from_manifest)
    sel = [(k, v) for k, v in orders.items() if k[0] in cards['platforms'] and v[0]['date'][:7] == mo.ym]
    skipped = [k for k, v in orders.items() if k[0] not in cards['platforms']]
    if skipped:
        print(f'-- WARNING: skipped platforms not in cards.json: {sorted({k[0] for k in skipped})}', file=sys.stderr)
    missing = [(k[0], k[2]) for k, _ in sel if k[0] in from_manifest and (k[0], k[2]) not in doc_numbers]
    if missing:
        raise SystemExit(f'No document number in the manifest for: {missing}')
    sel.sort(key=lambda kv: (kv[1][0]['date'], kv[1][0]['time'], kv[0]))
    out = ["SET NOCOUNT ON; SET XACT_ABORT ON;", c.guard(a.db),
           "DECLARE @kex int,@knt int,@lp int,@kprlp int,@org int,@orgvat int,@dwz int,@dwzvat int,@dws int,@kpr int,@fails int=0;"]
    new_cards = sorted({ref[4:] for k, v in sel for ref in [order_card(k[0], v[0], cards)] if isinstance(ref, str)})
    for key in new_cards:
        out.append(c.new_card_sql(key, cards['new_cards'][key])[0])
    for k, v in sel:
        out.append(order_sql(k, v, cards, T, doc_numbers, mo))
    out.append(c.renumber_block(mo))
    out.append("PRINT 'FAILS='+LTRIM(@fails);")
    print('\n'.join(out))
    print(f'-- {len(sel)} orders, new cards: {new_cards}', file=sys.stderr)


def gen_rollback(a, mo):
    cards = c.load_json('cards.json')
    kg = ",".join(f"'{c.guid('KNT', k)}'" for k in cards['new_cards'])
    print(f"""SET NOCOUNT ON; SET XACT_ABORT ON;
{c.guard(a.db)}
IF EXISTS (SELECT 1 FROM CDN.BnkZdarzenia WHERE BZd_ImportAppId='{APP}' AND (BZd_KwotaRoz<>0 OR BZd_Rozliczono2>1 OR BZd_Stan<>1))
  BEGIN RAISERROR('STOP: imported payments are already settled - rollback needs a manual decision',16,1); RETURN; END
IF EXISTS (SELECT 1 FROM CDN.ZapisyKPR k JOIN CDN.VatNag v ON v.VaN_KpRID=k.KPR_KPRID WHERE v.VaN_ImportAppId='{APP}' AND v.VaN_RokMies={mo.rm} AND k.KPR_Bufor=0)
  BEGIN RAISERROR('STOP: imported KPiR entries are confirmed (out of buffer)',16,1); RETURN; END
DECLARE @org TABLE(id int PRIMARY KEY); INSERT @org SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_ImportAppId='{APP}' AND VaN_RokMies={mo.rm};
DECLARE @dwz TABLE(id int PRIMARY KEY); INSERT @dwz SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId IN (SELECT id FROM @org);
DECLARE @dws TABLE(id int PRIMARY KEY); INSERT @dws SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId IN (SELECT id FROM @dwz);
DECLARE @kpr TABLE(id int PRIMARY KEY); INSERT @kpr SELECT VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @org) AND VaN_KpRID IS NOT NULL;
DECLARE @n1 int=(SELECT COUNT(*) FROM @org),@n2 int=(SELECT COUNT(*) FROM @dwz),@n3 int=(SELECT COUNT(*) FROM @dws),@fails int=0;
PRINT 'to delete: org='+LTRIM(STR(@n1))+' dwz='+LTRIM(STR(@n2))+' dws='+LTRIM(STR(@n3));
BEGIN TRAN;
  UPDATE CDN.VatNag SET VaN_KpRID=NULL WHERE VaN_VaNID IN (SELECT id FROM @org);
  DELETE FROM CDN.ZapisyKPR WHERE KPR_KPRID IN (SELECT id FROM @kpr);
  DELETE FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID IN (SELECT id FROM @org);
  DELETE FROM CDN.VatTab7 WHERE Va7_VaNID IN (SELECT id FROM @org UNION SELECT id FROM @dwz UNION SELECT id FROM @dws);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @dws); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @dws);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @dwz); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @dwz);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @org); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @org);
  DELETE FROM CDN.DokKodyJPKV7 WHERE DKV7_ParentTyp=2 AND DKV7_ParentID IN (SELECT id FROM @org UNION SELECT id FROM @dwz UNION SELECT id FROM @dws);
  DELETE k FROM CDN.Kontrahenci k WHERE k.Knt_ImportRowId IN ({kg})
    AND NOT EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_PodmiotTyp=1 AND VaN_PodID=k.Knt_KntId)
    AND NOT EXISTS (SELECT 1 FROM CDN.BnkZdarzenia WHERE BZd_PodmiotTyp=1 AND BZd_PodmiotID=k.Knt_KntId)
    AND NOT EXISTS (SELECT 1 FROM CDN.ZapisyKPR WHERE KPR_PodmiotTyp=1 AND KPR_PodID=k.Knt_KntId);
COMMIT;
{c.renumber_block(mo)}
SELECT (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_ImportAppId='{APP}' AND VaN_RokMies={mo.rm}) remaining;""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['import', 'rollback'])
    ap.add_argument('--month', required=True)
    ap.add_argument('--db', required=True, help="clone | prod | full database name")
    ap.add_argument('--csv')
    ap.add_argument('--manifest')
    a = ap.parse_args()
    a.db = c.db_name(a.db)
    mo = c.Month(a.month)
    inp = c.month_dir(mo.ym, 'input')
    a.csv = a.csv or os.path.join(inp, 'parsed_all_orders.csv')
    a.manifest = a.manifest or next((os.path.join(inp, f) for f in os.listdir(inp) if f.endswith('manifest.md')), None)
    (gen_import if a.action == 'import' else gen_rollback)(a, mo)


if __name__ == '__main__':
    main()
