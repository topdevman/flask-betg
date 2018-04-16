from flask import request, g, make_response, url_for, redirect

from urllib.parse import urlencode
import requests
from xml.etree import ElementTree
from datetime import datetime, timedelta

import config
from .main import app
from .apis import WilliamHill
from .models import db, TGT
from .common import *

# this is a primary CAS login endpoint
# https://developer.williamhill.com/cas-implementation-guidelines-developers-0
@app.route('/cas/login')
def cas_login():
    url = WilliamHill.CAS_HOST
    url += '/cas/login?'+urlencode(dict(
        service = config.SITE_BASE_URL+url_for('.cas_done'),
        joinin_link = 'test', # FIXME remove this when going to production
    ))
    return redirect(url);
@app.route('/cas/logout')
def cas_logout():
    url = WilliamHill.CAS_HOST + '/cas/logout'
    return redirect(url);

@app.route('/cas/done')
def cas_done():
    ticket = request.args.get('ticket')

    url = WilliamHill.CAS_HOST + '/cas/serviceValidate'
    ret = requests.get(url, params=dict(
        service = config.SITE_BASE_URL+url_for('.cas_done'),
        ticket = ticket,
        pgtUrl = config.SITE_BASE_URL+url_for('.cas_pgt'),
        # TODO renew?
    ), verify=False) # FIXME
    tree = ElementTree.fromstring(ret.text)
    ns = {'cas': 'http://www.yale.edu/tp/cas'}

    success = tree.find('cas:authenticationSuccess', ns)
    failure = tree.find('cas:authenticationFailure', ns)
    if failure is not None:
        return 'Auth failure! Code: {}<br/>{}'.format(
            failure.get('code', '<no code>'),
            failure.text.strip(),
        )
    if success is None:
        return 'Auth failure, unrecognized response'
    log.debug(success.getchildren())
    e_user = success.find('cas:user', ns)
    e_pgt = success.find('cas:proxyGrantingTicket', ns)
    if e_user is None or e_pgt is None:
        return 'Auth failure, bad response'
    user = e_user.text.strip()
    pgt = e_pgt.text.strip()

    o_tgt = TGT.query.filter_by(iou=pgt).first()
    if not o_tgt:
        return 'Auth failure - no PGT, please retry'

    tgt = o_tgt.tgt
    db.session.delete(o_tgt)
    # and remove obsolete records
    TGT.query.filter(
        TGT.timestamp < (datetime.utcnow() - timedelta(minutes=5))
    ).delete()
    db.session.commit()

    # We use TGT token here and don't generate JWT at this stage.
    # Client should then pass received token to /federated_login endpoint.
    return redirect(url_for('.cas_result', token = tgt, user = user))

@app.route('/cas/pgt')
def cas_pgt():
    log.debug('PGT endpoint: {}, vals={}, cookies={}'.format(
        request.method,
        request.values,
        request.cookies,
    ))
    iou = request.values.get('pgtIou')
    pgtid = request.values.get('pgtId')

    # save it
    if iou and pgtid: # because they may call us without any data
        tgt = TGT(iou=iou, tgt=pgtid)
        db.session.add(tgt)
        db.session.commit()

    return 'PGT saved' # dummy

@app.route('/cas/result')
def cas_result():
    user = request.values.get('user')
    token = request.values.get('token')
    return """
<html><head>
<title>{{"success":true, "token":"{token}"}}</title>
</head><body>
Login successful, user is {user}, tgt is {token}
</body></html>
    """.format(token=token, user=user)
