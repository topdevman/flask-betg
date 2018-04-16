#!/usr/bin/env python3
import sys, os
from datetime import datetime, timedelta
from types import SimpleNamespace
import math
from collections import namedtuple
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import quote

import requests
from dateutil.parser import parse as date_parse

if __name__ == '__main__':
    # debugging environment; other changes are in the bottom
    from apis import *
    from mock import log, config, dummyfunc, db
    from common import *
    ROOT = '.'
    try:
        sys.path.append('..')
        import config as config_module
        for p in dir(config_module):
            setattr(config, p, getattr(config_module, p))
    except ImportError:
        pass
else:
    # live environment
    import config
    from .helpers import notify_users, notify_event
    from .models import *
    from .apis import *
    from .common import *
    ROOT = os.path.dirname(__file__)+'/../'

class Identity(namedtuple('Identity', 'id name checker choices formatter')):
    # Formatter is needed for identities which use non-readable format
    # like Steam ID.
    # In pair with corresponding checker, it allows to store
    # both machine-readable and human-readable values.
    _all = {}
    def __new__(cls, id, name, checker, choices=None, formatter=None):
        if not checker:
            checker = lambda val: val
        ret = super().__new__(cls, id, name, checker, choices,
                              formatter or (lambda val: (val, str(val))))
        cls._all[id] = ret
        return ret
    @classmethod
    def get(cls, id):
        return cls._all.get(id)
    @classproperty
    def all(cls):
        return cls._all.values()
Identity('riot_summonerName', 'Riot Summoner Name ("name" or "region/name")',
            Riot.summoner_check)
Identity('steam_id','STEAM ID (numeric, URL or nickname)', Steam.pretty_id,
         formatter = lambda x: Steam.split_identity(x)[1])
Identity('starcraft_uid','StarCraft profile URL from battle.net or sc2ranks.com',
            StarCraft.check_uid)
# ea_gamertag, fifa_team, tibia_character - will be added in classes


