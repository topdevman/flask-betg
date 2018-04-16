from flask import request, jsonify, current_app, g, send_file, make_response, redirect
from flask import copy_current_request_context
from flask.ext import restful
from flask.ext.restful import fields, marshal
from flask.ext.socketio import send as sio_send, disconnect as sio_disconnect
from sqlalchemy.sql.expression import func
from sqlalchemy.exc import IntegrityError

from werkzeug.exceptions import HTTPException
from werkzeug.exceptions import MethodNotAllowed, Forbidden, NotFound
from werkzeug.exceptions import NotImplemented # noqa

import os
from io import BytesIO
from datetime import datetime, timedelta
import math
import json
import operator
import requests
from PIL import Image
import eventlet

import config

from v1.signals_definitions import game_badge_signal
from .models import * # noqa
from .helpers import * # noqa
from .apis import * # noqa
from .polling import * # noqa
from .helpers import MyRequestParser as RequestParser  # instead of system one
from .main import app, db, api, socketio, redis


# Players
@api.resource(
    '/players',
    '/players/',
    '/players/<id>',
)
class PlayerResource(restful.Resource):
    @classproperty
    def parser(cls):
        parser = RequestParser()
        partial = parser.partial = RequestParser()
        login = parser.login = RequestParser()
        fieldlist = [
            # name, type, required
            ('_force', gamertag_force_field, False),
            ('nickname', None, True),
            ('email', email_validator, True),
            ('password', encrypt_password, True),
            ('facebook_token', federatedRenewFacebook, False),  # should be last to avoid extra queries
            ('twitter_token', federatedRenewTwitter, False),  # should be last to avoid extra queries
            ('bio', None, False),
        ]
        identities = set()
        for identity in Identity.all:
            identities.add((identity.id, identity.checker, False))
        fieldlist.extend(identities)
        for name, type, required in fieldlist:
            if hasattr(Player, name):
                type = string_field(getattr(Player, name), ftype=type)
            parser.add_argument(
                name,
                required=required,
                type=type,
            )
            partial.add_argument(
                name,
                required=False,
                type=type,
            )
        login.add_argument('push_token',
                           type=string_field(Device.push_token,
                                             # 64 hex digits = 32 bytes
                                             ftype=hex_field(64)),
                           required=False)
        partial.add_argument('old_password', required=False)
        return parser

    @classmethod
    def fields(cls, public=True, stat=False, leaders=False):
        ret = dict(
            id=fields.Integer,
            nickname=fields.String,
            email=fields.String,
            facebook_connected=fields.Boolean(attribute='facebook_token'),
            twitter_connected=fields.Boolean(attribute='twitter_token'),
            bio=fields.String,
            has_userpic=fields.Boolean,

            ea_gamertag=fields.String,
            fifa_team=fields.String,
            riot_summonerName=fields.String,
            steam_id=fields.String,
            starcraft_uid=fields.String,
            tibia_character=fields.String,
        )
        if not public: ret.update(dict(
            balance=fields.Float,
            balance_info=fields.Raw(attribute='balance_obj'),  # because it is already JSON
            devices=fields.List(fields.Nested(dict(
                id=fields.Integer,
                last_login=fields.DateTime,
            ))),
        ))
        if stat: ret.update(dict(
            # some stats
            gamecount=fields.Integer,  # FIXME: optimize query somehow?
            winrate=fields.Float,
            # popularity = fields.Integer,
        ))
        if leaders: ret.update(dict(
            leaderposition=fields.Integer,
        ))
        return ret

    @classmethod
    def login_do(cls, player, args=None, created=False):
        if not args:
            args = cls.parser.login.parse_args()
        dev = Device.query.filter_by(player=player,
                                     push_token=args.push_token
                                     ).first()
        if not dev:
            dev = Device()
            dev.player = player
            dev.push_token = args.push_token
            db.session.add(dev)

            if args.push_token:
                # remove that token from other devices
                Device.query.filter(
                    Device.player != player,
                    Device.push_token == args.push_token,
                ).delete()
        dev.last_login = datetime.utcnow()

        db.session.commit()  # to create device id

        ret = jsonify(
            player=marshal(player, cls.fields(public=False)),
            token=makeToken(player, device=dev),
            created=created,
        )
        if created:
            ret.status_code = 201
        return ret

    @require_auth
    def get(self, user, id=None):
        if not id:
            # Leaderboard mode

            parser = RequestParser()
            parser.add_argument('filter')
            parser.add_argument('filt_op',
                                choices=['startswith', 'contains'],
                                default='startswith',
                                )
            parser.add_argument(
                'order',
                choices=sum(
                    [[s, '-' + s]
                     for s in
                     ('id',
                      'lastbet',
                      'popularity',
                      'winrate',
                      'gamecount',
                      )], []),
                required=False,
            )
            parser.add_argument('gametype',
                                choices=Poller.all_gametypes,
                                required=False)
            parser.add_argument('period',
                                required=False,
                                choices=[
                                    'today', 'yesterday', 'week', 'month',
                                ])
            # parser.add_argument('names_only', type=boolean_field)
            args = parser.parse_args()

            if args.filter:
                query = Player.search(args.filter, args.filt_op)
            else:
                query = Player.query

            orders = []
            if args.order:
                ordername = args.order.lstrip('-')
                if hasattr(Player, ordername + '_impl'):
                    # this parameter depends on games,
                    # so calculate and apply corresponding filters
                    filters = []
                    if args.gametype:
                        filters.append(Game.gametype == args.gametype)
                    if args.period:
                        till = None
                        if args.period == 'today':
                            since = timedelta(days=1)
                        elif args.period == 'yesterday':
                            since = timedelta(days=2)
                            till = timedelta(days=1)
                        elif args.period == 'week':
                            since = timedelta(weeks=1)
                        elif args.period == 'month':
                            since = timedelta(days=30)
                        else:
                            raise ValueError('unknown period ' + args.period)
                        now = datetime.utcnow()
                        filters.append(Game.accept_date >= now - since)
                        if till:
                            filters.append(Game.accept_date < now - till)
                    orders.append(getattr(Player, ordername + '_impl')(*filters))
                    g.winrate_filt = filters
                else:
                    orders.append(getattr(Player, ordername))
                # special handling for order by winrate:
                if args.order.endswith('winrate'):
                    # sort also by game count
                    orders.append(Player.gamecount)
                    # ...and always add player.id to stabilize order
            if not args.order or not args.order.endswith('id'):
                orders.append(Player.id)
            if args.order:
                orders = map(
                    operator.methodcaller(
                        'desc' if args.order.startswith('-') else 'asc'
                    ), orders
                )
            query = query.order_by(*orders)

            query = query.limit(20)

            return jsonify(
                players=fields.List(
                    fields.Nested(
                        self.fields(public=True, stat=True,
                                    leaders='winrate' in (args.order or ''))
                    )
                ).format(query),
            )

        parser = RequestParser()
        parser.add_argument('with_stat', type=boolean_field, default=False)
        args = parser.parse_args()

        player = Player.find(id)
        if not player:
            raise NotFound

        is_self = player == user
        ret = marshal(player,
                      self.fields(public=not is_self,
                                  stat=is_self or args.with_stat,
                                  leaders=args.with_stat))
        g.winrate_filt = None  # reset
        return ret

    def post(self, id=None):
        if id:
            raise MethodNotAllowed

        log.debug('NEW USER: ' + repr(request.get_data()))

        args_login = self.parser.login.parse_args()  # check before others
        args = self.parser.parse_args()

        player = Player()
        for key, val in args.items():
            if hasattr(player, key):
                setattr(player, key, val)
        if 'userpic' in request.files:
            UserpicResource.upload(request.files['userpic'], player)
        # TODO: validate fb token?
        db.session.add(player)
        db.session.commit()

        self.greet(player)

        datadog(
            'New player registered',
            'ID: {}, email: {}, nickname: {}'.format(
                player.id,
                player.email,
                player.nickname,
            ),
            **{
                'user.id': player.id,
                'user.nickname': player.nickname,
                'user.email': player.email,
            }
        )
        dd_stat.increment('user.registration')

        return self.login_do(player, args_login, created=True)

    def greet(self, user):
        mailsend(user, 'greeting')
        # we don't check result as it is not critical if this email is not sent
        mailsend(user, 'greet_personal',
                 sender='Doug from BetGame <doug@betgame.co.uk>',
                 delayed=timedelta(days=1),
                 )

    @require_auth(allow_nonfilled=True)
    def patch(self, user, id=None):
        if not id:
            raise MethodNotAllowed

        if Player.find(id) != user:
            abort('You cannot edit another player\'s info', 403)

        args = self.parser.partial.parse_args()

        if args.password:
            if not request.is_secure and not current_app.debug:
                abort('Please use secure connection', 406)
            # if only hash available then we have no password yet
            # and will not check old password field
            if len(user.password) > 16:
                # if old password not specified, don't check it -
                # it is not secure, but allows password recovery.
                # TODO: use special token for password recovery?..
                if args.old_password:
                    if not check_password(args.old_password, user.password):
                        abort('Previous password don\'t match')

        hadmail = bool(user.email)

        for key, val in args.items():
            if val and hasattr(user, key):
                setattr(user, key, val)
                if not hadmail and key == 'email':
                    self.greet(user)
        if 'userpic' in request.files:
            UserpicResource.upload(request.files['userpic'], user)

        db.session.commit()

        return marshal(user, self.fields(public=False))

    @app.route('/players/<id>/login', methods=['POST'])
    def player_login(id):
        parser = RequestParser()
        parser.add_argument('password', required=True)
        args = parser.parse_args()

        player = Player.find(id)
        if not player:
            abort('Incorrect login or password', 404)

        if not check_password(args.password, player.password):
            abort('Incorrect login or password', 403)

        datadog('Player regular login', 'nickname: {}'.format(player.nickname))
        dd_stat.increment('user.login')

        return PlayerResource.login_do(player)

    @staticmethod
    @app.route('/federated_login', methods=['POST'])
    @secure_only
    def federated():
        parser = RequestParser()
        parser.add_argument('svc', choices=['facebook', 'twitter', 'williamhill'],
                            default='facebook')
        parser.add_argument('token', required=True)
        args = parser.parse_args()

        log.debug('fed: svc={}, token={}'.format(args.svc, args.token))

        email = None
        # get identity, name, email and userpic
        if args.svc == 'facebook':
            try:
                args.token = federatedRenewFacebook(args.token)
            except ValueError as e:
                abort('[token]: {}'.format(e), problem='token')
            # get identity and name
            ret = requests.get(
                'https://graph.facebook.com/v2.3/me',
                params=dict(
                    access_token=args.token,
                    fields='id,email,name,picture',
                ),
            )
            jret = ret.json()
            if 'error' in jret:
                err = jret['error']
                abort('Error fetching email from Facebook: {} {} ({})'.format(
                    err.get('code', ret.status_code),
                    err.get('type', ret.reason),
                    err.get('message', 'no details'),
                ))
            if 'email' in jret:
                identity = email = jret['email']
            elif 'id' in jret:
                identity = jret['id']
            else:
                abort('Facebook didn\'t return neither email nor user id')

            userpic = jret.get('picture', {}).get('data')
            if userpic:
                userpic = None if userpic['is_silhouette'] else userpic['url']

            name = jret.get('name')
        elif args.svc == 'twitter':
            # get identity and name
            jret = Twitter.identity(args.token)
            if 'error' in jret:
                abort('Error fetching info from Twitter: {}'.format(
                    jret.get('error', 'no details')))
            name = jret.get('screen_name')
            email = jret.get('email')
            identity = jret['id']
            userpic = jret.get('profile_image_url')
        elif args.svc == 'williamhill':
            if not args.token.startswith('TGT-'):
                abort('Wrong token format')
            wh = WilliamHill(args.token)
            jret = wh.request('GET', 'accounts/me', accept_simple=True)
            log.debug(str(jret))
            try:
                jret = jret['whoAccounts']['account']
                williamhill_currency = jret['currencyCode']  # TODO handle it
                name = ' '.join(filter(None, [
                    jret.get('firstName'), jret.get('lastName'),
                ]))
                email = jret['email']
                identity = jret['accountId']
                userpic = None  # no userpic for WH
            except KeyError:
                abort('Failed to fetch account information from WilliamHill')

        if name:
            n = 1
            oname = name
            while Player.query.filter_by(nickname=name).count():
                name = '{} {}'.format(oname, n)
                n += 1

        if email:
            player = Player.query.filter_by(email=email).first()
        else:
            player = Player.query.filter_by(**{args.svc + '_id': identity}).first()
        created = False
        if not player:
            created = True
            player = Player()
            player.email = email
            player.password = encrypt_password(None)  # random salt
            player.nickname = name
            db.session.add(player)
        if userpic and not player.has_userpic:
            UserpicResource.fromurl(userpic, player)

        setattr(player, '{}_id'.format(args.svc), identity)
        setattr(player, '{}_token'.format(args.svc), args.token)

        datadog('Player federated ' + ('registration' if created else 'login'),
                'nickname: {}, service: {}, id: {}, email: {}'.format(
                    player.nickname,
                    args.svc,
                    identity,
                    email,
                ),
                service=args.svc)
        dd_stat.increment('user.login_' + args.svc)
        if created:
            dd_stat.increment('user.registration')

        return PlayerResource.login_do(player, created=created)

    @app.route('/players/<id>/reset_password', methods=['POST'])
    def reset_password(id):
        player = Player.find(id)
        if not player:
            abort('Unknown nickname, gamertag or email', 404)

        # send password recovery link
        ret = mailsend(player, 'recover',
                       link='https://betgame.co.uk/password.html'
                            '#userid={}&token={}'.format(
                           player.id,
                           makeToken(player)
                       ))
        if not ret:
            return jsonify(success=False, message='Couldn\'t send mail')

        return jsonify(
            success=True,
            message='Password recovery link sent to your email address',
        )

    @app.route('/players/<id>/pushtoken', methods=['POST'])
    @require_auth
    def pushtoken(user, id):
        if Player.find(id) != user:
            raise Forbidden

        if not g.device_id:
            abort('No device id in auth token, please auth again', problem='token')

        parser = RequestParser()
        parser.add_argument('push_token',
                            type=hex_field(64),  # = 32 bytes
                            required=True)
        args = parser.parse_args()

        # first try find device which already uses this token
        dev = Device.query.filter_by(player=user,
                                     push_token=args.push_token
                                     ).first()
        # if we found it, then we will actually just update its last login date
        if not dev:
            # if not found - get current one (which most likely has no token)
            dev = Device.query.get(g.device_id)
            if not dev:
                abort('Device id not found', 500)
            if dev.push_token:
                abort('This device already has push token specified')
            dev.push_token = args.push_token
            dev.failed = False  # just to ensure

            # and remove that token from other devices
            Device.query.filter(
                Device.player != user,
                Device.push_token == args.push_token,
            ).delete()

        # update last login as it may be another device object
        # than one that was used for actual login
        dev.last_login = datetime.utcnow()
        db.session.commit()
        return jsonify(success=True)

    @app.route('/players/<id>/logout', methods=['POST'])
    @require_auth
    def logout(user, id):
        if Player.find(id) != user:
            raise Forbidden

        if not g.device_id:
            abort('No device id in auth token, please auth again', problem='token')

        parser = RequestParser()
        parser.add_argument('push_token',
                            type=hex_field(64),  # = 32 bytes
                            required=False)
        args = parser.parse_args()

        # get current device (which most likely has push token)
        dev = None
        if args.push_token:
            dev = Device.query.filter_by(
                player=user,
                push_token=args.push_token
            ).first()
        if not dev:
            dev = Device.query.get(g.device_id)
        if not dev:
            abort('No device record found')
        if not dev.push_token:
            return jsonify(
                success=False,
                reason='This device is already without push token',
            )

        dev.push_token = None
        # TODO: delete device itself?
        db.session.commit()

        return jsonify(success=True)

    @app.route('/players/<id>/recent_opponents')
    @require_auth
    def recent_opponents(user, id):
        if Player.find(id) != user:
            raise Forbidden

        return jsonify(opponents=fields.List(fields.Nested(
            PlayerResource.fields(public=True)
        )).format(user.recent_opponents))

    @app.route('/players/<id>/winratehist')
    @require_auth
    def winratehist(user, id):
        if Player.find(id) != user:
            raise Forbidden
        parser = RequestParser()
        parser.add_argument('range', type=int, required=True)
        parser.add_argument('interval', required=True, choices=(
            'day', 'week', 'month'))
        args = parser.parse_args()

        params = {
            args.interval + 's': args.range,
        }
        return jsonify(
            history=[
                dict(
                    date=date,
                    games=total,
                    wins=float(wins),
                    rate=float(rate),
                ) for date, total, wins, rate in user.winratehist(**params)
            ],
        )

    @app.route('/players/<id>/leaderposition')
    @require_auth
    def leaderposition(user, id):
        player = Player.find(id)
        if not player:
            raise NotFound
        return jsonify(
            position=player.leaderposition,
        )

