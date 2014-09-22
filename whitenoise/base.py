from __future__ import absolute_import

from email.utils import parsedate, formatdate
import mimetypes
import os
import os.path
import re
from time import gmtime
from wsgiref.headers import Headers


class Redirect(object):
    def __init__(self, path):
        self.path = path

    def serve(self, environ, start_response):
        method = environ['REQUEST_METHOD']
        if method != 'GET' and method != 'HEAD':
            start_response('405 Method Not Allowed', [('Allow', 'GET, HEAD')])
            return []
        host = environ.get('HTTP_HOST', '')
        start_response('301 Moved Permanently', [('Location', '%s://%s%s' % (environ['wsgi.url_scheme'], host, self.path) if host else self.path)])
        return []


class StaticFile(object):
    ACCEPT_GZIP_RE = re.compile(r'\bgzip\b')
    BLOCK_SIZE = 16 * 4096
    # All mimetypes starting 'text/' take a charset parameter, plus the
    # additions in this set
    MIMETYPES_WITH_CHARSET = {'application/javascript', 'application/xml'}
    CHARSET = 'utf-8'
    # Ten years is what nginx sets a max age if you use 'expires max;'
    # so we'll follow its lead
    FOREVER = 10*365*24*60*60

    GZIP_SUFFIX = '.gz'

    def __init__(self, path, is_immutable, guess_type=mimetypes.guess_type, **config):
        self.path = path
        stat = os.stat(path)
        self.mtime_tuple = gmtime(stat.st_mtime)
        mimetype, encoding = guess_type(path)
        mimetype = mimetype or 'application/octet-stream'
        charset = self.get_charset(mimetype)
        params = {'charset': charset} if charset else {}
        self.headers = Headers([
            ('Last-Modified', formatdate(stat.st_mtime, usegmt=True)),
            ('Content-Length', str(stat.st_size)),
        ])
        self.headers.add_header('Content-Type', str(mimetype), **params)
        if encoding:
            self.headers['Content-Encoding'] = encoding

        max_age = self.FOREVER if is_immutable else config['max_age']
        if max_age is not None:
            self.headers['Cache-Control'] = 'public, max-age=%s' % max_age

        if config['allow_all_origins']:
            self.headers['Access-Control-Allow-Origin'] = '*'

        gzip_path = path + self.GZIP_SUFFIX
        if os.path.isfile(gzip_path):
            self.gzip_path = gzip_path
            self.headers['Vary'] = 'Accept-Encoding'
            # Copy the headers and add the appropriate encoding and length
            self.gzip_headers = Headers(self.headers.items())
            self.gzip_headers['Content-Encoding'] = 'gzip'
            self.gzip_headers['Content-Length'] = str(os.stat(gzip_path).st_size)
        else:
            self.gzip_path = self.gzip_headers = None

    def get_charset(self, mimetype):
        if mimetype.startswith('text/') or mimetype in self.MIMETYPES_WITH_CHARSET:
            return self.CHARSET

    def serve(self, environ, start_response):
        method = environ['REQUEST_METHOD']
        if method != 'GET' and method != 'HEAD':
            start_response('405 Method Not Allowed', [('Allow', 'GET, HEAD')])
            return []
        if self.file_not_modified(environ):
            start_response('304 Not Modified', [])
            return []
        path, headers = self.get_path_and_headers(environ)
        start_response('200 OK', headers.items())
        if method == 'HEAD':
            return []
        file_wrapper = environ.get('wsgi.file_wrapper', self.yield_file)
        fileobj = open(path, 'rb')
        return file_wrapper(fileobj)

    def file_not_modified(self, environ):
        try:
            last_requested = environ['HTTP_IF_MODIFIED_SINCE']
        except KeyError:
            return False
        # Exact match, no need to parse
        if last_requested == self.headers['Last-Modified']:
            return True
        return parsedate(last_requested) >= self.mtime_tuple

    def get_path_and_headers(self, environ):
        if self.gzip_path:
            if self.ACCEPT_GZIP_RE.search(environ.get('HTTP_ACCEPT_ENCODING', '')):
                return self.gzip_path, self.gzip_headers
        return self.path, self.headers

    def yield_file(self, fileobj):
        # Only used as a fallback in case environ doesn't supply a
        # wsgi.file_wrapper
        try:
            while True:
                block = fileobj.read(self.BLOCK_SIZE)
                if block:
                    yield block
                else:
                    break
        finally:
            fileobj.close()


class WhiteNoise(object):
    StaticFileClass = StaticFile
    RedirectClass = Redirect

    CONFIG = dict(
        max_age=60,
        # Set 'Access-Control-Allow-Orign: *' header on all files.
        # As these are all public static files this is safe (See
        # http://www.w3.org/TR/cors/#security) and ensures that things (e.g
        # webfonts in Firefox) still work as expected when your static files are
        # served from a CDN, rather than your primary domain.
        allow_all_origins=True,
        charset='utf-8',
        index_file='index.html',
        guess_type=mimetypes.guess_type,
    )

    def __init__(self, application, root=None, prefix=None, **kwargs):
        self.config = dict(self.CONFIG, **kwargs)
        unexpected = set(kwargs) - set(self.CONFIG)
        if unexpected:
            raise TypeError("Unexpected keyword arguments: %s." % ', '.join(unexpected))
        self.application = application
        self.files = {}
        if root is not None:
            self.add_files(root, prefix)

    def __call__(self, environ, start_response):
        static_file = self.files.get(environ['PATH_INFO'])
        if static_file is None:
            return self.application(environ, start_response)
        else:
            return static_file.serve(environ, start_response)

    def add_files(self, root, prefix=None, followlinks=False):
        index_file = self.config['index_file']
        prefix = (prefix or '').strip('/')
        prefix = '/{}/'.format(prefix) if prefix else '/'
        files = self.files
        for dir_path, _, filenames in os.walk(root, followlinks=followlinks):
            for filename in filenames:
                file_path = os.path.join(dir_path, filename)
                url = prefix + os.path.relpath(file_path, root).replace(os.sep, '/')
                if url.endswith(index_file):
                    noslash = url[:-len(index_file)-1]
                    withslash = url[:-len(index_file)]
                    if noslash:
                        files[noslash] = self.RedirectClass(withslash)
                    else:
                        files[noslash] = self.get_static_file(file_path, noslash)
                    files[withslash] = self.get_static_file(file_path, withslash)
                files[url] = self.get_static_file(file_path, url)

    def get_static_file(self, file_path, url):
        return self.StaticFileClass(
            file_path, self.is_immutable_file(file_path, url),
            **self.config
        )

    def is_immutable_file(self, static_file, url):
        """
        This should be implemented by sub-classes (see e.g. DjangoWhiteNoise)
        """
        return False
