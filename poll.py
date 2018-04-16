#!/usr/bin/env python3

import main, v1

if __name__ == '__main__':
    # for db access to work
    main.live().app_context().push()

    v1.polling.poll_all()
