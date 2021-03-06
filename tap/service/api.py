# coding=utf8
"""
Standard attribute in response:
    sys_timestamp:  unix_timestamp [int]
    sys_elapse:     [int]
    sys_error:      [string]
    sys_status:     same with http status code, [int]
"""

import os
import re
import sys
import time
import decimal
import datetime
import types
import random
import hashlib
try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

from dateutil import parser
from pyramid.view import view_config
from pyramid.response import Response
from pyramid.httpexceptions import HTTPNotFound
from pyramid.threadlocal import get_current_registry
import simplejson as json

try:
    import cx_Oracle
except ImportError:
    pass
try:
    import pyodbc
except ImportError:
    pass

from tap.service.common import conn_get, DBType
from tap.service.common import stmt_split
from tap.service.common import measure
from tap.service.common import dict2api
from tap.service.common import TapEncoder
from tap.service.common import cu
from tap.service.common import dbconn_ratio_parse
from tap.service.interpreter import ParaHandler, CFNInterpreter
from tap.service.cache import cache_fn, cache_fn1
from tap.service.auth import valid_key
from tap.service.exceptions import *
from tap.service.rpcstats import get_client
from tap.models import (
    DBSession,
    TapProject,
    TapApi,
    TapApiRelease,
)


# cache container
SOURCES_CONTAINER = {}


# debug signal
TAP_DEBUGINFO = int(os.environ.get('TAP_DEBUGINFO', '0'))


@view_config(route_name='api_run')
@view_config(route_name='api_run_v')
def main(request):
    """
    :param request:
    :return:
    """
    project_name = request.matchdict['project_name']
    api_name = request.matchdict['api_name']
    version = request.matchdict.get('version')

    version, config = load_config(project_name, api_name, version)
    config = dict2api(config)

    # run program
    try:
        result = Program(config, version).run(dict(request.params))
    except ApiAuthFail:
        result = dict(sys_status=403, sys_error="Auth Fail")
    status = result['sys_status']
    _tap_debuginfo(result)
    jresult = json.dumps(result, cls=TapEncoder)
    if 'jsonpCallback' in request.params:
        jsonp = request.params['jsonpCallback']
        jresult = jsonp + '(%s)' % jresult
    response = Response(jresult,
                        headerlist=[('Access-Control-Allow-origin', '*',)])
    response.content_type = "application/json"
    response.status = status
    return response


def _tap_debuginfo(result):
    """
    Remove debug info depend on TAP_DEBUGINFO environ variables
    :param result:
    :return:
    """
    if TAP_DEBUGINFO:
        return result

    removed_keys = []
    ignored_keys = ['sys_status']
    for key in result.keys():
        if key.startswith('sys_') and key not in ignored_keys:
            removed_keys.append(key)

    for key in removed_keys:
        del result[key]

    return result


@cache_fn1(int(os.environ.get('TAP_API_RELOAD', 1800)))
def load_config(project_name, api_name, version):
    if version:
        version = int(version)
        release = DBSession.query(TapApiRelease).filter_by(
            project_name=project_name,
            api_name=api_name, version=version
        ).first()
    else:
        project = DBSession.query(TapProject).filter_by(
            name=project_name).first()
        if not project:
            raise HTTPNotFound
        api = DBSession.query(TapApi).filter_by(project_id=project.id,
                                                name=api_name).first()
        if not api:
            raise HTTPNotFound
        release = DBSession.query(TapApiRelease)\
            .filter_by(api_id=api.id)\
            .order_by(TapApiRelease.version.desc())\
            .limit(1).first()
    if not release:
        raise HTTPNotFound
    config = json.loads(release.content)
    return release.version, config


def val_universal(val, dbtype):
    if isinstance(val, str):
        return cu(val)
    elif not val:
        return val
    elif isinstance(val, (long, decimal.Decimal, float, datetime.date,
                          unicode)):
        return val

    if dbtype == 'ORACLE':
        if isinstance(val, cx_Oracle.LOB):
            val = val.read()
            return cu(val)
    return val


