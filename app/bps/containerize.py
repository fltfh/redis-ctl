import time
import threading
import logging
from flask import request, render_template, g, abort
from sqlalchemy.exc import IntegrityError
from redistrib.connection import Connection

from app.bpbase import Blueprint
from app.utils import json_response
import models.cluster
import models.node
import models.proxy
import models.audit
import models.cont_image

bp = Blueprint('containerize', __name__, url_prefix='/containerize')


@bp.before_request
def access_control():
    if not bp.app.access_ctl_user_valid():
        abort(403)


@bp.route('/')
def manage_home():
    pods = bp.app.container_client.list_pods()
    if len(pods) == 0:
        return render_template('containerize/deploy/no_pod.html'), 400
    return render_template(
        'containerize/deploy/manage.html', pods=pods,
        clusters=models.cluster.list_all(),
        redis_images=models.cont_image.list_redis())


@bp.route('/nodes/')
def manage_redis():
    node_details = bp.app.polling_result()['nodes']
    nodes = models.node.list_eru_nodes(g.page * 20, 20)
    for n in nodes:
        n.detail = node_details.get('%s:%d' % (n.host, n.port), {})
    return render_template(
        'containerize/deploy/list_redis.html', page=g.page, nodes=nodes)


@bp.route('/proxies/')
def manage_proxy():
    return render_template(
        'containerize/deploy/list_proxies.html', page=g.page,
        proxies=models.proxy.list_eru_proxies(g.page * 20, 20))


@bp.route('/list_hosts/<pod>')
def list_pod_hosts(pod):
    pods = bp.app.container_client.list_pod_hosts(pod)
    return json_response([{
        'name': r['name'],
        'addr': r['addr'],
    } for r in pods if r['is_alive']])


@bp.route_post_json('/create_redis', True)
def create_redis():
    port = int(request.form.get('port', 6379))
    if not 6000 <= port <= 7999:
        raise ValueError('invalid port')
    container_info = bp.app.container_client.deploy_redis(
        request.form['pod'], request.form['aof'] == 'y',
        request.form['netmode'], request.form['cluster'] == 'y',
        host=request.form.get('host'), port=port,
        image=request.form.get('image'),
        micro_plan=request.form.get('micro_plan') == 'y')
    logging.debug('Container Redis deployed, info=%s', container_info)

    port = container_info.setdefault('port', port)
    try:
        models.node.create_eru_instance(container_info['address'], port,
                                        container_info['container_id'])
    except IntegrityError:
        if container_info is not None:
            bp.app.container_client.rm_containers(
                [container_info['container_id']])
        raise ValueError('exists')

    models.audit.eru_event(
        container_info['address'], port, models.audit.EVENT_TYPE_CREATE,
        bp.app.get_user_id(), request.form)
    return container_info


def _set_proxy_remote(proxy_addr, proxy_port, redis_host, redis_port):
    def set_remotes():
        time.sleep(1)
        with Connection(proxy_addr, proxy_port) as t:
            t.talk('SETREMOTES', redis_host, redis_port)
    threading.Thread(target=set_remotes).start()


@bp.route_post_json('/create_proxy', True)
def create_proxy():
    port = int(request.form.get('port', 8889))
    if not 8000 <= port <= 9999:
        raise ValueError('invalid port')
    cluster = models.cluster.get_by_id(int(request.form['cluster_id']))
    if cluster is None or len(cluster.nodes) == 0:
        raise ValueError('no such cluster')
    threads = 1
    micro_plan_cpu_slice = None
    if request.form.get('micro_plan') != 'y':
        threads = int(request.form['threads'])
    else:
        micro_plan_cpu_slice = int(request.form['cpu_slice'])
    container_info = bp.app.container_client.deploy_proxy(
        request.form['pod'], threads, request.form.get('read_slave') == 'rs',
        request.form['netmode'], host=request.form.get('host'),
        port=port, micro_plan_cpu_slice=micro_plan_cpu_slice)
    logging.debug('Container proxy deployed, info=%s', container_info)

    port = container_info.setdefault('port', port)
    try:
        models.proxy.create_eru_instance(
            container_info['address'], port, cluster.id,
            container_info['container_id'])
    except IntegrityError:
        if container_info is not None:
            bp.app.container_client.rm_containers(
                [container_info['container_id']])
        raise ValueError('exists')

    _set_proxy_remote(container_info['address'], port,
                      cluster.nodes[0].host, cluster.nodes[0].port)
    models.audit.eru_event(
        container_info['address'], port, models.audit.EVENT_TYPE_CREATE,
        bp.app.get_user_id(), request.form)
    return container_info


@bp.route_post('/revive')
def revive_container():
    bp.app.container_client.revive_container(request.form['id'])
    p = models.proxy.get_eru_by_container_id(request.form['id'])
    if p is not None:
        logging.info('Revive and setremotes for proxy %d, cluster #%d',
                     p.id, p.cluster_id)
        _set_proxy_remote(p.host, p.port, p.cluster.nodes[0].host,
                          p.cluster.nodes[0].port)
    return ''


#@base.post_async('/nodes/delete/eru')
@bp.route_post_json('/remove', True)
def remove_node():
    eru_container_id = request.form['id']
    if request.form['type'] == 'node':
        n = models.node.get_eru_by_container_id(eru_container_id)
        models.node.delete_eru_instance(eru_container_id)
    else:
        n = models.proxy.get_eru_by_container_id(eru_container_id)
        models.proxy.delete_eru_instance(eru_container_id)
    bp.app.container_client.rm_containers([eru_container_id])

    models.audit.eru_event(n.host, n.port, models.audit.EVENT_TYPE_DELETE,
                           bp.app.get_user_id())
