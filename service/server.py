"""Installation counts with latest private IP/location; public output is aggregated."""
import hashlib
import datetime
import gzip
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PRODUCTS = ('decent_remote', 'this_cup')
CUP_MAX_BODY = 4 * 1024 * 1024
CUP_PAYLOAD_FIELDS = {'profile','workflow','measurements','result','bean','taste','device','versions'}
SENSITIVE_KEY_PARTS = ('token','password','secret','accesskey','apikey','authorization','credential','serialnumber','macaddress','deviceid')
PRIVATE_ADDRESS_KEYS = {'ip','ipaddress','localip','baseurl','hostname','address'}
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?')
UPSERT = """INSERT INTO installations
(id_hash,product,country,first_seen,last_seen,version,region,city,last_ip,ip_updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id_hash) DO UPDATE SET
country=CASE WHEN excluded.last_seen>=last_seen AND (excluded.last_ip IS NOT NULL OR last_ip IS NULL) THEN excluded.country ELSE country END,
region=CASE WHEN excluded.last_seen>=last_seen AND (excluded.last_ip IS NOT NULL OR last_ip IS NULL) THEN excluded.region ELSE region END,
city=CASE WHEN excluded.last_seen>=last_seen AND (excluded.last_ip IS NOT NULL OR last_ip IS NULL) THEN excluded.city ELSE city END,
last_ip=CASE WHEN excluded.last_seen>=last_seen AND excluded.last_ip IS NOT NULL THEN excluded.last_ip ELSE last_ip END,
ip_updated_at=CASE WHEN excluded.last_seen>=last_seen AND excluded.last_ip IS NOT NULL THEN excluded.ip_updated_at ELSE ip_updated_at END,
version=CASE WHEN excluded.last_seen>=last_seen AND excluded.version IS NOT NULL THEN excluded.version ELSE version END,
first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen)"""


def country_code(value):
    return value if isinstance(value, str) and re.fullmatch('[A-Z]{2}', value) and value not in ('EU', 'XX') else 'ZZ'


def place_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(unicodedata.category(c).startswith('C') for c in value):
        return None
    return value.strip()


def public_ip(value):
    try:
        if not isinstance(value, str) or '%' in value:
            return None
        address = ipaddress.ip_address(value)
        return str(address) if address.is_global else None
    except ValueError:
        return None


def localized_name(value):
    names = value.get('names') if isinstance(value, dict) else None
    if not isinstance(names, dict):
        return None
    return place_name(names.get('zh-CN') or names.get('en'))


class EventConflict(ValueError):
    pass


def unique_object(pairs):
    value = {}
    for key,child in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = child
    return value


