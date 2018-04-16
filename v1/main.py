from flask import Blueprint, current_app
from flask.ext.sqlalchemy import SQLAlchemy
from flask.ext import restful
from flask.ext.socketio import SocketIO
from flask.ext.redis import FlaskRedis

from sqlalchemy.ext.mutable import Mutable

import config


app = Blueprint('v1', __name__)
db = SQLAlchemy()
api = restful.Api(prefix='/v1')
socketio = SocketIO()
redis = FlaskRedis()


_before1req = []
def before_first_request(func):
    """ decorator to launch func before 1st request """
    _before1req.append(func)

def init_app(flask_app):
    db.init_app(flask_app)
    api.init_app(flask_app)
    init_admin(flask_app)
    # FIXME! Socketio requires resource name to match on client and on server
    # so Nginx rewriting breaks it
    socketio.init_app(flask_app, resource='{}/v1/socket.io'.format(
        '/test' if config.TEST else ''
    ))
    redis.init_app(flask_app)
    flask_app.register_blueprint(app, url_prefix='/v1')
    flask_app.before_first_request_funcs.extend(_before1req)


# special column with mutation tracking
# TODO: move in some helpers module, after refactoring
class MutableDict(Mutable, dict):
    """http://docs.sqlalchemy.org/en/latest/orm/extensions/mutable.html"""
    @classmethod
    def coerce(cls, key, value):
        """Convert plain dictionaries to MutableDict."""

        if not isinstance(value, MutableDict):
            if isinstance(value, dict):
                return MutableDict(value)

            # this call will raise ValueError
            return Mutable.coerce(key, value)
        else:
            return value

    def __setitem__(self, key, value):
        """Detect dictionary set events and emit change events."""
        dict.__setitem__(self, key, value)
        self.changed()

    def __delitem__(self, key):
        """Detect dictionary del events and emit change events."""
        dict.__delitem__(self, key)
        self.changed()

    def __getstate__(self):
        return dict(self)

    def __setstate__(self, state):
        self.update(state)


class MutaleDictPickleType(db.PickleType):
    """
    Column with pickle support and mutations tracking in dicts.
    For usecase look at Badges model.
    """
    pass

MutableDict.associate_with(MutaleDictPickleType)

db.MutaleDictPickleType = MutaleDictPickleType
####

# now apply routes
from . import routes
from . import cas
from .admin import init as init_admin
