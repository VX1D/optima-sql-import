# optima-sql-import

Generates T-SQL that writes accounting documents directly into a Comarch Optima database (MS SQL Server):
VAT register entries, internal documents (DW), payments and KPiR ledger entries. The records match the ones
Optima creates when documents are entered in the GUI. Every import is idempotent and has a matching rollback.

Inputs: purchase CSV, service invoices (JSON), a marketplace order report (CSV in a zip), invoices imported from KSeF.
Output: T-SQL scripts on stdout, run with `bin/sql` or any SQL client.

## Requirements

- Python ≥ 3.9 (standard library only)
- Comarch Optima on MS SQL Server; `sqlcmd` on a Windows host reachable over ssh (for `bin/sql`)
- Internet access to `api.nbp.pl` for exchange rates (non-PLN amounts)

## Configuration

Copy the examples and fill them in (`config/*.json` is gitignored).

```bash
cp config/config.example.json    config/config.json
cp config/cards.example.json     config/cards.json
cp config/templates.example.json config/templates.json
```

| file | contents |
|---|---|
| `config.json` | database names (`db_prod`, `db_clone`), server-side backup/data paths, `guid_namespace`, `batch_tags` |
| `templates.json` | ids of template records (documents entered by hand in Optima) that new records are cloned from |
| `cards.json` | source → register and counterparty card, seller → card for marketplaces, definitions of new cards |

`guid_namespace` must be generated once (`python3 -c 'import uuid; print(uuid.uuid4())'`) and never changed:
record GUIDs, and therefore idempotency, depend on it. `batch_tags` are the `ImportAppId` values (max 5
characters) used to find and roll back each module's records.

Without local files the code falls back to `*.example.json` and prints a warning.

Environment variables:

| variable | meaning |
|---|---|
| `OPTIMA_DATA` | directory with monthly input (default `months/`) |
| `OPTIMA_CONFIG_DIR` | configuration directory (default `config/`) |
| `OPTIMA_SSH_HOST`, `OPTIMA_SQL_SERVER` | `bin/sql`: ssh target and SQL instance (required) |
| `OPTIMA_REMOTE_DIR`, `OPTIMA_SA_FILE` | `bin/sql`: working directory and `sa` password file on the Windows host |
| `PROD_WRITE=yes` | allows `bin/sql prod` to run scripts with write statements |

## Input

```
months/YYYY-MM/
  input/parsed_all_orders.csv     purchases
  input/*manifest.md              document numbers for sources with doc_number_from_manifest
  input/*.zip                     order report
  subscriptions.json              foreign service invoices
```

`parsed_all_orders.csv` (`;`-separated, UTF-8). Rows with the same `source|account|order_id` form one order.

| column | meaning |
|---|---|
| `source`, `account`, `order_id` | order key; `source` must exist in `cards.json: platforms` |
| `date`, `time` | order date (`YYYY-MM-DD`) and time, used for ordering |
| `currency`, `order_total` | order amount in its currency |
| `order_total_pln` | amount in PLN |
| `nbp_table`, `nbp_date`, `nbp_rate` | NBP rate used for conversion (non-PLN) |
| `seller` | seller name, for sources with one card per seller |

Manifest line: `<source> / <account> / <order_id> | ... purchase <document number>`.

`subscriptions.json`: a list of `{key, number, date, amount, currency, card, template, codes, description}`;
`card` is a card id or `NEW:<key from new_cards>`, `template` a key from `templates.json`, `codes` the JPK_V7
codes of the original document.

Order report: a CSV inside a zip; rows of type `order` with `OrderDate`, `PaymentId`, `PaymentStatus`,
`PaymentProvider`, `TotalToPayAmount`, `TotalPaidAmount`, `TotalPaidCurrency`, `PaymentLastDate` (ISO 8601).
Revenue = `TotalPaidAmount` of `PAID` orders with a payment above 0.

A complete synthetic month is in `examples/months/2026-03/`.

