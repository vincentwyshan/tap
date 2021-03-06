#coding=utf8

from __future__ import division

import time
import copy
import random
import datetime
import socket
import warnings
import threading
from functools import wraps
from optparse import OptionParser

from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer
from thrift.transport.TTransport import TTransportException
from thrift.Thrift import TApplicationException

import transaction

from tap.servicedef import TapService
from tap.scripts import init_session_from_cmd
from tap.scripts.dbtools import initdb
from tap.models import (
    DBSession,
    TapApi,
    TapApiStats,
    TapApiErrors,
)


STATS_ELAPSE = {}
# all-client, CLIENT_ID is -1
# (API_ID, CLIENT_ID): {
#     elapse_max,
#     elapse_min,
#     elapse_avg,
#     occurrence_total,
#     elapse_sum,
#     exception_total
# }

STATS_EXC = {}
# (API_ID, CLIENT_ID, exc_trace): {
#     exc_type,
#     exc_message,
#     exc_context,
#     occurrence_total,
#     occurrence_first,
#     occurrence_last
# }

# COUNTER = 0


class Handler(object):
    def ping(self):
        print 'ping'

    def report(self, params):
        """
        report api access information
        :param params-api_id:
        :param params-client_id: optional
        :param params-elapse:
        :param params-exc_type: optional
        :param params-exc_message: optional
        :param params-exc_trace: optional
        :param params-exc_context: optional, json
        """
        # testing
        # global COUNTER
        # COUNTER += 1
        # if COUNTER % 1000 == 0:
        #     print 'COUNTER:', COUNTER

        # 访问量和耗时统计(按 api_id)
        api_id = params['api_id']

        clients = [-1]
        # check clients
        client_id = params.get('client_id', None)
        if client_id:
            clients.append(client_id)

        # unload params data
        for client_id in clients:
            # initialize
            if (api_id, client_id) not in STATS_ELAPSE:
                STATS_ELAPSE[(api_id, client_id)] = dict(
                    elapse_max=0, elapse_min=0, elapse_avg=0,
                    occurrence_total=0, elapse_sum=0, exception_total=0
                )

            elapse = STATS_ELAPSE[(api_id, client_id)]
            elapse_now = float(params['elapse'])
            elapse['elapse_sum'] += elapse_now
            elapse['occurrence_total'] += 1
            if 'exc_trace' in params:
                elapse['exception_total'] += 1
            if elapse_now > elapse['elapse_max']:
                elapse['elapse_max'] = elapse_now
            elif (elapse['elapse_min'] == 0
                  or elapse_now < elapse['elapse_min']):
                elapse['elapse_min'] = elapse_now

            elapse['elapse_avg'] = elapse['elapse_sum'] / elapse['occurrence_total']

        # print 'ELAPSE:', len(STATS_ELAPSE)
        if 'exc_type' not in params:
            return

        # 出错统计
        exc_type = params['exc_type']
        exc_trace = params['exc_trace']
        exc_message = params['exc_message']
        exc_context = params['exc_context']

        # unload errors in params
        for client_id in clients:
            key = (api_id, client_id, exc_trace)
            if key not in STATS_EXC:
                STATS_EXC[key] = dict(
                    exc_type=exc_type,
                    exc_message=exc_message,
                    exc_context=exc_context,
                    occurrence_total=0,
                    occurrence_first=datetime.datetime.now(),
                    occurrence_last=None
                )
            STATS_EXC[key]['occurrence_total'] += 1
            STATS_EXC[key]['occurrence_last'] = datetime.datetime.now()


def flush_log(occurrence_time):
    r_stats, r_exc = 0, 0
    global STATS_ELAPSE, STATS_EXC
    # print 'NO client:', len(STATS_ELAPSE)
    all_stats = ([(key[0], key[1], elapse) for key, elapse in
                  STATS_ELAPSE.items()])
    STATS_ELAPSE = {}
    # print occurrence_time, 'ELAPSE:', len(STATS_ELAPSE)
    # print occurrence_time, 'Stats elapse:', len(all_stats)
    for api_id, client_id, elapse in all_stats:
        flush_log_stats(occurrence_time, api_id, client_id, elapse)
        r_stats += 1

    # 更新 错误统计
    all_exc = [(key[0], key[1], key[2], exc) for key, exc in
               STATS_EXC.items()]
    STATS_EXC = {}
    # print 'Stats exceptions:', len(all_exc)
    for api_id, client_id, exc_trace, exc in all_exc:
        flush_log_exc(occurrence_time, api_id, client_id, exc_trace, exc)
        r_exc += 1

    return r_stats, r_exc


