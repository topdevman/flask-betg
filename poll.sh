#!/bin/bash

cd "$(dirname "${BASH_SOURCE[0]}" )"
source ../env/bin/activate
./poll.py >> ../polling.log 2>&1
