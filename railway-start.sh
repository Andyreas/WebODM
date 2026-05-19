#!/bin/bash
cd /webodm

# Ensure psql can authenticate without interactive prompt
export PGPASSWORD="${WO_DATABASE_PASSWORD}"

./worker.sh start &
python ec2_lifecycle.py &
exec ./start.sh
