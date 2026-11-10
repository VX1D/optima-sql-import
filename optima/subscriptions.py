"""Subscriptions / foreign services without Polish VAT (ZAKUP + DW 23%, category 84, KPiR column 7 'Inne').

Input: months/YYYY-MM/subscriptions.json, a list of items:
  {"key":"SVC1","number":"INV-...","date":"2026-08-29","amount":"18","currency":"USD","card":144|"NEW:VENDOR",
   "template":"subscription_non_eu"|"subscription_foreign"|<VaNID>,"codes":["BFK"],"description":"..."}
Amount in a currency other than PLN -> PLN = amount x NBP table A rate from the last working day before the date;
the document is stored in PLN. JPK codes of the original come from the item: non-EU services BFK, EU (reverse charge) IMP,BFK.

  python3 -m optima.subscriptions import|rollback --month 2026-08 --db clone|prod
"""
import argparse, datetime as dt, json, os
from decimal import Decimal
from . import common as c, nbp

APP = c.APP['subscriptions']


def load_items(mo):
    p = c.month_dir(mo.ym, 'subscriptions.json')
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else []


def to_pln(item):
    date = dt.date.fromisoformat(item['date'])
    if item['currency'].upper() == 'PLN':
        return c.money(item['amount']), ''
    rate_date, rate, table = nbp.rate_before(item['currency'], date)
    return c.money(Decimal(item['amount']) * rate), f"{item['amount']} {item['currency']} x {rate} ({table}, {rate_date})"