@api.resource(
    '/friends',
    '/friends/',
    '/friends/<id>',
)
class FriendsResource(restful.Resource):
    @require_auth
    def get(self, user, id=None):
        if id:
            raise NotImplemented

        # TODO: fetch this user's friends
        # For now will just return set of predefined users
        players = Player.query.filter(Player.nickname.like('test_player_%'))

        return dict(
            players=fields.List(
                fields.Nested(
                    PlayerResource.fields(public=True)
                )
            ).format(players),
        )


# Userpic
class UploadableResource(restful.Resource):
    PARAM = None
    ROOT = os.path.dirname(__file__) + '/../uploads'
    SUBDIR = None
    ALLOWED = None

    @classmethod
    def url_for(cls, entity, ext):
        return '/uploads/{}/{}'.format(
            cls.SUBDIR,
            '{}.{}'.format(entity.id, ext),
        )

    @classmethod
    def file_for(cls, entity, ext):
        return os.path.join(
            cls.ROOT,
            cls.SUBDIR,
            '{}.{}'.format(entity.id, ext),
        )

    @classmethod
    def findfile(cls, entity):
        for ext in cls.ALLOWED:
            f = cls.file_for(entity, ext)
            if os.path.exists(f):
                return f
        return None

    @classmethod
    def onupload(cls, entity, ext):
        pass

    @classmethod
    def ondelete(cls, entity):
        pass

    @classmethod
    def found(cls, entity, ext):
        pass

    @classmethod
    def notfound(cls, entity):
        pass

    @classmethod
    def delfile(cls, entity):
        deleted = False
        for ext in cls.ALLOWED:
            f = cls.file_for(entity, ext)
            if os.path.exists(f):
                os.remove(f)
                log.debug('removed {}'.format(f))
                deleted = True
                cls.ondelete(entity)
        return deleted

    @classmethod
    def upload(cls, f, entity):
        ext = f.filename.lower().rsplit('.', 1)[-1]
        if ext not in cls.ALLOWED:
            abort('[{}]: {} files are not allowed'.format(
                cls.PARAM, ext.upper()))

        # FIXME: limit size

        cls.delfile(entity)

        f.save(cls.file_for(entity, ext))

        cls.onupload(entity, ext)

        datadog('{} uploaded'.format(cls.PARAM), 'original filename: {}'.format(
            f.filename))

    @classmethod
    def fromurl(cls, url, entity):
        if len(cls.ALLOWED) > 1:
            raise ValueError('This is only applicable for single-ext resources')

        cls.delfile(entity)
        ret = requests.get(url, stream=True)
        with open(cls.file_for(entity, cls.ALLOWED[0]), 'wb') as f:
            for chunk in ret.iter_content(1024):
                f.write(chunk)

        cls.onupload(entity, cls.ALLOWED[0])

        datadog('{} uploaded from url'.format(cls.PARAM), 'original filename: {}, url: {}'.format(
            f.filename, url))

    def get_entity(self, kwargs, is_put):
        raise NotImplementedError  # override this!

    def get(self, **kwargs):
        entity = self.get_entity(kwargs, False)
        for ext in self.ALLOWED:
            f = self.file_for(entity, ext)
            if os.path.exists(f):
                self.found(entity, ext)
                response = make_response()
                response.headers['X-Accel-Redirect'] = self.url_for(entity, ext)
                response.headers['Content-Type'] = ''  # autodetect by nginx
                return response
        else:
            self.notfound(entity)
            return (None, 204)  # HTTP code 204 NO CONTENT

    def put(self, **kwargs):
        entity = self.get_entity(kwargs, True)
        f = request.files.get(self.PARAM)
        if not f:
            abort('[{}]: please provide file!'.format(self.PARAM))

        self.upload(f, entity)

        return dict(success=True)

    def post(self, *args, **kwargs):
        return self.put(*args, **kwargs)

    def delete(self, **kwargs):
        return dict(
            deleted=self.delfile(self.get_entity(kwargs, True)),
        )


