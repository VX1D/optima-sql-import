"""Reclassifies one supplier's invoices imported from KSeF into the INNE register with category, JPK code and KPiR entry.

Optima's KSeF import puts the invoices into the ZAKUP register (0/1) with no category, deduction 1, KPiR column 3
and no KPiR entry. Target pattern (templates.json: ksef_kpr): register INNE, category ksef_category,
VatTab: RodzajZakupu 4, Odliczenia 0 (VAT as cost), KolumnaKPR 7, NKUP = gross; JPK code BFK;
KPiR 'Inne' = gross, date = receipt date, counterparty data from the document (KSeF), not from the card.

  python3 -m optima.ksef_reclass import|rollback --month 2026-08 --db clone|prod
Documents are selected automatically: supplier card (templates.json: ksef_card), VaN_RokMies = month, VaN_NrKSeF<>'',
register ZAKUP (import) / INNE (rollback).
"""
import argparse
from . import common as c

_T = c.load_json('templates.json')
CATEGORY = _T['ksef_category']
SUPPLIER_CARD = _T['ksef_card']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['import', 'rollback'])
    ap.add_argument('--month', required=True)
    ap.add_argument('--db', required=True)
    a = ap.parse_args()
    db = c.db_name(a.db)
    mo = c.Month(a.month)
    reg_src = 'ZAKUP' if a.action == 'import' else 'INNE'
    head = f"""SET NOCOUNT ON; SET XACT_ABORT ON;
{c.guard(db)}
DECLARE @d TABLE(id int PRIMARY KEY);
INSERT @d SELECT VaN_VaNID FROM CDN.VatNag WHERE VaN_PodID={SUPPLIER_CARD} AND VaN_RokMies={mo.rm} AND ISNULL(VaN_NrKSeF,'')<>'' AND VaN_Rejestr='{reg_src}'
  {"AND VaN_KpRID IS NULL" if a.action == 'import' else "AND VaN_KatID=" + str(CATEGORY)};
DECLARE @n int=(SELECT COUNT(*) FROM @d); PRINT 'KSeF documents to process: '+LTRIM(STR(@n));
IF @n=0 BEGIN PRINT 'NOTHING TO DO'; RETURN; END"""
    if a.action == 'rollback':
        print(f"""{head}
IF EXISTS (SELECT 1 FROM CDN.ZapisyKPR k JOIN CDN.VatNag v ON v.VaN_KpRID=k.KPR_KPRID WHERE v.VaN_VaNID IN (SELECT id FROM @d) AND k.KPR_Bufor=0) BEGIN RAISERROR('STOP: KPiR confirmed',16,1); RETURN; END
DECLARE @k TABLE(id int PRIMARY KEY); INSERT @k SELECT VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @d) AND VaN_KpRID IS NOT NULL;
BEGIN TRAN;
  UPDATE CDN.VatNag SET VaN_KpRID=NULL WHERE VaN_VaNID IN (SELECT id FROM @d);
  DELETE FROM CDN.ZapisyKPR WHERE KPR_KPRID IN (SELECT id FROM @k);
  DELETE FROM CDN.DokKodyJPKV7 WHERE DKV7_ParentTyp=2 AND DKV7_ParentID IN (SELECT id FROM @d);
  UPDATE CDN.VatTab SET VaT_RodzajZakupu=1, VaT_Odliczenia=1, VaT_KatID=NULL, VaT_KatOpis=NULL, VaT_KolumnaKPR=3, VaT_KwotaNKUP=VaT_Netto, VaT_VATNKUP=0 WHERE VaT_VaNID IN (SELECT id FROM @d);
  UPDATE CDN.VatNag SET VaN_Rejestr='ZAKUP', VaN_KatID=NULL, VaN_Kategoria='', VaN_KwotaNKUP=VaN_RazemNetto, VaN_VATNKUP=0, VaN_IdentKsieg='' WHERE VaN_VaNID IN (SELECT id FROM @d);
  UPDATE CDN.BnkZdarzenia SET BZd_KatID=NULL WHERE BZd_DokumentTyp=2 AND BZd_DokumentID IN (SELECT id FROM @d);
  UPDATE CDN.DokumentyKSeF SET DKF_RejestrDocelowy='ZAKUP' WHERE DKF_VaNId IN (SELECT id FROM @d);
  {c.renumber(mo)}
COMMIT;
SELECT VaN_VaNID,VaN_Rejestr,VaN_IdentKsieg,VaN_Lp,VaN_KpRID FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @d);""")
        return
    have = set(c.cols('CDN.VatNag'))
    snap = {x: f"(SELECT VaN_Knt{x[7:]} FROM CDN.VatNag WHERE VaN_VaNID=@id)" for x in c.knt_snapshot('KPR_Knt', 'CDN.ZapisyKPR')
            if f'VaN_Knt{x[7:]}' in have}
    kpr_over = {**snap, 'KPR_Lp': '0', 'KPR_RokMies': 'YEAR(@dz)*100+MONTH(@dz)', 'KPR_DataOpe': '@dz', 'KPR_DataGosp': '@dz',
                'KPR_Dokument': '@dok', 'KPR_PodID': '@knt', 'KPR_Inne': '@br', 'KPR_Towary': '0', 'KPR_NrKSeF': '@ksef',
                'KPR_Bufor': '1', 'KPR_Skreslony': '0', 'KPR_TS_Zal': c.TS0, 'KPR_TS_Mod': c.TS0}
    print(f"""{head}
DECLARE @id int,@knt int,@dz datetime,@dok nvarchar(512),@ksef nvarchar(64),@br decimal(15,2),@kpr int;
BEGIN TRAN;
  UPDATE CDN.VatTab SET VaT_RodzajZakupu=4, VaT_Odliczenia=0, VaT_KatID={CATEGORY}, VaT_KatOpis=(SELECT Kat_Opis FROM CDN.Kategorie WHERE Kat_KatID={CATEGORY}),
         VaT_KolumnaKPR=7, VaT_KwotaNKUP=VaT_Netto+VaT_VAT, VaT_VATNKUP=VaT_VAT WHERE VaT_VaNID IN (SELECT id FROM @d);
  UPDATE CDN.VatNag SET VaN_Rejestr='INNE', VaN_KatID={CATEGORY}, VaN_Kategoria=(SELECT Kat_Opis FROM CDN.Kategorie WHERE Kat_KatID={CATEGORY}),
         VaN_KwotaNKUP=VaN_RazemBrutto, VaN_VATNKUP=VaN_RazemVAT, VaN_IdentKsieg='', VaN_TS_Mod={c.TS0} WHERE VaN_VaNID IN (SELECT id FROM @d);
  UPDATE CDN.BnkZdarzenia SET BZd_KatID={CATEGORY} WHERE BZd_DokumentTyp=2 AND BZd_DokumentID IN (SELECT id FROM @d);
  UPDATE CDN.DokumentyKSeF SET DKF_RejestrDocelowy='INNE' WHERE DKF_VaNId IN (SELECT id FROM @d);
  INSERT CDN.DokKodyJPKV7 (DKV7_ParentTyp,DKV7_ParentID,DKV7_KV7ID,DKV7_Status) SELECT 2,id,{c.KV7['BFK']},0 FROM @d
    WHERE NOT EXISTS (SELECT 1 FROM CDN.DokKodyJPKV7 WHERE DKV7_ParentTyp=2 AND DKV7_ParentID=id);
  DECLARE cur CURSOR LOCAL FAST_FORWARD FOR SELECT VaN_VaNID,VaN_PodID,VaN_DataZap,VaN_Dokument,VaN_NrKSeF,VaN_RazemBrutto FROM CDN.VatNag WHERE VaN_VaNID IN (SELECT id FROM @d) ORDER BY VaN_VaNID;
  OPEN cur; FETCH NEXT FROM cur INTO @id,@knt,@dz,@dok,@ksef,@br;
  WHILE @@FETCH_STATUS=0 BEGIN
{c.clone('CDN.ZapisyKPR', 'KPR_KPRID', _T['ksef_kpr'], kpr_over, '@kpr')}
    UPDATE CDN.VatNag SET VaN_KpRID=@kpr WHERE VaN_VaNID=@id;
    FETCH NEXT FROM cur INTO @id,@knt,@dz,@dok,@ksef,@br;
  END
  CLOSE cur; DEALLOCATE cur;
  {c.renumber(mo)}
COMMIT;
SELECT v.VaN_VaNID,v.VaN_Rejestr,v.VaN_IdentKsieg,v.VaN_Lp,v.VaN_Dokument,v.VaN_RazemBrutto,t.VaT_Odliczenia,t.VaT_KolumnaKPR,k.KPR_Lp,CONVERT(char(10),k.KPR_DataOpe,120) kd,k.KPR_Inne
 FROM CDN.VatNag v JOIN CDN.VatTab t ON t.VaT_VaNID=v.VaN_VaNID LEFT JOIN CDN.ZapisyKPR k ON k.KPR_KPRID=v.VaN_KpRID WHERE v.VaN_VaNID IN (SELECT id FROM @d);""")


if __name__ == '__main__':
    main()
