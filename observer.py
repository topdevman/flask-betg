#!/usr/bin/env python3

# Observer daemon:
# listens on some port/path,
# may have some children configured,
# knows how many streams can it handle.
# Also each host accepts connections only from its known siblings.
#
# Messaging flow:
#
# Client -> Master: Please watch the stream with URL ... for game ...
# Master: checks if this stream is already watched
# Master -> Slave1: Can you watch one more stream? (details)
# Slave1 -> Master: no
# Master -> Slave2: Can you watch one more stream? (details)
# Slave2 -> Master: yes
# (or if none agreed - tries to watch itself)
# ...
# Slave2 -> Master: stream X finished, result is Xres
# Master -> Poller: stream X done

# API:
# PUT /streams/id - watch the stream (client->master->slave)
# GET /streams/id - check stream status (master->slave)

import eventlet
eventlet.monkey_patch() # before loading flask

from flask import Flask, jsonify, request, abort as flask_abort
from flask.ext import restful
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext.restful import fields, marshal
from flask.ext.restful.reqparse import RequestParser
from flask.ext.restful.utils import http_status_message
from werkzeug.exceptions import default_exceptions
from werkzeug.exceptions import HTTPException, BadRequest, MethodNotAllowed, Forbidden, NotImplemented, NotFound

import os
import signal
from datetime import datetime, timedelta
import itertools
from eventlet.green import subprocess
import requests
import logging

import config
from observer_conf import SELF_URL, PARENT, CHILDREN, MAX_STREAMS

# if stream happens to be online, wait some time...
WAIT_DELAY = 30 # seconds between retries
WAIT_MAX = 360 # 3 hours

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = config.DB_URL
app.config['ERROR_404_HELP'] = False # disable this flask_restful feature
db = SQLAlchemy(app)
api = restful.Api(app)

# Fix request context's remote_addr property to respect X-Real-IP header
from flask import Request
from werkzeug.utils import cached_property
class MyRequest(Request):
    @cached_property
    def remote_addr(self):
        """The remote address of the client, with respect to X-Real-IP header"""
        return self.headers.get('X-Real-IP') or super().remote_addr
app.request_class = MyRequest


# JSONful error handling
log = app.logger
# allow 507 error code
class InsufficientStorage(HTTPException):
    code = 507
    description = 'No observers available'
    # FIXME: for some reason this exception gets propagated to stdout
    # rather than handled by make_json_error
flask_abort.mapping[507] = InsufficientStorage
def make_json_error(ex):
    code = getattr(ex, 'code', 500)
    if hasattr(ex, 'data'):
        response = jsonify(**ex.data)
    else:
        response = jsonify(error_code = code, error = http_status_message(code))
    response.status_code = code
    return response
for code in flask_abort.mapping.keys():
    app.error_handler_spec[None][code] = make_json_error

def abort(message, code=400, **kwargs):
    data = {'error_code': code, 'error': message}
    if kwargs:
        data.update(kwargs)

    log.warning('Aborting request {} /{}: {}'.format(
        # GET /v1/smth
        request.method,
        request.base_url.split('//',1)[-1].split('/',1)[-1],
        ', '.join(['{}: {}'.format(*i) for i in data.items()])))

    try:
        flask_abort(code)
    except HTTPException as e:
        e.data = data
        raise
restful.abort = lambda code,message: abort(message,code) # monkey-patch to use our approach to aborting
restful.utils.error_data = lambda code: {
    'error_code': code,
    'error': http_status_message(code)
}


# Restrict list of allowed hosts
def getsiblings():
    import socket
    ret = set()
    for host in list(CHILDREN.values()) + ([PARENT[1], 'localhost']
                                           if PARENT else
                                           ['localhost']):
        if not host:
            continue # skip empty addrs, e.g. parent for master node
        host = host.split('://',1)[-1].split(':',1)[0] # cut off protocol and port
        h, a, ips = socket.gethostbyname_ex(host)
        ret.update(ips)
    return ret
NEIGHBOURS = getsiblings()
@app.before_request
def restrict_siblings():
    if request.remote_addr not in NEIGHBOURS:
        log.debug('Attempt to request from unknown address '+request.remote_addr)
        raise Forbidden