def flush_log_exc(occurrence_time, api_id, client_id, exc_trace, exc):
    hash_id = unicode(hash(exc_trace))
    with transaction.manager:
        api = DBSession.query(TapApi).get(api_id)
        if not api:
            warnings.warn("API:%s is not exist." % api_id)
            return

        q = DBSession.query(TapApiErrors)\
            .filter_by(api_id=api_id, client_id=client_id, hash_id=hash_id)
        stats = q.first()
        if not stats:
            stats = TapApiErrors(api_id=api_id, client_id=client_id,
                                 project_id=api.project_id, hash_id=hash_id,
                                 occurrence_total=0,
                                 occurrence_time=occurrence_time,
                                 occurrence_first=exc['occurrence_first'])
            DBSession.add(stats)
            DBSession.flush()

        print '\tName:', api.name, ', ClientId:', client_id, ', Error:', \
            exc['exc_type'], 'Occurrence:', exc['occurrence_total']
        stats = DBSession.query(TapApiErrors).with_lockmode('update')\
            .filter(TapApiErrors.id==stats.id).first()

        stats.occurrence_time = occurrence_time
        # stats.occurrence_time = exc['occurrence_time']
        stats.occurrence_total += exc['occurrence_total']
        stats.exc_type = exc['exc_type']
        stats.exc_message = exc['exc_message']
        stats.exc_trace = exc_trace
        stats.exc_context = exc['exc_context']
        if exc['occurrence_first'] < stats.occurrence_first:
            stats.occurrence_first = exc['occurrence_first']
        if (not stats.occurrence_last
            or exc['occurrence_last'] > stats.occurrence_last):
            stats.occurrence_last = exc['occurrence_last']


def flush_log_stats(occurrence_time, api_id, client_id, elapse):
    with transaction.manager:
        api = DBSession.query(TapApi).get(api_id)
        if not api:
            warnings.warn("API:%s is not exist." % api_id)
            return

        q = DBSession.query(TapApiStats)\
            .filter_by(api_id=api_id, occurrence_time=occurrence_time,
                       client_id=client_id)
        stats = q.first()
        if not stats:
            stats = TapApiStats(api_id=api_id, project_id=api.project_id,
                                occurrence_time=occurrence_time,
                                client_id=client_id)
            DBSession.add(stats)
            DBSession.flush()

        # row-lock update
        print '\tName:', api.name, ', ClientId:', client_id, \
            ', Occurrence:', elapse['occurrence_total']
        stats = DBSession.query(TapApiStats).with_lockmode('update')\
            .filter(TapApiStats.id == stats.id).first()

        stats.occurrence_time = occurrence_time
        stats.occurrence_total += elapse['occurrence_total']
        stats.exception_total += elapse['exception_total']
        if stats.elapse_max < elapse['elapse_max']:
            stats.elapse_max = elapse['elapse_max']
        if stats.elapse_min < elapse['elapse_min']:
            stats.elapse_min = elapse['elapse_min']
        stats.elapse_sum += elapse['elapse_sum']
        # print stats.occurrence_total, elapse
        stats.elapse_avg = stats.elapse_sum / stats.occurrence_total


def get_1day_intervals(ivalue):
    """
    Get all interval time point for 1 day
    :param ivalue:
    :return:
    """
    assert ivalue in ['1M', '5M', '10M', '30M', '1H']

    today = datetime.date.today()
    start = datetime.datetime(today.year, today.month, today.day)
    interval = 60
    if ivalue == '1M':
        pass
    elif ivalue == '5M':
        interval = 60 * 5
    elif ivalue == '10M':
        interval = 60 * 10
    elif ivalue == '30M':
        interval = 60 * 30
    elif ivalue == '1H':
        interval = 60 * 60

    _actpoints = []
    while (start.year == today.year and start.month == today.month and
               start.day == today.day):
        _actpoints.append(
            (start.hour, start.minute, start.second)
        )
        start += datetime.timedelta(seconds=interval)
    return _actpoints


def interval_flush(ivalue, run_event):
    while 1:
        try:
            interval_flush_worker(ivalue, run_event)
            if not run_event.is_set():
                return
        except:
            print '\n\n', '*' * 100
            print '[', datetime.datetime.now(), ']'
            import traceback
            traceback.print_exc()
            time.sleep(10)