### Polling ###
class Poller:
    gametypes = {} # list of supported types for this class
    gamemodes_primary = {}
    gamemodes_ingame = {}
    usemodes = False # do we need to init again for each mode?
    sameregion = False # do we need to ensure region is the same for both gamertags
    # region is stored as slash delimited prefix of gamertag.
    twitch = 0 # do we support twitch for this gametype?
    # 0 - not supported, 1 - optional, 2 - mandatory
    twitch_gametypes = {}
    identity_id = None
    twitch_identity_id = None
    honesty = False # special for honesty-based poller
    # human-readable description of how to play this game.
    # Might be dictionary if description should vary
    # for different gametypes in same poller.
    description = None
    subtitle = None
    category = None
    minutes = 0 # by default, poll as often as possible
    # List of tuples (creator, opponent, gamemode, startdate, winner).
    # For each of these tuples there should exist finished game
    # with specified result.
    tests = []

    @classproperty
    def identity(cls):
        return Identity.get(cls.identity_id)
    @classproperty
    def identity_name(cls):
        return cls.identity.name if cls.identity else None
    @classproperty
    def identity_check(cls):
        return cls.identity.checker if cls.identity else lambda val: val
    @classproperty
    def twitch_identity(cls):
        return Identity.get(cls.twitch_identity_id)

    @classmethod
    @lru_cache()
    def findPoller(cls, gametype):
        """
        Tries to find poller class for given gametype.
        Returns None on failure.
        """
        if gametype in cls.gametypes:
            return cls
        for sub in cls.__subclasses__():
            ret = sub.findPoller(gametype)
            if ret:
                return ret
    @classmethod
    def allPollers(cls):
        yield cls
        for sub in cls.__subclasses__():
            yield from sub.allPollers()

    @classproperty
    def gamemodes(cls):
        ret = {}
        ret.update(cls.gamemodes_ingame)
        ret.update(cls.gamemodes_primary)
        return ret
    @classproperty
    def all_gametypes(cls):
        types = set(cls.gametypes)
        for sub in cls.__subclasses__():
            types.update(sub.all_gametypes)
        return types
    @classproperty
    def all_gamemodes(cls):
        modes = set(cls.gamemodes)
        for sub in cls.__subclasses__():
            modes.update(sub.all_gamemodes)
        return modes


    @classmethod
    def gameStarted(cls, game):
        """
        This will be called once game invitation is accepted for this gametype.
        Default implementation does nothing,
        but subclass may want to save some information about current game state
        in the passed game object.

        This method may be called more than once for single game,
        but game will be actually started only after the last call.

        Please don't save current time in this method,
        instead you can use game.date_accepted field
        which shall hold the most relevant value.
        """
        pass
    def games(self, gametype, gamemode=None):
        ret = Game.query.filter_by(
            gametype = gametype,
            state = 'accepted',
        )
        if gamemode:
            ret = ret.filter_by(
                gamemode = gamemode,
            )
        return ret
    def poll(self, now, gametype=None, gamemode=None):
        if not gametype:
            for gametype in self.gametypes:
                if not self.usemodes:
                    self.prepare()
                self.poll(now, gametype)
            return
        if self.usemodes and not gamemode:
            for gamemode in self.gamemodes:
                self.prepare()
                self.poll(now, gametype, gamemode)
            return

        query = self.games(gametype, gamemode)
        count_games = query.count()
        count_ended = 0

        log.debug('{}: polling {} games of type {} {}'.format(
            self.__class__.__name__,
            count_games,
            gametype, gamemode
        ))

        hourly = now.minute % 60 == 0
        for game in query:
            if not hourly and now - game.accept_date > timedelta(hours=12):
                log.info('Skipping game {} because it is long-lasting'
                         .format(game))
                continue
            try:
                if self.pollGame(game):
                    count_ended += 1
            except Exception:
                log.exception('Failed to poll game {}'.format(game))

        db.session.commit()

        log.debug('Polling done, finished {} of {} games'.format(
            count_ended, count_games,
        ))

    @classmethod
    def gameDone(cls, game, winner, timestamp=None, details=None):
        """
        Mark the game as done, setting all needed fields.
        Winner is a string.
        Timestamp is in seconds, or it can be datetime object, or None for now.
        Returns True for convenience (`return self.gameDone(...)`).
        """
        log.debug('Marking game {} as done'.format(game))
        if winner == 'aborted':
            game.winner = 'draw'
            game.state = 'aborted'
        else:
            game.winner = winner
            game.state = 'finished'
        game.details = details
        if not timestamp:
            game.finish_date = datetime.utcnow()
        elif isinstance(timestamp, datetime):
            game.finish_date = timestamp
        else:
            game.finish_date = datetime.utcfromtimestamp(timestamp)
        db.session.commit() # to avoid observer overwriting it before us..

        # move funds (only if somebody won)
        if winner in ['creator', 'opponent']:
            if winner == 'creator':
                winner = game.creator
                looser = game.opponent
            elif winner == 'opponent':
                winner = game.opponent
                looser = game.creator
            winner.balance += game.bet
            looser.balance -= game.bet
            db.session.add(Transaction(
                player = winner,
                type = 'won',
                sum = game.bet,
                balance = winner.balance,
                game = game,
            ))
            db.session.add(Transaction(
                player = looser,
                type = 'lost',
                sum = -game.bet,
                balance = looser.balance,
                game = game,
            ))
        # and unlock bets (always)
        # withdrawing them finally from accounts
        game.creator.locked -= game.bet
        game.opponent.locked -= game.bet

        db.session.commit()

        notify_users(game)

        # cancel stream watcher (if any)
        if game.twitch_handle:
            try:
                ret = requests.delete(
                    '{}/streams/{}/{}'.format(
                        config.OBSERVER_URL,
                        game.twitch_handle,
                        game.gametype,
                    ),
                )
                log.info('Deleting watcher: %d' % ret.status_code)
            except Exception:
                log.exception('Failed to delete watcher')

        return True # for convenience

    @classmethod
    def gameEvent(cls, game, text):
        """
        Broadcast notification about game event to game session
        """
        return notify_event(
            game.root, 'system',
            game = game,
            text = text,
        )

    def prepare(self):
        """
        Prepare self for new polling, clear all caches.
        """
        pass

    def pollGame(self, game):
        """
        Shall be overriden by subclasses.
        Returns True if given game was successfully processed.
        """
        raise NotImplementedError


