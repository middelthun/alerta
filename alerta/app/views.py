import json
import datetime

from collections import defaultdict
from functools import wraps
from flask import request, current_app, render_template, abort

from alerta.app import app, db, mq
from alerta.app.switch import Switch
from alerta.common import config
from alerta.common import log as logging
from alerta.common.alert import Alert
from alerta.common.heartbeat import Heartbeat
from alerta.common import status_code, severity_code
from alerta.common.utils import DateEncoder
from alerta.app.utils import parse_fields, crossdomain


Version = '3.0.0'

LOG = logging.getLogger(__name__)
CONF = config.CONF


# Over-ride jsonify to support Date Encoding
def jsonify(*args, **kwargs):
    return current_app.response_class(json.dumps(dict(*args, **kwargs), cls=DateEncoder,
                                                 indent=None if request.is_xhr else 2), mimetype='application/json')


def jsonp(func):
    """Wraps JSONified output for JSONP requests."""
    @wraps(func)
    def decorated_function(*args, **kwargs):
        callback = request.args.get('callback', False)
        if callback:
            data = str(func(*args, **kwargs).data)
            content = str(callback) + '(' + data + ')'
            mimetype = 'application/javascript'
            return current_app.response_class(content, mimetype=mimetype)
        else:
            return func(*args, **kwargs)
    return decorated_function


