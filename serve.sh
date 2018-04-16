#!/bin/bash
if [ "$1" == "-d" ]; then
	# debug
	bind=localhost:${2:-8080}
	workers=1
	app='main:debug()'
	opts="--reload --preload --timeout 3600"
	shift; shift
else
	# deploy
	if [[ "$PWD" == *"test"* ]]
	then
		testing="-test"
		port=8011
	else
		testing=""
		port=8001
	fi
	bind=${1:-localhost:$port}
	workers=${2:-1} # was 3, but socket.io requires 1
	logfile="/home/betgame/betgame${testing}.log"
	app="main:live('$logfile')"
	opts="--name betgame${testing:--main} --access-logfile /home/betgame/access${testing}.log --error-logfile /home/betgame/errors${testing}.log"
	shift 2
fi
# --preload 
exec gunicorn "$@" $opts --workers ${workers} --worker-class eventlet --bind ${bind} -m 007 "$app"
