from sqlalchemy.orm import deferred
from sqlalchemy.ext.declarative import declared_attr

from v1.main import db

from v1.badges.badges_description import FIFA15_BADGES


class Fifa15Badges(object):
    # example
    @declared_attr
    def fifa15_xboxone_first_win(self):
        return deferred(
            db.Column(db.MutaleDictPickleType,
                      default=FIFA15_BADGES["badges"]["fifa15_xboxone_first_win"]["user_bounded"]),
            group=FIFA15_BADGES["group_name"]  # group of badges, can be grouped by games ids etc.
        )

    @declared_attr
    def fifa15_xboxone_10_wins(self):
        return deferred(
            db.Column(db.MutaleDictPickleType,
                      default=FIFA15_BADGES["badges"]["fifa15_xboxone_10_wins"]["user_bounded"]),
            group=FIFA15_BADGES["group_name"]
        )

    @declared_attr
    def fifa15_xboxone_first_loss(self):
        return deferred(
            db.Column(db.MutaleDictPickleType,
                      default=FIFA15_BADGES["badges"]["fifa15_xboxone_first_loss"]["user_bounded"]),
            group=FIFA15_BADGES["group_name"]
        )