def doc_sql(item, mo, T):
    num, date = item['number'], dt.date.fromisoformat(item['date'])
    pln, src = to_pln(item)
    due, pay = c.due_dates(date)
    rm = date.year * 100 + date.month
    vat = c.money(Decimal(pln) * Decimal('0.23'))
    gross = c.money(Decimal(pln) + Decimal(vat))
    g = {r: c.guid('SUB', num, r) for r in ('ORG', 'DWZ', 'DWS', 'BZD')}
    tpl = T[item['template']] if isinstance(item['template'], str) else item['template']
    desc = item.get('description', '') + (f' {src}' if src else '')
    s = [f"\n-- ===== {item['key']} {num} {date} {pln} PLN ({desc.strip()})",
         f"IF @fails>0 PRINT 'STOP: {num}';",
         f"ELSE IF EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_ImportRowId='{g['ORG']}' OR (VaN_Dokument={c.q(num)} AND VaN_Rejestr='ZAKUP')) PRINT 'SKIP: {num}';",
         "ELSE BEGIN", "BEGIN TRY BEGIN TRAN;",
         f"SET @knt={c.card_expr(item['card'])}; IF @knt IS NULL RAISERROR('card missing',16,1); SELECT @kex=Knt_Export FROM CDN.Kontrahenci WHERE Knt_KntId=@knt;",
         f"SET @t={tpl}; SELECT @tz=VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId=@t; SELECT @ts=VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId=@tz;",
         "SELECT @tvat=VaT_VaTID FROM CDN.VatTab WHERE VaT_VaNID=@t; SELECT @tzvat=VaT_VaTID FROM CDN.VatTab WHERE VaT_VaNID=@tz; SELECT @tsvat=VaT_VaTID FROM CDN.VatTab WHERE VaT_VaNID=@ts;",
         "SELECT @tzva7=Va7_Va7ID FROM CDN.VatTab7 WHERE Va7_VaNID=@tz; SELECT @tbzd=BZd_BZdID FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID=@t; SELECT @tkpr=VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID=@t;",
         "IF @tz IS NULL OR @ts IS NULL OR @tvat IS NULL OR @tzvat IS NULL OR @tsvat IS NULL OR @tzva7 IS NULL OR @tbzd IS NULL OR @tkpr IS NULL RAISERROR('incomplete template',16,1);",
         c.closed_month_guard(c.d(date)),
         f"SELECT @lp=ISNULL(MAX(VaN_Lp),0)+1 FROM CDN.VatNag WITH (UPDLOCK,HOLDLOCK) WHERE VaN_Rejestr='ZAKUP' AND VaN_Rodzaj=1 AND VaN_Typ=1 AND VaN_RokMies={rm};"]
    D = c.d(date)
    base = {**{x: D for x in ['VaN_DataObowiazkuPodatkowego', 'VaN_DataPrawaOdliczenia', 'VaN_DataZap', 'VaN_DataWys', 'VaN_DataOpe',
                              'VaN_DataKur', 'VaN_DataKurDoVAT']},
            **c.knt_snapshot('VaN_Knt', 'CDN.VatNag'), **c.knt_snapshot('VaN_Platnik', 'CDN.VatNag'),
            'VaN_RokMies': str(rm), 'VaN_DeklRokMies': str(rm), 'VaN_DeklRokMiesKasa': '0', 'VaN_Lp': '@lp', 'VaN_IdentKsieg': "''",
            'VaN_Dokument': c.q(num), 'VaN_PodID': '@knt', 'VaN_PlatnikID': '@knt', 'VaN_Termin': c.d(due), 'VaN_KpRID': 'NULL',
            'VaN_Waluta': "''", 'VaN_WalutaDoVAT': "''", 'VaN_KursL': '1', 'VaN_KursLDoVAT': '1',
            'VaN_RokOSS': str(date.year), 'VaN_KwartalOSS': str(mo.q), 'VaN_DataKurOSS': f"'{date.year}-{mo.q_end}'",
            'VaN_Opis': c.q(desc.strip()), 'VaN_TS_Zal': 'GETDATE()', 'VaN_TS_Mod': c.TS0, **c.operator_fields('VaN_', 'CDN.VatNag')}
    org = {**base, 'VaN_Export': '@kex', 'VaN_PodatekVat': '(SELECT Knt_PodatekVat FROM CDN.Kontrahenci WHERE Knt_KntId=@knt)',
           'VaN_VanOrgId': 'NULL', 'VaN_ImportAppId': f"'{APP}'", 'VaN_ImportRowId': f"'{g['ORG']}'",
           **{x: pln for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT', 'VaN_KwotaNKUP',
                               'VaN_RazemBruttoWal', 'VaN_RazemBruttoOSS', 'VaN_RazemNettoOSS']},
           **{x: '0' for x in ['VaN_RazemVAT', 'VaN_RazemVATDoVAT', 'VaN_VATNKUP', 'VaN_RazemVATOSS']}}
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', '@t', org, '@org'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', '@tvat', {'VaT_VaNID': '@org', 'VaT_VaTOrgId': 'NULL', **{x: pln for x in [
        'VaT_Netto', 'VaT_NettoDoVAT', 'VaT_KwotaNKUP', 'VaT_NettoWal', 'VaT_NettoOSS']}}, '@orgvat'))
    s.append("IF @kex<>0 BEGIN\n" + c.clone('CDN.VatTab7', 'Va7_Va7ID', '@tzva7', {
        'Va7_VaNID': '@org', 'Va7_RokMiesiac': str(rm), 'Va7_Stawka': '0', 'Va7_Flaga': '4', 'Va7_Netto': pln, 'Va7_VAT': '0', 'Va7_Odliczenia': '0'}) + "END")
    s.append(c.jpk_codes('@org', item['codes']))
    s.append(c.clone('CDN.BnkZdarzenia', 'BZd_BZdID', '@tbzd', {
        'BZd_PrwID': '@org', 'BZd_DokumentID': '@org', 'BZd_PodmiotID': '@knt', 'BZd_NumerPelny': c.q(num), 'BZd_NumerObcy': c.q(num),
        'BZd_SplitPayNrDok': c.q(num), 'BZd_DataDok': D, 'BZd_Termin': c.d(due), 'BZd_DataReal': c.d(pay), 'BZd_DataWys': D,
        'BZd_KwotaSys': pln, 'BZd_Kwota': pln, 'BZd_Waluta': "''", 'BZd_WalutaDok': "''", 'BZd_KursL': '1', 'BZd_DataKur': D,
        'BZd_KwotaRoz': '0', 'BZd_KwotaRozSys': '0', 'BZd_DataRoz': 'NULL', 'BZd_Rozliczono': '1', 'BZd_Rozliczono2': '1', 'BZd_Stan': '1',
        'BZd_ElixirO1': c.q(('Zapłata za ' + num)[:35]), 'BZd_ElixirO2': c.q(('Zapłata za ' + num)[35:]),
        'BZd_Opis': c.q(f'Rejestr VAT: {num}.') + "+CHAR(13)+CHAR(10)+(SELECT VaN_Kategoria FROM CDN.VatNag WHERE VaN_VaNID=@org)",
        'BZd_SplitPayNipE': "NULLIF((SELECT Knt_NipE FROM CDN.Kontrahenci WHERE Knt_KntId=@knt),'')",
        'BZd_ImportRowId': f"'{g['BZD']}'", 'BZd_ImportAppId': f"'{APP}'", 'BZd_TS_Zal': 'GETDATE()', 'BZd_TS_Mod': 'GETDATE()'}))
    dw = {**base, 'VaN_ImportAppId': "''", **{x: pln for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemNettoOSS']},
          **{x: vat for x in ['VaN_RazemVAT', 'VaN_RazemVATDoVAT', 'VaN_VATNKUP', 'VaN_RazemVATOSS']},
          **{x: gross for x in ['VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT', 'VaN_RazemBruttoWal', 'VaN_KwotaNKUP', 'VaN_RazemBruttoOSS']}}
    dwvat = {**{x: pln for x in ['VaT_Netto', 'VaT_NettoDoVAT', 'VaT_NettoWal', 'VaT_NettoOSS']},
             **{x: vat for x in ['VaT_VAT', 'VaT_VATDoVAT', 'VaT_VATWal', 'VaT_VATOSS', 'VaT_VATNKUP']}, 'VaT_KwotaNKUP': gross}
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', '@tz', {**dw, 'VaN_VanOrgId': '@org', 'VaN_ImportRowId': f"'{g['DWZ']}'"}, '@dwz'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', '@tzvat', {**dwvat, 'VaT_VaNID': '@dwz', 'VaT_VaTOrgId': '@orgvat'}, '@dwzvat'))
    s.append(c.clone('CDN.VatTab7', 'Va7_Va7ID', '@tzva7', {'Va7_VaNID': '@dwz', 'Va7_RokMiesiac': str(rm), 'Va7_Netto': pln, 'Va7_VAT': vat}))
    s.append(c.jpk_codes('@dwz', ['BFK']))
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', '@ts', {**dw, 'VaN_VanOrgId': '@dwz', 'VaN_ImportRowId': f"'{g['DWS']}'"}, '@dws'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', '@tsvat', {**dwvat, 'VaT_VaNID': '@dws', 'VaT_VaTOrgId': '@dwzvat'}))
    s.append(c.jpk_codes('@dws', ['IMP', 'BFK']))
    s.append(c.kpr_lp_next(mo))
    s.append(c.clone('CDN.ZapisyKPR', 'KPR_KPRID', '@tkpr', {
        **c.knt_snapshot('KPR_Knt', 'CDN.ZapisyKPR'), 'KPR_Lp': '@kprlp', 'KPR_RokMies': str(rm), 'KPR_DataOpe': D, 'KPR_DataGosp': D,
        'KPR_Dokument': c.q(num), 'KPR_PodID': '@knt', 'KPR_Towary': '0', 'KPR_Inne': pln, 'KPR_Bufor': '1', 'KPR_Skreslony': '0',
        'KPR_TS_Zal': c.TS0, 'KPR_TS_Mod': c.TS0, **c.operator_fields('KPR_', 'CDN.ZapisyKPR')}, '@kpr'))
    s.append("UPDATE CDN.VatNag SET VaN_KpRID=@kpr WHERE VaN_VaNID=@org;")
    s.append(f"COMMIT; PRINT 'OK {num} org='+LTRIM(@org)+' kpr='+LTRIM(@kpr);")
    s.append(f"END TRY BEGIN CATCH IF @@TRANCOUNT>0 ROLLBACK; PRINT 'FAIL {num}: '+ERROR_MESSAGE(); SET @fails=@fails+1; END CATCH")
    s.append("END")
    return '\n'.join(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['import', 'rollback'])
    ap.add_argument('--month', required=True)
    ap.add_argument('--db', required=True)
    a = ap.parse_args()
    db = c.db_name(a.db)
    mo, cards, T = c.Month(a.month), c.load_json('cards.json'), c.load_json('templates.json')
    items = load_items(mo)
    new_cards = sorted({i['card'][4:] for i in items if isinstance(i['card'], str)})
    if a.action == 'rollback':
        kg = ",".join(f"'{c.guid('KNT', k)}'" for k in new_cards) or "''"
        print(f"""SET NOCOUNT ON; SET XACT_ABORT ON;
{c.guard(db)}
IF EXISTS (SELECT 1 FROM CDN.BnkZdarzenia WHERE BZd_ImportAppId='{APP}' AND BZd_DataDok BETWEEN '{mo.first}' AND '{mo.last}' AND (BZd_KwotaRoz<>0 OR BZd_Rozliczono2>1)) BEGIN RAISERROR('STOP: payments settled',16,1); RETURN; END
DECLARE @org TABLE(id int PRIMARY KEY); INSERT @org SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_ImportAppId='{APP}' AND VaN_RokMies={mo.rm};
IF EXISTS (SELECT 1 FROM CDN.ZapisyKPR k JOIN CDN.VatNag v ON v.VaN_KpRID=k.KPR_KPRID WHERE v.VaN_VaNID IN (SELECT id FROM @org) AND k.KPR_Bufor=0) BEGIN RAISERROR('STOP: KPiR confirmed',16,1); RETURN; END
DECLARE @dwz TABLE(id int PRIMARY KEY); INSERT @dwz SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId IN (SELECT id FROM @org);
DECLARE @dws TABLE(id int PRIMARY KEY); INSERT @dws SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_VanOrgId IN (SELECT id FROM @dwz);
DECLARE @kpr TABLE(id int PRIMARY KEY); INSERT @kpr SELECT VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @org) AND VaN_KpRID IS NOT NULL;
DECLARE @fails int=0;
BEGIN TRAN;
  UPDATE CDN.VatNag SET VaN_KpRID=NULL WHERE VaN_VaNID IN (SELECT id FROM @org);
  DELETE FROM CDN.ZapisyKPR WHERE KPR_KPRID IN (SELECT id FROM @kpr);
  DELETE FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID IN (SELECT id FROM @org);
  DELETE FROM CDN.VatTab7 WHERE Va7_VaNID IN (SELECT id FROM @org UNION SELECT id FROM @dwz);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @dws); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @dws);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @dwz); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @dwz);
  DELETE FROM CDN.VatTab WHERE VaT_VaNID IN (SELECT id FROM @org); DELETE FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @org);
  DELETE k FROM CDN.Kontrahenci k WHERE k.Knt_ImportRowId IN ({kg}) AND NOT EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_PodID=k.Knt_KntId)
    AND NOT EXISTS (SELECT 1 FROM CDN.BnkZdarzenia WHERE BZd_PodmiotID=k.Knt_KntId AND BZd_PodmiotTyp=1) AND NOT EXISTS (SELECT 1 FROM CDN.ZapisyKPR WHERE KPR_PodID=k.Knt_KntId);
COMMIT;
{c.renumber_block(mo)}
SELECT (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_ImportAppId='{APP}' AND VaN_RokMies={mo.rm}) remaining;""")
        return
    out = ["SET NOCOUNT ON; SET XACT_ABORT ON;", c.guard(db),
           "DECLARE @knt int,@kex int,@t int,@tz int,@ts int,@tvat int,@tzvat int,@tsvat int,@tzva7 int,@tbzd int,@tkpr int,"
           "@lp int,@kprlp int,@org int,@orgvat int,@dwz int,@dwzvat int,@dws int,@kpr int,@fails int=0;"]
    for k in new_cards:
        out.append(c.new_card_sql(k, cards['new_cards'][k])[0])
    for i in items:
        out.append(doc_sql(i, mo, T))
    out.append(c.renumber_block(mo))
    out.append("PRINT 'FAILS='+LTRIM(@fails);")
    print('\n'.join(out))


if __name__ == '__main__':
    main()
