"""Read-only check queries, backup and clone refresh.

  python3 -m optima.checks preflight --month 2026-09                  # database state before an import (counts, templates, closed month)
  python3 -m optima.checks verify --month 2026-09 [--purchases-pln 1234.56]   # rules over the month, 0 violations = OK
  python3 -m optima.checks state --month 2026-09                      # registers / KPiR / categories next to the previous month
  python3 -m optima.checks backup --label BEFORE_IMPORT_2026_09       # BACKUP COPY_ONLY + VERIFYONLY (the only write)
  python3 -m optima.checks clone                                      # rebuild the test clone from a fresh production backup
"""
import argparse
from . import common as c

BAK_DIR = c.CFG['backup_dir']
DATA_DIR = c.CFG['sql_data_dir']
BAK_PREFIX = c.CFG.get('backup_prefix', 'OPTIMA')


def preflight(mo, T):
    t, br = T['purchase'], T['br']
    return f"""SET NOCOUNT ON;
SELECT DB_NAME() db,(SELECT COUNT(*) FROM CDN.VatNag) vatnag,(SELECT COUNT(*) FROM CDN.ZapisyKPR) kpr,(SELECT COUNT(*) FROM CDN.Kontrahenci) knt,
 (SELECT COUNT(*) FROM CDN.BnkZdarzenia) bzd,(SELECT COUNT(*) FROM CDN.VatTab) vattab,(SELECT COUNT(*) FROM CDN.VatTab7) vattab7,
 (SELECT COUNT(*) FROM CDN.DokKodyJPKV7) codes,(SELECT COUNT(*) FROM CDN.PodmiotyView) entities,(SELECT COUNT(*) FROM CDN.BnkZapisy) bzp,
 (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_RokMies={mo.rm}) vatnag_month,(SELECT COUNT(*) FROM CDN.ZapisyKPR WHERE KPR_RokMies={mo.rm}) kpr_month,
 (SELECT MAX(KPR_Lp) FROM CDN.ZapisyKPR WHERE KPR_RokMies={mo.prev_rm}) kpr_lp_prev,
 ISNULL((SELECT Fir_Wartosc FROM CDN.Firma WHERE Fir_Numer=1300),'') closed_until,
 (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_VaNID IN ({t['van']},{t['dwz']},{t['dws']},{T['other']['van']},{br['van']},{T['recurring_document']},{T['subscription_non_eu']},{T['subscription_foreign']})) templates_vatnag_8,
 (SELECT COUNT(*) FROM CDN.ZapisyKPR WHERE KPR_KPRID IN ({t['kpr']},{br['kpr']},{T['ksef_kpr']})) templates_kpr_3,
 (SELECT COUNT(*) FROM CDN.BnkZdarzenia WHERE BZd_BZdID IN ({t['bzd']},{T['other']['bzd']},{br['bzd']}) AND BZd_KwotaRoz=CASE WHEN BZd_BZdID={br['bzd']} THEN BZd_Kwota ELSE 0 END) templates_bzd_3,
 (SELECT COUNT(*) FROM CDN.BnkRaporty WHERE BRp_BRpID={br['cash_report']} AND BRp_Zamkniety=0) cash_report_open;"""