class ConnectionManager(object):
    def __init__(self, conns, secondary_conns, load_ratios=None):
        """

        :param conns: Primary connections
        :param secondary_conns: Secondary connections
        :param load_ratios: Load balance ratios
        """
        self.config_conns = conns
        self.config_secondary_conns = secondary_conns
        self.config_load_ratios = load_ratios

        # Default connection
        self._default_cursor = None
        self._default_connection = None
        self._default_db_name = None
        self._default_db_type = None

        # Named connections
        # db_name: [dbtype, secondary_choose_name, connection, cursor]
        self._connections = {}

        # Random connection choose result
        self._conn_ratio_result = None

    def _secondary_choose(self, db_cfg):
        """
        Choose a database connection config by load balance configuration
        :param db_cfg:
        :return:
        """
        # No primary config
        if not self.config_secondary_conns:
            return db_cfg

        if not self._conn_ratio_result:
            self._conn_ratio_result = dbconn_ratio_parse(self.config_load_ratios)
            for db_choice in self._conn_ratio_result.values():
                start = 0
                for db, ratio in db_choice.items():
                    db_choice[db] = [start + 1, start + ratio]
                    start = start + ratio

        # 生成 1 - 100 的随机数
        # Generate 1 - 100 random number
        choice = random.randint(1, 100)
        db_choice = self._conn_ratio_result[db_cfg.name]
        db_choose = None
        for db, ratio in db_choice.items():
            if ratio[0] <= choice <= ratio[1]:
                db_choose = db
                break

        for conn in self.config_conns:
            if conn.name == db_choose:
                return conn

        for conn in self.config_secondary_conns:
            if conn.name == db_choose:
                return conn

        raise Exception("Can't choose database for %s by config." % db_cfg.name)

    def _init_default(self):
        # No config, return None
        if not self.config_conns:
            return self._default_cursor

        # Choose connection
        cfg = self.config_conns[0]
        conn_cfg = self._secondary_choose(cfg)
        self._default_db_name = conn_cfg.name

        self._default_connection = conn_get(
            conn_cfg.dbtype, conn_cfg.connstring, conn_cfg.options)
        self._default_cursor = self._default_connection.cursor()
        self._default_db_type = conn_cfg.dbtype

        self._connections[cfg.name] = (
            conn_cfg.dbtype, conn_cfg.name,
            self._default_connection, self._default_cursor
        )

    def _init_connection(self, db_name):
        if not self.config_conns:
            return None

        for _conn in self.config_conns[1:]:
            if db_name != _conn.name:
                continue
            _conn = self._secondary_choose(_conn)
            conn = conn_get(
                _conn.dbtype,
                _conn.connstring,
                _conn.options
            )
            self._connections[db_name] = (
                _conn.dbtype, _conn.name, conn, conn.cursor()
            )

    def _get_conn(self, dbname):
        if dbname in self._connections:
            conn = self._connections[dbname]
        else:
            self._init_connection(dbname)
            conn = self._connections[dbname]
        return conn


    @property
    def default_cursor(self):
        if not self._default_cursor:
            self._init_default()
        return self._default_cursor

    @property
    def default_dbtype(self):
        if not self._default_cursor:
            self._init_default()
        return self._default_db_type

    @property
    def default_connection(self):
        if not self._default_connection:
            self._init_default()
        return self._default_cursor

    def connection(self, dbname):
        conn = self._get_conn(dbname)
        return conn[2]

    def connection_type(self, dbname):
        conn = self._get_conn(dbname)
        return conn[0]

    def cursor(self, dbname):
        conn = self._get_conn(dbname)
        return conn[3]

    def close(self):
        for conn in self._connections.values():
            conn = conn[2]
            conn.close()


class ModuleManager(object):
    modules = {}

    module_dir = None

    @classmethod
    def init_dir(cls):
        # Module directory
        module_dir = get_current_registry().settings['module_dir']
        if not os.path.isdir(module_dir):
            os.makedirs(module_dir)
            open(os.path.join(module_dir, '__init__.py'), 'w').write("")
        sys.path.append(module_dir)
        cls.module_dir = module_dir

    @classmethod
    def get_module(cls, source, paras):
        """
        :param source: Source code string
        :param paras: para names list
        :return: 
        """
        if not cls.module_dir:
            cls.init_dir()
        module_dir = cls.module_dir

        paras.sort()
        md5 = hashlib.md5( '%s%s' % (source, repr(paras))).hexdigest()
        lib_name = "lib_%s" % md5

        if lib_name in cls.modules:
            return cls.modules[lib_name]

        paras = str(", ".join(paras))
        source = re.sub(r"^def[\s\t\b]+main[\s\t\b]*\(", "def main(%s" % paras, source.strip())
        path = os.path.join(module_dir, lib_name + ".py")
        source = "# coding=utf8\n\n" + source
        open(path, 'w').write(source)

        mod = __import__(lib_name)
        cls.modules[lib_name] = mod
        return mod