@api.resource('/players/<id>/userpic')
class UserpicResource(UploadableResource):
    PARAM = 'userpic'
    SUBDIR = 'userpics'
    ALLOWED = ['png']

    @require_auth
    def get_entity(self, args, is_put, user):
        player = Player.find(args['id'])
        if not player:
            raise NotFound
        if is_put and player != user:
            raise Forbidden
        return player


# Balance
@app.route('/balance', methods=['GET'])
@require_auth
def balance_get(user):
    return jsonify(
        balance=user.balance_obj,
    )


@app.route('/balance/history', methods=['GET'])
@require_auth
def balance_history(user):
    parser = RequestParser()
    parser.add_argument('page', type=int, default=1)
    parser.add_argument('results_per_page', type=int, default=10)
    args = parser.parse_args()

    if args.results_per_page > 50:
        abort('[results_per_page]: max is 50')

    query = user.transactions
    total_count = query.count()
    query = query.paginate(args.page, args.results_per_page,
                           error_out=False).items

    return jsonify(
        transactions=fields.List(fields.Nested(dict(
            id=fields.Integer,
            date=fields.DateTime,
            type=fields.String,
            sum=fields.Float,
            balance=fields.Float,
            game_id=fields.Integer,
            comment=fields.String,
        ))).format(query),
        num_results=total_count,
        total_pages=math.ceil(total_count / args.results_per_page),
        page=args.page,
    )


@app.route('/balance/deposit', methods=['POST'])
@require_auth
def balance_deposit(user):
    parser = RequestParser()
    parser.add_argument('payment_id', required=False)
    parser.add_argument('total', type=float, required=True)
    parser.add_argument('currency', required=True)
    parser.add_argument('dry_run', type=boolean_field, default=False)
    args = parser.parse_args()

    datadog(
        'Payment received',
        ' '.join(
            ['{}: {}'.format(k, v) for k, v in args.items()]
        ),
    )

    rate = Fixer.latest(args.currency, 'USD')
    if not rate:
        abort('[currency]: Unknown currency {}'.format(args.currency),
              problem='currency')
    coins = args.total * rate

    if not args.dry_run:
        if not args.payment_id:
            abort('[payment_id]: required unless dry_run is true')
        # verify payment...
        ret = PayPal.call('GET', 'payments/payment/' + args.payment_id)
        if ret.get('state') != 'approved':
            abort('Payment not approved: {} - {}'.format(
                ret.get('name', '(no error code)'),
                ret.get('message', '(no error message)'),
            ), success=False)

        transaction = None
        for tr in ret.get('transactions', []):
            amount = tr.get('amount')
            if (
                        (float(amount['total']), amount['currency']) ==
                        (args.total, args.currency)
            ):
                transaction = tr
                break
        else:
            abort('No corresponding transaction found', success=False)

        for res in transaction.get('related_resources', []):
            sale = res.get('sale')
            if not sale:
                continue
            if sale.get('state') == 'completed':
                break
        else:
            abort('Sale is not completed', success=False)

        # now payment should be verified
        log.info('Payment approved, adding coins')

        user.balance += coins
        db.session.add(Transaction(
            player=user,
            type='deposit',
            sum=coins,
            balance=user.balance,
            comment='Converted from {} {}'.format(args.total, args.currency),
        ))
        db.session.commit()
    return jsonify(
        success=True,
        dry_run=args.dry_run,
        added=coins,
        balance=user.balance_obj,
    )


@app.route('/balance/withdraw', methods=['POST'])
@require_auth
def balance_withdraw(user):
    parser = RequestParser()
    parser.add_argument('coins', type=float, required=True)
    parser.add_argument('currency', default='USD')
    parser.add_argument('paypal_email', type=email, required=False)
    parser.add_argument('dry_run', type=boolean_field, default=False)
    args = parser.parse_args()

    if args.coins < config.WITHDRAW_MINIMUM:
        abort('Too small amount, minimum withdraw amount is {} coins'
              .format(config.WITHDRAW_MINIMUM))

    try:
        amount = dict(
            value=args.coins
                  * Fixer.latest('USD', args.currency)
                  * config.WITHDRAW_COEFFICIENT,
            currency=args.currency,
        )
    except (TypeError, ValueError):
        # for bad currencies, Fixer will return None
        # and coins*None results in TypeError
        abort('Unknown currency provided')

    if user.available < args.coins:
        abort('Not enough coins')

    if args.dry_run:
        return jsonify(
            success=True,
            paid=amount,
            dry_run=True,
            balance=user.balance_obj,
        )
    if not args.paypal_email:
        abort('[paypal_email] should be specified unless you are running dry-run')

    # first withdraw coins...
    user.balance -= args.coins
    db.session.add(Transaction(
        player=user,
        type='withdraw',
        sum=-args.coins,
        balance=user.balance,
        comment='Converted to {} {}'.format(
            amount,
            args.currency,
        ),
    ))
    db.session.commit()

    # ... and only then do actual transaction;
    # will return balance if failure happens

    try:
        ret = PayPal.call('POST', 'payments/payouts', dict(
            sync_mode=True,
        ), dict(
            sender_batch_header=dict(
                # sender_batch_id = None,
                email_subject='Payout from BetGame',
                recipient_type='EMAIL',
            ),
            items=[
                dict(
                    recipient_type='EMAIL',
                    amount=amount,
                    receiver=args.paypal_email,
                ),
            ],
        ))
        try:
            trinfo = ret['items'][0]
        except IndexError:
            trinfo = None
        stat = trinfo.get('transaction_status')
        if stat == 'SUCCESS':
            datadog(
                'Payout succeeded to {}, {} coins'.format(
                    args.paypal_email, args.coins),
            )
            return jsonify(success=True,
                           dry_run=False,
                           paid=amount,
                           transaction_id=trinfo.get('payout_item_id'),
                           balance=user.balance_obj,
                           )
        # TODO: add transaction id to our Transaction object
        log.debug(str(ret))
        log.warning('Payout failed to {}, {} coins, stat {}'.format(
            args.paypal_email, args.coins, stat))
        if stat in ['PENDING', 'PROCESSING']:
            # TODO: wait and retry
            pass

        abort('Couldn\'t complete payout: ' +
              trinfo.get('errors', {}).get('message', 'Unknown error'),
              500,
              status=stat,
              transaction_id=trinfo.get('payout_item_id'),
              paypal_code=ret.get('_code'),
              success=False,
              dry_run=False,
              )
    except Exception as e:
        # restore balance
        user.balance += args.coins
        db.session.add(Transaction(
            player=user,
            type='withdraw',
            sum=args.coins,
            balance=user.balance,
            comment='Withdraw operation aborted due to error',
        ))
        db.session.commit()

        log.error('Exception while performing payout', exc_info=True)

        if isinstance(e, HTTPException):
            raise

        abort('Couldn\'t complete payout', 500,
              success=False, dry_run=False)


