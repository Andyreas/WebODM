#!/usr/bin/env python3
"""
EC2 lifecycle manager for WebODM + NodeODX.
Starts EC2 when tasks are queued, stops it after idle timeout.

Start trigger:
  - Tasks QUEUED (10) or RUNNING (20)  → start EC2 immediately
  - Tasks FAILED (30) while EC2 stopped → start EC2 + restart those tasks
    (Failed tasks are almost always due to NodeODM being unreachable)

Disk cleanup:
  - After all tasks finish, SSH into EC2 and delete NodeODX working data
    for completed/failed tasks (results are already on Railway volume).
  - Runs just before the idle timer would fire, so disk is clean for
    the next processing job.
"""
import boto3
import paramiko
import time
import requests
import os
import logging
import sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [ec2-lifecycle] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

EC2_INSTANCE_ID   = os.environ["EC2_INSTANCE_ID"]
AWS_REGION        = os.environ.get("AWS_REGION", "eu-north-1")
NODEODX_URL       = os.environ.get("NODEODX_URL", "http://13.63.132.160:3000")
NODEODX_TOKEN     = os.environ.get("NODEODX_TOKEN", "PSKnodeodm2026")
NODEODX_DATA_DIR  = os.environ.get("NODEODX_DATA_DIR", "/var/nodeodx-data")
EC2_SSH_HOST      = os.environ.get("EC2_SSH_HOST", "13.63.132.160")
EC2_SSH_USER      = os.environ.get("EC2_SSH_USER", "ec2-user")
EC2_SSH_KEY       = os.environ.get("EC2_SSH_KEY", "")          # private key text
WO_URL            = os.environ.get("WO_INTERNAL_URL", "http://localhost:8000")
WO_USERNAME       = os.environ["WO_ADMIN_USER"]
WO_PASSWORD       = os.environ["WO_ADMIN_PASSWORD"]
STARTUP_BUFFER    = int(os.environ.get("EC2_STARTUP_BUFFER", "120"))
IDLE_TIMEOUT      = int(os.environ.get("EC2_IDLE_TIMEOUT", "600"))
POLL_INTERVAL     = 30
DISK_WARN_PERCENT = int(os.environ.get("EC2_DISK_WARN_PERCENT", "80"))

ec2     = boto3.client("ec2", region_name=AWS_REGION)
session = requests.Session()


# ── AWS helpers ──────────────────────────────────────────────────────────────

def get_instance_state():
    r = ec2.describe_instances(InstanceIds=[EC2_INSTANCE_ID])
    return r["Reservations"][0]["Instances"][0]["State"]["Name"]


def start_ec2():
    log.info("Starting EC2 instance %s ...", EC2_INSTANCE_ID)
    ec2.start_instances(InstanceIds=[EC2_INSTANCE_ID])
    while True:
        state = get_instance_state()
        if state == "running":
            break
        log.info("  EC2 state: %s — waiting...", state)
        time.sleep(10)
    log.info("EC2 running. Waiting %ds startup buffer for NodeODX...", STARTUP_BUFFER)
    time.sleep(STARTUP_BUFFER)
    log.info("NodeODX should be ready.")


def stop_ec2():
    log.info("Stopping EC2 instance %s.", EC2_INSTANCE_ID)
    ec2.stop_instances(InstanceIds=[EC2_INSTANCE_ID])


# ── SSH / disk-cleanup helpers ───────────────────────────────────────────────

def _ssh_client():
    """Returns a connected paramiko SSHClient, or None on failure."""
    if not EC2_SSH_KEY:
        log.warning("EC2_SSH_KEY not set — disk cleanup disabled.")
        return None
    try:
        import io
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = paramiko.RSAKey.from_private_key(io.StringIO(EC2_SSH_KEY))
        client.connect(EC2_SSH_HOST, username=EC2_SSH_USER, pkey=pkey, timeout=15)
        return client
    except Exception as e:
        log.warning("SSH connection failed: %s", e)
        return None


def _ssh_run(client, cmd):
    """Run a command over SSH and return (stdout, stderr, exit_code)."""
    _, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    code = stdout.channel.recv_exit_status()
    return out, err, code


def cleanup_nodeodx_disk():
    """
    Delete NodeODX working directories for tasks that are no longer
    QUEUED or RUNNING in WebODM (i.e. completed, failed, or cancelled).
    Also logs current disk usage.
    """
    client = _ssh_client()
    if client is None:
        return
    try:
        # Get disk usage
        out, _, _ = _ssh_run(client, "df -h / | tail -1")
        log.info("EC2 disk: %s", out)

        # List task directories on EC2
        out, _, code = _ssh_run(
            client,
            f"ls -1 {NODEODX_DATA_DIR} 2>/dev/null | grep -v tasks.json"
        )
        if code != 0 or not out:
            log.info("No NodeODX task directories found — disk already clean.")
            return

        ec2_task_ids = set(out.splitlines())

        # Get active task IDs from WebODM (queued=10, running=20)
        active_ids = set()
        try:
            r = session.get(f"{WO_URL}/api/projects/", timeout=10)
            data = r.json()
            projects = data if isinstance(data, list) else data.get("results", [])
            for project in projects:
                pid = project["id"]
                tr = session.get(f"{WO_URL}/api/projects/{pid}/tasks/", timeout=10)
                tdata = tr.json()
                task_list = tdata if isinstance(tdata, list) else tdata.get("results", [])
                for t in task_list:
                    if t.get("status") in (10, 20):
                        active_ids.add(str(t["id"]))
        except Exception as e:
            log.warning("Could not fetch WebODM tasks for cleanup: %s", e)
            return

        # Delete directories that are no longer active
        to_delete = ec2_task_ids - active_ids
        if not to_delete:
            log.info("Disk cleanup: nothing to remove.")
            return

        for tid in to_delete:
            path = f"{NODEODX_DATA_DIR}/{tid}"
            _, _, code = _ssh_run(client, f"sudo rm -rf '{path}'")
            if code == 0:
                log.info("Disk cleanup: removed %s", path)
            else:
                log.warning("Disk cleanup: failed to remove %s", path)

        # Log disk usage after cleanup
        out, _, _ = _ssh_run(client, "df -h / | tail -1")
        log.info("EC2 disk after cleanup: %s", out)

    finally:
        client.close()