def init_app(logfile=None):
    app.logger.setLevel(logging.DEBUG)

    logger = logging.FileHandler(logfile) if logfile else logging.StreamHandler()
    logger.setFormatter(logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s'))
    logger.setLevel(logging.DEBUG)
    app.logger.addHandler(logger)

    # now restart all active streams
    # and remove stale records
    with app.test_request_context():
        for stream in Stream.query:
            log.info('restarting stream {}/{}'.format(stream.handle, stream.gametype))
            if stream.state in ('waiting', 'watching'):
                add_stream(stream)
            elif stream.state in ('found', 'failed'):
                # was not yet deleted - delete now
                # FIXME: maybe send result (again)?
                db.session.delete(stream)
                db.session.commit()
            else:
                log.warning('Unexpected stream state '+stream.state)

    return app


# declare model
class Stream(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Twitch stream handle
    handle = db.Column(db.String(64), nullable=False)

    gametype = db.Column(db.String(64), default=None)

    # Which child watches this stream? None if self
    child = db.Column(db.String(64), default=None)

    # other metadata goes below

    # this is an ID of the primary Game object for this stream.
    # We don't use foreign key because we may reside on separate server
    # and use separate db.
    game_id = db.Column(db.Integer, unique=True)
    # supplementary game IDs - ones which follow the same stream.
    # Some of them might be reversed (winner for them should be inverted).
    # Format: 10,-20,15,3,-17
    # -n means reversed game
    game_ids_supplementary = db.Column(db.String, default='')

    state = db.Column(db.Enum('waiting', 'watching', 'found', 'failed'),
                      default='waiting')

    creator = db.Column(db.String(128))
    opponent = db.Column(db.String(128))

    __table_args__ = (
        db.UniqueConstraint('handle', 'gametype', name='_handle_gametype_uc'),
    )

    @classmethod
    def find(cls, id, gametype=None):
        q = cls.query.filter_by(handle=id)
        if gametype:
            q = q.filter_by(gametype=gametype)
        return q.first()

    def iter_games_revinfo(self):
        """
        Master node only!
        Iterate over all game objects related to this stream.
        Yields tuples of (Game, bool)
        where bool=True means current game is "inversed" (crea=oppo).
        """
        from v1.models import Game
        for gid in itertools.chain(
            [self.game_id],
            map(
                int,
                filter(
                    None,
                    self.game_ids_supplementary.split(','),
                ),
            ),
        ):
            if gid < 0:
                gid = -gid
                reverse = True
            else:
                reverse = False
            game = Game.query.get(gid)
            if not game:
                log.warning('Bad game id %d' % gid)
            yield game, reverse
    def iter_games(self):
        yield from (g for g,r in self.iter_games_revinfo())


# Main logic
ROOT = os.path.dirname(os.path.abspath(__file__))
class Handler:
    """
    This hierarchy is similar to Poller's one
    """
    gametypes = []
    path = None
    env = None
    process = None
    quorum = 5 # min results, or
    maxdelta = timedelta(seconds=10)
    onlylastresult = False

    @classmethod
    def find(cls, gametype):
        if gametype in cls.gametypes:
            return cls
        for sub in cls.__subclasses__():
            ret = sub.find(gametype)
            if ret:
                return ret

    def __init__(self, stream):
        self.stream = stream
        self.handle = stream.handle
        self.gametype = stream.gametype
        self.sub = None
        self._sysevts = set()

    def start(self):
        log.info('spawning handler')
        self.thread = eventlet.spawn(self.watch_tc)
        pool[self.handle, self.gametype] = self

    def abort(self):
        self.thread.kill()
        # this will automatically execute `finally` clause in `watch_tc`
        # which will remove us from pool

        # if subprocess is still alive, kill it
        if self.sub and self.sub.poll() is None: # still running?
            self.murderchild(self.sub)
    def murderchild(self, sub=None):
        sub = sub or self.sub
        if not sub:
            log.warning('Trying to kill subprocess but no one present')
            return
        exitcode = sub.poll()
        if exitcode is not None:
            log.warning('Trying to kill subprocess but it is already dead {}'.format(
                exitcode))
            return
        pgid = os.getpgid(sub.pid)
        log.info('Killing subprocess')
        os.killpg(pgid, signal.SIGTERM)
        eventlet.spawn_after(3, os.killpg, pgid, signal.SIGKILL)

    def sysevent(self, text):
        """
        Notify all related games about certain event
        """
        log.debug('Handler {} has event {}'.format(
            self, text))
        # propagate result to master
        requests.patch(
            '{}/streams/{}/{}'.format(SELF_URL, self.stream.handle, self.stream.gametype),
            data = dict(
                event = text,
            ),
        )
        log.debug('Event sent.')
    def sysevent_once(self, text, key=None):
        """
        Notify all related games about certain event unless already notified.
        Returns True if this was first call with given key/text, False otherwise.
        """
        if (key or text) in self._sysevts:
            return False
        self._sysevts.add(key or text)
        self.sysevent(text)
        return True
    def check_current_game(self):
        '''Check if the game currently playing on the stream
        matches one requested for this handler,
        and if the stream is online at all'''
        from v1.polling import Poller
        from v1.apis import Twitch

        poller = Poller.findPoller(self.stream.gametype)
        tgtype = poller.twitch_gametypes.get(self.stream.gametype)
        if not tgtype:
            raise ValueError('Invalid poller?? no gt for '+self.stream.gametype)
        # for debugging:
        if tgtype == 'None':
            tgtype = None
        cinfo = Twitch.channel(self.stream.handle)
        if cinfo['game'] != tgtype:
            log.info('Stream {}: expected game {}, got {} - will wait'.format(
                self.stream.handle, tgtype, cinfo['game']))
            return False

        # TODO: check if it is online

        return True
    def wait_for_correct_game(self, minutes=None):
        log.info('Waiting for correct game')
        waits = 0
        if not self.check_current_game():
            # log this only once
            self.sysevent('Twitch: wrong game running, waiting')
        while not self.check_current_game():
            eventlet.sleep(60) # check every minute
            waits += 1
            if tries and waits > minutes:
                log.info('Abandoning waiting')
                self.sysevent('Twitch: wrong game lasted for too long, aborting')
                return False
        log.info('Correct game detected')
        return True

    def watch_tc(self):
        log.info('watch_tc started')
        try:
            if not self.wait_for_correct_game(minutes=60): # 1 hour
                raise Exception('Wrong game is set for too long, abandoning '+
                                self.stream.handle)
            result = self.watch()
            waits = 0
            while result == 'offline':
                # re-add stream to session if needed
                # to avoid DetachedInstanceError
                # FIXME: why is it detached after watch() but not before?
                if not db.session.object_session(self.stream):
                    db.session.add(self.stream)

                if waits > WAIT_MAX:
                    # will be caught below
                    self.sysevent('Twitch: stream was offline for too long, aborting')
                    raise Exception('We waited for too long, '
                                    'abandoning stream '+self.stream.handle)
                log.info('Stream {} is offline, waiting'
                            .format(self.stream.handle))
                self.stream.state = 'waiting'
                db.session.commit()
                self.sysevent_once('Twitch: stream is offline, waiting',
                                   'offline_wait')

                # wait & retry
                eventlet.sleep(WAIT_DELAY)
                # check if currently plaing game is (still) correct
                self.wait_for_correct_game()
                result = self.watch()

                waits += 1
            return result
        except Exception: # will not catch GreenletExit
            log.exception('Watching failed')

            try: # will fail if stream is deleted
                self.stream.state = 'failed'
                db.session.commit()
                # mark it as Done anyway
                self.done('failed', datetime.utcnow().timestamp(),
                          'Watching failed due to internal error')
            except Exception: # stream was deleted?
                log.exception('Failed to mark stream as done-anyway')
        except eventlet.greenlet.GreenletExit:
            # do nothing (just perform `finally` block) but don't print traceback
            log.info('Watcher aborted for handle '+self.handle)
        finally:
            # kill subprocess if any
            if self.sub and self.sub.poll() == None:
                self.murderchild(self.sub)
            # mark that this stream has stopped
            # stream may be already deleted from db, so use saved handle
            # FIXME: for some reason this will be called twice for streams
            # which were waiting for correct gametype..
            if (self.handle, self.gametype) in pool:
                del pool[self.handle, self.gametype]
            else:
                log.warning('For some reason, was already deleted')

    def watch(self):
        # start subprocess and watch its output

        # first, chdir to this script's directory
        os.chdir(ROOT)

        # then, if required, chdir handler's requested dir (relative to script's)
        if self.path:
            os.chdir(self.path)
        cmd = 'exec ' + self.process.format(handle = self.stream.handle)
        if self.env:
            cmd = 'VIRTUAL_ENV_DISABLE_PROMPT=1 . {}/bin/activate; {}'.format(
                self.env, cmd)
        log.info('starting process...')
        sub = self.sub = subprocess.Popen(
            cmd,
            bufsize = 1, # line buffered
            universal_newlines = True, # text mode
            shell = True, # interpret ';'-separated commands
            stdout = subprocess.PIPE, # intercept it!
            stderr = subprocess.STDOUT, # intercept it as well
            preexec_fn = os.setsid,
        )
        log.info('process started')

        self.stream.state = 'watching'
        db.session.commit()

        # and now the main loop starts
        results = []
        last_res = None
        log.info('waiting for output')

        self.started()
        self.sysevent('Twitch Running')

        for line in sub.stdout:
            line = line.strip().decode()
            try:
                result = self.check(line)
            except Exception as e:
                log.exception('Error during checking line!')
                result = None # just skip this line
            else:
                log.info('Got line result: {}'.format(result))

            if isinstance(result, tuple):
                outcome = result[0]
            else:
                outcome = result

            if outcome == 'offline':
                # handle it specially:
                # force stop this process and retry in 30 seconds
                # (will be done in watch_tc)
                return 'offline'
            if outcome == 'abandon':
                # abandon previously retrieved data
                results = []
                last_res = None
                self.stream.state = 'watching' # roll back from 'found'
                continue

            if outcome is not None and outcome != 'done':
                if not isinstance(result, tuple):
                    log.warning('Invalid outcome, no details available: '+str(result))
                    result = (result, False, None) # consider it weak
                self.stream.state = 'found'
                if self.onlylastresult:
                    results = []
                results.append(result) # tuple
                #if not last_res:
                last_res = datetime.utcnow()

            # consider game done when either got quorum results
            # or maxdelta passed since first result
            # (in case they are enabled for this particular streamer)
            # or when outcome is 'done'
            log.debug('have for now: r: {}, '
                      'now: {}, '
                      'lr: {}, '
                      'md: {}'.format(
                          results,
                          datetime.utcnow(),
                          last_res,
                          (last_res + self.maxdelta) if last_res else '..',
                      ))
            if (outcome == 'done') or results and (
                (self.quorum and len(results) >= self.quorum) or
                (self.maxdelta and datetime.utcnow() > last_res + self.maxdelta)
            ):
                # FIXME: this clause is executed only on next line,
                # so if we got 3 results (<quorum) and none after that
                # then we will wait until next line,
                # which may be not soon.

                # kill the process as we don't need more results
                self.murderchild(sub)

                break # don't handle remaining output

        # now that process is stopped, handle results found
        if not results:
            log.warning('process failed with status {}, considering draw'.format(
                sub.poll()))
            results = [('failed', True,
                        'Observer terminated without returning any result! '
                        'Please contact support.')]
            self.sysevent('Twitch: stream finished but no results were retrieved')
            # FIXME: maybe better restart it?
            self.stream.state = 'failed'
            last_res = datetime.utcnow()
        log.debug('results list: '+str(results))

        # if there is any strong result, drop all weak ones
        for r in results:
            if r[1]: # strong
                # drop all weak results
                results = list(filter(lambda r: r[1], results))
                break

        # calculate most trusted result
        freqs = {}
        for r in results:
            freqs[r] = freqs.get(r, 0) + 1
        # Sort by frequency descending - i.e. most frequent goes first
        pairs = sorted(freqs.items(), key=lambda p: p[1], reverse=True)
        # use most frequently occuring result
        result = pairs[0][0]
        outcome, strong, details = result # decode it

        log.debug('got result: {}'.format(result))
        # handle result
        db.session.commit()
        self.done(outcome, last_res.timestamp(), details)

    def started(self):
        pass
    def check(self, line):
        """
        To be overriden.
        Takes one line from script\'s output;
        returns tuple: outcome, is_strong, details.
        For 'offline' and None outcomes can return just outcome.
        If outcome is weak, it will only be considered if there are stronger outcomes.
        """
        raise NotImplementedError('This should be overriden!')
    def done(self, result, timestamp, details=None):
        log.debug('Handler {} done, result {}, details {}'.format(
            self, result, details))
        # propagate result to master
        requests.patch(
            '{}/streams/{}/{}'.format(SELF_URL, self.stream.handle, self.stream.gametype),
            data = dict(
                winner = result,
                details = details,
                timestamp = timestamp,
            ),
        )
        log.debug('Result sent.')

class FifaOldHandler(Handler):
    gametypes = [
        'fifa14-xboxone',
        'fifa15-xboxone',
    ]
    gametypes = []
    path = 'fifastreamer'
    env = '../../env2'
    process = 'python2 -u fifa_streamer.py "http://twitch.tv/{handle}"'

    def check(self, line):
        log.debug('checking line: '+line)

        if 'Stream is offline' in line:
            return 'offline'

        if 'Impossible to recognize who won' in line:
            log.warning('Couldn\'t get result, skipping')
            return None #'draw'
        if 'Score:' in line:
            # this may raise an exception
            # if only one player name is present;
            # it will be catched in watch().
            if 'Players:' not in line:
                log.warning('No players data?..')
                return None
            left, right = line.split('Score: ',1)[1].split('Players:',1)
            rights = right.strip().split('\t\t', 1)
            if len(rights) == 2:
                onick1, onick2 = rights # o means original
            else:
                onick1 = onick2 = rights # only one nickname retrieved?..
            scores = [p for p in left.split()
                      if '-' in p and p[0].isdigit() and p[-1].isdigit()][0]
            score1, score2 = scores.split('-')
            team1, team2 = map(lambda x: x.strip(), left.split(scores))
            nick1, nick2 = map(lambda x: x.lower(), (onick1, onick2))
            score1, score2 = map(int, (score1, score2))

            details = '{} ({}) vs {} ({}): {} - {}'.format(
                onick1, team1,
                onick2, team2,
                score1, score2,
            )

            log.info('Got score data. Nicks {} / {}, scores {} / {}'.format(
                nick1, nick2, score1, score2))

            if score1 == score2:
                log.info('draw detected')
                return 'draw', True, details

            cl = self.stream.creator.lower()
            ol = self.stream.opponent.lower()
            log.debug('cl: {}, ol: {}'.format(cl, ol))
            creator = opponent = None
            if cl == nick1:
                creator = 1
            elif cl == nick2:
                creator = 2
            if ol == nick1:
                opponent = 1
            elif ol == nick2:
                opponent = 2
            if not creator and not opponent:
                log.warning('Wrong gamertags / good gamertag not detected! '
                            'Defaulting to draw.')
                return 'draw', False, 'Gamertags don\'t match -> draw... '+details
            if not creator:
                creator = 1 if opponent == 2 else 2

            if score1 > score2:
                winner = 1
            else:
                winner = 2
            return('creator' if winner == creator else 'opponent',
                   True,
                   details)
        return None
class FifaHandler(Handler):
    gametypes = [
        'fifa14-xboxone',
        'fifa15-xboxone',
    ]
#    gametypes = []
    path = 'fifanewstreamer'
    process = ('livestreamer -p "./ocr_test -debug -abc -skip 30" '
               '--player-continuous-http --verbose-player '
               '"http://twitch.tv/{handle}" best')

    # disable quorum-based mechanics
    quorum = None
    maxdelta = timedelta(minutes=1)
    onlylastresult = True

    def started(self):
        self.__approaching = False
        self.__teamcheck = list()

    def check(self, line):
        log.debug('checking line: '+line)

        if 'error: No streams found on this URL' in line:
            # stream is offline
            return 'offline'
        if 'Failed to read the frame from the stream' in line:
            # stream possibly went offline
            return 'done'
            # TODO: maybe consider this as game-end?
        if 'HTTP connection closed' in line or 'Stream ended' in line:
            # stream went offline
            return 'done'

        #if 'Impossible to recognize who won' in line:
        #    log.warning('Couldn\'t get result, skipping')
        #    return None #'draw'
        if not line.endswith('in-game') or 'non in-game' in line:
            if self.__approaching and 'non in-game' in line:
                # FIXME for now consider this  state a proper game-end
                #return 'done'
                log.warning('approaching and stopped')
            return None
        # FIXME penalties

        parts = line.split()
        _, time, team1, score1, _m, team2, score2, *_ = parts
        if _m != '-':
            log.debug('Line not recognized: '+line)
            return None
        if team1 == '@@@' or team2 == '@@@':
            log.debug('Teams not recognized: '+line)
            return None
        if ':' not in time:
            log.debug('Time not recognized: '+line)
            return None
        time = tuple(map(int, time.split(':'))) # (hh, mm) - may in theory raise ValueErr for 0:1:2 or x:y
        if time[0] < 0 or time[1] < 0:
            log.debug('Negative time: '+line)
            return None
        score1, score2 = map(int, (score1, score2))
        if score1 < 0 or score2 < 0:
            log.debug('Negative scores: '+line)
            return None
        if len(team1) not in (2,3) or len(team2) not in (2,3):
            log.debug('Bad team names, expected 3 chars: '+line)
            return None
        team1l, team2l = map(lambda t: t.casefold().translate({
            ord(n): '@' for n in '0123456789'
        }), (team1, team2))

        details = '{} vs {}: {} - {}'.format(
            team1, team2,
            score1, score2,
        )

        log.debug('Got score data. Teams {} / {}, scores {} / {}'.format(
            team1, team2, score1, score2))
        self.sysevent_once('Twitch: got score info', 'score_got')

        self.sysevent_once('Twitch: score changed: {} ({}) / {} ({})'.format(
            score1, team1, score2, team2,
        ), 'score_{}_{}'.format(score1, score2))

        if len(self.__teamcheck) < 10:
            have = set((team1l, team2l))
            self.__teamcheck.append(have)
            if len(self.__teamcheck) == 10:
                # do the check
                cl, ol = map(lambda u: u.casefold().translate({
                    ord(n): '@' for n in '0123456789'
                }), (self.stream.creator, self.stream.opponent))
                need = set((cl, ol))
                haveprobs = dict()
                for have in self.__teamcheck:
                    haveprobs[have] = haveprobs.get(have, 0)+1
                have = max(haveprobs.items(), key=lambda i: i[1])[0]

                # now `have` is most probable one, do check it
                if have != need:
                    self.sysevent('Unexpected teams! Requested {}, found {}'.format(
                        '/'.join(need),
                        '/'.join(have),
                    ))
                    if len(need-have) == 1:
                        # one team is bad, other is good; notify bad-team player
                        diffteam = (need-have)[0]
                        wronger = 'creator' if diffteam == cl else 'opponent'
                        self.sysevent(
                            '{%s}, did you choose another team?' % wronger
                        )

        # TODO: probably pre-handle & remember team names here
        # to check if stream went wrong

        if time[0] < 88:
            return None # too early anyway
        if time[0] in [88, 89]:
            log.debug('Approaching 90! {}'.format(time))
            self.__approaching = True
            return None
        if time[0] > 90:
            if time[0] < 100 and self.__approaching:
                self.__approaching = False
                return 'abandon'
            if time[0] < 118:
                return None # too early again
            if time[0] in [118, 119]:
                log.debug('Approaching 120! {}'.format(time))
                self.__approaching = True
                return None
            # TODO: penalties
        if not self.__approaching:
            log.info('Probably unexpected line (time {} but not approaching)'.format(time))
            #return None

        # Now we suppose we have correct result
        # so calculate it

        if score1 == score2:
            log.info('draw detected')
            return 'draw', True, details

        cl, ol = map(lambda u: u.casefold().translate({
            ord(n): '@' for n in '0123456789'
        }), (self.stream.creator, self.stream.opponent))
        log.debug('cl: {}, ol: {}'.format(cl, ol))
        creator = opponent = None
        if cl == team1l:
            creator = 1
        elif cl == team2l:
            creator = 2
        if ol == team1l:
            opponent = 1
        elif ol == team2l:
            opponent = 2
        if not creator and not opponent:
            log.warning('Wrong team names / good team name not detected! '
                        'Defaulting to draw.')
            return 'draw', False, 'Gamertags don\'t match -> draw... '+details
        if not creator:
            creator = 1 if opponent == 2 else 2

        if score1 > score2:
            winner = 1
        else:
            winner = 2
        return('creator' if winner == creator else 'opponent',
                True,
                details)
class TestHandler(Handler):
    gametypes = [
        'test',
    ]
    process = './test.sh'

    def check(self, line):
        print('line:',line)
        outcomes = dict(
            c='creator',
            o='opponent',
            d='draw',
        )
        if 'Done' in line:
            return outcomes.get(line[-1], None)


pool = {}
def add_stream(stream):
    """
    Tries to append given stream object (which is not yet committed) to watchlist.
    Returns string on failure (e.g. if list is full).
    """
    if len(pool) >= MAX_STREAMS:
        return 'busy'

    handler = Handler.find(stream.gametype)
    if not handler:
        return 'unsupported'

    log.info('Adding stream')
    handler(stream).start() # will add stream to pool

    return True

def abort_stream(stream):
    """
    If stream is running, abort it. Else do nothing.
    """
    if (stream.handle, stream.gametype) not in pool:
        return False
    pool[stream.handle, stream.gametype].abort()
    # will remove itself
    return True

def abort_all(*args):
    # FIXME: does it work at all?
    for stream in list(pool.values()): # we create copy as abort() will remove stream from pool
        # this considers we already got game result from somewhere -
        # or will restart soon
        stream.abort()
    log.info('All stream watchers aborted for restart')
    # Now we should somehow propagate that SIGTERM to gunicorn worker
    # but we cannot, so we'll just set it to system default (FIXME!)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGTERM, abort_all) # fire when gunicorn worker is terminated

def stream_done(stream, winner, timestamp, details=None):
    """
    Runs on master node only.
    Marks given stream as done, and notifies clients etc.
    """
    from v1.polling import Poller

    for game, reverse in stream.iter_games_revinfo():
        poller = Poller.findPoller(stream.gametype)
        if winner == 'failed':
            if poller.twitch == 2: # mandatory
                log.warning('Watching failed, considering it a draw')
                winner = 'draw'
            elif poller.twitch == 1: # optional
                log.warning('Watching failed, not updating game')
                winner = None # will be fetched by Polling later
        if winner:
            if winner in ['creator','opponent'] and reverse:
                winner = 'creator' if winner == 'opponent' else 'opponent'
            Poller.gameDone(game, winner, int(timestamp), details)

    # and anyway issue DELETE request, because this stream is unneeded anymore
    # (even if no games were changed)

    # no need to remove from pool, because we are on master
    # and it was already removed from pool anyway
    # but now let's delete it from DB

    # Notice: this is DELETE request to ourselves.
    # But we are still handling PATCH request, so it will hang.
    # So launch it as a green thread immediately after we finish
    eventlet.spawn(requests.delete,
                   '{}/streams/{}/{}'.format(SELF_URL, stream.handle, stream.gametype))

    return True

def stream_event(stream, text):
    """
    Runs on master node only.
    Notifies clients about some event happened on the stream.
    """
    from v1.polling import Poller
    if '{' in text:
        game = next(stream.iter_games())
        text = text.format(creator=game.creator.nickname, opponent=game.opponent.nickname)
    # FIXME: avoid dupes somehow, maybe exclude ingames?
    for game in stream.iter_games():
        #if not game.is_ingame:
        Poller.gameEvent(game, text)

def current_load():
    streams = len(pool)
    maximum = MAX_STREAMS
    # TODO: use load average as a base, and add some cap on it
    load = streams / maximum
    return load, streams, maximum


# now define our endpoints
def child_url(cname, sid=None, gametype=None):
    if cname in CHILDREN:
        return '{host}/streams/{sid}'.format(
            host = CHILDREN[cname],
            sid = '{}/{}'.format(sid, gametype) if sid else '',
        )
    return None

@api.resource(
    '/streams',
    '/streams/',
    '/streams/<id>/<gametype>',
)
class StreamResource(restful.Resource):
    fields = dict(
        handle = fields.String,
        gametype = fields.String,
        game_id = fields.Integer,
        game_ids_supplementary = fields.String,
        state = fields.String,
        creator = fields.String,
        opponent = fields.String,
    )

    def get(self, id=None, gametype=None):
        """
        Returns details (current state) for certain stream.
        """
        if not id:
            # at least for debugging
            q = Stream.query
            return dict(
                streams = fields.List(fields.Nested(self.fields)).format(q),
            )

        log.info('Stream queried with id '+id)

        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound

        if stream.child:
            # forward request
            return requests.get(child_url(stream.child, stream.handle, stream.gametype)).json()

        return marshal(stream, self.fields)

    def put(self, id=None, gametype=None):
        """
        Returns 409 if stream with this handle exists with different parameters.
        Returns 507 if no slots are available.
        Returns newly created stream id otherwise.
        Will return 201 code if new stream was added
        or 200 code if game was added to existing stream.
        """
        if not id or not gametype:
            raise MethodNotAllowed

        log.info('Stream put with id {}, gt {}'.format(id, gametype))

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('game_id', type=int, required=True)
        parser.add_argument('creator', required=True)
        parser.add_argument('opponent', required=True)
        # TODO...
        args = parser.parse_args()

        if Stream.query.filter_by(game_id = args.game_id).first():
            # FIXME: handle dup ID in supplementaries?
            abort('This game ID is already watched in some another stream')

        stream = Stream.find(id, gametype)
        if stream:
            # stream already exists; add this game to it as a supplementary game
            new = False

            if args.creator.casefold() == stream.creator.casefold():
                if args.opponent.casefold() != stream.opponent.casefold():
                    abort('Duplicate stream ID with wrong opponent nickname', 409)
                game = args.game_id
            elif args.opponent.casefold() == stream.creator.casefold():
                if args.creator.casefold() != stream.opponent.casefold():
                    abort('Duplicate stream ID with wrong reverse oppo nickname', 409)
                game = -args.game_id # reversed result
            else:
                abort('Duplicate stream ID with different players', 409)

            # now add game id
            if stream.game_ids_supplementary:
                stream.game_ids_supplementary += ','
            else:
                stream.game_ids_supplementary = ''
            stream.game_ids_supplementary += str(game)

        else:
            # new stream
            new = True

            stream = Stream()
            stream.handle = id
            stream.gametype = gametype
            for k, v in args.items():
                setattr(stream, k, v)

        ret = None
        # now find the child who will handle this stream
        for child, host in CHILDREN.items():
            # try to delegate this stream to that child
            # FIXME: implement some load balancing
            result = requests.put('{}/streams/{}/{}'.format(host, id, gametype),
                                  data = args)
            if result.status_code == 200: # accepted?
                ret = result.json()
                # remember which child accepted this stream
                stream.child = child
                break
        else:
            # nobody accepted? try to handle ourself
            try:
                result = add_stream(stream)
            except Exception as e:
                abort('Error adding stream: '+str(e))
            if result == True:
                stream.child = None
            elif result == 'busy':
                abort('All observers are busy', 507) # 507 Insufficient Stroage
            elif result == 'unsupported':
                abort('Gametype not supported')
            else:
                abort('Unknown error '+result, 500)

        if new:
            db.session.add(stream)
        db.session.commit()

        if ret:
            return ret
        return marshal(stream, self.fields), 201 if new else 200

    def patch(self, id=None, gametype=None):
        """
        Used to propagate stream result (or status update) from child to parent.
        Plese provide either (winner,details,timestamp) or (event)!
        """
        if not id or not gametype:
            raise MethodNotAllowed

        log.info('Stream patched with id {}, gt {}'.format(id, gametype))

        # this is called from child to parent
        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound

        parser = RequestParser(bundle_errors=True)
        parser.add_argument('winner')
        parser.add_argument('details', default=None)
        parser.add_argument('timestamp', type=float)
        parser.add_argument('event')
        args = parser.parse_args()

        if PARENT:
            # send this request upstream
            return requests.patch('{}/streams/{}/{}'.format(PARENT[1], id, gametype),
                                  data = args).json()
        else:
            if args.event:
                stream_event(stream, args.event)
            else:
                stream_done(stream, args.winner, args.timestamp, args.details)

        return jsonify(success = True)

    def delete(self, id=None, gametype=None):
        """
        Deletes all records for given stream.
        Also aborts watching if stream is still watched.
        """
        if not id or not gametype:
            raise MethodNotAllowed
        log.info('Stream delete for id {}, gt {}'.format(id, gametype))
        stream = Stream.find(id, gametype)
        if not stream:
            raise NotFound
        if stream.child:
            ret = requests.delete(child_url(stream.child, id, stream.gametype))
            if ret.status_code != 200:
                abort('Couldn\'t delete stream', ret.status_code, details=ret)
        else: # watching ourself:
            abort_stream(stream)
        db.session.delete(stream)
        db.session.commit()
        return jsonify(deleted=True)

@app.route('/load')
def load_ep():
    # TODO: allow querying `load average` of each child
    load, streams, maximum = current_load()
    for child in CHILDREN.values():
        ret = requests.get(child+'/load').json()
        load += ret.get('total', 0)
        streams += ret.get('current_streams', 0)
        maximum += ret.get('max_streams', 0)
    return jsonify(
        total = load / (len(CHILDREN)+1),
        current_streams = streams,
        max_streams = maximum,
    )


if __name__ == '__main__':
    init_app()
    app.run(port=8021, debug=False, use_debugger=False, use_reloader=False)
