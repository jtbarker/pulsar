'''Pulsar has a thread safe :class:`HttpClient` class for multiple asynchronous
HTTP requests.

To get started, one builds a client::

    >>> from pulsar.apps import http
    >>> client = http.HttpClient()

and than makes a request::

    >>> response = http.get('http://www.bbc.co.uk')


.. contents::
    :local:

Making requests
=================
Pulsar HTTP client has no dependencies and an API similar to requests_::

    from pulsar.apps import http
    client = http.HttpClient()
    resp = client.get('https://github.com/timeline.json')

``resp`` is a :class:`HttpResponse` object which contains all the information
about the request and, once finished, the result.

The ``resp`` is finished once the ``on_finished`` attribute
(a :class:`pulsar.Deferred`) is fired. In a :ref:`coroutine <coroutine>` one
can obtained a full response by yielding ``on_finished``::

    resp = yield client.get('https://github.com/timeline.json').on_finished

Cookie support
================

Cookies are handled by the client by storing cookies received with responses.
To disable cookie one can pass ``store_cookies=False`` during
:class:`HttpClient` initialisation.

If a response contains some Cookies, you can get quick access to them::

    >>> r = yield client.get(...).on_headers
    >>> type(r.cookies)
    <type 'dict'>

To send your own cookies to the server, you can use the cookies parameter::

    response = client.get(..., cookies={'sessionid': 'test'})


.. _http-authentication:

Authentication
======================

Headers authentication, either ``basic`` or ``digest``, can be added to a
client by invoking

* :meth:`HttpClient.add_basic_authentication` method
* :meth:`HttpClient.add_digest_authentication` method

In either case the authentication is handled by adding additional headers
to your requests.

TLS/SSL
=================
Supported out of the box::

    client = HttpClient()
    client.get('https://github.com/timeline.json')

you can include certificate file and key too, either
to a :class:`HttpClient` or to a specific request::

    client = HttpClient(certkey='public.key')
    res1 = client.get('https://github.com/timeline.json')
    res2 = client.get('https://github.com/timeline.json',
                      certkey='another.key')

.. _http-streaming:

Streaming
=========================

This is an event-driven client, therefore streaming support is native.

To stream data received from the client one can use either the
``data_received`` or ``data_processed``
:ref:`many time events <many-times-event>`. For example::

    def new_data(response, data=None):
        # response is the http response receiving data
        # data are bytes

    response = http.get(..., data_received=new_data)

The ``on_finished`` callback on a :class:`HttpResponse` is only fired when
the client has finished with the response.
Check the :ref:`proxy server <tutorials-proxy-server>` example for an
application using the :class:`HttpClient` streaming capabilities.


WebSocket
==============

The http client support websocket upgrades. First you need to have a
websocket handler::

    from pulsar.apps import ws

    class Echo(ws.WS):

        def on_message(self, websocket, message):
            websocket.write(message)

The websocket response is obtained by waiting for the
:attr:`HttpResponse.on_headers` event::

    ws = yield http.get('ws://...', websocket_handler=Echo()).on_headers

Redirects & Decompression
=============================

Synchronous Mode
=====================

Can be used in :ref:`synchronous mode <tutorials-synchronous>`::

    client = HttpClient(force_sync=True)

Events
==============
Events are used to customise the behaviour of the Http client when certain
headers or responses occurs. There are three
:ref:`one time events <one-time-event>` associated with an
:class:`HttpResponse` object:

* ``pre_request``, fired before the request is sent to the server. Callbacks
  receive the *response* argument.
* ``on_headers``, fired when response headers are available. Callbacks
  receive the *response* argument.
* ``post_request``, fired when the response is done. Callbacks
  receive the *response* argument.

Adding event handlers can be done at client level::

    def myheader_handler(response):
        ...
        return response    # !important, must return the response

    client.bind_event('on_headers', myheader_handler)

or at request level::

    response = client.get(..., on_headers=myheader_handler)

By default, the :class:`HttpClient` has one ``pre_request`` callback for
handling `HTTP tunneling`_, three ``on_headers`` callbacks for
handling *100 Continue*, *websocket upgrade* and *cookies*, and one
``post_request`` callback for handling redirects.


API
==========

The main class here is the :class:`HttpClient` which is a subclass of
:class:`pulsar.Client`.
You can use the client as a global singletone::


    >>> requests = HttpClient()

and somewhere else

    >>> resp = requests.post('http://bla.foo', body=...)

the same way requests_ works, otherwise use it where you need it.


HTTP Client
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpClient
   :members:
   :member-order: bysource


HTTP Request
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpRequest
   :members:
   :member-order: bysource

HTTP Response
~~~~~~~~~~~~~~~~~~

.. autoclass:: HttpResponse
   :members:
   :member-order: bysource


.. _requests: http://docs.python-requests.org/
.. _`uri scheme`: http://en.wikipedia.org/wiki/URI_scheme
.. _`HTTP tunneling`: http://en.wikipedia.org/wiki/HTTP_tunnel
'''
import os
import platform
import json
from collections import namedtuple
from base64 import b64encode
from io import StringIO, BytesIO