_gamedata_cache = None
# Game types
@app.route('/gametypes', methods=['GET'])
def gametypes():
    parser = RequestParser()
    parser.add_argument('betcount', type=boolean_field, default=False)
    parser.add_argument('latest', type=boolean_field, default=False)
    parser.add_argument('identities', type=boolean_field, default=True)
    parser.add_argument('filter')
    parser.add_argument('filt_op',
                        choices=['startswith', 'contains'],
                        default='startswith',
                        )
    args = parser.parse_args()
    if args.filter:
        args.identities = False

    counts = {}
    if args.betcount:
        bca = (db.session.query(Game.gametype,
                                func.count(Game.gametype),
                                func.max(Game.create_date),
                                )
               .group_by(Game.gametype).all())
        counts = {k: (c, d) for k, c, d in bca}
    times = []
    if args.latest:
        bta = (Game.query
               .with_entities(Game.gametype, func.max(Game.create_date))
               .group_by(Game.gametype)
               .order_by(Game.create_date.desc())
               .all())
        times = bta  # in proper order

    global _gamedata_cache
    if _gamedata_cache:
        gamedata = _gamedata_cache.copy()
    else:
        gamedata = []
        for poller in Poller.allPollers():
            if poller is TestPoller:
                continue
            for gametype, gametype_name in poller.gametypes.items():
                _getsub = lambda f: f.get(gametype) if isinstance(f, dict) else f
                data = dict(
                    id=gametype,
                    name=gametype_name,
                    subtitle=_getsub(poller.subtitle),
                    category=_getsub(poller.category),
                    description=_getsub(poller.description),
                )
                if data['description']:
                    # strip enclosing whites,
                    # then replace single \n's with spaces
                    # and double \n's with single \n's
                    data['description'] = '\n'.join(map(
                        lambda para: ' '.join(map(
                            lambda line: line.strip(),
                            para.split('\n')
                        )),
                        data['description'].strip().split('\n\n')
                    ))
                if poller.identity or poller.twitch_identity or poller.honesty:
                    data.update(dict(
                        supported=True,
                        gamemodes=poller.gamemodes,
                        identity=poller.identity_id,
                        identity_name=poller.identity_name,
                        honesty_only=poller.honesty,
                        twitch=poller.twitch,
                        twitch_identity=None,  # may be updated below
                        twitch_identity_name=None,
                    ))
                    if poller.twitch_identity:
                        data['twitch_identity'] = poller.twitch_identity.id
                        data['twitch_identity_name'] = poller.twitch_identity.name
                else:  # DummyPoller
                    data.update(dict(
                        supported=False,
                    ))
                gamedata.append(data)
        _gamedata_cache = gamedata.copy()
    if args.betcount:
        for data in gamedata:
            data['betcount'], data['lastbet'] = \
                counts.get(data['id'], (0, None))
    if args.filter:
        args.filter = args.filter.casefold()
        if args.filt_op == 'contains':
            args.filt_op = '__contains__'
        gamedata = list(filter(
            # search string in name or title
            lambda item: (
                getattr(
                    item['name'].casefold(),
                    args.filt_op
                )(args.filter)
                or getattr(
                    (item.get('subtitle') or '').casefold(),
                    args.filt_op
                )(args.filter)
            ),
            gamedata,
        ))

    ret = dict(
        gametypes=gamedata,
    )
    if args.identities:
        ret['identities'] = {i.id: i.name for i in Identity.all}
    if args.latest:
        ret['latest'] = [
            dict(
                gametype=gametype,
                date=date,
            ) for gametype, date in times
        ]
    return jsonify(**ret)


@app.route('/gametypes/<id>/image')
@app.route('/gametypes/<id>/background')
def gametype_image(id):
    if id not in Poller.all_gametypes:
        raise NotFound

    parser = RequestParser()
    parser.add_argument('w', type=int, required=False)
    parser.add_argument('h', type=int, required=False)
    args = parser.parse_args()

    filename = 'images/{}{}.png'.format(
        'bg/' if request.path.endswith('/background') else '',
        id,
    )
    try:
        img = Image.open(filename)
    except FileNotFoundError:
        raise NotFound  # 404
    ow, oh = img.size
    if args.w or args.h:
        if not args.h or (args.w and args.h and (args.w / args.h) > (ow / oh)):
            dw = args.w
            dh = round(oh / ow * dw)
        else:
            dh = args.h
            dw = round(ow / oh * dh)

        # resize
        img = img.resize((dw, dh), Image.ANTIALIAS)

        # crop if needed
        if args.w and args.h:
            if args.w != dw:
                # crop horizontally
                cw = (dw - args.w) / 2
                cl, cr = math.floor(cw), math.ceil(cw)
                img = img.crop(box=(cl, 0, img.width - cr, img.height))
            elif args.h != dh:
                # crop vertically
                ch = (dh - args.h) / 2
                cu, cd = math.floor(ch), math.ceil(ch)
                img = img.crop(box=(0, cu, img.width, img.height - cd))

    img_file = BytesIO()
    img.save(img_file, 'png')
    img_file.seek(0)
    return send_file(img_file, mimetype='image/png')


@app.route('/identities', methods=['GET'])
def identities():
    os.path.dirname(__file__)
    return jsonify(
        identities=[
            dict(
                id=i.id,
                name=i.name,
                choices=i.choices,
            ) for i in Identity.all
        ],
    )


