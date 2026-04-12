#!/bin/sh
set -e

BACKUP_CRON="${BACKUP_CRON:-0 2 * * *}"

# Dump all env vars into a sourceable file.
# shlex.quote handles spaces, newlines, and special characters correctly.
python3 -c "
import os, shlex
with open('/app/.env.docker', 'w') as f:
    for k, v in os.environ.items():
        f.write('export {}={}\n'.format(k, shlex.quote(v)))
"

# Wrapper: source the env file then run the backup.
# Needed because cron strips the environment before running jobs.
cat > /app/run_backup.sh << 'WRAPPER'
#!/bin/sh
. /app/.env.docker
cd /app
python backup.py
WRAPPER
chmod +x /app/run_backup.sh

# Write crontab. Redirect to PID 1 file descriptors so output appears in
# `docker logs` output.
printf '%s /app/run_backup.sh >> /proc/1/fd/1 2>> /proc/1/fd/2\n' "$BACKUP_CRON" \
    > /etc/cron.d/db-backup
echo "" >> /etc/cron.d/db-backup
chmod 0644 /etc/cron.d/db-backup
crontab /etc/cron.d/db-backup

echo "db-backup: cron started. Schedule: ${BACKUP_CRON}"
exec cron -f