import pulsar
from pulsar import is_failure
from pulsar.utils.pep import native_str, is_string, to_bytes, ispy33
from pulsar.utils.structures import mapping_iterator
from pulsar.utils.websocket import SUPPORTED_VERSIONS
from pulsar.utils.internet import CERT_NONE, SSLContext
from pulsar.utils.multipart import parse_options_header
from pulsar.utils.httpurl import (urlparse, parse_qsl, responses,
                                  http_parser, ENCODE_URL_METHODS,
                                  encode_multipart_formdata, urlencode,
                                  Headers, urllibr, get_environ_proxies,
                                  choose_boundary, urlunparse, request_host,
                                  is_succesful, HTTPError, URLError,
                                  get_hostport, cookiejar_from_dict,
                                  host_no_default_port, DEFAULT_CHARSET,
                                  JSON_CONTENT_TYPES)

from .plugins import (handle_cookies, handle_100, handle_101, handle_redirect,
                      Tunneling, TooManyRedirects)

from .auth import Auth, HTTPBasicAuth, HTTPDigestAuth


scheme_host = namedtuple('scheme_host', 'scheme netloc')
tls_schemes = ('https', 'wss')


def guess_filename(obj):
    """Tries to guess the filename of the given object."""
    name = getattr(obj, 'name', None)
    if name and name[0] != '<' and name[-1] != '>':
        return os.path.basename(name)


class RequestBase(object):
    inp_params = None
    history = None
    full_url = None
    scheme = None

    @property
    def unverifiable(self):
        '''Unverifiable when a redirect.

        It is a redirect when :attr:`history` has past requests.
        '''
        return bool(self.history)

    @property
    def origin_req_host(self):
        if self.history:
            return self.history[0].request.origin_req_host
        else:
            return request_host(self)

    @property
    def type(self):
        return self.scheme

    def get_full_url(self):
        return self.full_url


if not ispy33:  # pragma     nocover
    _RequestBase = RequestBase

    class RequestBase(_RequestBase):

        def is_unverifiable(self):
            return self.unverifiable

        def get_origin_req_host(self):
            return self.origin_req_host

        def get_type(self):
            return self.scheme


class HttpTunnel(RequestBase):
    first_line = None

    def __init__(self, request, scheme, host):
        self.request = request
        self.scheme = scheme
        self.host, self.port = get_hostport(scheme, host)
        self.full_url = '%s://%s:%s' % (scheme, self.host, self.port)
        self.parser = request.parser
        request.new_parser()
        self.headers = request.client.tunnel_headers.copy()

    def __repr__(self):
        return 'Tunnel %s' % self.full_url
    __str__ = __repr__

    @property
    def key(self):

        return self.request.key

    @property
    def address(self):
        return (self.host, self.port)

    @property
    def client(self):
        return self.request.client

    def encode(self):
        req = self.request
        self.headers['host'] = req.get_header('host')
        bits = req.target_address + (req.version,)
        self.first_line = 'CONNECT %s:%s %s\r\n' % bits
        return b''.join((self.first_line.encode('ascii'), bytes(self.headers)))

    def has_header(self, header_name):
        return header_name in self.headers

    def get_header(self, header_name, default=None):
        return self.headers.get(header_name, default)

    def remove_header(self, header_name):
        self.headers.pop(header_name, None)


