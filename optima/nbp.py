"""NBP average exchange rates (table A). Bookkeeping rule: the rate from the last working day BEFORE the event date."""
import datetime as dt
import json
import urllib.request
from decimal import Decimal

_cache = {}


def _rates(code, start, end):
    key = (code, start, end)
    if key not in _cache:
        url = f"https://api.nbp.pl/api/exchangerates/rates/a/{code.lower()}/{start}/{end}/?format=json"
        with urllib.request.urlopen(url, timeout=20) as r:
            _cache[key] = [(x['effectiveDate'], Decimal(str(x['mid'])), x['no']) for x in json.load(r)['rates']]
    return _cache[key]


def rate_before(code, date):
    """(rate_date, rate, table_no) from the last table A before `date`."""
    if code.upper() == 'PLN':
        return date.isoformat(), Decimal(1), ''
    start = (date - dt.timedelta(days=14)).isoformat()
    end = (date - dt.timedelta(days=1)).isoformat()
    rs = [x for x in _rates(code, start, end) if x[0] < date.isoformat()]
    if not rs:
        raise SystemExit(f'No NBP {code} rate before {date}')
    return rs[-1]