class FifaPoller(Poller, LimitedApi):
    gametypes = {
        'fifa14-xboxone': 'FIFA14',
        'fifa15-xboxone': 'FIFA15',
    }
    gamemodes = {
        'fifaSeasons': 'FIFA Seasons',
        'futSeasons': 'FUT Online Seasons',
        'fut': 'FUT',
        'friendlies': 'Friendlies',
        'coop': 'Co-op',
    }
    subtitle = {
        'fifa14-xboxone': '14',
        'fifa15-xboxone': '15',
        'fifa16-xboxone': '16',
    }
    category = 'Sport'
    identity_id = 'ea_gamertag'
    twitch_identity_id = 'fifa_team'
    usemodes = True
    twitch = 2
    twitch_gametypes = {
        'fifa14-xboxone': 'FIFA 14',
        'fifa15-xboxone': 'FIFA 15',
        'fifa16-xboxone': 'FIFA 16',
    }
    minutes = 30 # poll at most each 30 minutes

    gamertag_cache = {}
    def gamertag_checker(nick):
        # don't use @classmethod
        # because they will not work until class is fully defined
        cls = FifaPoller
        if nick.lower() in cls.gamertag_cache:
            if cls.gamertag_cache[nick.lower()]:
                return cls.gamertag_cache[nick.lower()]
            raise ValueError('Unknown gamertag: '+nick)

        url = 'https://www.easports.com/fifa/api/'\
            'fifa15-xboxone/match-history/friendlies/{}'.format(quote(nick))
        try:
            # FIXME
            # EA Sports is now almost down, so don't validate gamertag
            return nick
            # FIXME
            ret = requests.get(url)
            if ret.status_code == 404:
                # don't cache not-registered nicks as they can appear in future
                #cls.gamertag_cache[nick.lower()] = None
                raise ValueError(
                    'Gamertag {} seems to be unknown '
                    'for FIFA game servers'.format(nick))
            data = ret.json()['data']
            # normalized gamertag (with correct capitalizing)
            # if no data then cannot know correct capitalizing; return as is
            goodnick = data[0]['self']['user_info'][0] if data else nick
            cls.gamertag_cache[nick.lower()] = goodnick
            return goodnick
        except ValueError:
            log.warning('json error: '+str(ret))
            if getattr(g, 'gamertag_force', False):
                return nick
            raise
        except Exception as e: # json failure or missing key
            log.error('Failed to validate gamertag '+nick, exc_info=True)
            if 'ret' in locals():
                log.error(ret)
            log.error('Allowing it...')
            #raise ValueError('Couldn\'t validate this gamertag: {}'.format(nick))
            return nick
    with open(ROOT+'/fifa_teams.csv') as teamsfile:
        fifa_teams = {
            name: abbr for name,abbr in
            map(lambda x: x.strip().split(','), teamsfile)
        }
    def fifa_team_checker(val):
        cls = FifaPoller
        if len(val) in (2,3):
            if val not in cls.fifa_teams:
                log.warning('Unknown team id: '+val)
            return val
        # not 3-char, do reverse lookup
        rev = {k.casefold(): v for k, v in cls.fifa_teams.items()}
        out = rev.get(val.casefold())
        if not out:
            raise ValueError('Expected 3-letter team id or known team name, got {}'.format(val))
        return out
    Identity('ea_gamertag', 'XBox GamerTag', gamertag_checker)
    Identity('fifa_team', 'FIFA Team Name', fifa_team_checker,
             fifa_teams)
    del gamertag_checker
    del fifa_team_checker

    def prepare(self):
        self.gamertags = {}

    @classmethod
    def fetch(cls, gametype, gamemode, nick):
        url = 'https://www.easports.com/fifa/api/'\
            '{}/match-history/{}/{}'.format(
                gametype, gamemode, quote(nick))
        try:
            return cls.request_json('GET', url)['data']
        except Exception as e:
            log.error('Failed to fetch match info '
                      'for player {}, gt {} gm {}'.format(
                          nick, gametype, gamemode),
                      exc_info=False) # XXX disabled
            return []

    def pollGame(self, game, who=None, matches=None):
        if not who:
            for who in ['creator', 'opponent']:
                tag = getattr(game, 'gamertag_'+who)
                if tag in self.gamertags:
                    return self.pollGame(game, who, self.gamertags[tag])

            who = 'creator'
            matches = self.fetch(game.gametype, game.gamemode,
                                 game.gamertag_creator)
            # and cache it
            self.gamertags[game.gamertag_creator] = matches

        crea = SimpleNamespace(who='creator')
        oppo = SimpleNamespace(who='opponent')
        for user in [crea, oppo]:
            user.tag = getattr(game, 'gamertag_'+user.who)
        # `me` is the `who` player object
        me, other = (crea, oppo) if who == 'creator' else (oppo, crea)
        me.role = 'self'
        other.role = 'opponent'

        # now that we have who and matches, try to find corresponding match
        for match in reversed(matches): # from oldest to newest
            log.debug('match: {} cr {}, op {}'.format(
                match['timestamp'], *[
                    [match[u]['user_info'], match[u]['stats']['score']]
                    for u in ('self', 'opponent')
                ]
            ))
            # skip this match if it ended before game's start
            if math.floor(game.accept_date.timestamp()) \
                    > match['timestamp'] + 4*3600: # delta of 4 hours
                log.debug('Skipping match because of time')
                continue

            if other.tag.lower() not in map(
                lambda t: t.lower(),
                match['opponent']['user_info']
            ):
                log.debug('Skipping match because of participants')
                continue

            # Now we found the match we want! Handle it and return True
            log.debug('Found match! '+str(match))
            for user in (crea, oppo):
                user.score = match[user.role]['stats']['score']
            if crea.score > oppo.score:
                winner = 'creator'
            elif crea.score < oppo.score:
                winner = 'opponent'
            else:
                winner = 'draw'
            self.gameDone(game, winner, match['timestamp'],
                          'Score: {} - {}'.format(crea.score, oppo.score))
            return True