class HttpRequest(pulsar.Request, RequestBase):
    '''An :class:`HttpClient` request for an HTTP resource.

    :param files: optional dictionary of name, file-like-objects.
    :param allow_redirects: allow the response to follow redirects.

    .. attribute:: method

        The request method

    .. attribute:: version

        HTTP version for this request, usually ``HTTP/1.1``

    .. attribute:: history

        List of past :class:`HttpResponse` (collected during redirects).

    .. attribute:: wait_continue

        if ``True``, the :class:`HttpRequest` includes the
        ``Expect: 100-Continue`` header.

    '''
    _proxy = None
    _ssl = None
    _tunnel = None

    def __init__(self, client, url, method, inp_params, headers=None,
                 data=None, files=None, timeout=None, history=None,
                 charset=None, encode_multipart=True, multipart_boundary=None,
                 source_address=None, allow_redirects=False, max_redirects=10,
                 decompress=True, version=None, wait_continue=False,
                 websocket_handler=None, cookies=None, **ignored):
        self.client = client
        self.inp_params = inp_params
        self.unredirected_headers = Headers(kind='client')
        self.timeout = timeout
        self.method = method.upper()
        self.full_url = url
        self.set_proxy(None)
        self.history = history
        self.wait_continue = wait_continue
        self.max_redirects = max_redirects
        self.allow_redirects = allow_redirects
        self.charset = charset or 'utf-8'
        self.version = version
        self.decompress = decompress
        self.encode_multipart = encode_multipart
        self.multipart_boundary = multipart_boundary
        self.websocket_handler = websocket_handler
        self.data = data if data is not None else {}
        self.files = files
        self.source_address = source_address
        self.new_parser()
        if self._scheme in tls_schemes:
            self._ssl = client.ssl_context(**ignored)
        self.headers = client.get_headers(self, headers)
        if client.cookies:
            client.cookies.add_cookie_header(self)
        if cookies:
            cookiejar_from_dict(cookies).add_cookie_header(self)
        self.unredirected_headers['host'] = host_no_default_port(self._scheme,
                                                                 self._netloc)
        client.set_proxy(self)

    @property
    def address(self):
        '''``(host, port)`` tuple of the HTTP resource'''
        return self._tunnel.address if self._tunnel else (self.host, self.port)

    @property
    def target_address(self):
        return (self.host, int(self.port))

    @property
    def ssl(self):
        '''Context for TLS connections.

        If this is a tunneled request and the tunnel connection is not yet
        established, it returns ``None``.
        '''
        if not self._tunnel:
            return self._ssl

    @property
    def key(self):
        return (self.scheme, self.host, self.port, self.timeout)

    @property
    def proxy(self):
        '''Proxy server for this request.'''
        return self._proxy

    @property
    def netloc(self):
        if self._proxy:
            return self._proxy.netloc
        else:
            return self._netloc

    def __repr__(self):
        return self.first_line()
    __str__ = __repr__

    def _get_full_url(self):
        return urlunparse((self._scheme, self._netloc, self.path,
                           self.params, self.query, self.fragment))

    def _set_full_url(self, url):
        self._scheme, self._netloc, self.path, self.params,\
            self.query, self.fragment = urlparse(url)
        if not self._netloc and self.method == 'CONNECT':
            self._scheme, self._netloc, self.path, self.params,\
                self.query, self.fragment = urlparse('http://%s' % url)

    full_url = property(_get_full_url, _set_full_url)

    def first_line(self):
        url = self.full_url
        if not self._proxy:
            url = urlunparse(('', '', self.path or '/', self.params,
                              self.query, self.fragment))
        return '%s %s %s' % (self.method, url, self.version)

    def new_parser(self):
        self.parser = self.client.http_parser(kind=1,
                                              decompress=self.decompress)

    def set_proxy(self, scheme, *host):
        if not host and scheme is None:
            self.scheme = self._scheme
            self._set_hostport(self._scheme, self._netloc)
        else:
            le = 2 + len(host)
            if not le == 3:
                raise TypeError(
                    'set_proxy() takes exactly three arguments (%s given)'
                    % le)
            if not self._ssl:
                self.scheme = scheme
                self._set_hostport(scheme, host[0])
                self._proxy = scheme_host(scheme, host[0])
            else:
                self._tunnel = HttpTunnel(self, scheme, host[0])

    def _set_hostport(self, scheme, host):
        self._tunnel = None
        self._proxy = None
        self.host, self.port = get_hostport(scheme, host)

    def encode(self):
        '''The bytes representation of this :class:`HttpRequest`.

        Called by :class:`HttpResponse` when it needs to encode this
        :class:`HttpRequest` before sending it to the HTTP resourse.
        '''
        if self.method == 'CONNECT':    # this is SSL tunneling
            return b''
            # Call body before fist_line in case the query is changes.
        self.body = body = self.encode_body()
        first_line = self.first_line()
        if body:
            self.headers['content-length'] = str(len(body))
            if self.wait_continue:
                self.headers['expect'] = '100-continue'
                body = None
        headers = self.headers
        if self.unredirected_headers:
            headers = self.unredirected_headers.copy()
            headers.update(self.headers)
        buffer = [first_line.encode('ascii'), b'\r\n',  bytes(headers)]
        if body:
            buffer.append(body)
        return b''.join(buffer)

    def encode_body(self):
        '''Encode body or url if the :attr:`method` does not have body.

        Called by the :meth:`encode` method.
        '''
        body = None
        if self.method in ENCODE_URL_METHODS:
            self.files = None
            self._encode_url(self.data)
        elif isinstance(self.data, bytes):
            assert self.files is None, ('data cannot be bytes when files are '
                                        'present')
            body = self.data
        elif is_string(self.data):
            assert self.files is None, ('data cannot be string when files are '
                                        'present')
            body = to_bytes(self.data, self.charset)
        elif self.data or self.files:
            if self.files:
                body, content_type = self._encode_files()
            else:
                body, content_type = self._encode_params()
            self.headers['Content-Type'] = content_type
        return body

    def has_header(self, header_name):
        '''Check ``header_name`` is in this request headers.
        '''
        return (header_name in self.headers or
                header_name in self.unredirected_headers)

    def get_header(self, header_name, default=None):
        '''Retrieve ``header_name`` from this request headers.
        '''
        return self.headers.get(
            header_name, self.unredirected_headers.get(header_name, default))

    def remove_header(self, header_name):
        '''Remove ``header_name`` from this request.
        '''
        self.headers.pop(header_name, None)
        self.unredirected_headers.pop(header_name, None)

    def add_unredirected_header(self, header_name, header_value):
        self.unredirected_headers[header_name] = header_value

    # INTERNAL ENCODING METHODS
    def _encode_url(self, body):
        query = self.query
        if body:
            body = native_str(body)
            if isinstance(body, str):
                body = parse_qsl(body)
            else:
                body = mapping_iterator(body)
            query = parse_qsl(query)
            query.extend(body)
            self.data = query
            query = urlencode(query)
        self.query = query

    def _encode_files(self):
        fields = []
        for field, val in mapping_iterator(self.data or ()):
            if (is_string(val) or isinstance(val, bytes) or
                    not hasattr(val, '__iter__')):
                val = [val]
            for v in val:
                if v is not None:
                    if not isinstance(v, bytes):
                        v = str(v)
                    fields.append((field.decode('utf-8') if
                                   isinstance(field, bytes) else field,
                                   v.encode('utf-8') if isinstance(v, str)
                                   else v))
        for (k, v) in mapping_iterator(self.files):
            # support for explicit filename
            ft = None
            if isinstance(v, (tuple, list)):
                if len(v) == 2:
                    fn, fp = v
                else:
                    fn, fp, ft = v
            else:
                fn = guess_filename(v) or k
                fp = v
            if isinstance(fp, bytes):
                fp = BytesIO(fp)
            elif is_string(fp):
                fp = StringIO(fp)
            if ft:
                new_v = (fn, fp.read(), ft)
            else:
                new_v = (fn, fp.read())
            fields.append((k, new_v))
        #
        return encode_multipart_formdata(fields, charset=self.charset)

    def _encode_params(self):
        content_type = self.headers.get('content-type')
        # No content type given
        if not content_type:
            if self.encode_multipart:
                return encode_multipart_formdata(
                    self.data, boundary=self.multipart_boundary,
                    charset=self.charset)
            else:
                content_type = 'application/x-www-form-urlencoded'
                body = urlencode(self.data).encode(self.charset)
        elif content_type in JSON_CONTENT_TYPES:
            body = json.dumps(self.data).encode(self.charset)
        else:
            raise ValueError("Don't know how to encode body for %s" %
                             content_type)
        return body, content_type


