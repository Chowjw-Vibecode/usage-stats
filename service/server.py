"""Installation counts with latest private IP/location; public output is aggregated."""
import hashlib
import hmac
import ipaddress
import json
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
            if path not in ('/v1/summary', '/v2/summary', '/v1/check-in'):
                return self.reply(404, {'error': 'not_found'})
            if self.command == 'OPTIONS':
                return self.reply(204)
            if not service.allowed(path):
                return self.reply(429, {'error': 'rate_limited'})
            if path.endswith('/summary') and self.command == 'GET':
                return self.reply(200, service.store.summary(2 if path.startswith('/v2/') else 1, int(time.time())))
            if path != '/v1/check-in' or self.command not in ('POST', 'DELETE'):
                return self.reply(405, {'error': 'method_not_allowed'})
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self.reply(415, {'error': 'json_required'})
            if self.headers.get('Transfer-Encoding'):
                return self.reply(400, {'error': 'invalid_body'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                return self.reply(400, {'error': 'invalid_body'})
            if length <= 0 or length > 256:
                return self.reply(413 if length > 256 else 400, {'error': 'too_large' if length > 256 else 'invalid_body'})
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.reply(400, {'error': 'invalid_body'})
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                return self.reply(400, {'error': 'invalid_json'})
            if self.command == 'DELETE':
                service.authenticate_relay(self.headers)
                return self.reply(200, service.store.check_in(body, 'ZZ', int(time.time()), delete=True))
            location = service.location(self.headers)
            return self.reply(200, service.store.check_in(body, location['country'], int(time.time()), location=location))

        def do_GET(self):
            try:
                self.process()
            except PermissionError:
                self.reply(403, {'error': 'invalid_relay'})
            except ValueError as exc:
                message = str(exc)
                self.reply(422, {'error': message if message in ('invalid_installation', 'invalid_product', 'invalid_version', 'invalid_country') else 'invalid_request'})
            except Exception:
                self.reply(503, {'error': 'temporarily_unavailable'})

        do_POST = do_DELETE = do_OPTIONS = do_GET
    return Handler


if __name__ == '__main__':
    import maxminddb
    reader = maxminddb.open_database(os.environ['USAGE_GEOIP'])
    service = Service(Store(os.environ['USAGE_DB']), reader.get, os.environ.get('USAGE_RELAY_HASH', ''))
    ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('USAGE_PORT', '18782'))), handler(service)).serve_forever()