class RiotPoller(Poller):
    gametypes = {
        'league-of-legends': 'League of Legends',
    }
    gamemodes = {
        'RANKED_SOLO_5x5': 'Solo 5x5',
        'RANKED_TEAM_3x3': 'Team 3x3',
        'RANKED_TEAM_5x5': 'Team 5x5',
    }
    subtitle = 'The Game'
    category = 'Action'
    identity_id = 'riot_summonerName'
    sameregion = True
    description = """
        For this game betting is based on match outcome.
    """

    def prepare(self):
        self.matches = {}

    def pollGame(self, game):
        def parseSummoner(val):
            region, val = val.split('/', 1)
            name, id = val.rsplit('/', 1)
            return region, name, int(id)
        crea = SimpleNamespace()
        oppo = SimpleNamespace()
        region, crea.name, crea.sid = parseSummoner(game.gamertag_creator)
        region2, oppo.name, oppo.sid = parseSummoner(game.gamertag_opponent)
        if region2 != region:
            log.error('Invalid game, different regions! id {}'.format(game.id))
            # TODO: mark game as invalid?..
            return False

        def checkMatch(match_ref):
            # fetch match details
            mid = match_ref['matchId']
            if mid in self.matches:
                ret = self.matches[mid]
            else:
                ret = Riot.call(
                    region,
                    'v2.2',
                    'match/'+mid,
                )
                self.matches[mid] = ret
            crea.pid = oppo.pid = None # participant id
            for participant in ret['participantIdentities']:
                for user in [crea, oppo]:
                    if participant['player']['summonerId'] == user.sid:
                        user.pid = participant['participantId']
            if not oppo.pid:
                # Desired opponent didn't participate this match; skip it
                return False

            crea.tid = oppo.tid = None
            crea.won = oppo.won = None
            for participant in ret['participants']:
                for user in [crea, oppo]:
                    if participant['participantId'] == user.pid:
                        user.tid = participant['teamId']
                        user.won = participant['stats']['winner']

            if crea.tid == oppo.tid:
                log.warning('Creator and opponent are in the same team!')
                # skip this match
                return False

            self.gameDone(
                game,
                'creator' if crea.won else
                'opponent' if oppo.won else
                'draw',
                # creation is in ms, duration is in seconds; convert to seconds
                round(ret['matchCreation']/1000) + ret['matchDuration']
            )
            return True

        shift = 0
        while True:
            ret = Riot.call(
                region,
                'v2.2',
                'matchlist/by-summoner/'+str(crea.sid),
                data=dict(
                    beginTime = game.accept_date.timestamp()*1000, # in ms
                    beginIndex = shift,
                    rankedQueues = game.gamemode,
                ),
            )

            for match in ret['matches']:
                if checkMatch(match):
                    return True

            shift += 20
            if shift > ret['totalGames']:
                break

