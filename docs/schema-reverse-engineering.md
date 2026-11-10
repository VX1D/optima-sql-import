# Optima document records: schema reference

How Comarch Optima stores a VAT register document, what its triggers do, and the rules the generators follow.
Table, column and register names are Optima's own.

Glossary: **DW** (dowód wewnętrzny) = internal document for reverse-charge VAT; **KPiR** = tax ledger
(`ZapisyKPR`); **register** = `VaN_Rejestr` (`ZAKUP` purchases, `INNE` other, `WEW` internal, `SPRZEDAŻ` sales);
**card** = counterparty (`Kontrahenci`).

## 1. Record graph

The application writes the graph itself; triggers only validate, fill a few fields and cascade deletes.

| step | records |
|---|---|
| A. document added to a VAT register | `VatNag` (original) + `VatTab` + `VatTab7` + `DokKodyJPKV7` + `BnkZdarzenia` (payment) |
| B. internal documents generated | 2× `VatNag` (purchase DW 1/2, sales DW 4/1) + `VatTab` each + `VatTab7` (purchase DW only) + `DokKodyJPKV7` |
| C. posting to KPiR | `ZapisyKPR` (buffer) + `UPDATE VatNag SET VaN_KpRID` |

Not part of the graph: `DokAtrybuty`, `KasaPit`, `DokRelacje`, `BnkRozKwoty`, `TraNag`. `RejestrOperacji` (application
log) is optional: no FKs, no triggers.

Per register:

- **ZAKUP:** 3× `VatNag`, 3× `VatTab`, 2× `VatTab7` (original, purchase DW), 1× `BnkZdarzenia` and 1× `ZapisyKPR`
  (original only). JPK codes: original `IMP` (or `BFK` when `Export=0`), purchase DW `BFK`, sales DW `IMP`+`BFK`.
- **INNE:** 1× `VatNag`, 1× `VatTab`, 1× `BnkZdarzenia`, 1× `ZapisyKPR`, `DokKodyJPKV7` `BFK` (+`IMP` when `Export=1`).
  `VatTab7` only when `Export≠0`. No DW.

## 2. Triggers

**`VatNag_InsertUpdate_Trigger`**
- rolls back when `VaN_Rejestr=''`, when `VaN_KPRId` points to a missing `ZapisyKPR`, or when `VaN_PodmiotTyp/PodID`
  points to a missing counterparty;
- replaces an empty counterparty with `(1,1)`;
- forces `VaN_RokMies = YYYYMM(DataZap)`; sets `VaN_DeklRokMies=0` to `YYYYMM(DataZap)` when `RozliczacVat7<>0`;
- with `Typ=1`, `DeklRokMiesKasa=0`, `AutoVat7Kasa=0` sets `DeklRokMiesKasa = YYYYMM(Termin)`;
- `VaN_IdentKsieg=''` → assigns `MAX(VaN_IdentKsiegNr)+1` per `YY/REGISTER` (when `DokDefinicje` has no numbering
  scheme). Importers must leave it empty; a duplicate non-empty value raises an error without rollback;
- empty `VaN_ImportRowId` → `NEWID()`; a supplied value is kept. Index `VaNGUID` is UNIQUE.

**`ZapisyKPR_InsertUpdate_Trigger`**
- rolls back when `KPR_DataOpe <= CDN.Firma(Fir_Numer=1300)` (closed month);
- forces `KPR_RokMies`, replaces an empty counterparty with `(1,1)`;
- does not assign `KPR_Lp`.

**`BnkZdarzenia_*`** (5 triggers)
- roll back when `Kwota < KwotaRoz`, when settling a document in the buffer, or when `DokumentTyp=2` points to a
  missing `VatNag`;
- set `BZd_TerminR` (`fn_GetWorkingDayAfter(Termin)`), `BZd_DataWys` (from `VaN_DataWys`), `ImportRowId` if empty,
  and update the counterparty's credit limit;
- index `BZdGUID(BZd_ImportRowId)` is UNIQUE.

`VatTab`, `VatTab7`, `DokKodyJPKV7`: no triggers with logic.

## 3. Numbering

| field | rule |
|---|---|
| `VaN_Lp` | 1..N without gaps per `(Rejestr, Rodzaj, Typ, RokMies)`, in `DataZap` order. DW have the original's `Lp`. |
| `VaN_IdentKsieg` | assigned by the trigger, in insertion order. |
| `KPR_Lp` | in `KPR_DataOpe` order; continues from `max(KPR_Lp)` of the previous month. |

The application renumbers the month's buffer after inserts; the generators do the same (`common.renumber`).

## 4. Field rules

Columns not listed are copied unchanged from the template row.

### `VatNag`, original
- `Rodzaj/Typ/Rejestr/Wewnetrzna`: ZAKUP `1/1/'ZAKUP'/0`; INNE `0/1/'INNE'/0`.
- `DataWys = DataZap = DataOpe = DataObowiazkuPodatkowego = DataPrawaOdliczenia` = document date.
- `Termin` = date + 7 (`FplID=3`, transfer).
- `DataKur = DataKurDoVAT`: PLN → document date; other currency → NBP rate date (last working day before).
- `KursNumer = KursNumerDoVAT = 12`; `KursL = KursLDoVAT` = rate (PLN: 1); `KursM = 1`.
- `Waluta = WalutaDoVAT`: currency code, `''` for PLN.
- `RazemNetto = RazemNettoDoVAT = RazemBrutto = RazemBruttoDoVAT = KwotaNKUP` = PLN amount;
  `RazemBruttoWal` = amount in the currency; `RazemVAT = VATNKUP = 0`.
