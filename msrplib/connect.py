# Copyright (C) 2008-2009 AG Projects. See LICENSE for details
"""Establish MSRP connection.

This module provides means to obtain a connected and bound MSRPTransport
instance. It uniformly handles 4 different configurations you may find your
client engaged in:

    1. Calling endpoint, not using a relay (ConnectorDirect)
    2. Answering endpoint, not using a relay (AcceptorDirect)
    3. Calling endpoint, using a relay (ConnectorRelay)
    4. Answering endpoint, using a relay (AcceptorRelay)

The answering endpoint may skip using the relay if sure that it's accessible
directly. The calling endpoint is unlikely to need the relay.

Once you have an instance of the right class (use the convenience functions
get_connector() and get_acceptor() to get one), the procedure to establish the
connection is the same:

    full_local_path = connector.prepare()
    try:
        ... put full_local_path in SDP 'a:path' attribute
        ... get full_remote_path from remote's 'a:path: attribute
        ... (the order of the above steps is reversed if you're the
        ... answering party, but that does not affect connector's usage)
        msrptransport = connector.complete(full_remote_path)
    finally:
        connector.cleanup()

To customize connection's parameters, create a new protocol.URI object and pass
it to prepare() function, e.g.

    local_uri = protocol.URI(use_tls=False, port=5000)
    connector.prepare(local_uri)

prepare() may update local_uri in place with the actual connection parameters
used (e.g. if you specified port=0). 'port' attribute of local_uri is currently
only respected by AcceptorDirect.
"""

from __future__ import with_statement
import random
from twisted.internet.address import IPv4Address
from application.system import default_host_ip
from eventlet.twistedutil.protocol import GreenClientCreator, SpawnFactory
from eventlet.coros import event
from eventlet.api import timeout
from eventlet.proc import spawn_greenlet

from msrplib import protocol, MSRPError
from msrplib.transport import MSRPTransport, MSRPTransactionError, MSRPBadRequest
from msrplib.digest import process_www_authenticate

__all__ = ['MSRPRelaySettings',
           'MSRPTimeout',
           'MSRPConnectTimeout',
           'MSRPRelayConnectTimeout',
           'MSRPIncomingConnectTimeout',
           'MSRPBindSessionTimeout',
           'MSRPRelayAuthError',
           'MSRPAuthTimeout',
           'get_connector',
           'get_acceptor']

class MSRPRelaySettings(protocol.ConnectInfo):
    use_tls = True

    def __init__(self, domain, username, password, host=None, port=None, use_tls=None, credentials=None):
        protocol.ConnectInfo.__init__(self, host, use_tls=use_tls, port=port, credentials=credentials)
        self.domain = domain
        self.username = username
        self.password = password

    def __str__(self):
        result = "MSRPRelay %s://%s" % (self.scheme, self.host or self.domain)
        if self.port:
            result += ':%s' % self.port
        return result

    def __repr__(self):
        params = [self.domain, self.username, self.password, self.host, self.port]
        if params[-1] is None:
            del params[-1]
        if params[-1] is None:
            del params[-1]
        return '%s(%s)' % (type(self).__name__, ', '.join(repr(x) for x in params))

    @property
    def uri_domain(self):
       return protocol.URI(host=self.domain, port=self.port, use_tls=self.use_tls, session_id='')

class TimeoutMixin(object):

    @classmethod
    def timeout(cls, *throw_args):
        if not throw_args:
            throw_args = (cls, )
        return timeout(cls.seconds, *throw_args)

class MSRPTimeout(MSRPError, TimeoutMixin):
    seconds = 10

class MSRPConnectTimeout(MSRPTimeout):
    pass

class MSRPRelayConnectTimeout(MSRPTimeout):
    pass

class MSRPIncomingConnectTimeout(MSRPTimeout):
    pass

class MSRPBindSessionTimeout(MSRPTimeout):
    pass

class MSRPRelayAuthError(MSRPTransactionError):
    pass

class MSRPAuthTimeout(MSRPTransactionError, TimeoutMixin):
    code = 408
    comment = 'No response to AUTH request'
    seconds = 30

class ConnectBase(object):
    SRVConnectorClass = None

    def __init__(self, traffic_logger=None, state_logger=None):
        self.traffic_logger = traffic_logger
        self.state_logger = state_logger

    def generate_local_uri(self, port=0):
        return protocol.URI(port=port)

    def _connect(self, local_uri, remote_uri):
        if self.state_logger:
            self.state_logger.report_connecting(remote_uri)
        creator = GreenClientCreator(gtransport_class=MSRPTransport, local_uri=local_uri,
                                     traffic_logger=self.traffic_logger, state_logger=self.state_logger)
        connectFuncName = 'connect' + remote_uri.protocol_name
        connectFuncArgs = remote_uri.protocolArgs
        if remote_uri.host:
            args = (remote_uri.host, remote_uri.port or 2855) + connectFuncArgs
            msrp = getattr(creator, connectFuncName)(*args)
        else:
            if not remote_uri.domain:
                raise ValueError("remote_uri must have either 'host' or 'domain'")
            msrp = creator.connectSRV(remote_uri.scheme, remote_uri.domain,
                                      connectFuncName=connectFuncName,
                                      connectFuncArgs=connectFuncArgs,
                                      ConnectorClass=self.SRVConnectorClass)
        if self.state_logger:
            self.state_logger.report_connected(msrp)
        return msrp

    def _listen(self, local_uri, handler, factory=None):
        from twisted.internet import reactor
        if factory is None:
            factory = SpawnFactory(handler, MSRPTransport, local_uri, traffic_logger=self.traffic_logger, state_logger=self.state_logger)
        listenFuncName = 'listen' + local_uri.protocol_name
        listenFuncArgs = (local_uri.port or 0, factory) + local_uri.protocolArgs
        port = getattr(reactor, listenFuncName)(*listenFuncArgs, **{'interface': local_uri.host})
        local_uri.port = port.getHost().port
        if self.state_logger:
            self.state_logger.report_listen(local_uri, port)
        return port

    def cleanup(self, sync=True):
        pass