class Dota2Poller(Poller):
    gametypes = {
        'dota2': 'DOTA 2',
    }
    subtitle = '2'
    # no gamemodes for this game
    identity_id = 'steam_id'
    description = """
        For this game betting is based on match outcome.
        If both players played for the same fraction (radiant/dire),
        game is considered draw.
        Else winner is the player whose team won.
    """

    def prepare(self):
        self.matchlists = {}

    def pollGame(self, game):
        from_oppo = False
        matchlist = self.matchlists.get(game.gamertag_creator_val)
        if not matchlist:
            matchlist = self.matchlists.get(game.gamertag_opponent_val)
            from_oppo = True
        if not match:
            # TODO: handle pagination
            # Match list is sorted by start time descending,
            # and 100 matches are returned by default,
            # so maybe no need (as we check every 30 minutes)
            matchlist = Steam.dota2(
                method = 'GetMatchHistory',
                account_id = game.gamertag_creator_val, # it is in str, but doesn't matter here
                date_min = round(game.accept_date.timestamp()),
            ).get('matches')
            # TODO: merge all matches in cache, index by match id,
            # and search players in all matches available -
            # this may reduce requests count
            if not matchlist:
                raise ValueError('Couldn\'t fetch match list for account id {}'
                                 .format(game.gamertag_creator_val))
            self.matchlists[game.gamertag_creator_val] = match['matches']
        for match in matchlist:
            if match['start_time'] < game.accept_date.timestamp:
                # this match is too old, subsequent are older -> not found
                break
            player_ids = (Steam.id_to_64(p['account_id'])
                          for p in match['players']
                          if 'account_id' in p)
            if int(game.gamertag_creator_val
                   if from_oppo else
                   game.gamertag_opponent_val) in player_ids:
                # found the right match
                # now load its details to determine winner and duration

                match = Steam.dota2(
                    method = 'GetMatchDetails',
                    match_id = match['match_id'],
                )
                # TODO: update it in cache?

                # determine winner
                crea = SimpleNamespace()
                oppo = SimpleNamespace()
                crea.id, oppo.id = map(int,
                                       [game.gamertag_creator_val,
                                        game.gamertag_opponent_val])
                for player in match['players']:
                    for user in (crea, oppo):
                        if Steam.id_to_64(player.get('account_id')) == user.id:
                            user.info = player
                for user in (crea, oppo):
                    if not hasattr(user, 'info'):
                        raise Exception(
                            'Unexpected condition: crea or oppo not found.'
                            '{} {}'.format(
                                match,
                                game,
                            )
                        )
                    # according to
                    # https://wiki.teamfortress.com/wiki/WebAPI/GetMatchDetails#Player_Slot
                    user.dire = bool(user.info['player_slot'] & 0x80)
                    user.won = user.dire == (not match['radiant_won'])

                if crea.dire == oppo.dire:
                    # TODO: consider it failure?
                    winner = 'draw'
                else:
                    winner = 'creator' if crea.won else 'opponent'

                return self.gameDone(
                    game,
                    winner,
                    match['start_time'] + match['duration']
                )

class CSGOPoller(Poller):
    gametypes = {
        'counter-strike-global-offensive': 'CounterStrike: Global Offensive',
    }
    identity_id = 'steam_id'
    subtitle = 'Global Offensive'
    category = 'Action'
    description = """
        For this game betting is based on match outcome.
        If both players played in the same team, game is considered draw.
        Else winner is the player whose team won.
    """
    # We cannot determine time of last match,
    # so we have to poll current state of both players on game creation.
    # Then we wait for state to change for both of them
    # and to match (number of rounds, etc).

    # FIXME: poll more often!
    # Because else last match info may be already overwritten when we poll it.

    class Match(namedtuple('Match', [
        'wins', 't_wins', 'ct_wins',
        'max_players', 'kills', 'deaths',
        'mvps', 'damage', 'rounds',
        'total_matches_played',
    ])): # there are some other attributes but they are of no use for us
        __slots__ = () # to avoid memory wasting
        def __eq__(self, other):
            # all of the following properties should match
            # to consider two matches equal
            return all(map(
                lambda attr: getattr(self, attr) == getattr(other, attr),
                [ 'rounds', 'max_players', ]
            ))
    @classmethod
    def fetch_match(cls, userid):
        log.debug('Fetching matches for user id %s'%userid)
        ret = Steam.call(
            'ISteamUserStats', 'GetUserStatsForGame', 'v0002',
            appid=730, # CS:GO
            steamid=userid, # not worry whether it string or int
        ).get('playerstats', {})
        stats = {s['name']: s['value'] for s in ret.get('stats', [])}
        if not stats:
            raise ValueError('No stats available for player %s'%userid)
        return cls.Match(*[
            stats[stat if stat.startswith('total') else 'last_match_'+stat]
            for stat in cls.Match._fields
        ])

    @classmethod
    def gameStarted(cls, game):
        # store "<crea_total>:<oppo_total>" to know when match was played
        game.meta = ':'.join(map(
            lambda p: str(cls.fetch_match(p).total_matches_played),
            (game.gamertag_creator_val, game.gamertag_opponent_val)
        ))
    def prepare(self):
        self.matches = {}
    def pollGame(self, game):
        crea = SimpleNamespace(tag=game.gamertag_creator_val)
        oppo = SimpleNamespace(tag=game.gamertag_opponent_val)
        crea.total, oppo.total = map(int, game.meta.split(':'))
        for user in crea, oppo:
            if user.tag not in self.matches:
                self.matches[user.tag] = self.fetch_match(user.tag)
            user.match = self.matches[user.tag]
            if user.match.total_matches_played == user.total:
                # total_matches_played didn't change since game was started,
                # so it is not finished yet
                log.info('total for {} is still {}, skipping this game'.format(
                    user.total, user.tag,
                ))
                return False
            user.won = user.match.wins > user.match.rounds/2
        if crea.match != oppo.match: # this compares only common properties
            return False # they participated different matches for now
        if crea.won == oppo.won:
            winner = 'draw'
        else:
            user = 'creator' if crea.won else 'opponent'
        return self.gameDone(
            game,
            winner,
            None, # now - as we don't know exact match ending time
        )