def verify(mo, purchases_pln, T):
    rm = mo.rm
    exp = f"'{purchases_pln}'" if purchases_pln else "(SELECT CONVERT(varchar,SUM(KPR_Towary)) FROM CDN.ZapisyKPR WHERE KPR_RokMies=" + str(rm) + ")"
    return f"""SET NOCOUNT ON;
DECLARE @chk TABLE(name nvarchar(80), actual nvarchar(60), expected nvarchar(60));
INSERT @chk SELECT 'Lp/date inversions VatNag', (SELECT COUNT(*) FROM CDN.VatNag a JOIN CDN.VatNag b ON a.VaN_Rejestr=b.VaN_Rejestr AND a.VaN_Rodzaj=b.VaN_Rodzaj AND a.VaN_Typ=b.VaN_Typ AND a.VaN_RokMies={rm} AND b.VaN_RokMies={rm} AND a.VaN_Lp<b.VaN_Lp AND a.VaN_DataZap>b.VaN_DataZap), '0';
INSERT @chk SELECT 'Duplicate Lp (ZAKUP/INNE)', (SELECT COUNT(*) FROM (SELECT VaN_Rejestr,VaN_Lp FROM CDN.VatNag WHERE VaN_RokMies={rm} AND VaN_Typ=1 AND VaN_Rejestr IN ('ZAKUP','INNE') GROUP BY VaN_Rejestr,VaN_Lp HAVING COUNT(*)>1) x), '0';
INSERT @chk SELECT 'Lp/date inversions KPiR', (SELECT COUNT(*) FROM CDN.ZapisyKPR a JOIN CDN.ZapisyKPR b ON a.KPR_RokMies={rm} AND b.KPR_RokMies={rm} AND a.KPR_Lp<b.KPR_Lp AND a.KPR_DataOpe>b.KPR_DataOpe), '0';
INSERT @chk SELECT 'KPiR Lp without gaps (max-min+1=n)', (SELECT CASE WHEN COUNT(*)=0 OR MAX(KPR_Lp)-MIN(KPR_Lp)+1=COUNT(DISTINCT KPR_Lp) AND COUNT(DISTINCT KPR_Lp)=COUNT(*) THEN 'yes' ELSE 'NO' END FROM CDN.ZapisyKPR WHERE KPR_RokMies={rm}), 'yes';
INSERT @chk SELECT 'KPiR start = max(prev.)+1', (SELECT CASE WHEN MIN(KPR_Lp)=(SELECT MAX(KPR_Lp)+1 FROM CDN.ZapisyKPR WHERE KPR_RokMies={mo.prev_rm}) OR COUNT(*)=0 THEN 'yes' ELSE 'NO' END FROM CDN.ZapisyKPR WHERE KPR_RokMies={rm}), 'yes';
INSERT @chk SELECT 'DW Lp<>original', (SELECT COUNT(*) FROM CDN.VatNag d JOIN CDN.VatNag o ON o.VaN_VaNID=d.VaN_VanOrgId WHERE d.VaN_RokMies={rm} AND d.VaN_Lp<>o.VaN_Lp), '0';
INSERT @chk SELECT 'Document (ZAKUP/INNE/SPRZ) without KPiR', (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_RokMies={rm} AND VaN_Rejestr IN ('ZAKUP','INNE',N'SPRZEDAŻ') AND VaN_KpRID IS NULL), '0';
INSERT @chk SELECT 'Document without payment', (SELECT COUNT(*) FROM CDN.VatNag v WHERE VaN_RokMies={rm} AND VaN_Rejestr IN ('ZAKUP','INNE',N'SPRZEDAŻ') AND NOT EXISTS (SELECT 1 FROM CDN.BnkZdarzenia WHERE BZd_DokumentTyp=2 AND BZd_DokumentID=v.VaN_VaNID)), '0';
INSERT @chk SELECT 'Empty IdentKsieg', (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_RokMies={rm} AND ISNULL(VaN_IdentKsieg,'')=''), '0';
INSERT @chk SELECT 'Document without VatTab', (SELECT COUNT(*) FROM CDN.VatNag v WHERE VaN_RokMies={rm} AND NOT EXISTS (SELECT 1 FROM CDN.VatTab WHERE VaT_VaNID=v.VaN_VaNID)), '0';
INSERT @chk SELECT 'ZAKUP without DW pair', (SELECT COUNT(*) FROM CDN.VatNag o WHERE o.VaN_RokMies={rm} AND o.VaN_Rejestr='ZAKUP' AND o.VaN_Rodzaj=1 AND NOT EXISTS (SELECT 1 FROM CDN.VatNag d JOIN CDN.VatNag e ON e.VaN_VanOrgId=d.VaN_VaNID WHERE d.VaN_VanOrgId=o.VaN_VaNID)), '0';
INSERT @chk SELECT 'Imported Export <> card', (SELECT COUNT(*) FROM CDN.VatNag v JOIN CDN.Kontrahenci k ON k.Knt_KntId=v.VaN_PodID WHERE v.VaN_RokMies={rm} AND v.VaN_ImportAppId IN ('{c.APP['purchases']}','{c.APP['subscriptions']}') AND v.VaN_Export<>k.Knt_Export), '0';
INSERT @chk SELECT 'VatTab7 of imported original vs Export', (SELECT COUNT(*) FROM CDN.VatNag v WHERE v.VaN_RokMies={rm} AND v.VaN_ImportAppId IN ('{c.APP['purchases']}','{c.APP['subscriptions']}') AND (SELECT COUNT(*) FROM CDN.VatTab7 WHERE Va7_VaNID=v.VaN_VaNID)<>CASE WHEN v.VaN_Export<>0 THEN 1 ELSE 0 END), '0';
INSERT @chk SELECT 'VatTab7 DW (1/2 has, 4/1 not)', (SELECT COUNT(*) FROM CDN.VatNag v WHERE v.VaN_RokMies={rm} AND v.VaN_Rejestr='WEW' AND (SELECT COUNT(*) FROM CDN.VatTab7 WHERE Va7_VaNID=v.VaN_VaNID)<>CASE WHEN v.VaN_Rodzaj=1 THEN 1 ELSE 0 END), '0';
INSERT @chk SELECT 'DW VAT <> 23% of base', (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_RokMies={rm} AND VaN_Rejestr='WEW' AND VaN_RazemVAT<>ROUND(VaN_RazemNetto*0.23,2)), '0';
INSERT @chk SELECT 'Imported BZd description without category', (SELECT COUNT(*) FROM CDN.BnkZdarzenia WHERE BZd_ImportAppId IN ({c.APP_SQL}) AND BZd_DataDok BETWEEN '{mo.first}' AND '{mo.last}' AND BZd_Opis NOT LIKE '%'+CHAR(13)+CHAR(10)+'%'), '0';
INSERT @chk SELECT 'KSeF supplier invoices still in ZAKUP', (SELECT COUNT(*) FROM CDN.VatNag WHERE VaN_RokMies={rm} AND VaN_PodID={T['ksef_card']} AND VaN_Rejestr='ZAKUP'), '0';
INSERT @chk SELECT 'Imported cards: limit<>sum of BZd', (SELECT COUNT(*) FROM CDN.Kontrahenci k WHERE k.Knt_ImportRowId IS NOT NULL AND k.Knt_KntId>{c.OPE_FROM_CARD} AND k.Knt_LimitKredytuWykorzystany<>ISNULL((SELECT SUM(BZd_Kierunek*(BZd_KwotaSys-BZd_KwotaRozSys)) FROM CDN.BnkZdarzenia WHERE BZd_PodmiotTyp=1 AND BZd_PodmiotID=k.Knt_KntId AND BZd_Rozliczono>0),0)), '0';
INSERT @chk SELECT 'KPiR Towary = purchases from CSV', (SELECT CONVERT(varchar,SUM(KPR_Towary)) FROM CDN.ZapisyKPR WHERE KPR_RokMies={rm}), {exp};
SELECT name, actual, expected, CASE WHEN actual=expected THEN 'OK' ELSE '!!! ERROR' END result FROM @chk;
DECLARE @e int=(SELECT COUNT(*) FROM @chk WHERE actual<>expected); PRINT 'ERRORS='+LTRIM(@e);"""