class ConnectorDirect(ConnectBase):

    BOGUS_LOCAL_PORT = 12345

    def prepare(self, local_uri=None):
        if local_uri is None:
            local_uri = self.generate_local_uri(self.BOGUS_LOCAL_PORT)
        self.local_uri = local_uri
        return [self.local_uri]

    def getHost(self):
        return IPv4Address('TCP', default_host_ip, 0)

    def complete(self, full_remote_path):
        with MSRPConnectTimeout.timeout():
            msrp = self._connect(self.local_uri, full_remote_path[0])
            # can't do the following, because local_uri was already used in the INVITE
            #msrp.local_uri.port = msrp.getHost().port
        try:
            with MSRPBindSessionTimeout.timeout():
                msrp.bind(full_remote_path)
        except:
            msrp.loseConnection(sync=False)
            raise
        return msrp


class AcceptorDirect(ConnectBase):

    def prepare(self, local_uri=None):
        """Start listening for an incoming MSRP connection using port and
        use_tls from local_uri if provided.

        Return full local path, suitable to put in SDP a:path attribute.
        Note, that `local_uri' may be updated in place.
        """
        if local_uri is None:
            local_uri = self.generate_local_uri()
        self.transport_event = coros.event()
        local_uri.host = gethostbyname(local_uri.host)
        self.listening_port = self._listen(local_uri, self.transport_event.send)
        self.local_uri = local_uri
        return [local_uri]

    def getHost(self):
        return self.listener.getHost()

    def complete(self, full_remote_path):
        """Accept an incoming MSRP connection and bind it.
        Return MSRPTransport instance.
        """
        try:
            with MSRPIncomingConnectTimeout.timeout():
                msrp = self.transport_event.wait()
                if self.state_logger:
                    self.state_logger.report_accepted(msrp, self.local_uri)
        finally:
            self.cleanup()
        try:
            with MSRPBindSessionTimeout.timeout():
                msrp.accept_binding(full_remote_path)
        except:
            msrp.loseConnection(sync=False)
            raise
        return msrp

    def cleanup(self, sync=True):
        if self.listening_port is not None:
            self.listening_port.stopListening()
            self.listening_port = None
            self.transport_event = None


def _deliver_chunk(msrp, chunk):
    msrp.write(chunk.encode())
    with MSRPAuthTimeout.timeout():
        response = msrp.read_chunk()
    if response.method is not None:
        raise MSRPBadRequest
    if response.transaction_id!=chunk.transaction_id:
        raise MSRPBadRequest
    return response


class RelayConnectBase(ConnectBase):

    def __init__(self, relay, **kwargs):
        self.relay = relay
        ConnectBase.__init__(self, **kwargs)

    def _relay_connect(self, local_uri):
        conn = self._connect(local_uri, self.relay)
        try:
            local_uri.port = conn.getHost().port
            msrpdata = protocol.MSRPData(method="AUTH", transaction_id='%x' % random.getrandbits(64))
            msrpdata.add_header(protocol.ToPathHeader([self.relay.uri_domain]))
            msrpdata.add_header(protocol.FromPathHeader([local_uri]))
            response = _deliver_chunk(conn, msrpdata)
            if response.code == 401:
                www_authenticate = response.headers["WWW-Authenticate"]
                auth, rsp_auth = process_www_authenticate(self.relay.username, self.relay.password, "AUTH",
                                                          str(self.relay.uri_domain), **www_authenticate.decoded)
                msrpdata.transaction_id = '%x' % random.getrandbits(64)
                msrpdata.add_header(protocol.AuthorizationHeader(auth))
                response = _deliver_chunk(conn, msrpdata)
            if response.code != 200:
                raise MSRPRelayAuthError(comment=response.comment, code=response.code)
            conn.set_local_path(list(response.headers["Use-Path"].decoded))
            if self.state_logger:
                self.state_logger.report_session_reserved(conn)
        except:
            conn.loseConnection(sync=False)
            raise
        return conn

    def _relay_connect_timeout(self, local_uri):
        with MSRPRelayConnectTimeout.timeout():
            return self._relay_connect(local_uri)

    def prepare(self, local_uri=None):
        if local_uri is None:
            local_uri = self.generate_local_uri()
        self.msrp = self._relay_connect_timeout(local_uri)
        return self.msrp.full_local_path

    def getHost(self):
        return self.msrp.getHost()

    def cleanup(self, sync=True):
        if self.msrp is not None:
            self.msrp.loseConnection(sync=sync)
            self.msrp = None

class ConnectorRelay(RelayConnectBase):

    def complete(self, full_remote_path):
        try:
            with MSRPBindSessionTimeout.timeout():
                self.msrp.bind(full_remote_path)
            return self.msrp
        except:
            self.msrp.loseConnection(sync=False)
            raise
        finally:
            self.msrp = None

class AcceptorRelay(RelayConnectBase):

    def complete(self, full_remote_path):
        try:
            with MSRPBindSessionTimeout.timeout():
                self.msrp.accept_binding(full_remote_path)
            return self.msrp
        except:
            self.msrp.loseConnection(sync=False)
            raise
        finally:
            self.msrp = None

def get_connector(relay, **kwargs):
    if relay is None:
        return ConnectorDirect(**kwargs)
    return ConnectorRelay(relay, **kwargs)

def get_acceptor(relay, **kwargs):
    if relay is None:
        return AcceptorDirect(**kwargs)
    return AcceptorRelay(relay, **kwargs)