class StarCraftPoller(Poller):
    gametypes = {
        'starcraft': 'Starcraft II',
    }
    subtitle = 'II'
    # no gamemodes for this game
    identity_id = 'starcraft_uid'
    sameregion = True
    description = """
        For this game betting is based on match outcome.
    """

    def prepare(self):
        self.lists = {}
    def pollGame(self, game):
        """
        For SC2, we cannot determine user's opponent in match.
        So we just fetch histories for both players
        and look for identical match.
        """
        crea = SimpleNamespace(uid=game.gamertag_creator)
        oppo = SimpleNamespace(uid=game.gamertag_opponent)
        if crea.uid.split('/')[0] != oppo.uid.split('/')[0]:
            # should be filtered in endpoint...
            raise ValueError('Region mismatch')
        game_ts = game.accept_date.timestamp()
        for user in crea, oppo:
            if user.uid not in self.lists:
                ret = StarCraft.profile(user.uid, 'matches')
                if 'matches' not in ret:
                    raise ValueError('Couldn\'t fetch matches for user '+user.uid)
                self.lists[user.uid] = ret['matches']
            user.hist = [m for m in self.lists[user.uid]
                         if m['date'] >= game_ts]
        for mc in crea.hist:
            for mo in oppo.hist:
                if all(map(lambda field: mc[field] == mo[field],
                           ['map', 'type', 'speed', 'date'])):
                    # found the match
                    if mc['decision'] == mo['decision']:
                        winner = 'draw'
                    else:
                        winner = ('creator'
                                  if mc['decision'] == 'WIN' else
                                  'opponent')
                    return self.gameDone(game, winner, mc['date'])

class TibiaPoller(Poller, LimitedApi):
    gametypes = {
        'tibia': 'Tibia',
    }
    identity_id = 'tibia_character'
    description = """
        For Tibia, you bet on PvP battle outcome.
        After you and your friend make & accept bet on your Tibia character names,
        system will monitor if one of these characters dies.
        The one who died first from the hand of another character
        is considered looser,
        even if the killer was not the only cause of death
        (e.g. cooperated with monster or other player).

        If both characters killed each other in the same second (e.g. with poison),
        game result will be considered draw.

        Important: both characters should reside in the same world.
    """

    class Parser(HTMLParser):
        def __call__(self, page):
            self.tags = []
            self.name = None
            self.char_404 = False
            self.deaths_found = False
            self.deaths = []
            try:
                self.feed(page)
                self.close()
            except StopIteration:
                pass
            if self.char_404:
                return None, None
            return self.name, self.deaths
        @property
        def tag(self):
            return self.tags[-1] if self.tags else None
        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)
            self.attrs = dict(attrs)
        def handle_data(self, data):
            if self.name is None and self.tag == 'td' and data == 'Name:':
                self.name = ''
                return
            if self.name == '' and self.tag == 'td':
                self.name = data
                return
            if self.tag == 'b':
                if data == 'Could not find character':
                    self.char_404 = True
                    raise StopIteration
                if data == 'Character Deaths':
                    self.deaths_found = True
                    self.date = None
                    self.msg = None
                    self.players = None
                return
            if not self.deaths_found:
                return
            # here deaths_found == True
            if(self.tag == 'td'
               and self.attrs.get('width') == '25%'
               and self.attrs.get('valign') == 'top'):
                # date
                if self.msg:
                    self.deaths.append(
                        (self.date, self.msg, self.players)
                    )
                self.date = date_parse(data.replace('\xA0', ' '))
                self.msg = ''
                self.players = []
            elif self.date:
                self.msg += data
                if self.tag == 'a':
                    self.players.append(data)
        def handle_endtag(self, tag):
            self.tags.pop()
            if self.deaths_found and tag == 'table':
                self.deaths.append(
                    (self.date, self.msg, self.players)
                )
                raise StopIteration
    @classmethod
    def fetch(cls, playername):
        """
        If player found, returns tuple of normalized name and list of deaths.
        If player not found, returns (None, None).
        """
        page = cls.request(
            'GET',
            'http://www.tibia.com/community/',
            params=dict(
                subtopic = 'characters',
                name = playername,
            ),
        ).text
        parser = cls.Parser(convert_charrefs=True)
        ret = parser(page)
        log.debug('TibiaPoller: fetching character {}: {}'.format(
            playername, ret))
        return ret

    def identity_check(val):
        name, deaths = TibiaPoller.fetch(val.strip())
        if not name:
            raise ValueError('Unknown character '+val)
        # TODO: also save world somewhere
        return name
    Identity(identity_id, 'Tibia Character name', identity_check)
    del identity_check

    def prepare(self):
        self.players = {}
    def getDeaths(self, name):
        if name not in self.players:
            _, deaths = self.fetch(name)
            self.players[name] = deaths
        return self.players[name]
    def pollGame(self, game):
        crea = SimpleNamespace(uid=game.gamertag_creator, role='creator')
        oppo = SimpleNamespace(uid=game.gamertag_opponent, role='opponent')
        crea.other, oppo.other = oppo, crea
        for user in crea, oppo:
            user.deaths = self.getDeaths(user.uid)
            user.lost = False
            user.losedate = None

            for date, msg, killers in user.deaths:
                if date < game.accept_date:
                    continue
                if user.other.uid in killers:
                    user.lost = True
                    if not user.losedate:
                        user.losedate = date
        for user in crea, oppo:
            if user.lost:
                winner = user.other.role
                date = user.losedate
                if user.other.lost: # killed each other?
                    # winner is the one who did it first
                    if user.losedate == user.other.losedate:
                        winner = 'draw'
                    elif user.losedate > user.other.losedate:
                        # this one won!
                        date = user.other.losedate
                        winner = user.role
                    # else - killed each other but this was killed first
                return self.gameDone(game, user.other.role, user.losedate)
        return False


