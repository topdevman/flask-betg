#!/bin/bash
exec gunicorn "$@" \
	--name betgame-observer \
	--workers 1 --worker-class eventlet \
	--bind localhost:8021 -m 007 \
	'observer:init_app()' \
	&>> /home/betgame/observer.log