def state(mo):
    return f"""SET NOCOUNT ON;
PRINT '###REGISTERS {mo.ym} vs {mo.prev_rm}';
SELECT VaN_RokMies rm,VaN_Rejestr reg,VaN_Rodzaj r,VaN_Typ t,COUNT(*) n,SUM(VaN_RazemNetto) net,SUM(VaN_RazemVAT) vat,SUM(VaN_RazemBrutto) gross
 FROM CDN.VatNag WHERE VaN_RokMies IN ({mo.prev_rm},{mo.rm}) GROUP BY VaN_RokMies,VaN_Rejestr,VaN_Rodzaj,VaN_Typ ORDER BY 2,3,4,1;
PRINT '###KPIR'; SELECT KPR_RokMies,COUNT(*) n,MIN(KPR_Lp) lp_from,MAX(KPR_Lp) lp_to,SUM(KPR_Sprzedaz) sales,SUM(KPR_Towary) goods,SUM(KPR_Inne) other
 FROM CDN.ZapisyKPR WHERE KPR_RokMies IN ({mo.prev_rm},{mo.rm}) GROUP BY KPR_RokMies ORDER BY 1;
PRINT '###CATEGORIES'; SELECT KPR_RokMies,KPR_Kategoria,COUNT(*) n,SUM(KPR_Sprzedaz+KPR_Towary+KPR_Inne) amount FROM CDN.ZapisyKPR WHERE KPR_RokMies IN ({mo.prev_rm},{mo.rm}) GROUP BY KPR_RokMies,KPR_Kategoria ORDER BY 2,1;
PRINT '###CARDS_MONTH (counterparties in INNE/ZAKUP registers)'; SELECT VaN_RokMies,VaN_Rejestr,VaN_PodID,MAX(VaN_KntNazwa1) knt,COUNT(*) n,SUM(VaN_RazemBrutto) b
 FROM CDN.VatNag WHERE VaN_RokMies IN ({mo.prev_rm},{mo.rm}) AND VaN_Rejestr IN ('ZAKUP','INNE') GROUP BY VaN_RokMies,VaN_Rejestr,VaN_PodID ORDER BY 3,1;
PRINT '###VAT9M (import of services = DW 1/2)'; SELECT VaN_RokMies, SUM(VaN_RazemNetto) base, SUM(VaN_RazemVAT) vat FROM CDN.VatNag WHERE VaN_RokMies IN ({mo.prev_rm},{mo.rm}) AND VaN_Rejestr='WEW' AND VaN_Rodzaj=1 GROUP BY VaN_RokMies;"""


