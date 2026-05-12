#!/bin/bash
cd /webodm
./worker.sh start &
python ec2_lifecycle.py &
exec ./start.sh
