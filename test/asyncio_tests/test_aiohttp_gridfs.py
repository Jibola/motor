# Copyright 2016 MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test Motor's AIOHTTPGridFSHandler."""

import asyncio
import datetime
import email
import hashlib
import logging
import time

import aiohttp
import aiohttp.web
import gridfs

from motor.aiohttp import AIOHTTPGridFS
from motor.motor_asyncio import AsyncIOMotorClient

import test
from test.asyncio_tests import AsyncIOTestCase, asyncio_test


def format_date(d):
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", d.utctimetuple())


def parse_date(d):
    date_tuple = email.utils.parsedate(d)
    return datetime.datetime.fromtimestamp(time.mktime(date_tuple))


def expires(response):
    return parse_date(response.headers['Expires'])


class AIOHTTPGridFSHandlerTestBase(AsyncIOTestCase):
    fs = None
    file_id = None

    def tearDown(self):
        self.loop.run_until_complete(self.stop())
        super().tearDown()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        logging.getLogger('aiohttp.web').setLevel(logging.CRITICAL)

        cls.fs = gridfs.GridFS(test.env.sync_cx.motor_test)

        # Make a 500k file in GridFS with filename 'foo'
        cls.contents = b'Jesse' * 100 * 1024
        cls.contents_hash = hashlib.md5(cls.contents).hexdigest()

        # Record when we created the file, to check the Last-Modified header
        cls.put_start = datetime.datetime.utcnow().replace(microsecond=0)
        cls.file_id = 'id'
        cls.fs.delete(cls.file_id)
        cls.fs.put(cls.contents,
                   _id='id',
                   filename='foo',
                   content_type='my type')

        cls.put_end = datetime.datetime.utcnow().replace(microsecond=0)
        cls.app = cls.srv = cls.app_handler = None

    @classmethod
    def tearDownClass(cls):
        cls.fs.delete(cls.file_id)
        super().tearDownClass()

    @asyncio.coroutine
    def start_app(self, http_gridfs=None, extra_routes=None):
        self.app = aiohttp.web.Application()
        resource = self.app.router.add_resource('/fs/{filename}')
        handler = http_gridfs or AIOHTTPGridFS(self.db)
        resource.add_route('GET', handler)
        resource.add_route('HEAD', handler)

        if extra_routes:
            for route, handler in extra_routes.items():
                resource = self.app.router.add_resource(route)
                resource.add_route('GET', handler)

        self.app_handler = self.app.make_handler()
        server = self.loop.create_server(self.app_handler,
                                         host='localhost',
                                         port=8088)

        self.srv, _ = yield from asyncio.gather(server,
                                                self.app.startup(),
                                                loop=self.loop)

    @asyncio.coroutine
    def request(self, method, path, if_modified_since=None, headers=None):
        headers = headers or {}
        if if_modified_since:
            headers['If-Modified-Since'] = format_date(if_modified_since)

        session = aiohttp.ClientSession()

        try:
            method = getattr(session, method)

            resp = yield from method('http://localhost:8088%s' % path,
                                     headers=headers)
            yield from resp.read()
            return resp
        finally:
            yield from session.close()

    def get(self, path, **kwargs):
        return self.request('get', path, **kwargs)

    def head(self, path, **kwargs):
        return self.request('head', path, **kwargs)

    @asyncio.coroutine
    def stop(self):
        # aiohttp.rtfd.io/en/stable/web.html#aiohttp-web-graceful-shutdown
        self.srv.close()
        yield from self.srv.wait_closed()
        yield from self.app.shutdown()
        yield from self.app_handler.finish_connections(timeout=1)
        yield from self.app.cleanup()