def validate_cup_event(body):
    fields = {'schema','event_id','install_id','product','shot_id','event_type','occurred_at','payload'}
    if not isinstance(body,dict) or set(body) != fields or type(body['schema']) is not int or body['schema'] != 1:
        raise ValueError('invalid_cup_event')
    if not isinstance(body['event_id'],str) or not re.fullmatch('[a-f0-9]{64}',body['event_id']):
        raise ValueError('invalid_cup_event')
    if not isinstance(body['install_id'],str) or not re.fullmatch('[a-f0-9]{32}',body['install_id']) or body['product'] != 'this_cup':
        raise ValueError('invalid_cup_event')
    shot = body['shot_id']
    if not isinstance(shot,str) or not shot.strip() or len(shot)>512 or any(unicodedata.category(c).startswith('C') for c in shot):
        raise ValueError('invalid_cup_event')
    if body['event_type'] not in ('shot_completed','assessment_saved'):
        raise ValueError('invalid_cup_event')
    at = body['occurred_at']
    if not isinstance(at,str) or len(at)>64:
        raise ValueError('invalid_cup_event')
    try:
        # Decaid can store host-local ISO timestamps with no timezone. Preserve
        # that original representation rather than silently interpreting it.
        if 'T' not in at:
            raise ValueError()
        datetime.datetime.fromisoformat(at.replace('Z','+00:00'))
    except ValueError:
        raise ValueError('invalid_cup_event') from None
    payload = body['payload']
    if not isinstance(payload,dict) or not payload or set(payload)-CUP_PAYLOAD_FIELDS:
        raise ValueError('invalid_cup_event')
    pending = [(payload,0)]
    while pending:
        value,depth = pending.pop()
        if depth>32:
            raise ValueError('invalid_cup_event')
        if isinstance(value,dict):
            for key,child in value.items():
                if not isinstance(key,str) or not key or len(key)>256 or any(ord(c)<32 for c in key):
                    raise ValueError('invalid_cup_event')
                normalized_key = re.sub(r'[^a-z0-9]','',key.lower())
                if normalized_key in PRIVATE_ADDRESS_KEYS or any(part in normalized_key for part in SENSITIVE_KEY_PARTS):
                    raise ValueError('sensitive_cup_field')
                pending.append((child,depth+1))
        elif isinstance(value,list):
            pending.extend((child,depth+1) for child in value)
        elif isinstance(value,float):
            if not math.isfinite(value):
                raise ValueError('invalid_cup_event')
        elif isinstance(value,str):
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise ValueError('invalid_cup_event') from None
        elif value is not None and type(value) not in (str,int,bool):
            raise ValueError('invalid_cup_event')
    versions = payload.get('versions')
    if versions is not None and not isinstance(versions,dict):
        raise ValueError('invalid_cup_event')
    version = versions.get('onecup') if isinstance(versions,dict) else None
    if version is not None and (not isinstance(version,str) or len(version)>64 or not VERSION.fullmatch(version)):
        raise ValueError('invalid_version')
    device = payload.get('device')
    if device is not None and not isinstance(device,dict):
        raise ValueError('invalid_cup_event')
    machine_id = device.get('machine_id') if isinstance(device,dict) else None
    if machine_id is not None and (not isinstance(machine_id,str) or not re.fullmatch('[a-f0-9]{64}',machine_id)):
        raise ValueError('invalid_cup_event')
    return version