# Games
@api.resource(
    '/games',
    '/games/',
    '/games/<int:id>',
)
class GameResource(restful.Resource):
    @classproperty
    def fields_lite(cls):
        return {
            'id': fields.Integer,
            'creator': fields.Nested(PlayerResource.fields(public=True)),
            'opponent': fields.Nested(PlayerResource.fields(public=True)),
            'parent_id': fields.Integer,
            'is_root': fields.Boolean,
            'gamertag_creator': fields.String(attribute='gamertag_creator_text'),
            'gamertag_opponent': fields.String(attribute='gamertag_creator_text'),
            'identity_id': fields.String,
            'identity_name': fields.String,
            'twitch_handle': fields.String,
            'twitch_identity_creator': fields.String,
            'twitch_identity_opponent': fields.String,
            'twitch_identity_id': fields.String,
            'twitch_identity_name': fields.String,
            'gametype': fields.String,
            'gamemode': fields.String,
            'is_ingame': fields.Boolean,
            'bet': fields.Float,
            'has_message': fields.Boolean,
            'create_date': fields.DateTime,
            'state': fields.String,
            'accept_date': fields.DateTime,
            'aborter': fields.Nested(PlayerResource.fields(public=True),
                                     allow_null=True),
            'winner': fields.String,
            'details': fields.String,
            'finish_date': fields.DateTime,
            'tournament_id': fields.Integer,
        }

    @classproperty
    def fields(cls):
        ret = cls.fields_lite.copy()
        ret.update({
            'children': fields.List(fields.Nested(cls.fields_lite)),
        })
        return ret

    @require_auth
    def get(self, user, id=None):
        if id:
            game = Game.query.get_or_404(id)

            # TODO: allow?
            if not game.is_game_player(user):
                raise Forbidden

            return marshal(game, self.fields)

        parser = RequestParser()
        parser.add_argument('page', type=int, default=1)
        parser.add_argument('results_per_page', type=int, default=10)
        parser.add_argument(
            'order',
            choices=sum(
                [[s, '-' + s]
                 for s in
                 ('create_date',
                  'accept_date',
                  'gametype',
                  'creator_id',
                  'opponent_id',
                  )], []),
            required=False,
        )
        args = parser.parse_args()
        # cap
        if args.results_per_page > 50:
            abort('[results_per_page]: max is 50')

        query = user.games

        # TODO: filters
        if args.order:
            if args.order.startswith('-'):
                order = getattr(Game, args.order[1:]).desc()
            else:
                order = getattr(Game, args.order).asc()
            query = query.order_by(order)

        total_count = query.count()
        query = query.paginate(args.page, args.results_per_page,
                               error_out=False)

        return dict(
            games=fields.List(fields.Nested(self.fields)).format(query.items),
            num_results=total_count,
            total_pages=math.ceil(total_count / args.results_per_page),
            page=args.page,
        )

    @classproperty
    def postparser(cls):
        parser = RequestParser()
        parser.add_argument('root_id',
                            type=lambda id: Game.query.filter_by(id=id).one(),
                            required=False, dest='root')
        parser.add_argument('opponent_id', type=Player.find_or_fail,
                            required=False, dest='opponent')
        parser.add_argument('gamertag_creator', required=False)
        parser.add_argument('savetag', default='never', choices=(
            'never', 'ignore_if_exists', 'fail_if_exists', 'replace'))
        parser.add_argument('gamertag_opponent', required=False)
        parser.add_argument('twitch_handle',
                            type=Twitch.check_handle,
                            required=False)
        parser.add_argument('twitch_identity_creator', required=False)
        parser.add_argument('twitch_identity_opponent', required=False)
        parser.add_argument('gametype', choices=Poller.all_gametypes,
                            required=False)
        parser.add_argument('bet', type=float, required=False)
        parser.add_argument('tournament_id', type=Tournament.query.get_or_404,
                            required=False, dest='tournament')
        return parser

    @classmethod
    def post_parse_args(cls):
        args = cls.postparser.parse_args()
        args.gamemode = None # will be handled below
        return args
    @classmethod
    def post_parse_poller_args(cls, poller):
        if poller.gamemodes:
            gmparser = RequestParser()
            gmparser.add_argument('gamemode', choices=poller.gamemodes,
                                  required=False)
            gmargs = gmparser.parse_args()
            return gmargs
        return {}

    @classmethod
    def load_save_identities(cls, poller, args, role, user,
                             optional=False, game=None):
        """
        Check passed identities (in args),
        load them from user profiles if needed,
        and update profile's identities if required.

        :param poller: poller class for current game
        :param args: arguments to work with
        :param role: role to handle identities for (creator or opponent)
        :param user: current role's user object.
        :param optional: is it allowed to omit this identity;
        also will not update user's field if this is True.
        If not passed then will just validate identities.
        """
        had_creatag = args.get('gamertag_creator')
        for name, identity, required in (
            ('gamertag', poller.identity, not poller.honesty),
            ('twitch_identity', poller.twitch_identity, poller.twitch == 2),
        ):
            argname = '{}_{}'.format(name, role)

            # if certain identity is not supported for this game
            # then check its absence in args
            if not identity:
                if args.get(argname):
                    abort('[{}]: not supported for this game type'.format(
                        argname), problem=argname)
                continue # no need to check other clauses

            if args.get(argname):
                # was passed -> validate and maybe save
                try:
                    args[argname] = identity.checker(args[argname])
                except ValueError as e:
                    abort('[{}]: Invalid {}: {}'.format(
                        argname, identity.name, e
                    ), problem=argname)
            else:
                # try load this from user object
                args[argname] = getattr(user, identity.id)
                if required and not optional and not args[argname]:
                    abort('Please specify your {}'.format(
                        identity.name,
                    ), problem=argname)
        # Update creator's identity if requested
        if had_creatag and poller.identity \
                and not optional and args.get('savetag'):
            if args.savetag == 'replace':
                repl = True
            elif args.savetag == 'never':
                repl = False
            elif args.savetag == 'ignore_if_exists':
                repl = not getattr(user, poller.identity.id)
            elif args.savetag == 'fail_if_exists':
                repl = True
                if getattr(user, poller.identity.id) != args.gamertag_creator:
                    abort('{} is already set and is different!'.format(
                        poller.identity.name), problem='savetag')
            if repl:
                setattr(user, poller.identity.id, args.gamertag_creator)
    @classmethod
    def check_identity_equality(cls, poller, creators, opponents):
        """
        :param creators: object to get creator's identity from
        :param opponents: object to get opponent's identity from
        """
        for name, identity in (
            ('gamertag', poller.identity),
            ('twitch_identity', poller.twitch_identity)
        ):
            creaname, opponame = ('{}_{}'.format(name, role)
                                  for role in ('creator', 'opponent'))
            if getattr(creators, creaname) == getattr(opponents, opponame):
                abort('You cannot specify {} same as your opponent\'s!'.format(
                    identity.name,
                ))

    @classmethod
    def check_same_region(cls, poller, crea, oppo):
        # crea & oppo are identities (primary ones, not twitch)
        if not poller.sameregion:
            # no need to check
            return
        if not oppo:
            # opponent identity not passed - cannot check
            return
        # this is an additional check for regions
        region1 = crea.split('/', 1)[0]
        region2 = oppo.split('/', 1)[0]
        if region1 != region2:
            abort('You and your opponent should be in the same region; '
                    'but actually you are in {} and your opponent is in {}'.format(
                region1, region2))
    @classmethod
    def check_bet_amount(cls, bet, user):
        if bet < 0.99:  # FIXME: hardcoded min bet
            abort('Bet is too low', problem='bet')
        if bet > user.available:
            abort('You don\'t have enough coins', problem='coins')
    @require_auth
    def post(self, user, id=None):
        if id:
            raise MethodNotAllowed
        args = self.post_parse_args()

        poller = Poller.findPoller(args.gametype)
        if not poller or poller == DummyPoller:
            abort('Support for this game is coming soon!')

        args.update(self.post_parse_poller_args(poller))

        # check tournament-related settings
        if args.tournament:
            if args.bet or args.opponent:
                abort('Bet and opponent shall not be provided in tournament mode')
        else:
            if not (args.bet and args.opponent):
                abort('Please provide bet amount and choose your opponent '
                      'when not in tournament mode')
            if not (args.gamemode and args.gametype):
                abort('Please provide gamemode and gametype '
                      'when not in tournament mode')

        if args.tournament:
            # request opponent from tournament
            args.opponent = args.tournament.get_opponent(user)

        if args.opponent == user:
            abort('You cannot compete with yourself')

        # determine identities and update them on args
        self.load_save_identities(poller, args, 'creator', user)
        self.load_save_identities(poller, args, 'opponent', args.opponent,
                                  optional=True)
        self.check_identity_equality(poller, args, args)

        # Perform sameregion check
        self.check_same_region(
            poller,
            args.gamertag_creator,
            args.gamertag_opponent)

        # check twitch parameter if needed
        if poller.twitch == 2 and not args.twitch_handle:
            abort('Please specify your twitch stream URL',
                  problem='twitch_handle')
        if args.twitch_handle and not poller.twitch:
            abort('Twitch streams are not yet supported for this gametype')

        if not args.tournament:
            # check bet amount
            self.check_bet_amount(args.bet, user)

        game = Game()
        game.creator = user
        game.opponent = args.opponent
        log.debug('setting parent')
        if args.root:
            game.parent = args.root.root  # ensure we use real root
        game.gamertag_creator = args.gamertag_creator
        game.gamertag_opponent = args.gamertag_opponent
        game.twitch_handle = args.twitch_handle
        game.twitch_identity_creator = args.twitch_identity_creator
        game.twitch_identity_opponent = args.twitch_identity_opponent
        if args.tournament:
            game.bet = 0
            game.tournament = args.tournament
            game.gametype = args.tournament.gametype
            game.gamemode = args.tournament.gamemode
        else:
            game.gametype = args.gametype
            game.gamemode = args.gamemode
            game.bet = args.bet

        db.session.add(game)
        db.session.commit()

        log.debug('notifying')
        notify_users(game)

        return marshal(game, self.fields), 201

    def patch(self, id=None):
        if not id:
            raise MethodNotAllowed

        parser = RequestParser()
        parser.add_argument('state', choices=[
            'accepted', 'declined', 'cancelled'
        ], required=True)
        parser.add_argument('gamertag_opponent', required=False)
        parser.add_argument('twitch_identity_opponent', required=False)
        args = parser.parse_args()

        game = Game.query.get_or_404(id)

        user = check_auth()
        if user == game.creator:
            if args.state not in ['cancelled']:
                abort('Only {} can accept or decline this challenge'.format(
                    game.opponent.nickname,
                ))
        elif user == game.opponent:
            if args.state not in ['accepted', 'declined']:
                abort('Only {} can cancel this challenge'.format(
                    game.creator.nickname,
                ))
        else:
            abort('You cannot access this challenge', 403)

        if game.state != 'new':
            abort('This challenge is already {}'.format(game.state))

        if args.state == 'accepted':
            self.check_bet_amount(game.bet, user)

        poller = Poller.findPoller(game.gametype)

        if args.state == 'accepted':
            # handle identities
            self.load_save_identities(poller, args, 'opponent', user, game=game)
            self.check_identity_equality(poller, game, args)
            for name in 'gamertag', 'twitch_identity':
                argname = '{}_opponent'.format(name)
                if not args[argname]:
                    continue # was already checked -> not needed here
                if getattr(game, argname) != args[argname]:
                    log.warning(
                        'Game {}: changing {} opponent identity '
                        'from {} to {}'.format(
                            game.id,
                            'primary' if name == 'gamertag' else 'secondary',
                            getattr(game, argname),
                            args[argname],
                        )
                    )
                setattr(game, argname, args[argname])

            # Perform sameregion check
            self.check_same_region(
                poller,
                args.gamertag_opponent,
                game.gamertag_creator)


        # now all checks are done, perform actual logic

        if args.state == 'accepted':
            try:
                poller.gameStarted(game)
            except Exception as e:
                log.exception('Error in gameStarted for {}: {}'.format(
                    poller, e))
                abort('Failed to initialize poller, please contact support!', 500)

        # Now, before we save state change, start twitch stream if required
        # so that we can abort request if it failed
        if game.twitch_handle and args.state == 'accepted':
            try:
                ret = requests.put(
                    '{}/streams/{}/{}'.format(
                        config.OBSERVER_URL,
                        game.twitch_handle,
                        game.gametype,
                    ),
                    data=dict(
                        game_id=game.id,
                        creator=game.twitch_identity_creator
                        if poller.twitch_identity else
                        game.gamertag_creator,
                        opponent=game.twitch_identity_opponent
                        if poller.twitch_identity else
                        game.gamertag_opponent,
                    ),
                )
            except Exception:
                log.exception('Failed to start twitch stream!')
                abort('Cannot start twitch observing - internal error', 500)
            if ret.status_code not in (200, 201):
                jret = ret.json()
                if ret.status_code == 409:  # dup
                    # TODO: check it on creation??
                    abort('This twitch stream is already watched '
                          'for another game (or another players)')
                elif ret.status_code == 507:  # full
                    abort('Cannot start twitch observing, all servers are busy now; '
                          'please retry later', 500)
                abort('Couldn\'t start Twitch: ' + jret.get('error', 'Unknown err'))

        game.state = args.state
        game.accept_date = datetime.utcnow()

        if args.state == 'accepted':
            # bet is locked on creator's account; lock it on opponent's as well
            game.opponent.locked += game.bet
        else:
            # bet was locked on creator's account; unlock it
            game.creator.locked -= game.bet

        db.session.commit()

        notify_users(game)

        return marshal(game, self.fields)

    @require_auth
    def delete(self, user, id=None):
        if not id:
            raise MethodNotAllowed
        game = Game.query.get_or_404(id)
        if not game.is_game_player(user):
            raise Forbidden('You cannot access this challenge')
        if not game.aborter or game.aborter == user:
            # aborting not started, so initiate it.
            # or maybe it is already started, so just ask again
            # (make another event)
            game.aborter = user
            notify_event(
                game.root, 'abort',
                game=game,
            )
            return dict(
                started=True,
            )
        if game.aborter != game.other(user):
            abort('Internal error, wrong aborter', 500)
        Poller.gameDone(game, 'aborted',
                        details='Challenge was aborted by request of ' + game.aborter.nickname,
                        )
        return dict(
            aborted=True,
        )