## Usage

Every generator prints T-SQL to stdout. `--db` takes `clone`, `prod` or a full database name; the script refuses
to run on any other database.

| command | result |
|---|---|
| `python3 -m optima.purchases import\|rollback --month M --db D` | purchases: ZAKUP + 2 DW (reverse charge) or INNE, payment, KPiR |
| `python3 -m optima.subscriptions import\|rollback --month M --db D` | foreign service invoices: ZAKUP + 2 DW, KPiR column "Inne" |
| `python3 -m optima.ksef_reclass import\|rollback --month M --db D` | moves one supplier's invoices imported from KSeF to the INNE register with category and KPiR entry |
| `python3 -m optima.clone_document import\|rollback --month M --db D --template T --number N --date YYYY-MM-DD [--gross G --vat V]` | copy of a template document with a new number, date and amounts |
| `python3 -m optima.sales summary --month M` | revenue per day and currency from the order report → `sales-orders.csv`, `sales-BR.csv` |
| `python3 -m optima.sales import\|rollback --month M --db D [--amount A]` | monthly sales document `BR/01/MM/YYYY` with cash payment and KPiR entry |
| `python3 -m optima.checks preflight --month M` | record counts, template presence, closed-month setting |
| `python3 -m optima.checks verify --month M [--purchases-pln X]` | 19 consistency rules; prints `ERRORS=0` when all pass |
| `python3 -m optima.checks state --month M` | registers, KPiR and categories next to the previous month |
| `python3 -m optima.checks backup [--label L]` | `BACKUP … COPY_ONLY` of production + `RESTORE VERIFYONLY` |
| `python3 -m optima.checks clone` | rebuilds the clone database from a fresh production backup |

Running a script:

```bash
python3 -m optima.purchases import --month 2026-09 --db clone | bin/sql clone -
bin/sql prod script.sql            # SELECT only; write statements need PROD_WRITE=yes
```

### Monthly workflow

```bash
M=2026-09
python3 -m optima.checks clone | PROD_WRITE=yes bin/sql prod -
python3 -m optima.ksef_reclass  import --month $M --db clone | bin/sql clone -
python3 -m optima.purchases     import --month $M --db clone | bin/sql clone -
python3 -m optima.subscriptions import --month $M --db clone | bin/sql clone -
python3 -m optima.sales summary --month $M
python3 -m optima.sales         import --month $M --db clone | bin/sql clone -
python3 -m optima.checks verify --month $M --purchases-pln <CSV total> | bin/sql clone -
python3 -m optima.checks state  --month $M | bin/sql clone -
```

Then test the rollbacks on the clone (reverse order, `checks preflight` must match production), take a backup
(`checks backup`) and run the same imports with `--db prod` and `PROD_WRITE=yes`.

## Behaviour

- **Template cloning.** New rows are `INSERT … SELECT` from template rows, overriding only the variable columns.
  Column lists come from `optima/schema_cols.tsv`; unknown columns and literals longer than the column abort
  generation.
- **Idempotency.** Each record gets a UUIDv5 `ImportRowId` (unique index in Optima). Re-running an import prints
  `SKIP`.
- **Numbering.** `Lp` per register in date order, DW share the original's `Lp`, KPiR `Lp` continues from the
  previous month; accounting identifiers are assigned by Optima's triggers.
- **Safety.** `DB_NAME()` guard in every script; one transaction per document and stop after the first failure;
  closed-month check; rollbacks stop when payments are settled or KPiR entries confirmed.
- **Rates.** NBP table A, last working day before the event date.
- Tax declarations (VAT-9M, JPK) are not written.

Record graph, trigger behaviour and field rules: [`docs/schema-reverse-engineering.md`](docs/schema-reverse-engineering.md).

## Tests

```bash
python3 -m unittest discover -s tests
```

Offline: no database, NBP rates stubbed, data from `examples/`.

## License

MIT. Not affiliated with Comarch.