- `PodmiotTyp=1`, `PodID = PlatnikID` = card; `Knt*` and `Platnik*` = snapshot of the card.
- `Export` = card's `Knt_Export`; `PodatekVat` from the card; `RozliczacVat7=1`, `DeklRokMies=YYYYMM`.
- `IdentKsieg=''`, `ImportRowId` = generated GUID, `KpRID` = NULL until the KPiR entry exists.
- OSS: `RokOSS`, `KwartalOSS`, `DataKurOSS` = quarter end, `RazemBrutto/NettoOSS` = amount in the currency.
- `OpeZal*/OpeMod*/StaZal*/StaMod*` copied from the operator card (`templates.json: operator_card`);
  `TS_Zal/TS_Mod` = now, whole seconds.

### `VatTab` (1 per document)
- Original: `Stawka=0`, `Flaga=4`, `StawkaSymbol='NP'`, `VAT=0`, `Odliczenia=0`, `KolumnaKPR=KolumnaRYC=3`,
  `Netto = NettoDoVAT = KwotaNKUP` = PLN, `NettoWal = NettoOSS` = amount in the currency.
- Purchase DW: `VaTOrgId` = original's VaTID, `Stawka=23`, `Flaga=2`, `VAT = ROUND(Netto*0.23, 2)`, `Odliczenia=0`,
  `KolumnaKPR=0`, all amounts in PLN, `KwotaNKUP = Netto+VAT`.
- Sales DW: as the purchase DW, `VaTOrgId` = purchase DW's VaTID, `Odliczenia=1`.

### `VatNag`, DW
- Copy of the original with `Wewnetrzna=1`, `Rejestr='WEW'`; purchase DW `Rodzaj/Typ=1/2`, `VanOrgId` = original;
  sales DW `4/1`, `VanOrgId` = purchase DW.
- PLN only: `Waluta=''`, `KursL=1`, `DataKur` = document date, `RazemVAT=VAT`, `RazemBrutto=Netto+VAT`.
- `KpRID=NULL`, `Lp` = original's `Lp`, `ImportAppId=''`.

### `VatTab7`
Copy of the `VatTab` line (`RokMiesiac, RodzajZakupu, Stawka, Flaga, Netto, VAT, Odliczenia`) for the original
(when `Export≠0`) and the purchase DW. None for the sales DW.

### `DokKodyJPKV7`
`(ParentTyp=2, ParentID=VaNID, KV7ID, Status=0)`; `KV7ID` 27 = `IMP`, 37 = `BFK`.

### `BnkZdarzenia` (1 per original)
- `DokumentTyp=2`, `DokumentID = PrwID = VaNID`, `PodmiotTyp=1`, `PodmiotID` = card.
- `NumerPelny = NumerObcy = SplitPayNrDok = VaN_Dokument`.
- `DataDok = DataWys` = date, `Termin` = +7, `DataReal` = `Termin`, moved to Friday when it falls on a weekend.
- `Kwota` = amount in the currency, `KwotaSys` = PLN, `Waluta = WalutaDok`, `KursL`, `DataKur` as in the header.
- `Rozliczono=1`, `Rozliczono2=1`, `Stan=1`, `KwotaRoz=0`.
- `ElixirO1/O2 = 'Zapłata za ' + number`, `Opis = 'Rejestr VAT: ' + number + '.' + CRLF + category` (application
  texts), `SplitPayNipE` = card's tax id.

### `ZapisyKPR`
- `Bufor=1`, `DataOpe = DataGosp` = date, `Dokument = VaN_Dokument`, `PodID` + counterparty snapshot.
- Amount in `KPR_Towary` (purchases), `KPR_Inne` (services, reclassified KSeF invoices) or `KPR_Sprzedaz` (sales); other columns 0.
- Full amount as cost, regardless of the percentage defined on the category.

## 5. Idempotency

- `VaN_ImportRowId` = UUIDv5(`guid_namespace`, `source|account|order_id|role`), role ∈ {`ORG`, `DWZ`, `DWS`};
  `BZd_ImportRowId` analogously. Both indexes are UNIQUE and the triggers keep supplied values.
- Each script checks for existing GUIDs before writing and prints `SKIP`.
- `VaN_ImportAppId` / `BZd_ImportAppId` = the module's batch tag; rollbacks select by it.

## 6. Write order (one transaction per document)

1. Checks: closed month (`Firma(1300)`), existing `ImportRowId`, card exists.
2. `INSERT VatNag` (original, `IdentKsieg=''`) → `SCOPE_IDENTITY()` (not `@@IDENTITY`: the trigger runs UPDATEs).
3. `VatTab` → `VatTab7` → `DokKodyJPKV7`.
4. `BnkZdarzenia`.
5. ZAKUP: purchase DW (`VatNag` + `VatTab` + `VatTab7` + codes), then sales DW.
6. `ZapisyKPR` → `UPDATE VatNag SET VaN_KpRID` (the trigger checks the KPR exists).
7. After all documents: renumber the month's buffer.

## 7. Counterparty cards

- Sources with a fixed card (`cards.json: platforms.<source>.card`) post every order to that card.
- Sources with `card: null` use one card per legal seller (`sellers`): `Knt_Nazwa1` = legal entity, `Knt_Kod` = alias.
- A new card is a clone of a template card (`new_cards.<key>.tpl`); `Knt_Export` follows from the country
  (EU = 3, outside the EU = 1). `Kontrahenci` has unique `Knt_Kod` and `KntGUID`.
- An order whose seller has no card stops generation (`MISSING CARD`).
