#coding=utf8

from pyramid.config import Configurator
from sqlalchemy import engine_from_config
from pyramid.authentication import AuthTktAuthenticationPolicy
from pyramid.authorization import ACLAuthorizationPolicy

from tap.models import (
    DBSession,
    Base,
    )




def main(global_config, **settings):
    """ This function returns a Pyramid WSGI application.
    """
    engine = engine_from_config(settings, 'sqlalchemy.', pool_recycle=1800)
    DBSession.configure(bind=engine)
    Base.metadata.bind = engine

    config = Configurator(settings=settings)

    from tap.security import groupfinder, get_user, get_user_id

    # attach shortcut property
    config.add_request_method(get_user, 'user', reify=True)
    config.add_request_method(get_user_id, 'userid', reify=True)

    # template
    config.include('pyramid_mako')
    config.add_mako_renderer('.html')

    # Security policies
    authn_policy = AuthTktAuthenticationPolicy(
        settings['tap.secret'], callback=groupfinder, hashalg='sha512')
    authz_policy = ACLAuthorizationPolicy()
    config.set_authentication_policy(authn_policy)
    config.set_authorization_policy(authz_policy)

    add_route(config)
    add_srv_route(config)

    config.scan()
    return config.make_wsgi_app()


def add_route(config):
    config.add_static_view('static', 'static', cache_max_age=3600)
    config.add_route('home', '/', factory='tap.security.AuthControl')
    config.add_route('login', '/management/login')
    config.add_route('database', '/management/database',
                     factory='tap.security.AuthControl')
    config.add_route('database_view', '/management/database/{dbconn_id}',
                     factory='tap.security.AuthControl')
    config.add_route('database_execute',
                     '/management/database/{dbconn_id}/execute',
                     factory="tap.security.AuthControl")
    config.add_route('action', '/management/action',
                     factory='tap.security.AuthControl')
    config.add_route('project', '/management/project',
                     factory='tap.security.AuthControl')
    config.add_route('project_detail', '/management/project/{project_id}',
                     factory='tap.security.AuthControl')
    config.add_route('api_config', '/management/api/{api_id}',
                     factory="tap.security.AuthControl")
    config.add_route('api_release', '/management/api/{api_id}/release',
                     factory="tap.security.AuthControl")
    config.add_route('api_stats', '/management/api/{api_id}/stats',
                     factory="tap.security.AuthControl")
    config.add_route('api_test', '/management/api-test',
                     factory="tap.security.AuthControl")
    config.add_route("client_home", "/management/client",
                     factory="tap.security.AuthControl")
    config.add_route("client_detail", "/management/client/{client_id}",
                     factory="tap.security.AuthControl")
    config.add_route("auth_home", "/management/api/{api_id}/auth",
                     factory="tap.security.AuthControl")
    config.add_route("auth_detail",
                     "/management/api/{api_id}/auth/{auth_id}/{client_id}",
                     factory="tap.security.AuthControl")
    config.add_route("permission_home", "/management/permission",
                     factory="tap.security.AuthControl")
    config.add_route("charts", "/management/charts")



def add_srv_route(config):
    config.add_route('authorize', '/api/authorize')
    config.add_route('api_run', '/api/{project_name}/{api_name}')
    config.add_route('api_run_v', '/api/{project_name}/{api_name}/v{version}')


def main_service(global_config, **settings):
    """ This function returns a Pyramid WSGI application.
    """
    engine = engine_from_config(settings, 'sqlalchemy.')
    DBSession.configure(bind=engine)
    Base.metadata.bind = engine

    config = Configurator(settings=settings)

    add_srv_route(config)

    config.scan()
    return config.make_wsgi_app()


