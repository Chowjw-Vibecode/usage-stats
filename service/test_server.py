import hashlib
import http.client
import json
import tempfile
import sqlite3
from contextlib import closing
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from server import Service, Store, handler


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'usage.db')
        self.lookups = []
        def lookup(ip):
            self.lookups.append(ip)
            return {'country': {'iso_code': 'CN'}, 'subdivisions': [{'names': {'zh-CN': '浙江', 'en': 'Zhejiang'}}], 'city': {'names': {'zh-CN': '杭州', 'en': 'Hangzhou'}}}
        self.service = Service(self.store, lookup, hashlib.sha256(b'test-key').hexdigest())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.service))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method='POST', body=None, extra=None, path='/v1/check-in'):
        connection = http.client.HTTPConnection(*self.server.server_address)
        headers = {'Content-Type': 'application/json', 'X-Real-IP': '223.5.5.5', **(extra or {})}
        connection.request(method, path, json.dumps(body or {'install_id': 'a' * 32, 'product': 'this_cup', 'version': '0.2.39'}), headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, json.loads(raw) if raw else None, dict(response.headers)
        connection.close()
        return result

    def test_direct_and_idempotent(self):
        for _ in range(2):
            self.assertEqual(self.request()[0], 200)
        summary = self.store.summary(2, 2_000_000_000)
        self.assertEqual(summary['total_installations'], 1)
        self.assertEqual(summary['countries'][0]['country'], 'CN')
        self.assertEqual(self.lookups, ['223.5.5.5'] * 2)

    def test_country_headers_and_bodies_cannot_spoof(self):
        self.request(extra={'X-Usage-Country': 'US', 'CF-IPCountry': 'US', 'X-Forwarded-For': '8.8.8.8'})
        self.assertEqual(self.store.summary(2, 0)['countries'][0]['country'], 'CN')
        self.assertEqual(self.request(body={'install_id': 'b' * 32, 'country': 'US'})[0], 422)

    def test_relay_country_requires_auth(self):
        self.assertEqual(self.request(extra={'X-Usage-Relay-Key': 'wrong', 'X-Usage-Country': 'SG'})[0], 403)
        self.assertEqual(self.store.summary(2, 0)['total_installations'], 0)
        self.assertEqual(self.request(extra={'X-Usage-Relay-Key': 'test-key', 'X-Usage-Country': 'SG'})[0], 200)
        self.assertEqual(self.store.summary(2, 0)['countries'][0]['country'], 'SG')
        self.assertEqual(self.lookups, [])

    def test_legacy_hash_and_product_isolation(self):
        self.request(body={'install_id': 'a' * 32})
        self.request()
        self.assertEqual(self.store.summary(1, 0)['total_installations'], 1)
        self.assertEqual(self.store.summary(2, 0)['total_installations'], 2)
        with self.store.connect() as db:
            hashes = {r[0] for r in db.execute('SELECT id_hash FROM installations')}
        self.assertIn(hashlib.sha256(('a' * 32).encode()).hexdigest(), hashes)
        self.assertEqual(self.request('DELETE')[0], 200)
        self.assertEqual(self.store.summary(2, 0)['total_installations'], 1)

    def test_versions_and_monotonic_updates(self):
        self.store.check_in({'install_id': 'a' * 32, 'product': 'this_cup', 'version': '0.2.38'}, 'HK', 100)
        self.store.check_in({'install_id': 'a' * 32, 'product': 'this_cup', 'version': '0.2.37'}, 'US', 99)
        item = self.store.summary(2, 100)['products'][1]
        self.assertEqual(item['countries'][0]['country'], 'HK')
        self.assertEqual(item['versions'][0]['version'], '0.2.38')
        for version in ('01.2.3', '1.2.3-01', None, '', True):
            self.assertEqual(self.request(body={'install_id': 'a' * 32, 'version': version})[0], 422)

    def test_http_contract(self):
        self.assertEqual(self.request('OPTIONS')[0], 204)
        self.assertEqual(self.request('GET', path='/v2/summary')[0], 200)
        self.assertEqual(self.request('GET', path='/secrets')[0], 404)
        self.assertEqual(self.request('GET')[0], 405)
        self.assertEqual(self.request(extra={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request(body={'install_id': 'a' * 300})[0], 413)

    def test_unknown_private_ip_and_no_raw_identifier_storage(self):
        self.request(extra={'X-Real-IP': '127.0.0.1'})
        self.assertEqual(self.store.summary(2, 0)['countries'][0]['country'], 'ZZ')
        with self.store.connect() as db:
            columns = [r[1] for r in db.execute('PRAGMA table_info(installations)')]
        self.assertEqual(columns, ['id_hash', 'product', 'country', 'first_seen', 'last_seen', 'version', 'region', 'city', 'last_ip', 'ip_updated_at'])

    def test_old_six_column_migration_is_lossless_and_idempotent(self):
        path = Path(self.temp.name) / 'old.db'
        before = [(str(i)*64, 'this_cup' if i == 2 else 'decent_remote', 'CN', 10+i, 20+i, '0.2.38' if i else None) for i in range(3)]
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE installations (id_hash TEXT PRIMARY KEY, product TEXT, country TEXT, first_seen INTEGER, last_seen INTEGER, version TEXT)')
            db.executemany('INSERT INTO installations VALUES (?,?,?,?,?,?)', before)
            db.commit()
        for _ in range(2):
            migrated = Store(path)
            with migrated.connect() as db:
                self.assertEqual(db.execute('SELECT id_hash,product,country,first_seen,last_seen,version FROM installations ORDER BY id_hash').fetchall(), before)
                self.assertEqual(db.execute('SELECT region,city,last_ip FROM installations').fetchall(), [(None,None,None)]*3)

    def test_location_private_storage_and_public_aggregation(self):
        self.assertEqual(self.request()[0], 200)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT country,region,city,last_ip FROM installations').fetchone(), ('CN','浙江','杭州','223.5.5.5'))
        summary = self.request('GET', path='/v2/summary')[1]
        self.assertEqual(summary['cities'], [{'country':'CN','region':'浙江','city':'杭州','installations':1,'active_30d':1}])
        for secret in ['223.5.5.5','last_ip','id_hash','a'*32]:
            self.assertNotIn(secret, json.dumps(summary))
        legacy = self.store.summary(1, 0)
        self.assertNotIn('regions', legacy)
        self.assertNotIn('cities', legacy)

    def test_authenticated_relay_ip_and_forgery(self):
        self.request(extra={'X-Usage-IP':'8.8.8.8','X-Forwarded-For':'8.8.8.8'})
        self.assertEqual(self.lookups, ['223.5.5.5'])
        self.request(body={'install_id':'b'*32}, extra={'X-Usage-Relay-Key':'test-key','X-Usage-IP':'2001:4860:4860:0000:0000:0000:0000:8888','X-Usage-Country':'US'})
        self.assertEqual(self.lookups[-1], '2001:4860:4860::8888')
        with self.store.connect() as db:
            self.assertIn(('2001:4860:4860::8888',),db.execute('SELECT last_ip FROM installations').fetchall())
        count=len(self.lookups)
        self.assertEqual(self.request(extra={'X-Usage-Relay-Key':'wrong','X-Usage-IP':'8.8.8.8'})[0],403)
        self.assertEqual(len(self.lookups),count)

    def test_geo_failure_retains_ip_and_delete_does_not_lookup(self):
        def fail(ip):
            self.lookups.append(ip)
            raise ValueError('secret '+ip)
        self.service.lookup=fail
        self.assertEqual(self.request()[0],200)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT country,region,city,last_ip FROM installations').fetchone(),('ZZ',None,None,'223.5.5.5'))
        self.assertEqual(self.request('DELETE')[0],200)
        self.assertEqual(self.lookups,['223.5.5.5'])
        self.assertEqual(self.store.summary(2,0)['total_installations'],0)

    def test_location_ordering_tuple_groups_unknowns_and_reconciliation(self):
        body={'install_id':'a'*32,'product':'this_cup','version':'0.2.38'}
        self.store.check_in(body,'CN',100,location={'region':'A','city':'Same','last_ip':'223.5.5.5'})
        self.store.check_in(body,'US',99,location={'region':'B','city':'Old','last_ip':'8.8.8.8'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT country,region,city,last_ip FROM installations').fetchone(),('CN','A','Same','223.5.5.5'))
        for identifier,country,region in [('b','CN','B'),('c','US','A')]:
            self.store.check_in({'install_id':identifier*32},country,100,location={'region':region,'city':'Same'})
        self.store.check_in({'install_id':'d'*32},'ZZ',100)
        result=self.store.summary(2,100)
        self.assertEqual(len(result['cities']),4)
        for item in [result,*result['products']]:
            for field in ['countries','regions','cities']:
                self.assertEqual(sum(r['installations'] for r in item[field]),item['total_installations'])
                self.assertEqual(sum(r['active_30d'] for r in item[field]),item['active_30d'])
        self.store.check_in(body,'SG',101)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT country,region,city,last_ip FROM installations WHERE product='this_cup'").fetchone(),('CN','A','Same','223.5.5.5'))

    def test_malformed_geodata_is_not_published_and_private_ip_not_stored(self):
        self.service.lookup=lambda ip:{'country':{'iso_code':'CN'},'subdivisions':[{'names':{'zh-CN':'bad\nname'}}],'city':{'names':{'en':'x'*129}}}
        self.request()
        self.assertEqual(self.store.summary(2,0)['cities'][0]['region'],None)
        self.assertEqual(self.store.summary(2,0)['cities'][0]['city'],None)
        self.request(body={'install_id':'b'*32},extra={'X-Real-IP':'10.0.0.1','X-Usage-IP':'8.8.8.8'})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT last_ip FROM installations WHERE product='decent_remote'").fetchone(),(None,))
        self.assertEqual(self.request(body={'install_id':'c'*32,'last_ip':'8.8.8.8'})[0],422)

    def test_english_names_unknown_relay_and_safe_errors(self):
        self.service.lookup=lambda ip:{'country':{'iso_code':'US'},'subdivisions':[{'names':{'en':'California'}}],'city':{'names':{'en':'Springfield'}}}
        self.request()
        self.assertEqual(self.store.summary(2,0)['cities'][0]['city'],'Springfield')
        self.assertEqual(self.request(body={'install_id':'b'*32},extra={'X-Usage-Relay-Key':'test-key','X-Usage-IP':'127.0.0.1'})[0],200)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT country,region,city,last_ip FROM installations WHERE product='decent_remote'").fetchone(),('ZZ',None,None,None))
        def fail(headers): raise ValueError('8.8.8.8 must not leak')
        self.service.location=fail
        status,data,_=self.request()
        self.assertEqual(status,422)
        self.assertEqual(data,{'error':'invalid_request'})

    def test_versions_same_second_downgrade_and_missing_preserve_location(self):
        body={'install_id':'a'*32,'version':'0.2.38'}
        self.store.check_in(body,'CN',100,location={'region':'A','city':'City','last_ip':'223.5.5.5'})
        self.store.check_in({**body,'version':'0.2.37'},'US',100,location={'region':'B','last_ip':'8.8.8.8'})
        self.store.check_in({'install_id':'a'*32},'CN',101,location={'region':'C'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT version,country,region,city,last_ip,first_seen,last_seen FROM installations').fetchone(),('0.2.37','US','B',None,'8.8.8.8',100,101))
        self.store.check_in({'install_id':'b'*32},'US',3_000_000)
        for item in [self.store.summary(2,3_000_000),*self.store.summary(2,3_000_000)['products']]:
            for key in ['countries','regions','cities']:
                self.assertEqual(sum(row['active_30d'] for row in item[key]),item['active_30d'])
        self.assertEqual(self.store.summary(2,3_000_000)['active_30d'],1)

    def test_shared_rate_limit_has_no_client_id(self):
        self.service.limits.clear()
        for _ in range(100):
            self.assertTrue(self.service.allowed('/v1/check-in'))
        self.assertFalse(self.service.allowed('/v1/check-in'))
        self.assertTrue(self.service.allowed('/v2/summary'))
        self.assertEqual(set(self.service.limits), {'/v1/check-in', '/v2/summary'})

    def test_latest_valid_ip_same_second_and_country_only_relay_retention(self):
        body={'install_id':'a'*32,'version':'0.2.39'}
        self.store.check_in(body,'CN',100,location={'region':'浙江','city':'杭州','last_ip':'223.5.5.5'})
        self.store.check_in(body,'US',100,location={'region':'California','city':'Mountain View','last_ip':'8.8.8.8'})
        self.store.check_in(body,'SG',101)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT country,region,city,last_ip,ip_updated_at,last_seen FROM installations').fetchone(),('US','California','Mountain View','8.8.8.8',100,101))
        self.store.check_in(body,'CN',99,location={'last_ip':'114.114.114.114'})
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT last_ip,ip_updated_at FROM installations').fetchone(),('8.8.8.8',100))
        public=json.dumps(self.store.summary(2,102))
        self.assertNotIn('ip_updated_at',public)


if __name__ == '__main__':
    unittest.main()
