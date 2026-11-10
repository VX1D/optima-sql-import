"""Monthly sales from an order report -> summary + sales document BR/01/MM/YYYY.

1) summary:  python3 -m optima.sales summary --month 2026-08 [--zip report.zip]
   Reads the order report ('order' section); revenue = TotalPaidAmount of PAID orders with a payment > 0
   (cancelled / IN_PROGRESS / 0.00 refunds skipped; partial refunds counted at the amount paid),
   currencies -> PLN at the NBP table A rate from the last working day before the payment day (Polish time).
   Writes to months/YYYY-MM/: sales-orders.csv (every order) and sales-BR.csv (day x currency + totals + skipped).
2) BR:  python3 -m optima.sales import|rollback --month 2026-08 --db clone|prod [--amount 1234.56]
   Graph: VatNag SPRZEDAŻ 0/2 ZW + VatTab + VatTab7 + BnkZdarzenia (cash) + BnkZapisy (cash report, RVS/n/YYYY)
   + BnkRozKwoty (triggers: settlement, BnkRozRelacje, report revenue) + ZapisyKPR (Sprzedaż, Lp at the end).
"""
import argparse, collections, csv, datetime as dt, glob, io, os, zipfile, zoneinfo
from decimal import Decimal
from . import common as c, nbp

APP = c.APP['sales']
WARSAW = zoneinfo.ZoneInfo('Europe/Warsaw')


def month_path(mo):
    return c.month_dir(mo.ym)


