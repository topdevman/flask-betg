#!/usr/bin/env python3

import main, pdb, re

from random import choice
from faker import Faker
fake = Faker()

main.init_app()

with main.app.app_context():

    from v1.models import *
    from v1.helpers import *
    from v1.routes import *

    def add_tournaments():
        dt = datetime.utcnow()
        hour = timedelta(hours=1)
        for i in range(3):
            for j in range(3):
                for m in range(4):
                    t = Tournament(j + 1, dt + hour * i, dt + hour * (i+1), dt + hour * (i+2), m+1)
                    db.session.add(t)
        db.session.commit()

    def add_test_players():
        for i in range(10):
            player = Player()
            player.nickname = 'test_player_' + str(i)
            player.email = player.nickname + '@example.com'
            player.password = encrypt_password('111111')
            db.session.add(player)
        db.session.commit()

    def add_fake_users(count=100):
        fake_players = []
        for i in range(count):
            player = Player()
            player.nickname = fake.name()
            player.email = re.sub('\W+', '_', player.nickname) + '@example.com'
            player.password = encrypt_password('111111')
            db.session.add(player)
            fake_players.append(player)
        db.session.commit()
        for i in range(count * 10):
            p1 = choice(fake_players)
            p2 = choice(fake_players)
            while p1 == p2:
                p2 = choice(fake_players)
            game = Game()
            game.gamemode = 'fake'
            game.gametype = 'fake'
            game.creator_id = p1.id
            game.opponent_id = p2.id
            if  p1.winrate and p2.winrate and p1.winrate > p2.winrate:
                game.winner = 'creator'
            else:
                game.winner = 'opponent'
            game.state = 'finished'
            game.bet = 0
            db.session.add(game)
        db.session.commit()


    while True:
        pdb.set_trace()