@api.resource('/games/<int:game_id>/report')
class GameReportResource(restful.Resource):
    fields = {
        'result': fields.String,
        'created': fields.DateTime,
        'modified': fields.DateTime,
        'match': fields.Boolean,
        'ticket_id': fields.Integer,
    }

    def get_game(self, user, game_id):
        game = Game.query.get_or_404(game_id)
        if not game.is_game_player(user):
            raise Forbidden
        return game

    def get_report(self, user, game):
        report = Report.query.filter(Report.game == game, Report.player == user).first()
        if not report:
            raise NotFound
        return report

    def check_report(self, report, game):
        report.match = report.check_reports()
        if not report.match:
            ticket = None
            for t in game.tickets:
                if t.type == 'reports_mismatch':
                    ticket = t
            if not ticket:
                ticket = Ticket(game, 'reports_mismatch')
                db.session.add(ticket)
            db.session.flush()
            report.ticket = ticket
            if report.other_report:
                report.other_report.ticket = ticket
            db.session.commit()
            notify_event(game, 'report', message='reports don\' match, ticket {id} created'.format(
                id=ticket.id
            ))
        else:
            if report.match and report.other_report:
                winner = None
                if report.result == 'won':
                    winner = report.player
                if report.other_report.result == 'won':
                    winner = report.other_report.player
                game_winner = 'draw'
                if winner == game.creator:
                    game_winner = 'creator'
                if winner == game.opponent:
                    game_winner = 'opponent'
                poller = Poller.findPoller(game.gametype)
                endtime = min(report.created, report.other_report.created)
                poller.gameDone(game, game_winner, endtime)

    @property
    def result(self):
        parser = RequestParser()
        parser.add_argument('result', choices=[
            'won', 'lost', 'draw',
        ], required=False)
        args = parser.parse_args()
        return args.result

    @require_auth
    def post(self, user, game_id):
        game = self.get_game(user, game_id)

        db.session.flush()
        try:
            report = Report(game, user, self.result)
            db.session.add(report)
            db.session.flush()
        except IntegrityError:
            abort('You have already reported this game', problem='duplicate')
            return

        # update badges for participants of this game
        game_badge_signal.send(game)

        db.session.commit()

        notify_event(game, 'report', message='{user} reported {result}'.format(
            user=user.nickname,
            result=report.result,
        ))
        self.check_report(report, game)

        return marshal(report, self.fields)

    @require_auth
    def get(self, user, game_id):
        game = self.get_game(user, game_id)  # check if such game exists and accessible for this user
        report = self.get_report(user, game_id)
        self.check_report(report, game)
        return marshal(report, self.fields)

    @require_auth
    def patch(self, user, game_id):
        game = self.get_game(user, game_id)  # check if such game exists and accessible for this user
        report = self.get_report(user, game)
        report.modify(self.result)
        db.session.commit()
        notify_event(game, 'report', message='{user} changed his report to {result}'.format(
            user=user.nickname,
            result=report.result,
        ))
        self.check_report(report, game)
        return marshal(report, self.fields)

@api.resource(
    '/games/<int:game_id>/tickets',
    '/tickets/<int:ticket_id>'
)
class TicketResource(restful.Resource):
    @property
    def fields(self):
        return {
            'id': fields.Integer,
            'open': fields.Boolean,
            'game_id': fields.Integer,
            'type': fields.String,
            'messages': fields.List(fields.Nested(ChatMessageResource.fields))
        }

    @require_auth
    def get(self, user, game_id=None, ticket_id=None):
        if ticket_id:
            ticket = Ticket.query.get_or_404(ticket_id)
            if not ticket.game.is_game_player(user):
                raise Forbidden
            return marshal(ticket, self.fields)
        if game_id:
            game = Game.query.get_or_404(game_id)
            if not game.is_game_player(user):
                raise Forbidden
            return marshal(game.tickets, fields.List(self.fields))
        raise NotFound

@api.resource(
    '/games/<int:id>/msg',
    '/games/<int:id>/msg.mp4',
    '/games/<int:id>/msg.m4a',
)
class GameMessageResource(UploadableResource):
    PARAM = 'msg'
    SUBDIR = 'messages'
    ALLOWED = ['mpg', 'mp3', 'ogg', 'ogv', 'mp4', 'm4a']

    @require_auth
    def get_entity(self, args, is_put, user):
        game = Game.query.get_or_404(args['id'])
        if not game.is_game_player(user):
            raise Forbidden
        if is_put and user != game.creator:
            raise Forbidden
        if is_put and game.state != 'new':
            abort('This game is already {}'.format(game.state))
        return game


# Messaging
@api.resource(
    '/players/<player_id>/messages',
    '/players/<player_id>/messages/',
    '/players/<player_id>/messages/<int:id>',
    '/games/<int:game_id>/messages',
    '/games/<int:game_id>/messages/',
    '/games/<int:game_id>/messages/<int:id>',
    '/tickets/<int:ticket_id>/messages',
    '/tickets/<int:ticket_id>/messages/',
    '/tickets/<int:ticket_id>/messages/<int:id>',
    '/messages/<int:id>'
)
class ChatMessageResource(restful.Resource):
    @classproperty
    def fields(cls):
        return dict(
            id=fields.Integer,
            sender=fields.Nested(PlayerResource.fields()),
            receiver=fields.Nested(PlayerResource.fields()),
            # game = fields.Nested(GameResource.fields),
            text=fields.String,
            time=fields.DateTime,
            has_attachment=fields.Boolean,
            viewed=fields.Boolean,
        )

    def get_single(self, user, game_id=None, player_id=None, ticket_id=None, id=None):
        msg = ChatMessage.query.get_or_404(id)
        if not msg.is_for(user):
            raise Forbidden
        if game_id and game_id != msg.game_id:
            return redirect(api.url_for(ChatMessageResource, game_id=msg.game_id, id=id), 301)
        if player_id and player_id != msg.sender_id:
            return redirect(api.url_for(ChatMessageResource, player_id=msg.sender_id, id=id), 301)
        if ticket_id and ticket_id != msg.ticket_id:
            return redirect(api.url_for(ChatMessageResource, ticket_id=msg.ticket_id, id=id), 301)
        return marshal(msg, self.fields)


    @require_auth
    def get(self, user, game_id=None, player_id=None, ticket_id=None, id=None):
        if id:
            return self.get_single(game_id, player_id, ticket_id, id)

        player = None
        if player_id:
            player = Player.find(player_id)
            if not player:
                raise NotFound('wrong player id')

        game = None
        if game_id:
            game = Game.query.get_or_404(game_id)
        elif ticket_id:
            ticket = Ticket.query.get_or_404(game_id)
            game = ticket.game
            if not game:
                raise NotFound('wrong ticket id')

        if game and not game.is_game_player(user):
            abort('You cannot access this game', 403)

        if player:
            if user == player:
                # TODO:
                # SELECT * FROM messages
                # WHERE is_for(user)
                # GROUP BY other(user)
                # ORDER BY time DESC
                messages = ChatMessage.for_user(player)
            else:
                messages = ChatMessage.for_users(player, user)
            messages = messages.filter_by(game_id=None)
        elif game:
            messages = ChatMessage.query.filter_by(game_id=game.id)
        else:
            raise ValueError('no player nor game')

        parser = RequestParser()
        parser.add_argument('page', type=int, default=1)
        parser.add_argument('results_per_page', type=int, default=10)
        parser.add_argument(
            'order', default='time',
            choices=sum(
                [[s, '-' + s]
                 for s in (
                    'time',
                )], []),
        )
        args = parser.parse_args()
        if args.results_per_page > 50:
            abort('[results_per_page]: max is 50')

        # TODO: filtering

        if args.order.startswith('-'):
            order = getattr(ChatMessage, args.order[1:]).desc()
        else:
            order = getattr(ChatMessage, args.order).asc()
        messages = messages.order_by(order)

        total_count = messages.count()
        messages = messages.paginate(args.page, args.results_per_page,
                                     error_out=False).items

        ret = marshal(
            dict(messages=messages),
            dict(messages=fields.List(fields.Nested(self.fields))),
        )
        ret.update(dict(
            num_results=total_count,
            total_pages=math.ceil(total_count / args.results_per_page),
            page=args.page,
        ))
        return ret

    @require_auth
    def post(self, user, game_id=None, player_id=None, ticket_id=None, id=None):
        if id:
            raise MethodNotAllowed

        game = None
        player = None
        ticket = None
        if player_id:
            player = Player.find(player_id)
            if not player:
                raise NotFound('wrong player id')
            if player == user:
                abort('You cannot send message to yourself')
        elif game_id:
            game = Game.query.get_or_404(game_id)
            player = game.other(user)
            if not player:
                raise Forbidden('You cannot access this game', 403)
            game = game.root  # always attach messages to root game in session
        elif ticket_id:
            ticket = Ticket.query.get_or_404(game_id)
            if not ticket.game.is_game_player(user):
                raise Forbidden('You cannot access this ticket', 403)

        parser = RequestParser()
        parser.add_argument('text', required=False)
        args = parser.parse_args()

        msg = ChatMessage()
        msg.game = game
        msg.ticket = ticket
        msg.sender = user
        msg.receiver = player
        msg.text = args.text
        db.session.add(msg)

        if 'attachment' in request.files:
            db.session.commit()  # for id
            ChatMessageAttachmentResource.upload(
                request.files['attachment'],
                msg)
        elif not msg.text:
            db.session.expunge(msg)
            abort('Please provide either text or attachment, or both')

        db.session.commit()

        notify_chat(msg)

        return marshal(msg, self.fields)

    @require_auth
    def patch(self, user, game_id=None, player_id=None, ticket_id=None, id=None):
        if not id or any(game_id, player_id, ticket_id):
            raise MethodNotAllowed
        msg = ChatMessage.query.get_or_404(id)
        if msg.receiver_id != user.id:
            raise Forbidden(
                'You cannot patch message which is not addressed to you')

        parser = RequestParser()
        parser.add_argument('viewed', type=boolean_field, required=True)
        # we allow marking message as unread
        args = parser.parse_args()

        msg.viewed = args.viewed
        db.session.commit()
        return marshal(msg, self.fields)