def check_disk_and_warn():
    """Log a warning if EC2 disk usage exceeds DISK_WARN_PERCENT."""
    client = _ssh_client()
    if client is None:
        return
    try:
        out, _, _ = _ssh_run(client, "df / | tail -1 | awk '{print $5}' | tr -d '%'")
        pct = int(out) if out.isdigit() else 0
        if pct >= DISK_WARN_PERCENT:
            log.warning("EC2 disk usage is %d%% — consider cleanup!", pct)
        else:
            log.info("EC2 disk usage: %d%%", pct)
    finally:
        client.close()


# ── NodeODX helper ───────────────────────────────────────────────────────────

def nodeodx_queue_count():
    """Returns task queue count, or -1 if unreachable."""
    try:
        r = requests.get(
            f"{NODEODX_URL}/info",
            params={"token": NODEODX_TOKEN},
            timeout=5,
        )
        return r.json().get("taskQueueCount", 0)
    except Exception:
        return -1


# ── WebODM API helpers ───────────────────────────────────────────────────────

def webodm_login():
    try:
        r = session.post(
            f"{WO_URL}/api/token-auth/",
            json={"username": WO_USERNAME, "password": WO_PASSWORD},
            timeout=10,
        )
        if r.status_code == 200:
            session.headers["Authorization"] = f"JWT {r.json()['token']}"
            return True
    except Exception:
        pass
    return False


def _get_all_tasks():
    """Returns list of all task dicts across all projects."""
    tasks = []
    try:
        r = session.get(f"{WO_URL}/api/projects/", timeout=10)
        if r.status_code in (401, 403):
            webodm_login()
            r = session.get(f"{WO_URL}/api/projects/", timeout=10)
        data = r.json()
        projects = data if isinstance(data, list) else data.get("results", [])
        for project in projects:
            pid = project["id"]
            tr = session.get(f"{WO_URL}/api/projects/{pid}/tasks/", timeout=10)
            tdata = tr.json()
            task_list = tdata if isinstance(tdata, list) else tdata.get("results", [])
            for t in task_list:
                t["_project_id"] = pid
            tasks.extend(task_list)
    except Exception as e:
        log.warning("Could not fetch tasks: %s", e)
    return tasks


def has_active_tasks(tasks):
    """Returns True if any task is QUEUED (10) or RUNNING (20)."""
    return any(t.get("status") in (10, 20) for t in tasks)


def get_failed_tasks(tasks):
    """Returns list of tasks in FAILED (30) state."""
    return [t for t in tasks if t.get("status") == 30]


def restart_tasks(failed_tasks):
    """Restart a list of failed task dicts via WebODM API."""
    for t in failed_tasks:
        pid = t["_project_id"]
        tid = t["id"]
        try:
            r = session.post(
                f"{WO_URL}/api/projects/{pid}/tasks/{tid}/restart/",
                timeout=10,
            )
            if r.status_code in (200, 204):
                log.info("Restarted task %s (project %s).", tid, pid)
            else:
                log.warning("Could not restart task %s: HTTP %s", tid, r.status_code)
        except Exception as e:
            log.warning("Error restarting task %s: %s", tid, e)


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    log.info(
        "Starting — instance=%s, startup_buffer=%ds, idle_timeout=%ds",
        EC2_INSTANCE_ID, STARTUP_BUFFER, IDLE_TIMEOUT,
    )
    time.sleep(30)
    webodm_login()

    idle_since      = None
    cleanup_done    = False   # only clean once per idle period

    while True:
        try:
            tasks  = _get_all_tasks()
            active = has_active_tasks(tasks)
            failed = get_failed_tasks(tasks)
            queue  = nodeodx_queue_count()
            state  = get_instance_state()

            log.info(
                "EC2=%s  active=%s  failed=%d  nodeodx_queue=%s",
                state, active, len(failed), queue,
            )

            if state == "stopped":
                cleanup_done = False
                if active:
                    log.info("Active tasks detected — starting EC2.")
                    start_ec2()
                    idle_since = None
                elif failed:
                    log.info(
                        "%d failed task(s) while EC2 stopped — starting EC2 and restarting.",
                        len(failed),
                    )
                    start_ec2()
                    restart_tasks(failed)
                    idle_since = None

            elif not active and queue <= 0:
                if state == "running":
                    if idle_since is None:
                        idle_since = time.time()
                        log.info("No active tasks — idle timer started (%ds).", IDLE_TIMEOUT)
                    else:
                        elapsed = time.time() - idle_since
                        # Clean disk halfway through idle timeout (so it's done before shutdown)
                        if not cleanup_done and elapsed > IDLE_TIMEOUT / 2:
                            log.info("Running disk cleanup before shutdown...")
                            cleanup_nodeodx_disk()
                            cleanup_done = True
                        if elapsed > IDLE_TIMEOUT:
                            stop_ec2()
                            idle_since = None
            else:
                if idle_since:
                    log.info("Activity detected — idle timer reset.")
                idle_since   = None
                cleanup_done = False

        except Exception as e:
            log.error("Lifecycle manager error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
