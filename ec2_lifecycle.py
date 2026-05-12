#!/usr/bin/env python3
"""
EC2 lifecycle manager for WebODM + NodeODX.
Starts EC2 when tasks are queued, stops it after idle timeout.
"""
import boto3
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

EC2_INSTANCE_ID  = os.environ["EC2_INSTANCE_ID"]
AWS_REGION       = os.environ.get("AWS_REGION", "eu-north-1")
NODEODX_URL      = os.environ.get("NODEODX_URL", "http://13.63.132.160:3000")
NODEODX_TOKEN    = os.environ.get("NODEODX_TOKEN", "PSKnodeodm2026")
WO_URL           = os.environ.get("WO_INTERNAL_URL", "http://localhost:8000")
WO_USERNAME      = os.environ["WO_ADMIN_USER"]
WO_PASSWORD      = os.environ["WO_ADMIN_PASSWORD"]
STARTUP_BUFFER   = int(os.environ.get("EC2_STARTUP_BUFFER", "120"))
IDLE_TIMEOUT     = int(os.environ.get("EC2_IDLE_TIMEOUT", "600"))
POLL_INTERVAL    = 30

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


def has_active_tasks():
    """Returns True if any WebODM task is QUEUED (10) or RUNNING (20)."""
    # WebODM task statuses: 10=queued, 20=running, 30=failed, 40=completed, 50=cancelled
    ACTIVE = {10, 20}
    try:
        r = session.get(f"{WO_URL}/api/projects/", timeout=10)
        if r.status_code == 401:
            webodm_login()
            r = session.get(f"{WO_URL}/api/projects/", timeout=10)
        for project in r.json().get("results", []):
            pid = project["id"]
            tr = session.get(
                f"{WO_URL}/api/projects/{pid}/tasks/",
                timeout=10,
            )
            if any(t.get("status") in ACTIVE for t in tr.json().get("results", [])):
                return True
    except Exception as e:
        log.warning("Could not check WebODM tasks: %s", e)
    return False


# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    log.info(
        "Starting — instance=%s, startup_buffer=%ds, idle_timeout=%ds",
        EC2_INSTANCE_ID, STARTUP_BUFFER, IDLE_TIMEOUT,
    )
    # Wait for WebODM to be ready before trying to log in
    time.sleep(30)
    webodm_login()

    idle_since = None

    while True:
        try:
            active = has_active_tasks()
            queue  = nodeodx_queue_count()
            state  = get_instance_state()

            log.info("EC2=%s  active_tasks=%s  nodeodx_queue=%s", state, active, queue)

            if active and state == "stopped":
                start_ec2()
                idle_since = None

            elif not active and queue <= 0:
                if state == "running":
                    if idle_since is None:
                        idle_since = time.time()
                        log.info("No active tasks — idle timer started (%ds).", IDLE_TIMEOUT)
                    elif time.time() - idle_since > IDLE_TIMEOUT:
                        stop_ec2()
                        idle_since = None
            else:
                if idle_since:
                    log.info("Activity detected — idle timer reset.")
                idle_since = None

        except Exception as e:
            log.error("Lifecycle manager error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
