#!/usr/bin/env python3
from flask import Flask, request, Response, session, render_template
from flask import abort, redirect, url_for

import requests

import config

app = Flask(__name__, static_folder = 'static-m')
app.config['APPLICATION_ROOT'] = '/m'
# for session...
app.config['SECRET_KEY'] = config.JWT_SECRET
app.config['API'] = 'http://betgame.co.uk/v1'
app.config['API_ROOT'] = 'http://betgame.co.uk/v1'

# Fix app root...
class ReverseProxied:
    def __init__(self, app):
        self.app = app
    def __call__(self, env, start_resp):
        script_name = env.get('HTTP_X_SCRIPT_NAME', '')
        if script_name:
            env['SCRIPT_NAME'] = script_name
            path_info = env['PATH_INFO']
            print('pi: %s, sn: %s' % (path_info, script_name))
            if path_info.startswith(script_name):
                env['PATH_INFO'] = path_info[len(script_name):]
        scheme = env.get('HTTP_X_SCHEME', '')
        if scheme:
            env['wsgi.url_scheme'] = scheme
        return self.app(env, start_resp)
app.wsgi_app = ReverseProxied(app.wsgi_app)


# API access
class GameTypes:
    """
    Caching access to /gametypes endpoint
    """
    cache = None
    @classmethod
    def _load(cls):
        # TODO: expiration timeouts
        if not cls.cache:
            cls.cache = requests.get(app.config['API_ROOT']+'/gametypes').json()
            cls.dcache = {x['id']: x for x in cls.cache['gametypes']}
        return cls.dcache

    @classmethod
    def get(cls, name=None):
        if not name:
            return cls._load().values()
        return cls._load().get(name)


@app.before_request
def beta_auth():
    auth = request.authorization
    if auth:
        login, password = auth.username, auth.password
        if login == 'tester' and password == 'bet123':
            # passed
            return
    # require auth
    return Response(
        'This is a testing site, please authenticate!',
        401, {'WWW-Authenticate': 'Basic realm="Login Required"'})


@app.route('/')
def bets():
    modes = None
    if session.get('gametype'):
        modes = GameTypes.get(session['gametype'])['gamemodes']
        print('m: '+str(modes))
    return render_template('newbet.html')

@app.route('/gametype', methods=['GET','POST'])
def gametype():
    if request.method == 'POST':
        session['gametype'] = request.form.get('gametype')
        return redirect(url_for('bets'))
    return render_template('gametype.html', games=GameTypes.get())

@app.route('/leaders')
def leaderboard():
    return render_template('leaderboard.html')

@app.route('/challenges')
def challenges():
    return render_template('challenges.html')

@app.route('/profile')
def profile():
    return render_template('profile.html')

if __name__ == '__main__':
    app.run(debug=True)

@app.route('/m')
def mob():
    return 'Hello World'
