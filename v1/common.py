from flask import current_app
### Logging ###
class log_cls:
    """
    Just a handy wrapper for current_app.logger
    """
    def __getattr__(self, name):
        return getattr(current_app.logger, name)
log = log_cls()


class classproperty:
    """
    Cached class property; evaluated only once
    """
    def __init__(self, fget):
        self.fget = fget
        self.obj = {}
    def __get__(self, owner, cls):
        if cls not in self.obj:
            self.obj[cls] = self.fget(cls)
        return self.obj[cls]