@app.route('/test', methods=['OPTIONS', 'PUT', 'POST', 'DELETE', 'GET'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def test():

    return jsonify(
        status="ok",
        method=request.method,
        json=request.json,
        data=request.data,
        args=request.args,
        app_root=app.root_path,
    )


@app.route('/', methods=['GET'])
def routes():

    rules = []
    for rule in app.url_map.iter_rules():
        rule.methods = ','.join([r for r in rule.methods if r not in ['OPTIONS', 'HEAD']])
        if rule.endpoint not in ['test', 'static']:
            rules.append(rule)
    return render_template('index.html', rules=rules)


@app.route('/api/alerts', methods=['GET'])
@jsonp
def get_alerts():

    try:
        query, sort, limit, query_time = parse_fields(request)
    except Exception, e:
        return jsonify(status="error", message=str(e))

    fields = dict()
    fields['history'] = {'$slice': CONF.history_limit}

    if 'status' not in query:
        query['status'] = 'open'

    alerts = db.get_alerts(query=query, fields=fields, sort=sort, limit=limit)
    total = db.get_count(query=query)  # because total may be greater than limit

    found = 0
    severity_count = defaultdict(int)
    status_count = defaultdict(int)

    alert_response = list()
    if len(alerts) > 0:

        last_time = None

        for alert in alerts:
            body = alert.get_body()
            found += 1
            severity_count[body['severity']] += 1
            status_count[body['status']] += 1

            if not last_time:
                last_time = body['lastReceiveTime']
            elif body['lastReceiveTime'] > last_time:
                last_time = body['lastReceiveTime']

            alert_response.append(body)

        return jsonify(
            status="ok",
            total=found,
            more=total > limit,
            alerts=alert_response,
            severityCounts=severity_count,
            statusCounts=status_count,
            lastTime=last_time,
            autoRefresh=Switch.get('auto-refresh-allow').is_on(),
        )
    else:
        return jsonify(
            status="error",
            message="not found",
            total=0,
            more=False,
            alerts=[],
            severityCounts=severity_count,
            statusCounts=status_count,
            lastTime=query_time,
            autoRefresh=Switch.get('auto-refresh-allow').is_on()
        )


@app.route('/api/alert', methods=['OPTIONS', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def create_alert():

    # Create a new alert
    try:
        incomingAlert = Alert.parse_alert(request.data)
    except ValueError, e:
        return jsonify(status="error", message=str(e))

    try:
        suppress = incomingAlert.transform_alert()
    except RuntimeError, e:
        # self.statsd.metric_send('alerta.alerts.error', 1)
        return jsonify(status="error", message=str(e))

    if suppress:
        LOG.info('Suppressing alert %s', incomingAlert.get_id())
        return jsonify(status="error", message="alert suppressed by transform")

    if db.is_duplicate(incomingAlert):
        # Duplicate alert .. 1. update existing document with lastReceiveTime, lastReceiveId, text,
        #                       value, status, tags and origin
        #                    2. increment duplicate count
        #                    3. update and push status if changed

        alert = db.save_duplicate(incomingAlert)

    elif db.is_correlated(incomingAlert):
        # Diff sev alert ... 1. update existing document with severity, createTime, receiveTime,
        #                       lastReceiveTime, previousSeverity,
        #                       severityCode, lastReceiveId, text, value, tags and origin
        #                    2. set duplicate count to zero
        #                    3. push history and status if changed

        alert = db.save_correlated(incomingAlert)

    else:
        # New alert so ... 1. insert entire document
        #                  2. push history and status
        #                  3. set duplicate count to zero

        LOG.info('%s : New alert -> insert', incomingAlert.get_id())
        alert = db.save_alert(incomingAlert)

    print alert
    if alert:
        return jsonify(status="ok", id=alert.id)
    else:
        return jsonify(status="error", message="alert insert or update failed")


@app.route('/api/alert/<id>', methods=['OPTIONS', 'GET'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def get_alert(id):

    alert = db.get_alert(id=id)

    if alert:
        return jsonify(status="ok", total=1, alert=alert.get_body())
    else:
        return jsonify(status="ok", message="not found", total=0, alert=None)


@app.route('/api/alert/<id>/status', methods=['OPTIONS', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def set_status(id):

    data = request.json

    if data and 'status' in data:
        modifiedAlert = db.set_status(id=id, status=data['status'], text=data.get('text', ''))

        # Forward alert to notify topic and logger queue
        # mq.send(modifiedAlert, CONF.outbound_topic)
        # LOG.info('%s : Alert forwarded to %s and %s', modifiedAlert.get_id(), CONF.outbound_queue, CONF.outbound_topic)

    else:
        return jsonify(status="error", message="no data")

    if modifiedAlert:
        return jsonify(status="ok")
    else:
        return jsonify(status="error", message="failed to set alert status")


@app.route('/api/alert/<id>/tag', methods=['OPTIONS', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def tag_alert(id):

    data = request.json

    if data and 'tags' in data:
        response = db.tag_alert(id, data['tags'])
    else:
        return jsonify(status="error", message="no data")

    if response:
        return jsonify(status="ok")
    else:
        return jsonify(status="error", message="failed to tag alert")


@app.route('/api/alert/<id>', methods=['OPTIONS', 'DELETE', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def delete_alert(id):

    if request.method == 'DELETE' or (request.method == 'POST' and request.json['_method'] == 'delete'):
        response = db.delete_alert(id)

        if response:
            return jsonify(status="ok")
        else:
            return jsonify(status="error", message="failed to delete alert")


# Return severity and status counts
@app.route('/api/alert/counts', methods=['GET'])
@jsonp
def get_counts():

    try:
        query, _, _, query_time = parse_fields(request)
    except Exception, e:
        return jsonify(response={"status": "error", "message": str(e)})

    counts = db.get_counts(query=query)

    found = 0
    severity_count = defaultdict(int)
    status_count = defaultdict(int)

    for count in counts:
        found += 1
        severity_count[count['severity']] += 1
        status_count[count['status']] += 1

    return jsonify(
        status="ok",
        total=found,
        more=False,
        severityCounts=severity_count,
        statusCounts=status_count,
        lastTime=query_time,
        autoRefresh=Switch.get('auto-refresh-allow').is_on()
    )


@app.route('/pagerduty', methods=['POST'])
def pagerduty():

    if not request.json or not 'messages' in request.json:
        abort(400)

    for message in request.json['messages']:

        LOG.debug('%s', json.dumps(message))

        id = message['data']['incident']['incident_key']
        html_url = message['data']['incident']['html_url']
        incident_number = message['data']['incident']['incident_number']
        incident_url = '<a href="%s">#%s</a>' % (html_url, incident_number)

        LOG.info('PagerDuty incident #%s webhook for alert %s', incident_number, id)

        LOG.error('previous status %s', db.get_alert(id=id).status)

        if message['type'] == 'incident.trigger':
            status = status_code.OPEN
            user = message['data']['incident']['assigned_to_user']['name']
            text = 'Incident %s assigned to %s' % (incident_url, user)
        elif message['type'] == 'incident.acknowledge':
            status = status_code.ACK
            user = message['data']['incident']['assigned_to_user']['name']
            text = 'Incident %s acknowledged by %s' % (incident_url, user)
        elif message['type'] == 'incident.unacknowledge':
            status = status_code.OPEN
            text = 'Incident %s unacknowledged due to timeout' % incident_url
        elif message['type'] == 'incident.resolve':
            status = status_code.CLOSED
            if message['data']['incident']['resolved_by_user']:
                user = message['data']['incident']['resolved_by_user']['name']
            else:
                user = 'n/a'
            text = 'Incident %s resolved by %s' % (incident_url, user)
        elif message['type'] == 'incident.assign':
            status = status_code.ASSIGN
            user = message['data']['incident']['assigned_to_user']['name']
            text = 'Incident %s manually assigned to %s' % (incident_url, user)
        elif message['type'] == 'incident.escalate':
            status = status_code.OPEN
            user = message['data']['incident']['assigned_to_user']['name']
            text = 'Incident %s escalated to %s' % (incident_url, user)
        elif message['type'] == 'incident.delegate':
            status = status_code.OPEN
            user = message['data']['incident']['assigned_to_user']['name']
            text = 'Incident %s reassigned due to escalation to %s' % (incident_url, user)
        else:
            status = status_code.UNKNOWN
            text = message['type']
            LOG.warn('Unknown PagerDuty message type: %s', message)

        LOG.info('PagerDuty webhook %s change status to %s', message['type'], status)

        pdAlert = db.update_status(id=id, status=status, text=text)
        db.tag_alert(id=id, tag='incident=#%s' % incident_number)

        LOG.error('returned status %s', pdAlert.status)
        LOG.error('current status %s', db.get_alert(id=id).status)

        # Forward alert to notify topic and logger queue
        if pdAlert:
            pdAlert.origin = 'pagerduty/webhook'
            mq.send(pdAlert, CONF.outbound_queue)
            mq.send(pdAlert, CONF.outbound_topic)
            LOG.info('%s : Alert forwarded to %s and %s', pdAlert.get_id(), CONF.outbound_queue, CONF.outbound_topic)

    return jsonify(status="ok")


# Return a list of heartbeats
@app.route('/api/heartbeats', methods=['GET'])
@jsonp
def get_heartbeats():

    heartbeats = db.get_heartbeats()
    hb_list = list()
    for hb in heartbeats:
        hb_list.append(hb.get_body())

    return jsonify(
        status="ok",
        total=len(heartbeats),
        heartbeats=hb_list,
        time=datetime.datetime.utcnow()
    )

@app.route('/api/heartbeat', methods=['OPTIONS', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def create_heartbeat():

    try:
        heartbeat = Heartbeat.parse_heartbeat(request.data)
    except ValueError, e:
        return jsonify(status="error", message=str(e))

    heartbeat_id = db.save_heartbeat(heartbeat)

    return jsonify(status="ok", id=heartbeat_id)

@app.route('/api/heartbeat/<id>', methods=['OPTIONS', 'DELETE', 'POST'])
@crossdomain(origin='*', headers=['Origin', 'X-Requested-With', 'Content-Type', 'Accept'])
@jsonp
def delete_heartbeat(id):

    if request.method == 'DELETE' or (request.method == 'POST' and request.json['_method'] == 'delete'):
        response = db.delete_heartbeat(id)

        if response:
            return jsonify(status="ok")
        else:
            return jsonify(status="error", message="failed to delete heartbeat")