@api.resource(
    '/players/<player_id>/messages/<int:id>/attachment',
    '/games/<int:game_id>/messages/<int:id>/attachment',
    '/tickets/<int:ticket_id>/messages/<int:id>/attachment',
)
class ChatMessageAttachmentResource(UploadableResource):
    PARAM = 'attachment'
    SUBDIR = 'attachments'
    ALLOWED = ['mp4', 'm4a', 'mov', 'png', 'jpg']

    @require_auth
    def get_entity(self, args, is_put, user):
        msg = ChatMessage.query.get_or_404(args['id'])
        if not msg.is_for(user):
            raise Forbidden

        if 'player_id' in args:
            player = Player.find(args['player_id'])
            if not player:
                raise NotFound
            if player != user and not msg.is_for(player):
                abort('Player ID mismatch')
        elif 'game_id' in args:
            game = Game.query.get(args['game_id'])
            if not game:
                raise NotFound('wrong game id')
            if not game.is_game_player(user):
                raise Forbidden('You cannot access this game')
        elif 'ticket_id' in args:
            ticket = Ticket.query.get_or_404(args['ticket_id'])
            game = ticket.game
            if not game:
                raise NotFound('wrong game id')
            if not game.is_game_player(user):
                raise Forbidden('You cannot access this ticket')
        else:
            raise ValueError('no ids')
        return msg

    @classmethod
    def onupload(cls, entity, ext):
        if not entity.has_attachment:
            entity.has_attachment = True
            db.session.commit()

    @classmethod
    def ondelete(cls, entity):
        if entity.has_attachment:
            entity.has_attachment = False
            db.session.commit()

    found = onupload
    notfound = ondelete


# Events
@api.resource(
    '/games/<int:game_id>/events',
    '/games/<int:game_id>/events/',
    '/games/<int:game_id>/events/<int:id>',
)
class EventResource(restful.Resource):
    @classproperty
    def fields(cls):
        return {
            'id': fields.Integer,
            'root_id': fields.Integer,
            'time': fields.DateTime,
            'type': fields.String,
            'message': fields.Nested(ChatMessageResource.fields,
                                     allow_null=True),
            'text': fields.String,
            'game': fields.Nested(GameResource.fields_lite,
                                  allow_null=True),
        }

    @classproperty
    def fields_more(cls):
        # for push notifications
        ret = cls.fields.copy()
        ret['root'] = fields.Nested(GameResource.fields)
        return ret

    @require_auth
    def get(self, user, game_id, id=None):
        root = Game.query.get_or_404(game_id)
        if not root.is_root:
            abort('This game is not root of hierarchy, use id %d' % root.root.id)
        if id:
            event = Event.query.filter_by(root=root, id=id).first_or_404()
            return marshal(event, self.fields)
        # TODO custom filters, pagination
        events = Event.query.filter(
            Event.root_id == root.id,
        ).order_by(
            Event.time,
        )
        return marshal(
            dict(events=events),
            dict(events=fields.List(fields.Nested(self.fields))),
        )


# Beta testers
@api.resource(
    '/betatesters',
    '/betatesters/<int:id>',
)
class BetaResource(restful.Resource):
    @classproperty
    def fields(cls):
        return dict(
            id=fields.Integer,
            email=fields.String,
            name=fields.String,
            gametypes=CommaListField,
            platforms=CommaListField,
            console=CommaListField,
            create_date=fields.DateTime,
            flags=JsonField,
        )

    @require_auth
    def get(self, user, id=None):
        if id:
            raise MethodNotAllowed
        user = check_auth()
        if user.id not in config.ADMIN_IDS:
            raise Forbidden

        return jsonify(
            betatesters=fields.List(fields.Nested(self.fields)).format(
                Beta.query,
            ),
        )

    def post(self, id=None):
        if id:
            raise MethodNotAllowed

        def nonempty(val):
            if not val:
                raise ValueError('Should not be empty')
            return val

        parser = RequestParser()
        parser.add_argument('email', type=email, required=True)
        parser.add_argument('name', type=nonempty, required=True)
        parser.add_argument('games',
                            default='')
        parser.add_argument('platforms',
                            type=multival_field(Beta.PLATFORMS, True),
                            default='')
        parser.add_argument('console', default='')
        args = parser.parse_args()

        beta = Beta()
        beta.email = args.email
        beta.name = args.name
        beta.gametypes = args.games
        beta.platforms = ','.join(args.platforms)
        beta.console = args.console
        db.session.add(beta)
        db.session.commit()

        datadog('Beta registration', repr(args))
        dd_stat.increment('beta.registration')

        return jsonify(
            success=True,
            betatester=marshal(
                beta,
                self.fields,
            ),
        )

    @require_auth
    def patch(self, user, id=None):
        if not id:
            raise MethodNotAllowed

        beta = Beta.query.get_or_404(id)

        parser = RequestParser()
        parser.add_argument('flags')
        args = parser.parse_args()

        for k, v in args.items():
            if v is not None and hasattr(beta, k):
                setattr(beta, k, v)
        log.info('flags for {}: {}'.format(beta.id, beta.flags))
        # and create backup for flags
        def merge(src, dst):
            for k, v in src.items():
                if isinstance(v, dict):
                    node = dst.setdefault(k, {})
                    merge(v, node)
                elif isinstance(v, list):
                    dst.setdefault(k, [])
                    dst[k] = list(set(dst[k] + v))  # merge items
                else:
                    dst[k] = v
            return dst

        try:
            src = json.loads(beta.flags)
            try:
                dst = json.loads(beta.backup or '')
            except ValueError:
                dst = {}
            merge(src, dst)
            beta.backup = json.dumps(dst)
        except ValueError:
            pass

        db.session.commit()

        return marshal(beta, self.fields)

@socketio.on('connect')
def socketio_conn():
    log.info('socket connected')
    # TODO check auth...
    #return False # if auth failed
_sockets = {} # sid -> sender
@socketio.on('auth')
def socketio_auth(token=None):
    log.info('Auth request for socket {}, token {}'.format(
        request.sid, token))
    if request.sid in _sockets:
        # already authorized
        log.info('already authorized')
        return False
    try:
        user = parseToken(token)
    except Exception:
        log.exception('Socket auth failed')
        sio_disconnect()
        return

    log.info('Socket auth success for {}'.format(request.sid))
    @copy_current_request_context
    def sender():
        p = redis.pubsub()
        redis_base = '{}.event.%s'.format('test' if config.TEST else 'prod')
        p.subscribe(redis_base % user.id)
        try:
            while True:
                msg = p.get_message()
                if not msg:
                    eventlet.sleep(.5)
                    continue

                log.debug('got msg: %s'%msg)
                if 'message' not in msg.get('type', ''): # msg or pmsg
                    continue
                mdata = msg.get('data')
                try:
                    if isinstance(mdata, bytes):
                        mdata = mdata.decode()
                    data = json.loads(mdata)
                except ValueError:
                    log.warning('Bad msg, not a json: '+str(mdata))
                    continue
                log.debug('handling msg')
                sio_send(data)
        finally:
            p.unsubscribe()
    _sockets[request.sid] = eventlet.spawn(sender)
@socketio.on('disconnect')
def socketio_disconn():
    sender = _sockets.pop(request.sid, None)
    log.debug('socket disconnected, will kill? - {} {}'.format(sender, request.sid))
    if sender:
        sender.kill()