class HttpResponse(pulsar.ProtocolConsumer):
    '''A :class:`pulsar.ProtocolConsumer` for the HTTP client protocol.

    Initialised by a call to the :class:`HttpClient.request` method.

    There are two events you can yield in a coroutine:

    .. attribute:: on_headers

        fired once the response headers are received.

    .. attribute:: on_finished

        Fired once the whole request has finished

    Public API:
    '''
    _tunnel_host = None
    _has_proxy = False
    _content = None
    _data_sent = None
    _history = None
    _status_code = None
    _cookies = None
    ONE_TIME_EVENTS = pulsar.ProtocolConsumer.ONE_TIME_EVENTS + ('on_headers',)

    @property
    def parser(self):
        if self._request:
            return self._request.parser

    def __str__(self):
        return '%s' % (self.status_code or '<None>')

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self)

    @property
    def status_code(self):
        '''Numeric status code such as 200, 404 and so forth.

        Available once the :attr:`on_headers` has fired.'''
        return self._status_code

    @property
    def url(self):
        '''The request full url.'''
        if self._request is not None:
            return self._request.full_url

    @property
    def history(self):
        return self._history

    @property
    def headers(self):
        if not hasattr(self, '_headers'):
            if self.parser and self.parser.is_headers_complete():
                self._headers = Headers(self.parser.get_headers())
        return getattr(self, '_headers', None)

    @property
    def is_error(self):
        if self.status_code:
            return not is_succesful(self.status_code)
        elif self.on_finished.done():
            return is_failure(self.on_finished.result)
        else:
            return False

    @property
    def cookies(self):
        '''Dictionary of cookies set by the server or ``None``.
        '''
        return self._cookies

    @property
    def on_headers(self):
        return self.event('on_headers')

    def recv_body(self):
        '''Flush the response body and return it.'''
        return self.parser.recv_body()

    def get_status(self):
        code = self.status_code
        if code:
            return '%d %s' % (code, responses.get(code, 'Unknown'))

    def get_content(self):
        '''Retrieve the body without flushing'''
        b = self.parser.recv_body()
        if b or self._content is None:
            self._content = self._content + b if self._content else b
        return self._content

    def content_string(self, charset=None, errors=None):
        '''Decode content as a string.'''
        data = self.get_content()
        if data is not None:
            return data.decode(charset or 'utf-8', errors or 'strict')

    def json(self, charset=None, **kwargs):
        '''Decode content as a JSON object.'''
        return json.loads(self.content_string(charset), **kwargs)

    def decode_content(self, object_hook=None):
        '''Return the best possible representation of the response body.

        :param object_hook: optional object hook function to pass to the
            ``json`` decoder if the content type is a ``json`` format.
        '''
        ct = self.headers.get('content-type')
        if ct:
            ct, options = parse_options_header(ct)
            charset = options.get('charset')
            if ct in JSON_CONTENT_TYPES:
                return self.json(charset, object_hook=object_hook)
            elif ct.startswith('text/'):
                return self.content_string(charset)
        return self.get_content()

    def raise_for_status(self):
        '''Raises stored :class:`HTTPError` or :class:`URLError`, if occured.
        '''
        if self.is_error:
            if self.status_code:
                raise HTTPError(self.url, self.status_code,
                                self.content_string(), self.headers, None)
            else:
                raise URLError(self.on_finished.result.error)

    def info(self):
        '''Required by python CookieJar.

        Return :attr:`headers`.'''
        return self.headers

    #######################################################################
    ##    PROTOCOL IMPLEMENTATION
    def start_request(self):
        self.transport.write(self._request.encode())

    def data_received(self, data):
        request = self._request
        # request.parser my change (100-continue)
        # Always invoke it via request
        if request.parser.execute(data, len(data)) == len(data):
            if request.parser.is_headers_complete():
                self._status_code = request.parser.get_status_code()
                if not self.event('on_headers').done():
                    self.fire_event('on_headers')
                if (not self.has_finished and
                        request.parser.is_message_complete()):
                    self.finished()
        else:
            raise pulsar.ProtocolError('%s\n%s' % (self, self.headers))