class HonestyPoller(Poller):
    gametypes = {
        'halo-5': 'Halo 5',
        'inba2015': 'Inba2k16',
    }
    honesty = True # we don't need any identities to be active
    description = 'This game is supported in Honesty mode only.'
    twitch = 0

    def poll(self, *args, **kwargs):
        # just do nothing
        pass

class DummyPoller(Poller):
    """
    This poller covers all game types not supported yet.
    """
    gametypes = {
        'battlefield-4': 'Battlefield 4',
        'call-of-duty-advanced-warfare': 'Call Of Duty - Advanced Warfare',
        'call-of-duty-black-ops-3': 'Call Of Duty - Black Ops III',
        'destiny': 'Destiny',
        'grand-theft-auto-5': 'Grand Theft Auto V',
        'minecraft': 'Minecraft',
        'rocket-league': 'Rocket League',
        #'diablo': 'Diablo III', # -- we have no image..
    }
    identity_id = ''
    category = {
        'minecraft': 'Action',
        'rocket-league': 'Action',
    }
    subtitle = {
        'grand-theft-auto': 'V',
        'call-of-duty-advanced-warfare': 'Advanced Warfare',
        'call-of-duty-black-ops-3': 'Black Ops III',
    }

    def pollGame(self, game):
        pass

class TestPoller(Poller):
    gametypes = {
        'test': 'Test',
    }
    identity_id = ''
    twitch = 2
    twitch_gametypes = {
        'test': 'None', # to allow missing streams
    }
    def pollGame(self, game):
        pass

# validation
for poller in Poller.allPollers():
    if poller.identity_id and not poller.identity:
        raise ValueError('Bad identity id: '+poller.identity_id)
    if poller.twitch_identity_id and not poller.twitch_identity:
        raise ValueError('Bad twitch identity id: '+poller.identity_id)

def poll_all():
    log.info('Polling started')

    # we run each 5 minutes, so round value to avoid delays interfering matching
    now = datetime.utcfromtimestamp(
        datetime.utcnow().timestamp() // (5*60) * (5*60)
    )

    # TODO: run them all simultaneously in background, to use 2sec api delays
    for poller in Poller.allPollers():
        if not poller.identity: # root or dummy
            continue
        if poller.minutes and now.minute % poller.minutes != 0:
            log.info('Skipping poller {} because of timeframes'.format(poller))
            continue
        pin = poller()
        pin.poll(now)

    log.info('Polling done')