class Program(object):
    def __init__(self, config, ver_num):
        """
        :param config: api config
        :param ver_num: api version number
        :return:
        """
        self.config = config
        self.ver_num = ver_num

        self.conn = ConnectionManager(config.dbconn, config.dbconn_secondary,
                                      config.dbconn_ratio)

        self.rpc_client = None

    def _has_write(self, statement):
        statement = statement.strip().lower()
        if (statement.startswith('update') or statement.startswith('insert')
                or statement.startswith('create')
                or statement.startswith('delete')):
            return True
        return False

    def _source_prepare_repl(self, source, paras):
        """
        handling replace variable binding, @@name
        :param source:
        :param paras:
        :return:
        """
        for name, value in paras.items():
            reg_name = ur'([^\w])@@%s\b' % name
            source = re.sub(reg_name, ur'\1 %s ' % unicode(value), source)
        return source

    def _source_prepare(self, source, paras, dbtype):
        """
        处理代码中: 值绑定，参数绑定
        :param source:
        :param paras:
        :param dbtype:
        :return:
        """
        if dbtype == 'MSSQL':
            return self._source_prepare_pyodbc(source, paras, dbtype)

        # reg_name = ur'([^\w])@([\w+])\b'
        # if dbtype in ('MYSQL', 'PGSQL'):
        #     source = re.sub(reg_name, ur'\1%(\2)s', source)
        # elif dbtype == 'ORACLE':
        #     source = re.sub(reg_name, ur'\1:\2', source)
        # # TODO 怎么处理paras
        # return source, paras

        use_paras = []
        for name in paras.keys():
            reg_name = ur'([^\w])@%s\b' % name
            _source = source
            if dbtype in ('MYSQL', 'PGSQL'):
                source = re.sub(reg_name, ur'\1%%(%s)s' % name, source)
            elif dbtype == 'ORACLE':
                source = re.sub(reg_name, ur'\1:%s' % name, source)
            # check is source changed
            if _source != source:
                use_paras.append(name)
        paras = dict((k.encode('utf8'), paras[k]) for k in use_paras)
        return source, paras

    def _source_prepare_pyodbc(self, source, paras, dbtype):
        # pyodbc 不能执行 name binding
        para_position = {}
        positions = []
        for name in paras.keys():
            # 找出参数名在代码中出现的所有位置
            matches = re.finditer(
                ur'@%s\b' % name,
                source)
            index = [m.start() for m in matches]
            positions.extend(index)
            for idx in index:
                para_position[idx] = name

        for name in paras.keys():
            reg_name = ur'([^\w])@%s\b' % name
            source = re.sub(reg_name, ur'\1?', source)

        positions.sort()
        paras = [paras[para_position[i]] for i in positions]
        return source, tuple(paras)

    def run(self, paras):
        stats = dict(api_id=str(self.config.id),
                     project_id=str(self.config.project_id),
                     client_id='')
        with measure() as time_used:
            try:
                if self.config.auth_type == 'AUTH':
                    access_allow, client_id = valid_key(
                        paras.get('access_key'), self.config)
                    stats['client_id'] = str(client_id or '')
                    if not access_allow:
                        raise ApiAuthFail

                func = self.run_stmts
                if self.config.source.source_type == 'PYTHON':
                    func = self.run_python
                paras = ParaHandler.prepare(paras, self.config.paras)
                result = cache_fn(self.config, self.ver_num, func, paras)
                result['sys_status'] = 200
            except BaseException as e:
                import traceback
                trace = traceback.format_exc()
                self.report_stats_exc(stats, str(e), trace)
                result = dict(
                    sys_elapse=[],
                    data=[],
                    sys_error=cu('[%s]: %s' % (type(e).__name__, str(e))),
                    sys_trace=trace,
                    sys_status=500
                )
                if e.__class__ == ApiAuthFail:
                    result['sys_status'] = 403
            try:
                # TODO does it really need
                self.conn.default_cursor.execute("commit")
            except:
                pass

        time_used = time_used()
        stats['elapse'] = str(time_used)
        self.report_stats(stats)

        if isinstance(result, dict):
            elapse = result['sys_elapse']
            elapse.append(['TOTAL', time_used])
            result['sys_timestamp_current'] = time.time()

        return result

    def run_python(self, paras):
        """
        Run python scripts
        :param paras:
        :return: dict
        """
        elapse = []
        result = OrderedDict(
            sys_elapse=elapse
        )
        func_main = None
        with measure() as time_total:
            variables = {}
            for k, v in paras.items():
                if k in ('main', '__builtins__'):
                    continue
                variables[k] = v
            source = self.config.source.source.encode('utf8')
            source = source.replace('\r', '')
            # source = ('#coding=utf8\r\n%s' % source)
            # hash_source = hash(source)

            # if hash_source in SOURCES_CONTAINER:
            #     container = SOURCES_CONTAINER[hash_source]
            # else:
            #     exec source in container
            #     SOURCES_CONTAINER[hash_source] = container

            # container.update(variables)

            # assign database connections
            variables['g_cursor'] = self.conn.default_cursor
            variables['g_result'] = result
            for dbcfg in self.config.dbconn:
                name = dbcfg.name
                variables['g_cursor_%s' % str(name)] = self.conn.cursor(name)
            func_main = ModuleManager.get_module(source, variables.keys()).main

        elapse.append(['COMPILE', time_total()])

        with measure() as time_total:
            data = func_main(**variables)
            if data:
                result['data'] = [[val_universal(v, None) for v in row]
                                   for row in data]
            else:
                pass
                # result['data'] = []
        elapse.append(['EXECUTION TOTAL', time_total()])

        result['sys_timestamp_exec'] = time.time()

        return result

    def run_stmts(self, paras):
        """
        Run SQL statements
        :param paras:
        :return:
        """
        elapse = []  # num, elapse
        final_result = None

        result = OrderedDict()
        with measure() as time_total:
            final_result = self.run_stmts_step(
                elapse, final_result, paras, result)
        elapse.append(['EXECUTION TOTAL', time_total()])

        if final_result:
            result['data'] = final_result
        result['sys_elapse'] = elapse
        result['sys_timestamp_exec'] = time.time()

        return result

    def run_stmts_step(self, elapse, final_result, paras, result):
        # Prepare database connection
        source = self.config.source.source
        charset, source = self.source_charset(source)
        if len(self.config.dbconn) == 0:
            raise Exception("Please set a database")
        writable = self.config.writable
        stmts = stmt_split(source)
        _last_cursor = None
        for i in range(len(stmts)):
            stmt = stmts[i]
            with measure() as time_used:
                ex_result = self.run_stmt(
                    stmt, paras, writable, charset, result, elapse)
                _last_cursor, code_info, _last_dbtype, has_data = ex_result
            elapse.append(['ST.%s' % i, time_used()])
        if _last_cursor and has_data:
            # using the last cursor to get final data
            final_result = self.fetch_result(
                _last_cursor, 'data', _last_dbtype, elapse
            )
        return final_result

    def fetch_result(self, cursor, name, dbtype, elapse):
        with measure() as time_cu:
            result = []
            if cursor.description:
                rows = cursor.fetchall()
                if rows:
                    rows = [[val_universal(v, dbtype) for v in row]
                            for row in rows]
                cols = [col[0] for col in cursor.description]
                result.append(cols)
                result.extend(rows)
        elapse.append(['UNIVERSAL_CHR-%s' % name, time_cu()])
        return result

    def run_stmt(self, stmt, paras, writable, charset, result, elapse):
        """

        :param stmt: Source code statement
        :param paras:  Parameters
        :param writable:
        :param charset:
        :param result:
        :param elapse:
        :return: cursor, code_info, dbtype
        """
        stmt = stmt.strip(u';')

        code_info = CFNInterpreter.parse_one(stmt)
        has_data = False

        # fn_case
        if self.run_stmt_case(code_info, paras) is not True:
            return self.conn.default_cursor, code_info, self.conn.default_dbtype, has_data

        # fn_bind_var: fn_bind_var can't mix with other functions and can't
        #              have sql scripts followed
        if code_info.bind_var:
            self.run_stmt_bind_var(code_info, paras, result)
            # return self.conn.default_cursor, code_info, self.conn.default_dbtype, has_data

        # writable check
        if not writable and self._has_write(code_info.script):
            raise TapNotAllowWrite

        # fn_dbswitch: Choose database
        cursor, dbtype = self.run_stmt_dbswitch(code_info)

        # Handle the % characters
        if dbtype in ('MYSQL', 'PGSQL'):
            stmt = code_info.script.replace(u'%', u'%%')
        else:
            stmt = code_info.script

        # Prepare sql
        if not stmt:
            return cursor, code_info, dbtype, has_data

        stmt = self._source_prepare_repl(stmt, paras)
        stmt, para = self._source_prepare(stmt, paras, dbtype)

        if charset:
            stmt = stmt.encode(charset)

        # Execute sql
        if para:
            cursor.execute(stmt, para)
        else:
            cursor.execute(stmt)
        has_data = True

        # Fetch data
        data = None
        if code_info.bind_obj or code_info.bind_tab or code_info.export:
            data = self.fetch_result(cursor, code_info.bind_tab, dbtype, elapse)
            has_data = False

        # fn_export: export variables
        self.run_stmt_export(code_info, paras, data)

        # fn_bind_tab:
        if code_info.bind_tab:
            assert code_info.bind_tab != 'data', "Don't bind result to `data`"
            result[code_info.bind_tab] = data
        # fn_bind_obj:
        elif code_info.bind_obj:
            self.run_stmt_bind_obj(code_info, data, result)

        return cursor, code_info, dbtype, has_data

    def run_stmt_bind_obj(self, code_info, data, result):
        if len(data) >= 2:
            data = dict(zip(data[0], data[1]))
        else:
            fill_none = [None] * len(code_info.bind_obj)
            data = dict(zip(code_info.bind_obj, fill_none))
        for k, v in data.items():
            if k not in code_info.bind_obj:
                continue
            if k != 'data' and not k.startswith('sys_'):
                result[k] = v

    def run_stmt_dbswitch(self, code_info):
        if code_info.db_name:
            cursor = self.conn.cursor(code_info.db_name)
            dbtype = self.conn.connection_type(code_info.db_name)
        else:
            cursor = self.conn.default_cursor
            dbtype = self.conn.default_dbtype

        return cursor, dbtype

    def run_stmt_bind_var(self, code_info, paras, result):
        """
        fn_bind_var: fn_bind_var('var', 'test value')
                     fn_bind_var('var', @para_name)
        :param code_info:
        :param paras:
        :return:
        """
        bind_var = code_info.bind_var

        # Search symbol @ and replace it
        start_search = False
        first_quote = None
        second_quote = None
        for i in range(len(bind_var)):
            char = bind_var[i]
            if not first_quote:
                if char in ('"', "'"):
                    first_quote = char
                continue
            if not start_search:
                if char == first_quote: 
                    start_search = True
                continue
            # No symbol @
            if not second_quote and char in ('"', "'"):
                break
            # Has symbol @
            if char == '@':
                bind_var = list(bind_var)
                bind_var[i] = ""
                bind_var = "".join(bind_var)
                break

        vals = eval(bind_var, paras)
        result[vals[0]] = vals[1]

    def run_stmt_case(self, code_info, paras):
        """
        fn_case check
        Support: >, <, >=, <=, ==, and, or
        :return:
        """
        if not code_info.case_statement:
            return True

        case_statement = code_info.case_statement
        for para_name in paras.keys():
            if para_name in ('and', 'or'):
                continue
            case_statement = re.sub(ur'\b%s\b' % para_name, "paras['%s']" %
                                    para_name, case_statement)
        result = eval(case_statement)
        # print case_statement, result
        return result

    def run_stmt_export(self, code_info, paras, data):
        """
        fn_export, 导出变量
        :param code_info:
        :param paras:
        :return:
        """
        if not code_info.export:
            return

        if not data or len(data) < 2:
            # script = code_info.script
            # script = (script.encode('utf8') if isinstance(script, unicode)
            #           else script)
            # raise Exception("fn_export failed: %s" % script)
            for name in code_info.export:
                paras[name] = None
                return

        cols = data[0]
        row = data[1]
        if not row:
            # script = code_info.script
            # script = (script.encode('utf8') if isinstance(script, unicode)
            #           else script)
            # raise Exception("fn_export failed: %s" % script)
            for name in code_info.export:
                paras[name] = None
                return

        row = dict(zip(cols, row))
        for name in code_info.export:
            if name not in row:
                # raise Exception("fn_export failed: not found field %s" % name)
                paras[name] = None
            else:
                paras[name] = row[name]

    def report_stats_exc(self, stats, exc_message, exc_trace):
        exc_type, exc_value, tb = sys.exc_info()
        context = None
        if tb is not None:
            prev = tb
            curr = tb.tb_next
            while curr is not None:
                prev = curr
                curr = curr.tb_next
            context = prev.tb_frame.f_locals
            context['paras'] = tb.tb_frame.f_locals['paras']
            for k, v in context.items():
                if not isinstance(v, (str, unicode, long, decimal.Decimal,
                                      datetime.date, types.NoneType)):
                    v = repr(v)
                    if len(v) > 3000:
                        v = v[:3000]
                    context[k] = v
        context = json.dumps(context, cls=TapEncoder)
        stats['exc_type'] = str(exc_type)
        stats['exc_message'] = exc_message
        stats['exc_trace'] = exc_trace
        stats['exc_context'] = str(context)

    def report_stats(self, stats):
        try:
            if not self.rpc_client:
                self.rpc_client = get_client()
            self.rpc_client.report(stats)
        except:
            import traceback
            traceback.print_exc()

    def source_charset(self, source):
        charset = re.findall(ur'^\!charset\=(\w+)', source)
        if len(charset) == 1:
            charset = charset[0]
            return charset, re.sub(ur'\!charset\=(\w+)', '', source)
        return None, source

