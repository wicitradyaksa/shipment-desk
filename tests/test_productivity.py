import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
import app,auth,workspace,productivity,profiles
import test_sales


class ProductivityTests(unittest.TestCase):
    def setUp(self):
        self.base=test_sales.SalesTests();self.base.setUp();self.users=self.base.users
    def tearDown(self):self.base.tearDown()
    def state(self,role='Admin'):
        with app.connect() as db:return workspace.state(db,self.users[role])
    def act(self,op,data,role='Admin'):
        with app.connect() as db:return workspace.action(db,'/api/workspace/'+op,data,self.users[role])
    def task(self,role='Admin'):return self.state(role)['items'][0]['task']
    def data(self,r,**changes):return dict(task_id=r['id'],version=r['version'],**changes)

    def test_business_calendar_weekend_and_fixed_offset(self):
        c=productivity.DEFAULT
        start='2026-10-16T09:00:00+00:00' # Friday 17:00 Taiwan
        end=productivity.deadline(start,2,c)
        self.assertEqual(end,'2026-10-19T02:00:00+00:00')
        self.assertEqual(productivity.business_seconds(start,end,c),7200)

    def test_claim_is_atomic_and_wrong_team_denied(self):
        self.base.quote(cargo_id='cold');r=self.task('Operations')
        with self.assertRaises(auth.AccessError):self.act('claim',self.data(r),'Sales')
        def claim(_):
            try:self.act('claim',self.data(r),'Operations');return 'ok'
            except auth.AccessError:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sorted(pool.map(claim,range(2))),['conflict','ok'])
        self.assertEqual(self.task('Operations')['owner_id'],self.users['Operations']['id'])

    def test_sales_transfer_moves_record_access(self):
        self.base.quote();r=self.task()
        with app.connect() as db:
            auth.add_user(db,dict(name='Second Sales',username='second',role='Sales',password='Test-password-only'))
            other=auth.public(db.execute("SELECT * FROM users WHERE username='second'").fetchone())
        self.act('assign',self.data(r,owner_id=other['id']))
        self.assertEqual(self.state('Sales')['items'],[])
        with app.connect() as db:
            self.assertEqual(workspace.state(db,other)['items'][0]['owner_id'],other['id'])
        with self.assertRaises(auth.AccessError):self.act('followup',self.data(r,waiting='Working',note='Old owner'),'Sales')

    def test_waiting_followup_resolves_with_source_and_preserves_history(self):
        q=self.base.quote();r=self.task('Sales')
        future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
        self.act('followup',self.data(r,waiting='Waiting for client',note='Called buyer',followup=future),'Sales')
        r=self.task('Sales');self.assertEqual(r['waiting'],'Waiting for client')
        with app.connect() as db:db.execute("UPDATE work_tasks SET followup='2000-01-01T00:00:00+00:00' WHERE id=?",(r['id'],))
        self.assertTrue(self.state('Sales')['items'][0]['followup_due'])
        self.base.action(q,'sent',note='Sent proposal')
        new=self.task('Sales');self.assertNotEqual(new['id'],r['id'])
        with app.connect() as db:
            self.assertIsNotNone(db.execute('SELECT completed FROM work_tasks WHERE id=?',(r['id'],)).fetchone()[0])
        self.assertEqual(self.state('Sales')['scores'][0]['completed'],1)

    def test_targets_validate_and_do_not_rewrite_existing_deadline(self):
        self.base.quote();before=self.task()
        c=productivity.DEFAULT.copy();c['targets']={t:dict(hours=8,on_time_percent=95) for t in productivity.TEAMS}
        with self.assertRaises(auth.AccessError):self.act('settings',c,'Sales')
        self.act('settings',c)
        self.assertIsNone(self.task()['due'])
        self.base.quote(customer='New client')
        self.assertTrue(any(i['task']['due'] for i in self.state()['items']))
        with self.assertRaises(auth.AccessError):self.act('settings',c)
        bad=dict(c,version=1,weekdays=[])
        with self.assertRaises(ValueError):self.act('settings',bad)
        with self.assertRaises(auth.AccessError):self.act('deadline',self.data(before,due=datetime.now(timezone.utc).isoformat(),note='No authority'),'Sales')


class ProfileTests(unittest.TestCase):
    def setUp(self):self.base=test_sales.SalesTests();self.base.setUp();self.users=self.base.users
    def tearDown(self):self.base.tearDown()
    def act(self,op,data,role='Sales'):
        with app.connect() as db:return profiles.action(db,'/api/profiles/'+op,data,self.users[role])
    def test_profiles_permissions_duplicates_versions_and_archive(self):
        data=dict(kind='Customer',name='Client A',email='client@example.com',address='Billing address')
        pid=self.act('save',data)['id']
        with self.assertRaises(ValueError):self.act('save',dict(data,name=' client   a '))
        with self.assertRaises(auth.AccessError):self.act('save',dict(data,id=pid,version=1),'Operations')
        self.act('save',dict(data,id=pid,version=1,address='New address'))
        with self.assertRaises(auth.AccessError):self.act('save',dict(data,id=pid,version=1))
        self.act('archive',dict(id=pid,version=2,active=False,reason='Old customer'))
        self.act('archive',dict(id=pid,version=3,active=True,reason='Customer returned'))
        with app.connect() as db:
            self.assertEqual(profiles.state(db,self.users['Sales'])['profiles'][0]['active'],1)
            with self.assertRaises(auth.AccessError):profiles.state(db,self.users['Viewer'])
        with self.assertRaises(auth.AccessError):self.act('save',dict(kind='Carrier',name='Carrier'),'Sales')
        self.act('save',dict(kind='Carrier',name='Carrier'),'Operations')
