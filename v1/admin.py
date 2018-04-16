from flask import redirect, url_for, request, session

from flask_admin import Admin, BaseView, expose
from flask_admin.contrib.sqla import ModelView
from flask_admin.model.base import get_redirect_target, get_mdict_item_or_list, flash, gettext
from flask.ext.basicauth import BasicAuth
from flask.ext.login import LoginManager, login_user, current_user, logout_user

from sqlalchemy.orm.exc import NoResultFound

from .models import ChatMessage, Ticket, Player, db
from .polling import Poller
from datetime import datetime

admin = Admin(name='admin', template_mode='bootstrap3')
login_manager = LoginManager()


@login_manager.user_loader
def load_user(user_id):
    try:
        return Player.query.get(user_id)
    except NoResultFound:
        return None


def init(app):
    admin.init_app(app)
    login_manager.init_app(app)
    admin.add_view(TicketView(Ticket, db.session))
    admin.add_view(LoginView(name='Login', endpoint='login'))
    admin.add_view(LogoutView(name='Logout', endpoint='logout'))



class LoginView(BaseView):
    def is_visible(self):
        return not current_user.is_authenticated

    @expose('/', methods=('GET', 'POST'))
    def index(self):
        from .models import Player  # avoiding cyclic reference =(
        from .helpers import check_password

        if request.form and 'login' in request.form and 'password' in request.form:
            user = Player.find(request.form['login'])
            if user and check_password(request.form['password'], user.password) and login_user(user):
                return redirect(url_for('admin.index'))
        return self.render('admin/login.html')


class AdminView(BaseView):
    def is_visible(self):
        return current_user.is_authenticated and current_user.is_active

    def is_accessible(self):
        return current_user.is_authenticated and current_user.is_active

class LogoutView(AdminView):
    @expose('/')
    def index(self):
        logout_user()
        return redirect(url_for('admin.index'))

class TicketView(ModelView, AdminView):
    can_create = False
    can_delete = False
    can_edit = False
    can_view_details = True
    details_template = 'admin/ticket/details.html'

    @expose('/reopen/', methods=['POST'])
    def reopen_ticket(self):
        return_url = get_redirect_target() or self.get_url('.index_view')
        id = get_mdict_item_or_list(request.args, 'id')
        if id is None:
            return redirect(return_url)

        ticket = self.get_one(id)

        if ticket is None:
            flash(gettext('Record does not exist.'))
            return redirect(return_url)

        if ticket.open:
            flash('Ticket already open.')
            return redirect(return_url)
        ticket.open = True
        db.session.commit()
        return redirect(url_for('.details_view', id=id))

    @expose('/resolve/', methods=['POST'])
    def resolve_ticket(self):
        return_url = get_redirect_target() or self.get_url('.index_view')
        id = get_mdict_item_or_list(request.args, 'id')
        if id is None:
            return redirect(return_url)

        ticket = self.get_one(id)

        if ticket is None:
            flash(gettext('Record does not exist.'))
            return redirect(return_url)

        if not ticket.open:
            flash('Ticket already closed.')
            return redirect(return_url)

        winner_id = request.form.get('winner_id', None)
        draw = request.form.get('draw', None)
        game_winner = 'draw'
        if winner_id and not draw:
            winner = Player.query.get(winner_id)
            if winner == ticket.game.creator:
                game_winner = 'creator'
            if winner == ticket.game.opponent:
                game_winner = 'opponent'
        poller = Poller.findPoller(ticket.game.gametype)
        poller.gameDone(ticket.game, game_winner, datetime.utcnow())
        ticket.open = False
        db.session.commit()
        return redirect(url_for('.details_view', id=id))

    @expose('/send_msg/', methods=['POST'])
    def send_msg(self):
        return_url = get_redirect_target() or self.get_url('.index_view')
        id = get_mdict_item_or_list(request.args, 'id')
        if id is None:
            return redirect(return_url)

        ticket = self.get_one(id)

        if ticket is None:
            flash(gettext('Record does not exist.'))
            return redirect(return_url)

        receiver_id = request.form.get('receiver_id', None)
        text = request.form.get('text', None)
        if receiver_id and text:
            try:
                receiver = Player.query.get(receiver_id)
            except NoResultFound:
                return redirect(return_url)
            message = ChatMessage()
            message.admin_message = True
            message.receiver = receiver
            message.text = text
            message.ticket = ticket
            db.session.add(message)
            db.session.commit()
        return redirect(url_for('.details_view', id=id))

    @expose('/details/')
    def details_view(self):
        """
            Details model view
        """
        return_url = get_redirect_target() or self.get_url('.index_view')

        if not self.can_view_details:
            return redirect(return_url)

        id = get_mdict_item_or_list(request.args, 'id')
        if id is None:
            return redirect(return_url)

        ticket = self.get_one(id)

        if ticket is None:
            flash(gettext('Record does not exist.'))
            return redirect(return_url)

        if self.details_modal and request.args.get('modal'):
            template = self.details_modal_template
        else:
            template = self.details_template

        players = [report.player for report in ticket.game.reports]

        for message in ticket.messages:
            if not message.admin_message:
                message.viewed = True
        db.session.commit()
        return self.render(template,
                           model=ticket,
                           game=ticket.game,
                           players=players,
                           details_columns=self._details_columns,
                           get_value=self.get_list_value,
                           return_url=return_url)