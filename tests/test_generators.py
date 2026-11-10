"""Offline tests on the synthetic month in examples/: no database, NBP rates stubbed."""
import contextlib, datetime as dt, io, os, re, subprocess, sys, unittest
from decimal import Decimal
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ['OPTIMA_DATA'] = os.path.join(ROOT, 'examples', 'months')
os.environ['OPTIMA_CONFIG_DIR'] = os.path.join(ROOT, 'config')
os.environ['OPTIMA_QUIET_EXAMPLE'] = '1'
sys.path.insert(0, ROOT)

from optima import common as c, nbp, purchases, subscriptions, sales, ksef_reclass, checks, clone_document  # noqa: E402

M = '2026-03'
_orig_load = c.load_json


def run(mod, *args):
    out = io.StringIO()
    with mock.patch.object(sys, 'argv', [mod.__name__, *args]), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(io.StringIO()), \
            mock.patch.object(nbp, 'rate_before', lambda cur, d: ((d - dt.timedelta(days=1)).isoformat(), Decimal('4.0000'), '000/A/NBP/TEST')):
        mod.main()
    return out.getvalue()


class Generators(unittest.TestCase):
    def test_every_script_has_db_guard(self):
        for mod, extra in [(purchases, []), (subscriptions, []), (ksef_reclass, []), (sales, ['--amount', '203.99']),
                           (clone_document, ['--template', 'recurring_document', '--number', 'SOP.T', '--date', '2026-03-25'])]:
            for action in ('import', 'rollback'):
                with self.subTest(mod=mod.__name__, action=action):
                    sql = run(mod, action, '--month', M, '--db', 'clone', *extra)
                    self.assertIn(f"IF @db<>'{c.DB_CLONE}'", sql)

    def test_purchases_deterministic_and_idempotent(self):
        a = run(purchases, 'import', '--month', M, '--db', 'clone')
        self.assertEqual(a, run(purchases, 'import', '--month', M, '--db', 'clone'))
        self.assertEqual(a.count('PRINT \'SKIP already imported'), 6)
        self.assertEqual(a.count("-- ===== new card"), 2)
        self.assertIn('C0000000000000001_10000001', a)

    def test_purchases_internal_docs_23_percent(self):
        sql = run(purchases, 'import', '--month', M, '--db', 'clone')
        doc = sql.split('| A-0002 |')[1].split('-- =====')[0]
        self.assertIn('27.60', doc)
        self.assertIn('147.60', doc)

    def test_purchases_missing_card_aborts(self):
        cards = c.load_json('cards.json')
        del cards['sellers']['Marketplace C']['Seller Two']
        with mock.patch.object(c, 'load_json', lambda n: cards if n == 'cards.json' else _orig_load(n)):
            with self.assertRaises(SystemExit) as e:
                run(purchases, 'import', '--month', M, '--db', 'clone')
        self.assertIn('MISSING CARD', str(e.exception))

    def test_rollback_blocks_settled_and_confirmed(self):
        sql = run(purchases, 'rollback', '--month', M, '--db', 'prod')
        self.assertIn('STOP: imported payments are already settled', sql)
        self.assertIn('STOP: imported KPiR entries are confirmed', sql)

    def test_sales_summary(self):
        out = run(sales, 'summary', '--month', M)
        self.assertIn('TOTAL 203.99 PLN; included 3/5', out)

    def test_checks_are_read_only(self):
        for action in ('preflight', 'verify', 'state'):
            sql = run(checks, action, '--month', M)
            self.assertNotRegex(re.sub(r'(DECLARE|INSERT) @\w+.*', '', sql), r'\b(INSERT|UPDATE|DELETE)\s+(INTO\s+)?CDN\.')


class Core(unittest.TestCase):
    def test_clone_rejects_too_long_literal(self):
        with self.assertRaises(SystemExit):
            c.clone('CDN.VatNag', 'VaN_VaNID', 1, {'VaN_Dokument': c.q('X' * 5000)})

    def test_clone_rejects_unknown_column(self):
        with self.assertRaises(AssertionError):
            c.clone('CDN.VatNag', 'VaN_VaNID', 1, {'VaN_NoSuchColumn': '1'})

    def test_due_date_weekend_moves_to_friday(self):
        due, pay = c.due_dates(dt.date(2026, 3, 7))
        self.assertEqual((due, pay), (dt.date(2026, 3, 14), dt.date(2026, 3, 13)))

    def test_month(self):
        m = c.Month('2026-01')
        self.assertEqual((m.rm, m.prev_rm, m.last, m.q_end), (202601, 202512, dt.date(2026, 1, 31), '03-31'))

    def test_nbp_rate_from_previous_working_day(self):
        rates = [('2026-03-05', Decimal('4.20'), 'A1'), ('2026-03-06', Decimal('4.21'), 'A2')]
        with mock.patch.object(nbp, '_rates', lambda *a: rates):
            self.assertEqual(nbp.rate_before('EUR', dt.date(2026, 3, 6))[1], Decimal('4.20'))
            self.assertEqual(nbp.rate_before('EUR', dt.date(2026, 3, 9))[1], Decimal('4.21'))


class BinSql(unittest.TestCase):
    def test_prod_write_refused_without_consent(self):
        env = {**os.environ, 'OPTIMA_SSH_HOST': 'nobody@nowhere.invalid', 'OPTIMA_SQL_SERVER': 'X\\Y'}
        env.pop('PROD_WRITE', None)
        p = subprocess.run([os.path.join(ROOT, 'bin', 'sql'), 'prod', '-'], input='UPDATE CDN.VatNag SET VaN_Lp=1;',
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertIn('REFUSED', p.stderr)


if __name__ == '__main__':
    unittest.main()
