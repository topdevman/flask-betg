#!/usr/bin/env python
from flask import Flask, jsonify
from flask.ext.restful.utils import http_status_message
from flask.ext.cors import CORS
from flask.ext.script import Manager
from flask.ext.migrate import MigrateCommand, Migrate

from werkzeug.exceptions import default_exceptions
import logging
from logging.handlers import SysLogHandler
import socket
import datadog

import config


app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = config.DB_URL
app.config['ERROR_404_HELP'] = False # disable this flask_restful feature
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024 # limit upload size for userpics
app.config['SECRET_KEY'] = config.SECRET_KEY

datadog.initialize(api_key=config.DATADOG_API_KEY)

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
def make_json_error(ex):
    code = getattr(ex, 'code', 500)
    if hasattr(ex, 'data'):
        response = jsonify(**ex.data)
    else:
        response = jsonify(
            error_code = code,
            error = ex.__dict__.get('description') # __dict__ to avoid using classwide default
                or http_status_message(code),
        )
    response.status_code = code
    return response
for code in default_exceptions.keys():
    # apply decorator
    app.errorhandler(code)(make_json_error)

def init_app(app=app):
    if not app.logger:
        raise ValueError('no logger')
    import v1
    v1.init_app(app)

    # disable logging for cors beforehand
    logging.getLogger(app.logger_name+'.cors').disabled = True
    CORS(app, origins=config.CORS_ORIGINS)

    return app

def setup_logging(app, f = None, level = logging.DEBUG):
    app.logger.setLevel(level)

    logger = logging.FileHandler(f) if f else logging.StreamHandler()
    logger.setFormatter(logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s'))
    logger.setLevel(level)
    app.logger.addHandler(logger)

    return # no PaperTrail support for now
    if config.LOCAL:
        return
    class ContextFilter(logging.Filter):
        hostname = socket.gethostname()
        def filter(self, record):
            record.hostname = self.hostname
            return True
    # papertail logging
    logger = SysLogHandler(address=(config.PT_HOSTNAME, config.PT_PORT))
    logger.setFormatter(logging.Formatter(
        '%(asctime)s BetGameAPI{}: '
        '[%(levelname)s] %(message)s'.format('-test' if config.TEST else ''),
        datefmt='%b %d %H:%M:%S'))
    logger.setLevel(level)
    app.logger.addFilter(ContextFilter())
    app.logger.addHandler(logger)

def live(logfile=None):
    setup_logging(app, logfile)
    return init_app()

def debug():
    app.debug = True #-- this breaks exception handling?..
    setup_logging(app)
    return init_app()


if __name__ == '__main__':
    init_app(app)

    manager = Manager(app)
    manager.add_command('db', MigrateCommand)
    from v1.main import db

    migrate = Migrate(app, db)

    # TODO: move manager to separate file manage.py

    # TODO: separate manager for updating badges
    @manager.command
    def insert_badges():
        """Set Badges for every Player
        Run only once, after applying migration.
        """
        from v1.models import Badges, Player

        for player in Player.query.all():
            player.badges = Badges()
            db.session.add(player.badges)
            db.session.commit()

    @manager.command
    def update_badges():
        """This command will add new badges for every player.
        If you need to update existent badges info (from column "default")
        use "update_badges_info" command
        """
        # player.__table__._columns["locked"].default.arg
        pass

    @manager.command
    def update_badges_info(*column_names):
        """Updates existent values for badges.
        New value will be taken from column "default".
        You cant update user bounded values, such as: "received", "value"
        :param column_names: column names that need update
        """
        pass

    manager.run()
