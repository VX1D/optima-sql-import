"""New document as a clone of an existing document of the same type (same counterparty, category, VAT rate, codes,
VatTab7, payment, KPiR) with a new number, date and optionally amounts. For recurring costs with Polish VAT
(e.g. a monthly invoice registered in INNE 0/1 with 23% VAT included and KPiR 'Inne'). No DW.

  python3 -m optima.clone_document import|rollback --month 2026-08 --db clone|prod \
      --template recurring_document --number 'INV-0001' --date 2026-08-25 [--gross 99.99 --vat 18.70]
Without --gross/--vat the amounts are copied from the template (only if identical - check the document!).
"""
import argparse
from decimal import Decimal
from . import common as c

APP = c.APP['clone_document']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['import', 'rollback'])
    ap.add_argument('--month', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--template', required=True, help='key from templates.json or a VaNID')
    ap.add_argument('--number', required=True)
    ap.add_argument('--date', required=True)
    ap.add_argument('--gross')
    ap.add_argument('--vat')
    ap.add_argument('--description', default='')
    a = ap.parse_args()
    db = c.db_name(a.db)
    mo, T = c.Month(a.month), c.load_json('templates.json')
    tpl = T[a.template] if not a.template.isdigit() else int(a.template)
    NUM, D = a.number, f"'{a.date}'"
    G, GB = c.guid('GP', NUM), c.guid('GP', NUM, 'BZD')
    if a.action == 'rollback':
        print(f"""SET NOCOUNT ON; SET XACT_ABORT ON;
{c.guard(db)}
DECLARE @v int=(SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_ImportRowId='{G}'); DECLARE @k int=(SELECT VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID=@v);
IF EXISTS (SELECT 1 FROM CDN.ZapisyKPR WHERE KPR_KPRID=@k AND KPR_Bufor=0) BEGIN RAISERROR('STOP: KPiR confirmed',16,1); RETURN; END
BEGIN TRAN; UPDATE CDN.VatNag SET VaN_KpRID=NULL WHERE VaN_VaNID=@v; DELETE FROM CDN.ZapisyKPR WHERE KPR_KPRID=@k;
 DELETE FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID=@v; DELETE FROM CDN.VatTab7 WHERE Va7_VaNID=@v;
 DELETE FROM CDN.DokKodyJPKV7 WHERE DKV7_ParentTyp=2 AND DKV7_ParentID=@v; DELETE FROM CDN.VatTab WHERE VaT_VaNID=@v; DELETE FROM CDN.VatNag WHERE VaN_VaNID=@v;
 {c.renumber(mo)}
COMMIT; SELECT COUNT(*) remaining FROM CDN.VatNag WHERE VaN_Dokument={c.q(NUM)};""")
        return
    amt_van, amt_vat, amt_bzd, amt_kpr = {}, {}, {}, {}
    if a.gross:
        gross, vat = Decimal(a.gross), Decimal(a.vat or 0)
        B, V, N = c.money(gross), c.money(vat), c.money(gross - vat)
        amt_van = {**{x: N for x in ['VaN_RazemNetto', 'VaN_RazemNettoDoVAT', 'VaN_RazemNettoOSS']},
                  **{x: V for x in ['VaN_RazemVAT', 'VaN_RazemVATDoVAT', 'VaN_RazemVATOSS', 'VaN_VATNKUP']},
                  **{x: B for x in ['VaN_RazemBrutto', 'VaN_RazemBruttoDoVAT', 'VaN_RazemBruttoWal', 'VaN_RazemBruttoOSS', 'VaN_KwotaNKUP']}}
        amt_vat = {**{x: N for x in ['VaT_Netto', 'VaT_NettoDoVAT', 'VaT_NettoWal', 'VaT_NettoOSS']},
                  **{x: V for x in ['VaT_VAT', 'VaT_VATDoVAT', 'VaT_VATWal', 'VaT_VATOSS', 'VaT_VATNKUP']}, 'VaT_KwotaNKUP': B}
        amt_bzd = {'BZd_Kwota': B, 'BZd_KwotaSys': B}
        amt_kpr = {'KPR_Inne': B}
    s = ["SET NOCOUNT ON; SET XACT_ABORT ON;", c.guard(db),
         f"IF EXISTS (SELECT 1 FROM CDN.VatNag WHERE VaN_ImportRowId='{G}' OR VaN_Dokument={c.q(NUM)}) BEGIN PRINT 'SKIP {NUM}'; RETURN; END",
         "DECLARE @org int,@kpr int,@tkpr int,@tbzd int,@tva7 int,@tvat int;",
         f"SELECT @tkpr=VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID={tpl}; SELECT @tbzd=BZd_BZdID FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID={tpl};",
         f"SELECT @tva7=Va7_Va7ID FROM CDN.VatTab7 WHERE Va7_VaNID={tpl}; SELECT @tvat=VaT_VaTID FROM CDN.VatTab WHERE VaT_VaNID={tpl};",
         "IF @tkpr IS NULL OR @tbzd IS NULL OR @tvat IS NULL BEGIN RAISERROR('incomplete template',16,1); RETURN; END",
         c.closed_month_guard(D).replace("RAISERROR('month closed',16,1);", "BEGIN RAISERROR('month closed',16,1); RETURN; END"),
         "BEGIN TRAN;"]
    dates = {x: D for x in ['VaN_DataObowiazkuPodatkowego', 'VaN_DataPrawaOdliczenia', 'VaN_DataZap', 'VaN_DataWys', 'VaN_DataOpe',
                            'VaN_DataKur', 'VaN_DataKurDoVAT', 'VaN_Termin']}
    s.append(c.clone('CDN.VatNag', 'VaN_VaNID', tpl, {**dates, **amt_van, 'VaN_RokMies': str(mo.rm), 'VaN_DeklRokMies': str(mo.rm), 'VaN_Lp': '0',
             'VaN_IdentKsieg': "''", 'VaN_Dokument': c.q(NUM), 'VaN_KpRID': 'NULL', 'VaN_ImportAppId': f"'{APP}'", 'VaN_ImportRowId': f"'{G}'",
             'VaN_Opis': c.q(a.description), 'VaN_TS_Zal': 'GETDATE()', 'VaN_TS_Mod': c.TS0, 'VaN_KwartalOSS': str(mo.q),
             'VaN_DataKurOSS': f"'{mo.y}-{mo.q_end}'", **c.operator_fields('VaN_', 'CDN.VatNag')}, '@org'))
    s.append(c.clone('CDN.VatTab', 'VaT_VaTID', '@tvat', {'VaT_VaNID': '@org', **amt_vat}))
    s.append("IF @tva7 IS NOT NULL BEGIN\n" + c.clone('CDN.VatTab7', 'Va7_Va7ID', '@tva7', {
        'Va7_VaNID': '@org', 'Va7_RokMiesiac': str(mo.rm), **({'Va7_Netto': amt_vat['VaT_Netto'], 'Va7_VAT': amt_vat['VaT_VAT']} if amt_vat else {})}) + "END")
    s.append(f"INSERT CDN.DokKodyJPKV7 (DKV7_ParentTyp,DKV7_ParentID,DKV7_KV7ID,DKV7_Status) SELECT DKV7_ParentTyp,@org,DKV7_KV7ID,DKV7_Status FROM CDN.DokKodyJPKV7 WHERE DKV7_ParentTyp=2 AND DKV7_ParentID={tpl};")
    s.append(c.clone('CDN.BnkZdarzenia', 'BZd_BZdID', '@tbzd', {
        'BZd_PrwID': '@org', 'BZd_DokumentID': '@org', 'BZd_NumerPelny': c.q(NUM), 'BZd_NumerObcy': c.q(NUM), 'BZd_SplitPayNrDok': c.q(NUM),
        **{x: D for x in ['BZd_DataDok', 'BZd_Termin', 'BZd_DataReal', 'BZd_DataWys', 'BZd_DataKur']}, **amt_bzd,
        'BZd_ElixirO1': c.q(('Zapłata za ' + NUM)[:35]), 'BZd_ElixirO2': c.q(('Zapłata za ' + NUM)[35:]),
        'BZd_Opis': c.q(f'Rejestr VAT: {NUM}.') + "+CHAR(13)+CHAR(10)+(SELECT VaN_Kategoria FROM CDN.VatNag WHERE VaN_VaNID=@org)",
        'BZd_KwotaRoz': '0', 'BZd_KwotaRozSys': '0', 'BZd_DataRoz': 'NULL', 'BZd_Rozliczono': '1', 'BZd_Rozliczono2': '1', 'BZd_Stan': '1',
        'BZd_ImportRowId': f"'{GB}'", 'BZd_ImportAppId': f"'{APP}'", 'BZd_TS_Zal': 'GETDATE()', 'BZd_TS_Mod': 'GETDATE()'}))
    s.append(c.clone('CDN.ZapisyKPR', 'KPR_KPRID', '@tkpr', {'KPR_Lp': '0', 'KPR_RokMies': str(mo.rm), 'KPR_DataOpe': D, 'KPR_DataGosp': D,
             'KPR_Dokument': c.q(NUM), **amt_kpr, 'KPR_Bufor': '1', 'KPR_Skreslony': '0', 'KPR_TS_Zal': c.TS0, 'KPR_TS_Mod': c.TS0}, '@kpr'))
    s.append("UPDATE CDN.VatNag SET VaN_KpRID=@kpr WHERE VaN_VaNID=@org;")
    s.append(c.renumber(mo))
    s.append("COMMIT;")
    s.append("SELECT v.VaN_VaNID,v.VaN_Rejestr,v.VaN_IdentKsieg,v.VaN_Lp,CONVERT(char(10),v.VaN_DataZap,120) d,v.VaN_RazemNetto,v.VaN_RazemVAT,v.VaN_RazemBrutto,k.KPR_Lp,k.KPR_Inne FROM CDN.VatNag v JOIN CDN.ZapisyKPR k ON k.KPR_KPRID=v.VaN_KpRID WHERE v.VaN_VaNID=@org;")
    print('\n'.join(s))


if __name__ == '__main__':
    main()
