import csv
import io
import unittest
from concurrent.futures import ThreadPoolExecutor
import app
import auth
import intake
import test_sales


class IntakeTests(unittest.TestCase):
    def setUp(self):self.base=test_sales.SalesTests();self.base.setUp();self.users=self.base.users
    def tearDown(self):self.base.tearDown()
    def csv(self,*rows):
        output=io.StringIO();writer=csv.DictWriter(output,fieldnames=intake.FIELDS);writer.writeheader()
        for i,row in enumerate(rows):writer.writerow({**test_sales.specs_fixture(),'order_reference':'ORDER-'+str(i),**row})
        return output.getvalue()
    def call(self,op,data,role='Sales'):
        with app.connect() as db:return intake.action(db,'/api/intake/'+op,data,self.users[role],app.create)
    def commit(self,source,preview=None,role='Sales'):
        preview=preview or self.call('preview',dict(csv=source),role)
        return self.call('commit',dict(csv=source,preview_hash=preview['preview_hash'],confirmed=True),role)
    def count(self):
        with app.connect() as db:return db.execute('SELECT count(*) FROM quotes').fetchone()[0]

    def test_preview_no_writes_and_import_uses_existing_pricing_and_review(self):
        source=self.csv({},dict(cargo_id='cold',route_id='',charter_days='2'))
        with app.connect() as db:
            db.execute('PRAGMA query_only=ON');preview=intake.preview(db,dict(csv=source),self.users['Sales'])
        self.assertEqual(self.count(),0)
        self.assertEqual(preview['rows'][0]['message'],'Proposal ready');self.assertEqual(preview['rows'][1]['message'],'Operations review')
        result=self.commit(source,preview);self.assertEqual(result['created'],2)
        with app.connect() as db:
            rows=list(db.execute('SELECT status,owner_id,pricing FROM quotes'))
            self.assertEqual({r['status'] for r in rows},{'Proposal ready','Operations review'})
            self.assertTrue(all(r['owner_id']==self.users['Sales']['id'] for r in rows))
            self.assertEqual(db.execute('SELECT count(*) FROM shipments').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM intake_orders').fetchone()[0],2)

    def test_invalid_rows_block_entire_batch_and_blank_optional_numbers_work(self):
        source=self.csv({},dict(email='bad'))
        preview=self.call('preview',dict(csv=source))
        self.assertEqual(preview['rows'][1]['status'],'Error')
        with self.assertRaises(ValueError):self.commit(source,preview)
        self.assertEqual(self.count(),0)
        source=self.csv(dict(outbound_km='',other=''))
        self.assertEqual(self.commit(source)['created'],1)

    def test_idempotency_concurrent_retry_and_global_customer_order_deduplication(self):
        source=self.csv({});preview=self.call('preview',dict(csv=source))
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.commit(source,preview),range(2)))
        self.assertEqual(self.count(),1);self.assertEqual(sum(bool(r.get('retry')) for r in results),1)
        second=self.commit(source+'\r\n',role='Admin')
        self.assertEqual(second['created'],0);self.assertEqual(second['skipped'],1)
        self.assertEqual(self.count(),1)

    def test_stale_pricing_or_duplicate_snapshot_rejected(self):
        source=self.csv({});preview=self.call('preview',dict(csv=source))
        with app.connect() as db:db.execute('UPDATE pricing_config SET version=version+1')
        with self.assertRaises(auth.AccessError):self.commit(source,preview)
        self.assertEqual(self.count(),0)
        preview=self.call('preview',dict(csv=source));self.commit(source,preview,'Admin')
        with self.assertRaises(auth.AccessError):self.commit(source,preview)
        self.assertEqual(self.count(),1)

    def test_headers_dates_limits_duplicate_rows_and_roles(self):
        for source in ('unknown\r\nvalue','order_reference,order_reference\r\nx,x'):
            with self.assertRaises(ValueError):self.call('preview',dict(csv=source))
        source=self.csv({},dict(order_reference='ORDER-0'))
        self.assertEqual(self.call('preview',dict(csv=source))['rows'][1]['status'],'Error')
        source=self.csv(dict(pickup='2020-01-01T09:00:00+08:00'))
        self.assertEqual(self.call('preview',dict(csv=source))['rows'][0]['status'],'Error')
        with self.assertRaises(ValueError):self.call('preview',dict(csv=self.csv(*({} for _ in range(101)))))
        for role in ('Viewer','Operations'):
            with self.assertRaises(auth.AccessError):self.call('preview',dict(csv=self.csv({})),role)
            with self.assertRaises(auth.AccessError):self.commit(self.csv({}),dict(preview_hash='bad'),role)
            with app.connect() as db:
                with self.assertRaises(auth.AccessError):intake.state(db,self.users[role])