class HttpClient(pulsar.Client):
    '''A :class:`pulsar.Client` for HTTP/HTTPS servers.

    As :class:`pulsar.Client` it handles
    a pool of asynchronous :class:`pulsar.Connection`.

    .. attribute:: headers

        Default headers for this :class:`HttpClient`.

        Default: :attr:`DEFAULT_HTTP_HEADERS`.

    .. attribute:: cookies

        Default cookies for this :class:`HttpClient`.

    .. attribute:: timeout

        Default timeout for the connecting sockets. If 0 it is an asynchronous
        client.

    .. attribute:: encode_multipart

        Flag indicating if body data is encoded using the
        ``multipart/form-data`` encoding by default.
        It can be overwritten during a :meth:`request`.

        Default: ``True``

    .. attribute:: proxy_info

        Dictionary of proxy servers for this client.

    .. attribute:: DEFAULT_HTTP_HEADERS

        Default headers for this :class:`HttpClient`

    '''
    MANY_TIMES_EVENTS = pulsar.Client.MANY_TIMES_EVENTS + ('on_headers',)
    consumer_factory = HttpResponse
    allow_redirects = False
    max_redirects = 10
    '''Maximum number of redirects.

    It can be overwritten on :meth:`request`.'''
    client_version = pulsar.SERVER_SOFTWARE
    '''String for the ``User-Agent`` header.'''
    version = 'HTTP/1.1'
    '''Default HTTP request version for this :class:`HttpClient`.

    It can be overwritten on :meth:`request`.'''
    DEFAULT_HTTP_HEADERS = Headers([
        ('Connection', 'Keep-Alive'),
        ('Accept', '*/*'),
        ('Accept-Encoding', 'deflate'),
        ('Accept-Encoding', 'gzip')],
        kind='client')
    DEFAULT_TUNNEL_HEADERS = Headers([
        ('Connection', 'Keep-Alive'),
        ('Proxy-Connection', 'Keep-Alive')],
        kind='client')
    request_parameters = ('encode_multipart', 'max_redirects', 'decompress',
                          'allow_redirects', 'multipart_boundary', 'version',
                          'timeout', 'websocket_handler')
    # Default hosts not affected by proxy settings. This can be overwritten
    # by specifying the "no" key in the proxy_info dictionary
    no_proxy = set(('localhost', urllibr.localhost(), platform.node()))

    def setup(self, proxy_info=None, cache=None, headers=None,
              encode_multipart=True, multipart_boundary=None,
              keyfile=None, certfile=None, cert_reqs=CERT_NONE,
              ca_certs=None, cookies=None, store_cookies=True,
              max_redirects=10, decompress=True, version=None,
              websocket_handler=None, parser=None):
        self.store_cookies = store_cookies
        self.max_redirects = max_redirects
        self.cookies = cookiejar_from_dict(cookies)
        self.decompress = decompress
        self.version = version or self.version
        dheaders = self.DEFAULT_HTTP_HEADERS.copy()
        dheaders['user-agent'] = self.client_version
        if headers:
            dheaders.override(headers)
        self.headers = dheaders
        self.tunnel_headers = self.DEFAULT_TUNNEL_HEADERS.copy()
        self.proxy_info = dict(proxy_info or ())
        if not self.proxy_info and self.trust_env:
            self.proxy_info = get_environ_proxies()
            if 'no' not in self.proxy_info:
                self.proxy_info['no'] = ','.join(self.no_proxy)
        self.encode_multipart = encode_multipart
        self.multipart_boundary = multipart_boundary or choose_boundary()
        self.websocket_handler = websocket_handler
        self.https_defaults = {'keyfile': keyfile,
                               'certfile': certfile,
                               'cert_reqs': cert_reqs,
                               'ca_certs': ca_certs}
        self.http_parser = parser or http_parser
        # Add hooks
        self.bind_event('pre_request', Tunneling())
        self.bind_event('on_headers', handle_101)
        self.bind_event('on_headers', handle_100)
        self.bind_event('on_headers', handle_cookies)
        self.bind_event('post_request', handle_redirect)

    @property
    def websocket_key(self):
        if not hasattr(self, '_websocket_key'):
            self._websocket_key = native_str(b64encode(os.urandom(16)),
                                             DEFAULT_CHARSET)
        return self._websocket_key

    def get(self, url, **kwargs):
        '''Sends a GET request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        kwargs.setdefault('allow_redirects', True)
        return self.request('GET', url, **kwargs)

    def options(self, url, **kwargs):
        '''Sends a OPTIONS request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        kwargs.setdefault('allow_redirects', True)
        return self.request('OPTIONS', url, **kwargs)

    def head(self, url, **kwargs):
        '''Sends a HEAD request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('HEAD', url, **kwargs)

    def post(self, url, **kwargs):
        '''Sends a POST request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('POST', url, **kwargs)

    def put(self, url, **kwargs):
        '''Sends a PUT request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('PUT', url, **kwargs)

    def patch(self, url, **kwargs):
        '''Sends a PATCH request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('PATCH', url, **kwargs)

    def delete(self, url, **kwargs):
        '''Sends a DELETE request and returns a :class:`HttpResponse` object.

        :params url: url for the new :class:`HttpRequest` object.
        :param \*\*kwargs: Optional arguments for the :meth:`request` method.
        '''
        return self.request('DELETE', url, **kwargs)

    def request(self, method, url, response=None, **params):
        '''Constructs and sends a request to a remote server.

        It returns an :class:`HttpResponse` object.

        :param method: request method for the :class:`HttpRequest`.
        :param url: URL for the :class:`HttpRequest`.
        :parameter response: optional pre-existing :class:`HttpResponse` which
            starts a new request (for redirects, digest authentication and
            so forth).
        :param params: optional parameters for the :class:`HttpRequest`
            initialisation.

        :rtype: a :class:`HttpResponse` object.
        '''
        request = self._build_request(method, url, response, params)
        return self.response(request, response)

    def again(self, response, method=None, url=None, params=None,
              history=False, new_response=None, request=None):
        '''Create a new request from ``response``.

        The input ``response`` must be done.
        '''
        assert response.has_finished, 'response has not finished'
        if not new_response:
            new_response = self.build_consumer()
        new_response.chain_event(response, 'post_request')
        if history:
            new_response._history = []
            new_response._history.extend(response._history or ())
            new_response._history.append(response)
        #
        if not request:
            request = response.request
            if params is None:
                params = request.inp_params.copy()
            if not method:
                method = request.method
            if not url:
                url = request.full_url
            request = self._build_request(method, url, new_response, params)
        #
        connection = new_response.connection or response.connection
        return self.response(request, new_response, connection=connection)

    def add_basic_authentication(self, username, password):
        '''Add a :class:`HTTPBasicAuth` handler to the ``pre_requests`` hook.
        '''
        self.bind_event('pre_request', HTTPBasicAuth(username, password))

    def add_digest_authentication(self, username, password):
        '''Add a :class:`HTTPDigestAuth` handler to the ``pre_requests`` hook.
        '''
        self.bind_event('pre_request', HTTPDigestAuth(username, password))

    #def add_oauth2(self, client_id, client_secret):
    #    self.bind_event('pre_request', OAuth2(client_id, client_secret))

    #    INTERNALS

    def _build_request(self, method, url, response, params):
        nparams = self.update_parameters(self.request_parameters, params)
        if response:
            nparams['history'] = response.history
        return HttpRequest(self, url, method, params, **nparams)

    def get_headers(self, request, headers=None):
        #Returns a :class:`Header` obtained from combining
        #:attr:`headers` with *headers*. Can handle websocket requests.
        if request.scheme in ('ws', 'wss'):
            d = Headers((
                ('Connection', 'Upgrade'),
                ('Upgrade', 'websocket'),
                ('Sec-WebSocket-Version', str(max(SUPPORTED_VERSIONS))),
                ('Sec-WebSocket-Key', self.websocket_key),
                ('user-agent', self.client_version)
                ), kind='client')
        else:
            d = self.headers.copy()
        if headers:
            d.override(headers)
        return d

    def ssl_context(self, **kwargs):
        params = self.https_defaults.copy()
        for name in kwargs:
            if name in params:
                params[name] = kwargs[name]
        return SSLContext(**params)

    def set_proxy(self, request):
        if request.scheme in self.proxy_info:
            hostonly = request.host
            no_proxy = [n for n in self.proxy_info.get('no', '').split(',')
                        if n]
            if not any(map(hostonly.endswith, no_proxy)):
                url = self.proxy_info[request.scheme]
                p = urlparse(url)
                if not p.scheme:
                    raise ValueError('Could not understand proxy %s' % url)
                request.set_proxy(p.scheme, p.netloc)

    def can_reuse_connection(self, connection, response):
        # Reuse connection only if the headers has Connection keep-alive
        if response and response.headers:
            return response.headers.has('connection', 'keep-alive')
        return False