if __name__ == '__main__':
    notify_users = dummyfunc(
        '** Notifying users about state change in game {}')
    notify_event = dummyfunc(
        '** Notifying users about event happened '
        'in game {game}, event {text}')

    # This is a mock class which simulates database model functionality
    class Game:
        # actually it should be a reference to this or another Game object
        class Query(SimpleNamespace):
            _game = None
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
            def filter_by(self, **kwargs):
                # copy self
                dup = self.__class__(**self.__dict__)
                dup.__dict__.update(kwargs)
                return dup
            def count(self):
                return 1
            def __iter__(self):
                key = tuple((k,v) for k,v in self.__dict__.items()
                        if not k.startswith('_'))
                print('** Querying games with %s'%dict(key))

                if not self.__class__._game:
                    self.__class__._game = Game()
                game = self.__class__._game
                for attr in ('gametype', 'gamemode', 'start',
                             'gamertag_creator', 'gamertag_opponent'):
                    setattr(game, attr, getattr(
                        self, attr, getattr(
                            args,
                            attr[9:] if 'tag_' in attr else attr
                        )
                    ))
                yield game
            def first(self):
                return next(iter(self))
            def clear(self):
                self.__class__._game = None
        query = Query()
        root = 'Gaming session'
        _isDone = False
        _silent = True
        def __init__(self):
            self.gamertag_creator = None
            self.gamertag_opponent = None
            self.gametype = None
            self.gamemode = None
            self.start = datetime.now()

            self.meta = None
            self._silent = False
        @property
        def accept_date(self):
            return getattr(self, 'start')
        def __setattr__(self, k, v):
            if not self._silent and v != getattr(self, k):
                print('** Setting property on game obj: {}={}'.format(k,v))
            super().__setattr__(k, v)

    def gameDone(game, winner, timestamp=None, details=None):
        print('** Marking game {} as done - '
              'winner {}, timestamp {}, details {}'.format(
                  game, winner, timestamp, details))
        game._isDone = True
    Poller.gameDone = gameDone

    from argparse import ArgumentParser
    parser = ArgumentParser(description='You can run either all-tests mode with -t '
                            'or custom-test mode with positional arguments.')
    parser.add_argument('-t', '--tests', action='store_true', default=False,
                        help='Testing mode. '
                        'In this mode other options should not be specified; '
                        'their values will be taken from Poller.tests field '
                        'for each poller class.')
    parser.add_argument('creator', nargs='?',
                        help='First opponent\'s identity')
    parser.add_argument('opponent', nargs='?',
                        help='Second opponent\'s identity')
    parser.add_argument('gametype', nargs='?',
                        help='This determines which poller will be used')
    parser.add_argument('gamemode', nargs='?',
                        help='Can be omitted '
                        'if you want to test all gamemodes of given poller '
                        'or if it doesn\'t use gamemodes at all.')
    parser.add_argument('-s', '--start', type=date_parse, default=None,
                        help='Time when the virtual game invitation was accepted; '
                        'defaults to now.')
    now = datetime.utcfromtimestamp(
        datetime.utcnow().timestamp() // (5*60) * (5*60)
    )
    parser.add_argument('-n', '--now', nargs=1, default=now,
                        help='Time when the test is performed (for "minutes" field), '
                        'defaults to current time rounded by 5 minutes.')
    args = parser.parse_args()

    if not args.tests and not args.gametype:
        parser.error(
            'Please specify either --tests or creator, opponent and gametype')

    if args.tests:
        for poller in Poller.allPollers():
            print('Testing poller %s'%poller.__name__)
            for gametype in ([args.gametype]
                             if args.gametype else
                             poller.gametypes):
                tests = poller.tests
                if isinstance(tests, dict):
                    tests = tests.get(gametype, [])
                for creator, opponent, gamemode, start, winner in tests:
                    args.creator = creator
                    args.opponent = opponent
                    args.gamemode = gamemode
                    args.start = date_parse(start)

                    for role in 'creator', 'opponent':
                        print('Checking gamertag '+getattr(args, role))
                        setattr(args, role,
                                poller.identity_check(
                                    getattr(args, role)))

                    print()
                    print('Notifying poller that game was started')
                    poller.gameStarted(Game.query.first()) # FIXME

                    pin = poller()
                    if args.gamemode or not poller.usemodes:
                        print()
                        print('Preparing poller')
                        pin.prepare()
                    print()
                    print('Polling')
                    pin.poll(args.now, gametype, args.gamemode)

                    game = Game.query.first()
                    if not game._isDone:
                        print('This poller didn\'t end this game!')
                        sys.exit(1)
                    print('Game was ended.')
                    if game.winner != winner:
                        print('This game\'s winner doesn\'t match expected!')
                        print('Expected {}, got {}'.format(
                            winner, game.winner,
                        ))
                        sys.exit(1)

                    Game.query.clear()

        sys.exit()

    poller = Poller.findPoller(args.gametype)
    if not poller:
        parser.error('Unknown gametype '+args.gametype)
    if args.gamemode and not poller.usemodes:
        parser.error('This poller doesn\'t support game modes')

    for role in 'creator', 'opponent':
        print('Checking gamertag '+getattr(args, role))
        setattr(args, role,
                poller.identity_check(
                    getattr(args, role)))

    print()
    print('Notifying poller that game was started')
    poller.gameStarted(Game.query.first()) # FIXME

    pin = poller()
    if args.gamemode or not poller.usemodes:
        print()
        print('Preparing poller')
        pin.prepare()
    print()
    print('Polling')
    pin.poll(args.now, args.gametype, args.gamemode)

    if(Game.query.first()._isDone):
        print('Game was ended!')
    else:
        print('Didn\'t end the game.')
    print('Done.')