def summarize(mo, zip_path):
    if not zip_path:
        cand = glob.glob(os.path.join(month_path(mo), 'input', '*.zip'))
        if not cand:
            raise SystemExit('No order report .zip in input/ - pass --zip')
        zip_path = cand[0]
    with zipfile.ZipFile(zip_path) as z:
        name = [n for n in z.namelist() if n.endswith('.csv')][0]
        lines = list(csv.reader(io.StringIO(z.read(name).decode('utf-8-sig'))))
    # the report has several sections (order / lineItem / summary), each with its own 'Type,...' header;
    # the full export has quoted buyer data (commas inside fields) and may lack OrderDate
    hdr = lines[0]
    rows = [dict(zip(hdr, l)) for l in lines[1:] if l and l[0] == 'order']
    if any(len(l) != len(hdr) for l in lines[1:] if l and l[0] == 'order'):
        raise SystemExit('order report: an order row has a different field count than the header')
    iso = lambda v: dt.datetime.fromisoformat(v.replace('Z', '+00:00')).astimezone(WARSAW) if v and v != 'null' else None
    order_date = lambda r: r.get('OrderDate') or r['PaymentLastDate']
    out, total = [], Decimal(0)
    for r in sorted(rows, key=order_date):
        paid, cur = Decimal(r['TotalPaidAmount'] or '0'), r['TotalPaidCurrency']
        pdt = iso(r['PaymentLastDate'])  # 'null' = order without a payment
        pdate = pdt.date() if pdt else None
        ok = r['PaymentStatus'] == 'PAID' and paid > 0 and pdate is not None
        rate_date, rate, table = nbp.rate_before(cur, pdate) if cur != 'PLN' and pdate else ('', Decimal(1), '')
        pln = Decimal(c.money(paid * rate)) if ok else Decimal(0)
        total += pln
        odt = iso(order_date(r)) or pdt
        out.append(dict(order_date=odt.strftime('%Y-%m-%d %H:%M') if odt else '',
                        payment_date=pdate.isoformat() if pdate else '', payment_id=r['PaymentId'], status=r['PaymentStatus'],
                        operator=r['PaymentProvider'], to_pay=r['TotalToPayAmount'], paid=r['TotalPaidAmount'],
                        currency=cur, nbp_table=table, rate_date=rate_date, rate=str(rate), amount_pln=str(pln),
                        included='YES' if ok else 'NO (' + ('paid 0.00' if r['PaymentStatus'] == 'PAID' else r['PaymentStatus'] or 'no payment') + ')'))
    base = month_path(mo)
    n_inc = sum(o['included'] == 'YES' for o in out)
    with open(os.path.join(base, 'sales-orders.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]), delimiter=';'); w.writeheader(); w.writerows(out)
        w.writerow({'order_date': 'TOTAL', 'amount_pln': str(total), 'included': f"{n_inc} orders"})
    days = collections.OrderedDict()
    for o in sorted((o for o in out if o['included'] == 'YES'), key=lambda o: (o['payment_date'], o['currency'])):
        e = days.setdefault((o['payment_date'], o['currency']), [0, Decimal(0), o['rate'], o['nbp_table'], Decimal(0)])
        e[0] += 1; e[1] += Decimal(o['paid']); e[4] += Decimal(o['amount_pln'])
    by_cur = collections.defaultdict(lambda: [0, Decimal(0), Decimal(0)])
    with open(os.path.join(base, 'sales-BR.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(['payment_day', 'currency', 'orders', 'sum_currency', 'nbp_rate', 'nbp_table', 'sum_pln'])
        for (day, cur), (n, sm, rate, table, pln) in days.items():
            w.writerow([day, cur, n, sm, rate, table, pln]); by_cur[cur][0] += n; by_cur[cur][1] += sm; by_cur[cur][2] += pln
        w.writerow([]); w.writerow(['CURRENCY SUMMARY'])
        for cur, (n, sm, pln) in by_cur.items():
            w.writerow(['', cur, n, sm, '', '', pln])
        w.writerow(['TOTAL', '', sum(v[0] for v in by_cur.values()), '', '', '', total])
        w.writerow([]); w.writerow(['SKIPPED'])
        for o in out:
            if o['included'] != 'YES':
                w.writerow([o['payment_date'], o['currency'], o['payment_id'], o['to_pay'], o['paid'], o['status'], o['included']])
    for cur, (n, sm, pln) in by_cur.items():
        print(f'{cur}: {n} orders, {sm} {cur} = {pln} PLN')
    print(f'TOTAL {total} PLN; included {n_inc}/{len(out)}; files in {base}')
    return total


def total_from_summary(mo):
    p = os.path.join(month_path(mo), 'sales-BR.csv')
    for r in csv.reader(open(p, encoding='utf-8-sig'), delimiter=';'):
        if r and r[0] == 'TOTAL':
            return r[-1]
    raise SystemExit('No TOTAL in sales-BR.csv - run: summary first')


def br_sql(mo, db, amt, T):
    t = T['br']; BRP = t['cash_report']
    doc = f'BR/01/{mo.m:02d}/{mo.y}'
    g = {r: c.guid('BR', doc, r) for r in ('VAN', 'BZD', 'BZP', 'BRK')}
    D, amt = c.d(mo.last), c.money(amt)
    desc = c.q(f'Rejestr VAT: {doc}.') + "+CHAR(13)+CHAR(10)+(SELECT VaN_Kategoria FROM CDN.VatNag WHERE VaN_VaNID=@van)"
    s = ["SET NOCOUNT ON; SET XACT_ABORT ON;", c.guard(db),
         "DECLARE @van int,@vat int,@bzd int,@bzp int,@kpr int,@lp int,@kprlp int,@bzplp int,@bzpnr int;",
         f"IF EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_ImportRowId='{g['VAN']}' OR VaN_Dokument='{doc}') BEGIN PRINT 'SKIP: {doc} already exists'; RETURN; END",
         f"IF EXISTS (SELECT 1 FROM CDN.BnkRaporty WHERE BRp_BRpID={BRP} AND (BRp_Zamkniety<>0 OR BRp_DataDok>{D})) BEGIN RAISERROR('STOP: cash report {BRP} closed/later',16,1); RETURN; END",
         f"IF CONVERT(char(10),{D},120) <= ISNULL((SELECT Fir_Wartosc FROM CDN.Firma WHERE Fir_Numer=1300),'') BEGIN RAISERROR('month closed',16,1); RETURN; END",
         "BEGIN TRAN;",
         f"SELECT @lp=ISNULL(MAX(VaN_Lp),0)+1 FROM CDN.VatNag WITH (UPDLOCK,HOLDLOCK) WHERE VaN_Rejestr=N'SPRZEDAŻ' AND VaN_Rodzaj=0 AND VaN_Typ=2 AND VaN_RokMies={mo.rm};"]
    van = {**{x: D for x in ['VaN_DataObowiazkuPodatkowego', 'VaN_DataPrawaOdliczenia', 'VaN_DataZap', 'VaN_DataWys',
                             'VaN_DataOpe', 'VaN_DataKur', 'VaN_DataKurDoVAT', 'VaN_Termin']},
           'VaN_RokMies': str(mo.rm), 'VaN_DeklRokMies': str(mo.rm), 'VaN_Lp': '@lp', 'VaN_IdentKsieg': "''",
           'VaN_Dokument': c.q(doc), 'VaN_KpRID': 'NULL', 'VaN_ImportAppId': f"'{APP}'", 'VaN_ImportRowId': f"'{g['VAN']}'",
           **{x: amt for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT',
                               'VaN_RazemBruttoWal', 'VaN_KwotaNKUP', 'VaN_RazemBruttoOSS', 'VaN_RazemNettoOSS']},
           'VaN_RokOSS': str(mo.y), 'VaN_KwartalOSS': str(mo.q), 'VaN_DataKurOSS': f"'{mo.y}-{mo.q_end}'",
           'VaN_TS_Zal': 'GETDATE()', 'VaN_TS_Mod': c.TS0, **c.operator_fields('VaN_', 'CDN.VatNag')}
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', t['van'], van, '@van'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', t['vat'], {'VaT_VaNID': '@van', **{x: amt for x in [
        'VaT_Netto', 'VaT_NettoDoVAT', 'VaT_NettoWal', 'VaT_NettoOSS', 'VaT_KwotaNKUP']}}, '@vat'))
    s.append(c.clone('CDN.VatTab7', 'Va7_Va7ID', t['va7'], {'Va7_VaNID': '@van', 'Va7_RokMiesiac': str(mo.rm), 'Va7_Netto': amt}))
    s.append(c.clone('CDN.BnkZdarzenia', 'BZd_BZdID', t['bzd'], {
        'BZd_PrwID': '@van', 'BZd_DokumentID': '@van', 'BZd_NumerPelny': c.q(doc), 'BZd_NumerObcy': c.q(doc), 'BZd_SplitPayNrDok': c.q(doc),
        **{x: D for x in ['BZd_DataDok', 'BZd_Termin', 'BZd_DataReal', 'BZd_DataWys', 'BZd_DataKur']},
        'BZd_Kwota': amt, 'BZd_KwotaSys': amt, 'BZd_KwotaRoz': '0', 'BZd_KwotaRozSys': '0', 'BZd_DataRoz': 'NULL',
        'BZd_Rozliczono': '1', 'BZd_Rozliczono2': '1', 'BZd_Stan': '1',
        'BZd_ElixirO1': c.q('Zapłata za ' + doc), 'BZd_Opis': desc, 'BZd_ImportRowId': f"'{g['BZD']}'", 'BZd_ImportAppId': f"'{APP}'",
        'BZd_TS_Zal': 'GETDATE()', 'BZd_TS_Mod': 'GETDATE()', **c.operator_fields('BZd_', 'CDN.BnkZdarzenia')}, '@bzd'))
    s.append(f"SELECT @bzplp=ISNULL(MAX(BZp_BRpLP),0)+1 FROM CDN.BnkZapisy WITH (UPDLOCK,HOLDLOCK) WHERE BZp_BRpID={BRP};")
    s.append(f"SELECT @bzpnr=ISNULL(MAX(BZp_NumerNr),0)+1 FROM CDN.BnkZapisy WITH (UPDLOCK,HOLDLOCK) WHERE BZp_NumerString=N'RVS/@numerS/{mo.y}';")
    s.append(c.clone('CDN.BnkZapisy', 'BZp_BZpID', t['bzp'], {
        'BZp_BRpID': str(BRP), 'BZp_BRpLP': '@bzplp', 'BZp_NumerNr': '@bzpnr', 'BZp_NumerString': f"N'RVS/@numerS/{mo.y}'",
        'BZp_NumerObcy': c.q(doc), 'BZp_DataDok': D, 'BZp_Kwota': amt, 'BZp_KwotaSys': amt, 'BZp_KwotaRoz': '0', 'BZp_KwotaRozSys': '0',
        'BZp_DataRoz': 'NULL', 'BZp_Rozliczono': '1', 'BZp_Rozliczono2': '1', 'BZp_DataKur': 'CAST(GETDATE() AS date)', 'BZp_Opis': desc,
        'BZp_ImportRowId': f"'{g['BZP']}'", 'BZp_ImportAppId': f"'{APP}'", 'BZp_TS_Zal': 'GETDATE()', 'BZp_TS_Mod': 'GETDATE()',
        **c.operator_fields('BZp_', 'CDN.BnkZapisy')}, '@bzp'))
    s.append(c.clone('CDN.BnkRozKwoty', 'BRK_BRKID', t['brk'], {
        'BRK_Kwota': amt, 'BRK_KwotaSysL': amt, 'BRK_KwotaSysP': amt, 'BRK_DataDok': D, 'BRK_LDokID': '@bzd', 'BRK_PDokID': '@bzp',
        'BRK_LNumer': c.q(doc), 'BRK_PNumer': c.q(doc), 'BRK_LKwota': amt, 'BRK_PKwota': amt,
        'BRK_ImportRowId': f"'{g['BRK']}'", 'BRK_ImportAppId': f"'{APP}'", 'BRK_TS_Zal': 'GETDATE()', 'BRK_TS_Mod': 'GETDATE()',
        **c.operator_fields('BRK_', 'CDN.BnkRozKwoty')}))
    s.append(c.kpr_lp_next(mo))
    s.append(c.clone('CDN.ZapisyKPR', 'KPR_KPRID', t['kpr'], {
        'KPR_Lp': '@kprlp', 'KPR_RokMies': str(mo.rm), 'KPR_DataOpe': D, 'KPR_DataGosp': D, 'KPR_Dokument': c.q(doc),
        'KPR_Sprzedaz': amt, 'KPR_Bufor': '1', 'KPR_Skreslony': '0', 'KPR_TS_Zal': c.TS0, 'KPR_TS_Mod': c.TS0,
        **c.operator_fields('KPR_', 'CDN.ZapisyKPR')}, '@kpr'))
    s.append("UPDATE CDN.VatNag SET VaN_KpRID=@kpr WHERE VaN_VaNID=@van;")
    s.append(c.renumber(mo))
    s.append("COMMIT;")
    s.append(f"SELECT 'VatNag' t,VaN_VaNID id,VaN_IdentKsieg ident,VaN_RazemBrutto amount FROM CDN.VatNag WHERE VaN_VaNID=@van "
             f"UNION ALL SELECT 'BZd',BZd_BZdID,LTRIM(BZd_Rozliczono)+'/'+LTRIM(BZd_Rozliczono2)+'/'+LTRIM(BZd_Stan)+' roz='+LTRIM(STR(BZd_KwotaRoz,15,2)),BZd_Kwota FROM CDN.BnkZdarzenia WHERE BZd_BZdID=@bzd "
             f"UNION ALL SELECT 'BZp',BZp_BZpID,BZp_NumerPelny+' lp='+LTRIM(BZp_BRpLP)+' '+LTRIM(BZp_Rozliczono)+'/'+LTRIM(BZp_Rozliczono2)+' roz='+LTRIM(STR(BZp_KwotaRoz,15,2)),BZp_Kwota FROM CDN.BnkZapisy WHERE BZp_BZpID=@bzp "
             f"UNION ALL SELECT 'BRR',COUNT(*),'',0 FROM CDN.BnkRozRelacje WHERE BRR_BRKID IN (SELECT BRK_BRKID FROM CDN.BnkRozKwoty WHERE BRK_ImportRowId='{g['BRK']}') "
             f"UNION ALL SELECT 'KPR',KPR_KPRID,'Lp '+LTRIM(KPR_Lp),KPR_Sprzedaz FROM CDN.ZapisyKPR WHERE KPR_KPRID=@kpr "
             f"UNION ALL SELECT 'RKB',BRp_BRpID,'revenue',BRp_Przychody FROM CDN.BnkRaporty WHERE BRp_BRpID={BRP};")
    return '\n'.join(s)


def br_rollback(mo, db, T):
    BRP = T['br']['cash_report']
    doc = f'BR/01/{mo.m:02d}/{mo.y}'
    g = {r: c.guid('BR', doc, r) for r in ('VAN', 'BZD', 'BZP', 'BRK')}
    return f"""SET NOCOUNT ON; SET XACT_ABORT ON;
{c.guard(db)}
DECLARE @van int=(SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_ImportRowId='{g['VAN']}');
DECLARE @bzd int=(SELECT BZd_BZdID FROM CDN.BnkZdarzenia WHERE BZd_ImportRowId='{g['BZD']}');
DECLARE @bzp int=(SELECT BZp_BZpID FROM CDN.BnkZapisy WHERE BZp_ImportRowId='{g['BZP']}');
DECLARE @kpr int=(SELECT VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID=@van);
IF EXISTS (SELECT 1 FROM CDN.BnkRaporty WHERE BRp_BRpID={BRP} AND BRp_Zamkniety<>0) BEGIN RAISERROR('STOP: cash report closed',16,1); RETURN; END
IF EXISTS (SELECT 1 FROM CDN.ZapisyKPR WHERE KPR_KPRID=@kpr AND KPR_Bufor=0) BEGIN RAISERROR('STOP: KPiR confirmed',16,1); RETURN; END
BEGIN TRAN;
  DELETE FROM CDN.BnkRozKwoty WHERE BRK_ImportRowId='{g['BRK']}';
  DELETE FROM CDN.BnkZapisy WHERE BZp_BZpID=@bzp;
  DELETE FROM CDN.BnkZdarzenia WHERE BZd_BZdID=@bzd;
  UPDATE CDN.VatNag SET VaN_KpRID=NULL WHERE VaN_VaNID=@van;
  DELETE FROM CDN.ZapisyKPR WHERE KPR_KPRID=@kpr;
  DELETE FROM CDN.VatTab7 WHERE Va7_VaNID=@van;
  DELETE FROM CDN.VatTab WHERE VaT_VaNID=@van;
  DELETE FROM CDN.VatNag WHERE VaN_VaNID=@van;
  {c.renumber(mo)}
COMMIT;
SELECT (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_Dokument='{doc}') vatnag,(SELECT BRp_Przychody FROM CDN.BnkRaporty WHERE BRp_BRpID={BRP}) report_revenue;"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['summary', 'import', 'rollback'])
    ap.add_argument('--month', required=True)
    ap.add_argument('--db')
    ap.add_argument('--zip')
    ap.add_argument('--amount', help='default: TOTAL from sales-BR.csv')
    a = ap.parse_args()
    mo, T = c.Month(a.month), c.load_json('templates.json')
    if a.action == 'summary':
        summarize(mo, a.zip); return
    db = c.db_name(a.db)
    if not db:
        raise SystemExit('--db is required')
    print(br_sql(mo, db, a.amount or total_from_summary(mo), T) if a.action == 'import' else br_rollback(mo, db, T))


if __name__ == '__main__':
    main()
