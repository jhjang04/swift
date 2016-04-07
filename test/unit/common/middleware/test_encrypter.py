# Copyright (c) 2015 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import base64
import json
import urllib

import unittest
import mock

from swift.common.middleware import encrypter
from swift.common.swob import Request, HTTPException, HTTPCreated, HTTPAccepted
from swift.common.utils import FileLikeIter
from swift.common.middleware.crypto import Crypto

from test.unit.common.middleware.crypto_helpers import fetch_crypto_keys, \
    md5hex, fake_iv, encrypt
from test.unit.common.middleware.helpers import FakeSwift
from test.unit.common.middleware.test_proxy_logging import FakeAppThatExcepts


@mock.patch('swift.common.middleware.crypto.Crypto.create_iv',
            lambda *args: fake_iv())
class TestEncrypter(unittest.TestCase):

    def test_basic_put_req(self):
        key = fetch_crypto_keys()['object']
        plaintext = 'FAKE APP'
        plaintext_etag = md5hex(plaintext)
        ciphertext = encrypt(plaintext, key, fake_iv())
        ciphertext_etag = md5hex(ciphertext)

        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(plaintext)),
                'x-object-meta-test': 'encrypt me',
                'x-object-sysmeta-test': 'do not encrypt me'}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=plaintext, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)
        self.assertEqual(plaintext_etag, resp.headers['Etag'])

        # verify metadata items
        self.assertEqual(1, len(app.calls), app.calls)
        self.assertEqual('PUT', app.calls[0][0])
        req_hdrs = app.headers[0]

        self.assertEqual(ciphertext_etag, req_hdrs['Etag'])

        # encrypted version of plaintext etag
        actual = base64.b64decode(req_hdrs['X-Object-Sysmeta-Crypto-Etag'])
        self.assertEqual(encrypt(plaintext_etag, key, fake_iv()), actual)
        actual = json.loads(urllib.unquote_plus(
            req_hdrs['X-Object-Sysmeta-Crypto-Meta-Etag']))
        self.assertEqual(Crypto({}).get_cipher(), actual['cipher'])
        self.assertEqual(fake_iv(), base64.b64decode(actual['iv']))

        # encrypted version of plaintext etag for container update
        actual = req_hdrs['X-Backend-Container-Update-Override-Etag']
        self.assertEqual(plaintext_etag, actual)

        # content-type is not encrypted
        self.assertEqual('text/plain', req_hdrs['Content-Type'])

        # user meta is encrypted
        self.assertEqual(base64.b64encode(encrypt('encrypt me', key,
                                                  fake_iv())),
                         req_hdrs['X-Object-Meta-Test'])
        actual = req_hdrs['X-Object-Transient-Sysmeta-Crypto-Meta-Test']
        actual = json.loads(urllib.unquote_plus(actual))
        self.assertEqual(Crypto({}).get_cipher(), actual['cipher'])
        self.assertEqual(fake_iv(), base64.b64decode(actual['iv']))

        # sysmeta is not encrypted
        self.assertEqual('do not encrypt me',
                         req_hdrs['X-Object-Sysmeta-Test'])

        # verify object is encrypted by getting direct from the app
        get_req = Request.blank('/v1/a/c/o', environ={'REQUEST_METHOD': 'GET'})
        resp = get_req.get_response(app)
        self.assertEqual(ciphertext, resp.body)
        self.assertEqual(ciphertext_etag, resp.headers['Etag'])

    def test_basic_put_req_with_other_footers(self):
        # verify handling of another middleware's footer callback
        key = fetch_crypto_keys()['object']
        plaintext = 'FAKE APP'
        plaintext_etag = md5hex(plaintext)
        ciphertext = encrypt(plaintext, key, fake_iv())
        ciphertext_etag = md5hex(ciphertext)
        other_footers = {
            'Etag': 'other etag',
            'X-Object-Sysmeta-Other': 'other sysmeta',
            'X-Backend-Container-Update-Override-Etag': 'other override'}

        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys,
               'swift.callback.update_footers':
                   lambda footers: footers.update(other_footers)}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(plaintext))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=plaintext, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)
        self.assertEqual(plaintext_etag, resp.headers['Etag'])

        # verify metadata items
        self.assertEqual(1, len(app.calls), app.calls)
        self.assertEqual('PUT', app.calls[0][0])
        req_hdrs = app.headers[0]

        # verify that other middleware's footers made it to app, including any
        # container update overrides but not Etag
        other_footers.pop('Etag')
        for k, v in other_footers.items():
            self.assertEqual(v, req_hdrs[k])

        # verify encryption footers are ok
        self.assertEqual(ciphertext_etag, req_hdrs['Etag'])
        actual = base64.b64decode(req_hdrs['X-Object-Sysmeta-Crypto-Etag'])
        self.assertEqual(encrypt(plaintext_etag, key, fake_iv()), actual)
        actual = json.loads(urllib.unquote_plus(
            req_hdrs['X-Object-Sysmeta-Crypto-Meta-Etag']))
        self.assertEqual(Crypto({}).get_cipher(), actual['cipher'])
        self.assertEqual(fake_iv(), base64.b64decode(actual['iv']))

    def test_basic_post_req(self):
        body = 'FAKE APP'
        env = {'REQUEST_METHOD': 'POST',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        hdrs = {'x-object-meta-test': 'encrypt me',
                'x-object-sysmeta-test': 'do not encrypt me'}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=body, headers=hdrs)
        key = fetch_crypto_keys()['object']
        app = FakeSwift()
        app.register('POST', '/v1/a/c/o', HTTPAccepted, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('202 Accepted', resp.status)

        # verify metadata items
        self.assertEqual(1, len(app.calls), app.calls)
        self.assertEqual('POST', app.calls[0][0])
        req_hdrs = app.headers[0]

        # user meta is encrypted
        self.assertEqual(base64.b64encode(encrypt('encrypt me', key,
                                                  fake_iv())),
                         req_hdrs['X-Object-Meta-Test'])
        actual = req_hdrs['X-Object-Transient-Sysmeta-Crypto-Meta-Test']
        actual = json.loads(urllib.unquote_plus(actual))
        self.assertEqual(Crypto({}).get_cipher(), actual['cipher'])
        self.assertEqual(fake_iv(), base64.b64decode(actual['iv']))

        # sysmeta is not encrypted
        self.assertEqual('do not encrypt me',
                         req_hdrs['X-Object-Sysmeta-Test'])

    def test_backend_response_etag_is_replaced(self):
        body = 'FAKE APP'
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=body, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated,
                     {'Etag': 'ciphertextEtag'})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)
        self.assertEqual(md5hex(body), resp.headers['Etag'])

    def test_multiseg_no_client_etag(self):
        chunks = ['some', 'chunks', 'of data']
        body = ''.join(chunks)
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys,
               'wsgi.input': FileLikeIter(chunks)}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)
        # verify object is encrypted by getting direct from the app
        get_req = Request.blank('/v1/a/c/o', environ={'REQUEST_METHOD': 'GET'})
        self.assertEqual(encrypt(body, fetch_crypto_keys()['object'],
                                 fake_iv()), get_req.get_response(app).body)

    def test_multiseg_good_client_etag(self):
        chunks = ['some', 'chunks', 'of data']
        body = ''.join(chunks)
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys,
               'wsgi.input': FileLikeIter(chunks)}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body)),
                'Etag': md5hex(body)}
        req = Request.blank(
            '/v1/a/c/o', environ=env, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)
        # verify object is encrypted by getting direct from the app
        get_req = Request.blank('/v1/a/c/o', environ={'REQUEST_METHOD': 'GET'})
        self.assertEqual(encrypt(body, fetch_crypto_keys()['object'],
                                 fake_iv()), get_req.get_response(app).body)

    def test_multiseg_bad_client_etag(self):
        chunks = ['some', 'chunks', 'of data']
        body = ''.join(chunks)
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': fetch_crypto_keys,
               'wsgi.input': FileLikeIter(chunks)}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body)),
                'Etag': 'badclientetag'}
        req = Request.blank(
            '/v1/a/c/o', environ=env, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('422 Unprocessable Entity', resp.status)

    def test_missing_key_callback(self):
        body = 'FAKE APP'
        env = {'REQUEST_METHOD': 'PUT'}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=body, headers=hdrs)
        app = FakeSwift()
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('500 Internal Error', resp.status)
        self.assertEqual('swift.crypto.fetch_crypto_keys not in env',
                         resp.body)

    def test_error_in_key_callback(self):
        def raise_exc():
            raise Exception('Testing')

        body = 'FAKE APP'
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.fetch_crypto_keys': raise_exc}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=body, headers=hdrs)
        app = FakeSwift()
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('500 Internal Error', resp.status)
        self.assertEqual('swift.crypto.fetch_crypto_keys had exception:'
                         ' Testing', resp.body)

    def test_encryption_override(self):
        # simulate another middleware wanting to set footers
        other_footers = {
            'Etag': 'other etag',
            'X-Object-Sysmeta-Other': 'other sysmeta',
            'X-Backend-Container-Update-Override-Etag': 'other override'}
        body = 'FAKE APP'
        env = {'REQUEST_METHOD': 'PUT',
               'swift.crypto.override': True,
               'swift.callback.update_footers':
                   lambda footers: footers.update(other_footers)}
        hdrs = {'content-type': 'text/plain',
                'content-length': str(len(body))}
        req = Request.blank(
            '/v1/a/c/o', environ=env, body=body, headers=hdrs)
        app = FakeSwift()
        app.register('PUT', '/v1/a/c/o', HTTPCreated, {})
        resp = req.get_response(encrypter.Encrypter(app, {}))
        self.assertEqual('201 Created', resp.status)

        # verify that other middleware's footers made it to app
        req_hdrs = app.headers[0]
        for k, v in other_footers.items():
            self.assertEqual(v, req_hdrs[k])

        # verify object is NOT encrypted by getting direct from the app
        get_req = Request.blank('/v1/a/c/o', environ={'REQUEST_METHOD': 'GET'})
        self.assertEqual(body, get_req.get_response(app).body)

    def test_filter(self):
        factory = encrypter.filter_factory({})
        self.assertTrue(callable(factory))
        self.assertIsInstance(factory({}), encrypter.Encrypter)

    def test_app_exception(self):
        app = encrypter.Encrypter(
            FakeAppThatExcepts(), {})
        req = Request.blank('/', environ={'REQUEST_METHOD': 'PUT'})
        with self.assertRaises(HTTPException) as catcher:
            req.get_response(app)
        self.assertEqual(FakeAppThatExcepts.get_error_msg(),
                         catcher.exception.body)


if __name__ == '__main__':
    unittest.main()