class AIOHTTPGridFSHandlerTest(AIOHTTPGridFSHandlerTestBase):
    @asyncio_test
    def test_basic(self):
        yield from self.start_app()
        # First request
        response = yield from self.get('/fs/foo')

        self.assertEqual(200, response.status)
        self.assertEqual(self.contents, (yield from response.read()))
        self.assertEqual(
            len(self.contents), int(response.headers['Content-Length']))
        self.assertEqual('my type', response.headers['Content-Type'])
        self.assertEqual('public', response.headers['Cache-Control'])
        self.assertTrue('Expires' not in response.headers)

        etag = response.headers['Etag']
        last_mod_dt = parse_date(response.headers['Last-Modified'])
        self.assertEqual(self.contents_hash, etag.strip('"'))
        self.assertTrue(self.put_start <= last_mod_dt <= self.put_end)

        # Now check we get 304 NOT MODIFIED responses as appropriate
        for ims_value in (
            last_mod_dt,
            last_mod_dt + datetime.timedelta(seconds=1)
        ):
            response = yield from self.get('/fs/foo',
                                           if_modified_since=ims_value)
            self.assertEqual(304, response.status)
            self.assertEqual(b'', (yield from response.read()))

        # If-Modified-Since in the past, get whole response back
        response = yield from self.get(
            '/fs/foo',
            if_modified_since=last_mod_dt - datetime.timedelta(seconds=1))
        self.assertEqual(200, response.status)
        self.assertEqual(self.contents, (yield from response.read()))

        # Matching Etag
        response = yield from self.get('/fs/foo',
                                       headers={'If-None-Match': etag})
        self.assertEqual(304, response.status)
        self.assertEqual(b'', (yield from response.read()))

        # Mismatched Etag
        response = yield from self.get('/fs/foo',
                                       headers={'If-None-Match': etag + 'a'})
        self.assertEqual(200, response.status)
        self.assertEqual(self.contents, (yield from response.read()))

    @asyncio_test
    def test_404(self):
        yield from self.start_app()
        response = yield from self.get('/fs/bar')
        self.assertEqual(404, response.status)

    @asyncio_test
    def test_head(self):
        yield from self.start_app()
        response = yield from self.head('/fs/foo')

        etag = response.headers['Etag']
        last_mod_dt = parse_date(response.headers['Last-Modified'])

        self.assertEqual(200, response.status)
        # Empty body for HEAD request.
        self.assertEqual(b'', (yield from response.read()))
        self.assertEqual(
            len(self.contents), int(response.headers['Content-Length']))
        self.assertEqual('my type', response.headers['Content-Type'])
        self.assertEqual(self.contents_hash, etag.strip('"'))
        self.assertTrue(self.put_start <= last_mod_dt <= self.put_end)
        self.assertEqual('public', response.headers['Cache-Control'])

    @asyncio_test
    def test_bad_route(self):
        handler = AIOHTTPGridFS(self.db)
        yield from self.start_app(extra_routes={'/x/{wrongname}': handler})
        response = yield from self.get('/x/foo')
        self.assertEqual(500, response.status)
        msg = 'Bad AIOHTTPGridFS route "/x/{wrongname}"'
        self.assertIn(msg, (yield from response.text()))

    @asyncio_test
    def test_content_type(self):
        yield from self.start_app()
        # Check that GridFSHandler uses file extension to guess Content-Type
        # if not provided
        for filename, expected_type in [
            ('bar', 'octet-stream'),
            ('bar.png', 'png'),
            ('ht.html', 'html'),
            ('jscr.js', 'javascript'),
        ]:
            # 'fs' is PyMongo's blocking GridFS
            _id = self.fs.put(b'', filename=filename)
            self.addCleanup(self.fs.delete, _id)

            for method in self.get, self.head:
                response = yield from method('/fs/' + filename)
                self.assertEqual(200, response.status)
                # mimetypes are platform-defined, be fuzzy
                self.assertIn(
                    expected_type,
                    response.headers['Content-Type'].lower())

    @asyncio_test
    def test_post(self):
        # Only allow GET and HEAD, even if a POST route is added.
        handler = AIOHTTPGridFS(self.db)
        yield from self.start_app(extra_routes={'/fs/{filename}': handler})
        result = yield from self.request('post', '/fs/foo')
        self.assertEqual(405, result.status)


class AIOHTTPTZAwareGridFSHandlerTest(AIOHTTPGridFSHandlerTestBase):
    @asyncio_test
    def test_tz_aware(self):
        client = AsyncIOMotorClient(
            test.env.uri,
            tz_aware=True,
            io_loop=self.loop)

        yield from self.start_app(AIOHTTPGridFS(client.motor_test))
        now = datetime.datetime.utcnow()
        ago = now - datetime.timedelta(minutes=10)
        hence = now + datetime.timedelta(minutes=10)

        response = yield from self.get('/fs/foo', if_modified_since=ago)
        self.assertEqual(200, response.status)

        response = yield from self.get('/fs/foo', if_modified_since=hence)
        self.assertEqual(304, response.status)


class AIOHTTPCustomHTTPGridFSTest(AIOHTTPGridFSHandlerTestBase):
    @asyncio_test
    def test_get_gridfs_file(self):
        def getter(bucket, filename, request):
            # Test overriding the get_gridfs_file() method, path is
            # interpreted as file_id instead of filename.
            return bucket.open_download_stream(file_id=filename)

        def cache_time(path, modified, mime_type):
            return 10

        def extras(response, gridout):
            response.headers['quux'] = 'fizzledy'

        yield from self.start_app(AIOHTTPGridFS(self.db,
                                                get_gridfs_file=getter,
                                                get_cache_time=cache_time,
                                                set_extra_headers=extras))

        # We overrode get_gridfs_file so we expect getting by filename *not* to
        # work now; we'll get a 404. We have to get by file_id now.
        response = yield from self.get('/fs/foo')
        self.assertEqual(404, response.status)

        response = yield from self.get('/fs/' + str(self.file_id))
        self.assertEqual(200, response.status)

        self.assertEqual(self.contents, (yield from response.read()))
        cache_control = response.headers['Cache-Control']
        self.assertRegex(cache_control, r'max-age=\d+')
        self.assertEqual(10, int(cache_control.split('=')[1]))
        expiration = parse_date(response.headers['Expires'])
        now = datetime.datetime.utcnow()

        # It should expire about 10 seconds from now
        self.assertTrue(
            datetime.timedelta(seconds=8)
            < expiration - now
            < datetime.timedelta(seconds=12))

        self.assertEqual('fizzledy', response.headers['quux'])