def interval_flush_worker(ivalue, run_event):
    intervals = get_1day_intervals(ivalue)

    _intervals = copy.deepcopy(intervals)
    time_awake = None
    r_stats, r_exc = 0, 0
    while True:
        if not run_event.is_set():
            return
        try:
            if time_awake:
                occurrence_time = datetime.datetime(
                    time_awake.year, time_awake.month, time_awake.day, 
                    time_awake.hour, time_awake.minute)
                r_stats, r_exc = flush_log(occurrence_time)
        except:
            import traceback
            traceback.print_exc()
        finally:
            time.sleep(1)

        # Calculate the awake time and sleep seconds
        now = datetime.datetime.now()
        now_tuple = (now.hour, now.minute, now.second)
        interval = None
        if _intervals:
            interval_tuple = _intervals.pop(0)
            while _intervals:
                if now_tuple > interval_tuple:
                    interval_tuple = _intervals.pop(0)
                    continue
                time_awake = datetime.datetime(
                    now.year, now.month, now.day,
                    interval_tuple[0], interval_tuple[1], interval_tuple[2]
                )
                interval = (time_awake - now).seconds
                break
        # No interval, go to next day
        if interval is None:
            nextday = now + datetime.timedelta(days=1)
            _intervals = copy.deepcopy(intervals)
            interval_tuple = _intervals.pop(0)
            time_awake = datetime.datetime(
                nextday.year, nextday.month, nextday.day, interval_tuple[0],
                interval_tuple[1], interval_tuple[2]
            )
            interval = (time_awake - now).seconds

        print '[%s:%s] stats: %s, exceptions: %s' % (ivalue, now, r_stats, r_exc)
        time.sleep(interval)


def client_ensure(func):
    """
    this wraper make sure initialize a new client when connect thrift server
    failed. wraped thrift Client must have a new_client function to initialize
    transport connection.
    """
    @wraps(func)
    def wraper(*kargs, **kwarg):
        try:
            return func(*kargs, **kwarg)
        except (TTransportException, TApplicationException, socket.error):
            self = kargs[0]
            self.new_client()
        except BaseException as e:
            # print e
            import traceback
            traceback.print_exc()
            self = kargs[0]
            self.new_client()
    return wraper


PORT = 10101


class ClientPool(object):
    def __init__(self, host, pool_size=30):
        self.pool_size = pool_size
        self.lock = threading.RLock()
        self.host = host

        self.pool = []

    def connect(self):
        return Client(self)

    def _create_client(self):
        transport = TSocket.TSocket(self.host, PORT)
        # Time out 2 seconds
        transport.setTimeout(1000*2)
        transport = TTransport.TBufferedTransport(transport)

        # Wrap in a protocol
        protocol = TBinaryProtocol.TBinaryProtocol(transport)

        # Create a client to use the protocol encoder
        client = TapService.Client(protocol)

        transport.open()

        return client

    def check_in(self, client):
        # print "check in:", id(client), len(self.pool)
        self.lock.acquire()
        try:
            while len(self.pool) >= self.pool_size:
                self.pool.pop(0)
            self.pool.append(client)
        finally:
            self.lock.release()

    def check_out(self):
        self.lock.acquire()
        try:
            client = None
            if len(self.pool) > 0:
                client = self.pool.pop(0)
            if not client:
                client = self._create_client()
            # print time.time(), "check out:", id(client)
            return client
        finally:
            self.lock.release()


class Client(object):
    def __init__(self, pool):
        self.client = pool.check_out()
        self.pool = pool

    @client_ensure
    def ping(self):
        self.client.ping()

    def new_client(self):
        self.client = self.pool.check_out()

    @client_ensure
    def report(self, params):
        self.client.report(params)

    def close(self):
        self.pool.check_in(self.client)
        self.pool = None

    def __del__(self):
        if hasattr(self, 'pool') and self.pool:
            self.pool.check_in(self.client)


def run_server():
    processor = TapService.Processor(Handler())
    transport = TSocket.TServerSocket(host="0.0.0.0", port=PORT)
    tfactory = TTransport.TBufferedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    #server = TServer.TSimpleServer(processor, transport, tfactory, pfactory)

    # You could choose one of these for a multiple threaded server
    server = TServer.TThreadedServer(processor, transport, tfactory, pfactory)
    #server = TServer.TThreadPoolServer(processor, transport, tfactory, pfactory)

    print 'Starting the server...'
    server.serve()
    print 'Starting done.'


POOLS = {}


def get_client(host='127.0.0.1'):
    if host not in POOLS:
        POOLS[host] = ClientPool(host)
    return POOLS[host].connect()


def main():
    usage = "usage: %prog production.ini [options]"
    parser = OptionParser(usage=usage)
    parser.add_option('-i', type="string", dest="interval", default="1M",
                      help="stats interval: [1M/5M/10M/30M/1H], default is 1M")
    (options, args) = parser.parse_args()

    # init_session_from_cmd()
    initdb()

    run_event = threading.Event()
    run_event.set()

    backend = threading.Thread(target=interval_flush,
                               args=(options.interval, run_event))
    backend.daemon = True
    backend.start()

    try:
        run_server()
    except KeyboardInterrupt:
        print "Waiting worker threads quit..."
        run_event.clear()
        backend.join()