@api.resource(
    '/tournaments',
    '/tournaments/',
    '/tournaments/<int:id>',
    '/tournaments/<int:id>/',
)
class TournamentResource(restful.Resource):
    participant_fields = {
        'player': fields.Nested(PlayerResource.fields(), allow_null=True),
        'round': fields.Integer,
        'defeated': fields.Boolean,
    }
    round_fields = {
        'start': fields.DateTime,
        'end': fields.DateTime,
    }
    fields_single = {
        'id': fields.Integer,
        'open_date': fields.DateTime,
        'start_date': fields.DateTime,
        'finish_date': fields.DateTime,
        'rounds_dates': fields.List(fields.Nested(round_fields)),
        'participants_by_round': fields.List(fields.List(
            fields.Nested(participant_fields, allow_null=True)
        )),
        'participants_cap': fields.Integer,
        'participants_count': fields.Integer,
        'gamemode': fields.String,
        'gametype': fields.String,
    }

    fields_many = {
        'id': fields.Integer,
        'open_date': fields.DateTime,
        'start_date': fields.DateTime,
        'finish_date': fields.DateTime,
        'participants_cap': fields.Integer,
        'participants_count': fields.Integer,
        'gamemode': fields.String,
        'gametype': fields.String,
    }

    @require_auth
    def post(self, user, id=None):
        if id:
            raise MethodNotAllowed
        parser = RequestParser()

        parser.add_argument('rounds_count', type=int)
        parser.add_argument('open_date', type=lambda s: datetime.fromtimestamp(int(s)))
        parser.add_argument('start_date', type=lambda s: datetime.fromtimestamp(int(s)))
        parser.add_argument('finish_date', type=lambda s: datetime.fromtimestamp(int(s)))
        parser.add_argument('finish_date', type=lambda s: datetime.fromtimestamp(int(s)))
        parser.add_argument('finish_date', type=lambda s: datetime.fromtimestamp(int(s)))
        parser.add_argument('buy_in', type=float)

        parser.add_argument('gametype', choices=Poller.all_gametypes, required=True)
        parser.add_argument('gamemode', type=str, required=True)

        args = parser.parse_args()

        poller = Poller.findPoller(args.gametype)
        if not poller or poller == DummyPoller:
            abort('Support for this game is coming soon!')

        if args.gamemode not in poller.gamemodes:
            abort('Unknown gamemode')


        if args.rounds_count < 1:
            abort('Tournament must have 1 or more rounds', problem='rounds_count')

        if args.buy_in < 0.99:
            abort('Buy in too low', problem='buy_in')

        #  TODO: check dates

        tournament = Tournament(
            rounds_count=args.rounds_count,
            open_date=args.open_date,
            start_date=args.start_date,
            finish_date=args.finish_date,
            payin=args.buy_in,
            gamemode=args.gamemode,
            gametype=args.gametype,
        )
        db.session.add(tournament)
        db.session.commit()
        return marshal(tournament, self.fields_single)

    @require_auth
    def patch(self, user, id=None):
        """
        participate in tournament
        """
        if not id:
            raise MethodNotAllowed
        tournament = Tournament.query.get_or_404(id)
        success, message, problem = tournament.add_player(user)
        if success:
            return self.get_single(id)
        else:
            abort(message, problem=problem)

    def get_single(self, id):
        return marshal(Tournament.query.get_or_404(id), self.fields_single)

    def get_many(self):
        parser = RequestParser()
        parser.add_argument('page', type=int, default=1)
        parser.add_argument('results_per_page', type=int, default=10)
        parser.add_argument('order', default='id', choices=sum([
            [s, '-' + s]
            for s in (
                'id',
                'open_date',
                'start_date',
                'finish_date',
            )
            ], [])
        )
        args = parser.parse_args()
        # cap
        if args.results_per_page > 50:
            abort('[results_per_page]: max is 50')

        query = Tournament.query.filter(Tournament.available == True)

        # TODO: filters
        if args.order:
            if args.order.startswith('-'):
                order = getattr(Tournament, args.order[1:]).desc()
            else:
                order = getattr(Tournament, args.order).asc()
            query = query.order_by(order)

        total_count = query.count()
        query = query.paginate(args.page, args.results_per_page,
                               error_out=False)
        return dict(
            tournaments=fields.List(fields.Nested(self.fields_many)).format(query.items),
            num_results=total_count,
            total_pages=math.ceil(total_count / args.results_per_page),
            page=args.page,
        )

    def get(self, id=None):
        if id:
            return self.get_single(id)
        return self.get_many()


# Debugging-related endpoints
@app.route('/debug/push_state/<state>', methods=['POST'])
@require_auth
def push_state(state, user):
    if state not in Game.state.prop.columns[0].type.enums:
        abort('Unknown state ' + state, 404)

    parser = GameResource.postparser.copy()
    parser.remove_argument('opponent_id')
    args = parser.parse_args()

    game = Game()
    game.creator = game.opponent = user
    game.state = state
    for k, v in args.items():
        if hasattr(game, k):
            setattr(game, k, v)

    result = notify_users(game, justpush=True)

    return jsonify(
        pushed=result,
        game=marshal(game, GameResource.fields)
    )


@app.route('/debug/push_event/<int:root_id>/<etype>', methods=['POST'])
@require_auth
def push_event(root_id, etype, user):
    root = Game.query.get_or_404(root_id).root
    parser = RequestParser()
    parser.add_argument('message', type=int)
    parser.add_argument('game', type=int)
    parser.add_argument('text')
    parser.add_argument('newstate')
    parser.add_argument('aborter', type=int)
    args = parser.parse_args()
    if args.message:
        args.message = ChatMessage.query.get_or_404(args.message)
    if args.game:
        args.game = Game.query.get_or_404(args.game)
        if args.aborter:
            args.game.aborter = Player.query.get_or_404(args.aborter)
    try:
        success = notify_event(root, etype, debug=True, **args)
    except ValueError as e:
        abort(str(e))
    return jsonify(success=success)


@app.route('/debug/echo')
def debug_echo():
    return '<{}>\n{}\n'.format(
        repr(request.get_data()),
        repr(request.form),
    )

@app.route('/debug/pdb')
def debug_pdb():
    import pdb
    pdb.set_trace()

@app.route('/debug/raise')
def debug_raise():
    raise Forbidden('Hello World!')


@app.route('/debug/datadog')
def debug_datadog():
    datadog('Debug', 'Debug received')
    dd_stat.increment('player.registered')
    return ''


@app.route('/debug/money')
@require_auth
def debug_money(user):
    # Allow this endpoint only for users whose first transaction was "other"
    tran = user.transactions.first()
    if tran.type != 'other':
        raise Forbidden

    SUM = 100
    user.balance += SUM
    db.session.add(Transaction(
        player=user,
        type='deposit',
        sum=SUM,
        balance=user.balance,
        comment='Debugging income',
    ))
    db.session.commit()
    return jsonify(success=True)


@app.route('/debug/@llg@mes')
def debug_allgames():
    games = Game.query.filter_by(state='accepted').order_by(Game.id.desc())

    return """<html><head><script src="//ajax.googleapis.com/ajax/libs/jquery/1.11.3/jquery.min.js"></script><script>
$(function() {
    $('body').on('click', 'a[data-id]', function(e) {
        e.preventDefault();
        var id = $(this).data('id'),
            winner = $(this).attr('class');
        $.ajax({
            type: 'POST',
            url: '/v1/debug/f@keg@me/'+id+'/'+winner,
            success: function(ret) {
                $('span[data-id='+id+'].actions').addClass('finished');
                $('span[data-id='+id+'].state').text('finished');
                $('span[data-id='+id+'].winner').text(winner);
            },
            error: function(xhr) {
                alert(xhr.responseJSON.error);
            },
        });
    });
});
</script><style>
span.new, span.finished, span.declined {
    display: none;
}
</style></head><body><table border=1>
<tr>
    <th>Game ID</th>
    <th>Creator</th>
    <th>Opponent</th>
    <th>Stream</th>
    <th>When created</th>
    <th>Bet</th>
    <th>State</th>
    <th>Winner</th>
</tr>
""" + '\n\n'.join([
        """
<tr>
    <th>{id}</th>
    <th>{creator} / {twitch_identity_creator}</th>
    <th>{opponent} / {twitch_identity_opponent}</th>
    <th><a href="http://twitch.tv/{twitch_handle}">{twitch_handle}</a></th>
    <th>{create_date}</th>
    <th>{bet}</th>
    <th><span data-id="{id}" class="state">{state}</span>
    <th>
<span data-id="{id}" class="winner">{winner}</span>
<span data-id="{id}" class="actions {state}">- set to
    <a href="#" data-id="{id}" class="creator">creator</a>,
    <a href="#" data-id="{id}" class="opponent">opponent</a>,
    <a href="#" data-id="{id}" class="draw">draw</a>
</span>
    </th>
<tr/>
        """.format(
            id=game.id,
            creator=game.creator.nickname,
            opponent=game.opponent.nickname,
            twitch_handle=game.twitch_handle or '',
            twitch_identity_creator=game.twitch_identity_creator,
            twitch_identity_opponent=game.twitch_identity_opponent,
            create_date=game.create_date,
            bet=game.bet,
            state=game.state,
            winner=game.winner,
        ) for game in games
    ])


@app.route('/debug/f@keg@me/<int:id>/<winner>', methods=['POST'])
def debug_fake_result(id, winner):
    game = Game.query.get_or_404(id)
    if winner not in ('creator', 'opponent', 'draw'):
        abort('Bad winner ' + winner)
    if game.state != 'accepted':
        abort('Unexpected game state ' + game.state)
    poller = Poller.findPoller(game.gametype)
    if not poller:
        abort('No poller found for gt ' + game.gametype)
    poller.gameDone(game, winner, datetime.utcnow())
    return jsonify(success=True)
@app.route('/debug/socksend')
def debug_socksend():
    socketio.send({'hello': 'world'})
    return 'ok'
@app.route('/debug/redissend')
@require_auth
def debug_redissend(user):
    redis_base = '{}.event.%s'.format('test' if config.TEST else 'prod')
    redis.publish(redis_base%user.id, json.dumps(
        {'data':'Hello World.'}
    ))
    return 'ok'

@app.route('/debug/revision')
def debug_revision():
    import subprocess
    return subprocess.check_output('/usr/bin/git rev-parse HEAD'.split())
