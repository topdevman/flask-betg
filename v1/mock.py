from types import SimpleNamespace
import traceback

class log:
    def debug_print(meth):
        def doprint(cls, msg, exc_info=(meth=='exception')):
            print('{}: {}'.format(meth.upper(), msg))
            if exc_info:
                import traceback
                traceback.print_exc()
        return doprint
    for meth in 'debug info warning error exception'.split():
        locals()[meth] = classmethod(debug_print(meth))

config = SimpleNamespace(
    PAYPAL_SANDBOX = None,
    # just dummy address, as we have no observer here
    OBSERVER_URL = 'http://localhost/',
)

def dummyfunc(message, order=[], ondone=None):
    def func(*args, **kwargs):
        for arg in order:
            if arg in kwargs:
                args.append(kwargs.pop(arg))
        print(message.format(*args, **kwargs))
        if ondone:
            ondone()
    return func

db = SimpleNamespace(
    session = SimpleNamespace()
)
db.session.add = dummyfunc('** Adding object {} to database session')
db.session.commit = dummyfunc('** Commiting DB')