def backup(label):
    f = rf"{BAK_DIR}\{BAK_PREFIX}_{label}.bak"
    return f"""SET NOCOUNT ON; USE master;
BACKUP DATABASE [{c.DB_PROD}] TO DISK=N'{f}' WITH COPY_ONLY, INIT, CHECKSUM, NAME=N'COPY_ONLY {label}';
RESTORE VERIFYONLY FROM DISK=N'{f}' WITH CHECKSUM;"""


def refresh_clone():
    f = rf"{BAK_DIR}\{BAK_PREFIX}_SANITY.bak"
    return f"""SET NOCOUNT ON; USE master;
BACKUP DATABASE [{c.DB_PROD}] TO DISK=N'{f}' WITH COPY_ONLY, INIT, CHECKSUM, NAME=N'COPY_ONLY sanity';
RESTORE VERIFYONLY FROM DISK=N'{f}' WITH CHECKSUM;
IF DB_ID('{c.DB_CLONE}') IS NOT NULL ALTER DATABASE [{c.DB_CLONE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
RESTORE DATABASE [{c.DB_CLONE}] FROM DISK=N'{f}'
 WITH REPLACE, MOVE N'{c.DB_PROD}' TO N'{DATA_DIR}\\{c.DB_CLONE}.mdf',
      MOVE N'{c.DB_PROD}_log' TO N'{DATA_DIR}\\{c.DB_CLONE}_log.ldf', RECOVERY;
ALTER DATABASE [{c.DB_CLONE}] SET MULTI_USER;
SELECT (SELECT COUNT(*) FROM [{c.DB_CLONE}].CDN.VatNag) clone_vatnag,(SELECT COUNT(*) FROM [{c.DB_PROD}].CDN.VatNag) prod_vatnag;"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['preflight', 'verify', 'state', 'backup', 'clone'])
    ap.add_argument('--month')
    ap.add_argument('--purchases-pln')
    ap.add_argument('--label')
    a = ap.parse_args()
    T = c.load_json('templates.json')
    if a.action == 'backup':
        print(backup(a.label or 'BEFORE_IMPORT')); return
    if a.action == 'clone':
        print(refresh_clone()); return
    if not a.month:
        raise SystemExit('--month is required')
    mo = c.Month(a.month)
    print({'preflight': lambda: preflight(mo, T), 'verify': lambda: verify(mo, a.purchases_pln, T),
           'state': lambda: state(mo)}[a.action]())


if __name__ == '__main__':
    main()
