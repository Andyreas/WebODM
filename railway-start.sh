#!/bin/bash
cd /webodm
./worker.sh start &
exec ./start.sh
