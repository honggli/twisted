# Copyright (c) 2008 Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.web.distrib}.
"""

from os.path import abspath
from xml.dom.minidom import parseString
try:
    import pwd
except ImportError:
    pwd = None

from zope.interface.verify import verifyObject

from twisted.python import log, filepath
from twisted.internet import reactor, defer
from twisted.trial import unittest
from twisted.spread import pb
from twisted.web import http, distrib, client, resource, static, server
from twisted.web.test.test_web import DummyRequest
from twisted.web.test._util import _render


class MySite(server.Site):
    def stopFactory(self):
        if hasattr(self, "logFile"):
            if self.logFile != log.logfile:
                self.logFile.close()
            del self.logFile


class PBServerFactory(pb.PBServerFactory):
    def buildProtocol(self, addr):
        self.proto = pb.PBServerFactory.buildProtocol(self, addr)
        return self.proto


class DistribTest(unittest.TestCase):
    port1 = None
    port2 = None
    sub = None

    def tearDown(self):
        dl = [defer.Deferred(), defer.Deferred()]
        self.f1.proto.notifyOnDisconnect(lambda: dl[0].callback(None))
        if self.sub is not None:
            self.sub.publisher.broker.notifyOnDisconnect(
                lambda: dl[1].callback(None))
            self.sub.publisher.broker.transport.loseConnection()
        http._logDateTimeStop()
        if self.port1 is not None:
            dl.append(self.port1.stopListening())
        if self.port2 is not None:
            dl.append(self.port2.stopListening())
        return defer.gatherResults(dl)

    def testDistrib(self):
        # site1 is the publisher
        r1 = resource.Resource()
        r1.putChild("there", static.Data("root", "text/plain"))
        site1 = server.Site(r1)
        self.f1 = PBServerFactory(distrib.ResourcePublisher(site1))
        self.port1 = reactor.listenTCP(0, self.f1)
        self.sub = distrib.ResourceSubscription("127.0.0.1",
                                                self.port1.getHost().port)
        r2 = resource.Resource()
        r2.putChild("here", self.sub)
        f2 = MySite(r2)
        self.port2 = reactor.listenTCP(0, f2)
        d = client.getPage("http://127.0.0.1:%d/here/there" % \
                           self.port2.getHost().port)
        d.addCallback(self.failUnlessEqual, 'root')
        return d



class _PasswordDatabase:
    def __init__(self, users):
        self._users = users


    def getpwall(self):
        return iter(self._users)


    def getpwnam(self, username):
        for user in self._users:
            if user[0] == username:
                return user
        raise KeyError()



class UserDirectoryTests(unittest.TestCase):
    """
    Tests for L{UserDirectory}, a resource for listing all user resources
    available on a system.
    """
    def setUp(self):
        self.alice = ('alice', 'x', 123, 456, 'Alice,,,', self.mktemp(), '/bin/sh')
        self.bob = ('bob', 'x', 234, 567, 'Bob,,,', self.mktemp(), '/bin/sh')
        self.database = _PasswordDatabase([self.alice, self.bob])
        self.directory = distrib.UserDirectory(self.database)


    def test_interface(self):
        """
        L{UserDirectory} instances provide L{resource.IResource}.
        """
        self.assertTrue(verifyObject(resource.IResource, self.directory))


    def _404Test(self, name):
        """
        Verify that requesting the C{name} child of C{self.directory} results
        in a 404 response.
        """
        request = DummyRequest([name])
        result = self.directory.getChild(name, request)
        d = _render(result, request)
        def cbRendered(ignored):
            self.assertEqual(request.responseCode, 404)
        d.addCallback(cbRendered)
        return d


    def test_getInvalidUser(self):
        """
        L{UserDirectory.getChild} returns a resource which renders a 404
        response when passed a string which does not correspond to any known
        user.
        """
        return self._404Test('carol')


    def test_getUserWithoutResource(self):
        """
        L{UserDirectory.getChild} returns a resource which renders a 404
        response when passed a string which corresponds to a known user who has
        neither a user directory nor a user distrib socket.
        """
        return self._404Test('alice')


    def test_getPublicHTMLChild(self):
        """
        L{UserDirectory.getChild} returns a L{static.File} instance when passed
        the name of a user with a home directory containing a I{public_html}
        directory.
        """
        home = filepath.FilePath(self.bob[-2])
        public_html = home.child('public_html')
        public_html.makedirs()
        request = DummyRequest(['bob'])
        result = self.directory.getChild('bob', request)
        self.assertIsInstance(result, static.File)
        self.assertEqual(result.path, public_html.path)


    def test_getDistribChild(self):
        """
        L{UserDirectory.getChild} returns a L{ResourceSubscription} instance
        when passed the name of a user suffixed with C{".twistd"} who has a
        home directory containing a I{.twistd-web-pb} socket.
        """
        home = filepath.FilePath(self.bob[-2])
        home.makedirs()
        web = home.child('.twistd-web-pb')
        request = DummyRequest(['bob'])
        result = self.directory.getChild('bob.twistd', request)
        self.assertIsInstance(result, distrib.ResourceSubscription)
        self.assertEqual(result.host, 'unix')
        self.assertEqual(abspath(result.port), web.path)


    def test_render(self):
        """
        L{UserDirectory} renders a list of links to available user content.
        """
        public_html = filepath.FilePath(self.alice[-2]).child('public_html')
        public_html.makedirs()
        web = filepath.FilePath(self.bob[-2])
        web.makedirs()
        # This really only works if it's a unix socket, but the implementation
        # doesn't currently check for that.  It probably should someday, and
        # then skip users with non-sockets.
        web.child('.twistd-web-pb').setContent("")

        request = DummyRequest([''])
        result = _render(self.directory, request)
        def cbRendered(ignored):
            document = parseString(''.join(request.written))

            # Each user should have an li with a link to their page.
            [alice, bob] = document.getElementsByTagName('li')
            self.assertEqual(alice.firstChild.tagName, 'a')
            self.assertEqual(alice.firstChild.getAttribute('href'), 'alice/')
            self.assertEqual(alice.firstChild.firstChild.data, 'Alice (file)')
            self.assertEqual(bob.firstChild.tagName, 'a')
            self.assertEqual(bob.firstChild.getAttribute('href'), 'bob.twistd/')
            self.assertEqual(bob.firstChild.firstChild.data, 'Bob (twistd)')

        result.addCallback(cbRendered)
        return result


    def test_passwordDatabase(self):
        """
        If L{UserDirectory} is instantiated with no arguments, it uses the
        L{pwd} module as its password database.
        """
        directory = distrib.UserDirectory()
        self.assertIdentical(directory._pwd, pwd)
    if pwd is None:
        test_passwordDatabase.skip = "pwd module required"