class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('BEGIN IMMEDIATE;\n' + Path(__file__).with_name('schema.sql').read_text())
            columns = {row[1] for row in db.execute('PRAGMA table_info(installations)')}
            for column in ('region', 'city', 'last_ip'):
                if column not in columns:
                    db.execute('ALTER TABLE installations ADD COLUMN ' + column + ' TEXT')
            if 'ip_updated_at' not in columns:
                db.execute('ALTER TABLE installations ADD COLUMN ip_updated_at INTEGER')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def check_in(self, body, country, now, delete=False, location=None):
        if not isinstance(body, dict) or set(body) - {'install_id', 'product', 'version'}:
            raise ValueError('invalid_installation')
        identifier = body.get('install_id')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('invalid_installation')
        product = body.get('product', 'decent_remote')
        if product not in PRODUCTS:
            raise ValueError('invalid_product')
        version = body.get('version')
        if 'version' in body and (not isinstance(version, str) or len(version) > 64 or not VERSION.fullmatch(version)):
            raise ValueError('invalid_version')
        identity = identifier if product == 'decent_remote' else product + ':' + identifier
        digest = hashlib.sha256(identity.encode()).hexdigest()
        with self.connect() as db:
            if delete:
                db.execute('DELETE FROM installations WHERE id_hash=? AND product=?', (digest, product))
            else:
                loc = location if isinstance(location, dict) else {}
                address = public_ip(loc.get('last_ip'))
                db.execute(UPSERT, (digest, product, country_code(country), now, now, version,
                                    place_name(loc.get('region')), place_name(loc.get('city')), address, now if address else None))
        return {'deleted': True} if delete else {'accepted': True, 'next_check_in_after_s': 604800}

    def summary(self, schema, now):
        with self.connect() as db:
            rows = db.execute('SELECT product,country,version,region,city,COUNT(*),SUM(last_seen>=?) FROM installations GROUP BY product,country,version,region,city', (now - 30 * 86400,)).fetchall()

        def group(selected, columns, names):
            grouped = {}
            for row in selected:
                key = tuple(row[column] for column in columns)
                count, active = grouped.get(key, (0, 0))
                grouped[key] = (count + row[5], active + row[6])
            ordered = sorted(grouped.items(), key=lambda item: (-item[1][0], tuple((v is None, v or '') for v in item[0])))
            return [{**dict(zip(names, key)), 'installations': counts[0], 'active_30d': counts[1]} for key, counts in ordered]

        def totals(selected):
            result = {'total_installations': sum(row[5] for row in selected), 'active_30d': sum(row[6] for row in selected), 'countries': group(selected, (1,), ('country',))}
            if schema == 2:
                result['regions'] = group(selected, (1, 3), ('country', 'region'))
                result['cities'] = group(selected, (1, 3, 4), ('country', 'region', 'city'))
            return result

        selected = rows if schema == 2 else [r for r in rows if r[0] == 'decent_remote']
        result = {'schema': schema, 'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'scope': 'reporting installations; not people or all users', **totals(selected)}
        if schema == 2:
            result['products'] = [{'product': p, **totals([r for r in rows if r[0] == p]), 'versions': group([r for r in rows if r[0] == p], (2,), ('version',))} for p in PRODUCTS]
        return result

    def cup_event(self,body,location,now):
        validate_cup_event(body)
        digest = hashlib.sha256(('this_cup:'+body['install_id']).encode()).hexdigest()
        machine_id = (body['payload'].get('device') or {}).get('machine_id')
        canonical = json.dumps(body,ensure_ascii=True,separators=(',',':'),sort_keys=True,allow_nan=False).encode()
        content_hash = hashlib.sha256(canonical).hexdigest()
        payload = gzip.compress(json.dumps(body['payload'],ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False).encode(),mtime=0)
        loc = location if isinstance(location,dict) else {}
        address = public_ip(loc.get('last_ip'))
        country = country_code(loc.get('country'))
        with self.connect() as db:
            # Serialize duplicate detection with insertion. Concurrent retries
            # cannot either duplicate an event or partially update installation.
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT content_sha256 FROM cup_events WHERE event_id=?',(body['event_id'],)).fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise EventConflict('event_conflict')
                return {'accepted':True,'event_id':body['event_id'],'duplicate':True}
            db.execute('INSERT INTO cup_events (event_id,installation_hash,machine_id,shot_id,type,occurred_at,received_at,content_sha256,payload_gzip,source_ip,country,region,city) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (body['event_id'],digest,machine_id,body['shot_id'],body['event_type'],body['occurred_at'],now,content_hash,payload,address,country,place_name(loc.get('region')),place_name(loc.get('city'))))
            # The cup's version describes when it was made, not necessarily
            # the client currently uploading a previously offline queue.
            db.execute(UPSERT,(digest,'this_cup',country,now,now,None,place_name(loc.get('region')),place_name(loc.get('city')),address,now if address else None))
        return {'accepted':True,'event_id':body['event_id'],'duplicate':False}

    def read_cup_event(self,event_id):
        """Private analysis helper; never exposed by the HTTP handler."""
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM cup_events WHERE event_id=?',(event_id,)).fetchone()
        if row is None:
            return None
        event = dict(row)
        event['payload'] = json.loads(gzip.decompress(event.pop('payload_gzip')))
        return event


class Service:
    def __init__(self, store, lookup, relay_hash=''):
        self.store, self.lookup, self.relay_hash = store, lookup, relay_hash
        self.limits, self.lock = {}, threading.Lock()

    def authenticate_relay(self, headers):
        relay = headers.get('X-Usage-Relay-Key')
        if relay is None:
            return False
        digest = hashlib.sha256(relay.encode()).hexdigest()
        if not self.relay_hash or not hmac.compare_digest(digest, self.relay_hash):
            raise PermissionError('invalid_relay')
        return True

    def location(self, headers):
        relay = self.authenticate_relay(headers)
        address = public_ip(headers.get('X-Usage-IP') if relay else headers.get('X-Real-IP'))
        result = {'country': 'ZZ', 'region': None, 'city': None, 'last_ip': address}
        if address is None:
            # Older authenticated relays supplied country only. Never locate the relay server.
            if relay:
                value = headers.get('X-Usage-Country', 'ZZ')
                if country_code(value) == 'ZZ' and value != 'ZZ':
                    raise ValueError('invalid_country')
                result['country'] = value
            return result
        try:
            data = self.lookup(address) or {}
            country = data.get('country')
            subdivisions = data.get('subdivisions')
            result['country'] = country_code(country.get('iso_code') if isinstance(country, dict) else None)
            result['region'] = localized_name(subdivisions[0]) if isinstance(subdivisions, list) and subdivisions else None
            result['city'] = localized_name(data.get('city'))
        except Exception:
            # GeoIP failures must neither discard a valid IP nor leak it in errors/logs.
            result.update(country='ZZ', region=None, city=None)
        return result

    def country(self, headers):
        return self.location(headers)['country']

    def allowed(self, route):
        with self.lock:
            queue = self.limits.setdefault(route, deque())
            current = time.monotonic()
            while queue and queue[0] <= current - 60:
                queue.popleft()
            if len(queue) >= 100:
                return False
            queue.append(current)
            return True


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # no IP, URL, headers or installation ID in access logs

        def setup(self):
            super().setup()
            self.connection.settimeout(8)

        def reply(self, status, value=None):
            body = json.dumps(value, separators=(',', ':')).encode() if value is not None else b''
            self.send_response(status)
            for key, val in {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS', 'Content-Length': str(len(body))}.items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(body)

        def process(self):
            path = self.path.split('?', 1)[0]
            if path not in ('/v1/summary', '/v2/summary', '/v1/check-in','/v1/cup-events'):
                return self.reply(404, {'error': 'not_found'})
            if self.command == 'OPTIONS':
                return self.reply(204)
            if not service.allowed(path):
                return self.reply(429, {'error': 'rate_limited'})
            if path.endswith('/summary') and self.command == 'GET':
                return self.reply(200, service.store.summary(2 if path.startswith('/v2/') else 1, int(time.time())))
            cup = path == '/v1/cup-events'
            if not (cup and self.command=='POST' or path=='/v1/check-in' and self.command in ('POST','DELETE')):
                return self.reply(405, {'error': 'method_not_allowed'})
            if cup:
                self.connection.settimeout(30)
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self.reply(415, {'error': 'json_required'})
            if self.headers.get('Transfer-Encoding'):
                return self.reply(400, {'error': 'invalid_body'})
            if cup and self.headers.get('Content-Encoding','identity').strip().lower() != 'identity':
                return self.reply(415, {'error':'encoding_not_supported'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.reply(400, {'error': 'invalid_body'})
            maximum = CUP_MAX_BODY if cup else 256
            if length <= 0 or length > maximum:
                return self.reply(413 if length > maximum else 400, {'error': 'too_large' if length > maximum else 'invalid_body'})
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400, {'error': 'invalid_body'})
            try:
                body = json.loads(raw,object_pairs_hook=unique_object if cup else None)
            except (ValueError, UnicodeError, RecursionError):
                return self.reply(400, {'error': 'invalid_json'})
            if self.command == 'DELETE':
                service.authenticate_relay(self.headers)
                return self.reply(200, service.store.check_in(body, 'ZZ', int(time.time()), delete=True))
            location = service.location(self.headers)
            if cup:
                return self.reply(200,service.store.cup_event(body,location,int(time.time())))
            return self.reply(200, service.store.check_in(body, location['country'], int(time.time()), location=location))

        def do_GET(self):
            try:
                self.process()
            except PermissionError:
                self.reply(403, {'error': 'invalid_relay'})
            except EventConflict:
                self.reply(409, {'error':'event_conflict'})
            except ValueError as exc:
                message = str(exc)
                self.reply(422, {'error': message if message in ('invalid_installation', 'invalid_product', 'invalid_version', 'invalid_country','invalid_cup_event','sensitive_cup_field') else 'invalid_request'})
            except Exception:
                self.reply(503, {'error': 'temporarily_unavailable'})

        do_POST = do_DELETE = do_OPTIONS = do_GET
    return Handler


if __name__ == '__main__':
    import maxminddb
    reader = maxminddb.open_database(os.environ['USAGE_GEOIP'])
    service = Service(Store(os.environ['USAGE_DB']), reader.get, os.environ.get('USAGE_RELAY_HASH', ''))
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('USAGE_PORT', '18782'))), handler(service)).serve_forever()
