import hashlib
import copy
import gzip
import http.client
import json
import tempfile
import time
import sqlite3
from contextlib import closing
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from server import Service, Store, handler
from server import CUP_MAX_BODY


class Tests(unittest.TestCase):
    def cup(self,**changes):
        return dict(schema=1,event_id='c'*64,install_id='a'*32,product='this_cup',shot_id='cup-20261112-1',event_type='shot_completed',occurred_at='2026-11-12T08:30:00+08:00',payload={'profile':{'title':'测试曲线','steps':[{'pressure':6}]},'workflow':{'dose':18},'measurements':[{'time':i/10,'pressure':6.1,'temperature':92.1,'flow':2.0} for i in range(255)],'bean':{'name':'测试豆','roast':'浅烘'},'taste':None,'device':{'model':'DE1Pro'},'versions':{'onecup':'0.2.40','system':None,'firmware':'1358'}},**changes)

    def test_cup_append_lossless_compression_and_installation_summary(self):
        event=self.cup()
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        stored=self.store.read_cup_event(event['event_id'])
        self.assertEqual(stored['payload'],event['payload'])
        self.assertEqual(stored['source_ip'],'223.5.5.5')
        self.assertEqual(stored['city'],'杭州')
        with self.store.connect() as db:
            zipped=db.execute('SELECT payload_gzip FROM cup_events').fetchone()[0]
        self.assertIsInstance(zipped,bytes)
        self.assertLess(len(zipped),len(json.dumps(event['payload']).encode()))
        self.assertEqual(json.loads(gzip.decompress(zipped)),event['payload'])
        self.assertEqual(self.store.summary(2,0)['products'][1]['versions'],[{'version':None,'installations':1,'active_30d':1}])

    def test_offline_old_cup_does_not_downgrade_current_installation_version(self):
        self.request(body={'install_id':'a'*32,'product':'this_cup','version':'0.2.40'})
        event=self.cup();event['payload']['versions']['onecup']='0.2.39'
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        self.assertEqual(self.store.summary(2,0)['products'][1]['versions'],[{'version':'0.2.40','installations':1,'active_30d':1}])
        self.assertEqual(self.store.read_cup_event(event['event_id'])['payload']['versions']['onecup'],'0.2.39')

    def test_cup_nested_prefixed_credentials_are_rejected_without_persistence(self):
        for key in ('bridgeToken','apiKey','credential','passwordHash','plugin_secret_v2','pluginAccessKey','authAuthorization','machineSerialNumber','bluetoothMacAddress','selectedDeviceId','ip','ipAddress','local_ip','base-url','hostname','address'):
            event=self.cup();event['payload']['workflow']['context']={'extras':{key:'must-not-store'}}
            status,data,_=self.request(body=event,path='/v1/cup-events')
            self.assertEqual((status,data),(422,{'error':'sensitive_cup_field'}),key)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cup_events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM installations').fetchone()[0],0)

    def test_cup_skip_limit_and_public_source_urls_are_preserved(self):
        event=self.cup();event['payload']['workflow']['context']={'extras':{'skip':True,'limit':5,'ip_flow_limit':4,'sourceUrl':'https://example.org/profile','url':'https://example.org/source'}}
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        self.assertEqual(self.store.read_cup_event(event['event_id'])['payload'],event['payload'])

    def test_cup_idempotence_is_canonical_and_metadata_cannot_rewrite_history(self):
        event=self.cup();first=self.request(body=event,path='/v1/cup-events')
        before=self.store.read_cup_event(event['event_id'])
        duplicate=dict(reversed(list(event.items())))
        status,data,_=self.request(body=duplicate,path='/v1/cup-events',extra={'X-Real-IP':'8.8.8.8'})
        self.assertEqual(status,200);self.assertTrue(data['duplicate'])
        self.assertEqual(self.store.read_cup_event(event['event_id']),before)
        altered=copy.deepcopy(event);altered['payload']['workflow']['dose']=19
        self.assertEqual(self.request(body=altered,path='/v1/cup-events')[0],409)
        self.assertEqual(self.store.read_cup_event(event['event_id']),before)

    def test_cup_assessment_appends_and_installation_delete_preserves_events(self):
        original=self.cup()
        self.request(body=original,path='/v1/cup-events')
        assessment=copy.deepcopy(original);assessment.update(event_id='d'*64,event_type='assessment_saved');assessment['payload']['taste']={'enjoyment':1,'sweetness':7,'flavors':['花香']}
        self.request(body=assessment,path='/v1/cup-events')
        modified=copy.deepcopy(assessment);modified['event_id']='e'*64;modified['payload']['taste']['sweetness']=8
        self.request(body=modified,path='/v1/cup-events')
        self.assertEqual(self.request('DELETE')[0],200)
        self.assertEqual(self.store.summary(2,0)['total_installations'],0)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cup_events').fetchone()[0],3)
        before=self.store.read_cup_event(original['event_id'])
        for statement in ('DELETE FROM cup_events','UPDATE cup_events SET country=\'US\''):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.connect() as db:db.execute(statement)
            self.assertEqual(self.store.read_cup_event(original['event_id']),before)
        self.assertIsNone(self.store.read_cup_event(original['event_id'])['payload']['taste'])
        self.assertEqual(self.store.read_cup_event(assessment['event_id'])['payload']['taste']['sweetness'],7)

    def test_cup_http_size_encoding_and_no_public_raw_endpoint(self):
        event=self.cup()
        self.assertGreater(len(json.dumps(event)),256)
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        self.assertEqual(self.request('GET',path='/v1/cup-events')[0],405)
        self.assertEqual(self.request('DELETE',body=event,path='/v1/cup-events')[0],405)
        self.assertEqual(self.request(body=event,path='/v1/cup-events',extra={'Content-Encoding':'gzip'})[0],415)
        connection=http.client.HTTPConnection(*self.server.server_address)
        connection.request('POST','/v1/cup-events',headers={'Content-Type':'application/json','Content-Length':str(CUP_MAX_BODY+1)})
        response=connection.getresponse();self.assertEqual(response.status,413);response.read();connection.close()
        public=json.dumps(self.store.summary(2,0))
        for value in ('cup_events','cup-20261112-1','测试曲线','测试豆','1358','223.5.5.5','content_sha256'):
            self.assertNotIn(value,public)

    def test_cup_rejects_sensitive_nonfinite_and_invalid_events_without_rows(self):
        for key in ('Authorization','access_key','token','password','serialNumber','deviceId','macAddress'):
            event=self.cup();event['payload']['profile']['steps'][0][key]='must-not-store'
            self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422,key)
        for value in (float('nan'),float('inf'),float('-inf')):
            event=self.cup();event['payload']['result']={'weight':value}
            self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422)
        for key,value in [('schema',True),('product','decent_remote'),('event_type','unknown'),('event_id','z'*64),('occurred_at','not a timestamp')]:
            event=self.cup();event[key]=value
            self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422)
        event=self.cup();event['payload']['extra']={}
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cup_events').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM installations').fetchone()[0],0)

    def test_cup_uses_trusted_ip_and_authenticates_relay(self):
        event=self.cup()
        self.assertEqual(self.request(body=event,path='/v1/cup-events',extra={'X-Usage-IP':'8.8.8.8','X-Forwarded-For':'8.8.8.8'})[0],200)
        self.assertEqual(self.store.read_cup_event(event['event_id'])['source_ip'],'223.5.5.5')
        event['event_id']='d'*64
        self.assertEqual(self.request(body=event,path='/v1/cup-events',extra={'X-Usage-Relay-Key':'wrong'})[0],403)
        self.assertIsNone(self.store.read_cup_event(event['event_id']))

    def test_cup_transaction_rolls_back_if_installation_update_fails(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_install BEFORE INSERT ON installations BEGIN SELECT RAISE(ABORT,'test failure'); END")
        self.assertEqual(self.request(body=self.cup(),path='/v1/cup-events')[0],503)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cup_events').fetchone()[0],0)

    def test_cup_machine_identity_is_optional_validated_and_not_public(self):
        event=self.cup();event['payload']['device'].update(machine_id='f'*64,identity_status='known')
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        stored=self.store.read_cup_event(event['event_id'])
        self.assertEqual(stored['machine_id'],'f'*64)
        with self.store.connect() as db:
            self.assertIn('cup_events_machine_received',[row[1] for row in db.execute('PRAGMA index_list(cup_events)')])
        self.assertNotIn('f'*64,json.dumps(self.store.summary(2,0)))
        for invalid in ('DE1Pro','1.2.3.4','abcdef',1,{},'G'*64):
            event=self.cup();event['event_id']='d'*64;event['payload']['device']['machine_id']=invalid
            self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422)
        event=self.cup();event['event_id']='e'*64;event['payload']['device'].update(machine_id=None,identity_status='unknown')
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],200)
        self.assertIsNone(self.store.read_cup_event(event['event_id'])['machine_id'])

    def test_cup_duplicate_json_keys_and_deep_payload_are_rejected(self):
        raw=json.dumps(self.cup()).replace('"schema": 1','"schema": 1, "schema": 1')
        connection=http.client.HTTPConnection(*self.server.server_address)
        connection.request('POST','/v1/cup-events',raw,headers={'Content-Type':'application/json'})
        response=connection.getresponse();self.assertEqual(response.status,400);response.read();connection.close()
        event=self.cup();value={}
        for _ in range(34):value={'child':value}
        event['payload']['profile']=value
        self.assertEqual(self.request(body=event,path='/v1/cup-events')[0],422)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cup_events').fetchone()[0],0)

    def test_cup_slow_body_can_arrive_after_legacy_eight_second_timeout(self):
        raw=json.dumps(self.cup()).encode()
        connection=http.client.HTTPConnection(*self.server.server_address,timeout=15)
        connection.putrequest('POST','/v1/cup-events')
        connection.putheader('Content-Type','application/json')
        connection.putheader('Content-Length',str(len(raw)))
        connection.endheaders()
        connection.send(raw[:100])
        time.sleep(8.2)
        connection.send(raw[100:])
        response=connection.getresponse();self.assertEqual(response.status,200);response.read();connection.close()

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
