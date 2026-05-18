from app.plugins import PluginBase, Menu, MountPoint
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.conf import settings
import boto3
import os
import shutil
import subprocess
import logging
try:
    from urllib.request import urlopen
    from urllib.error import URLError
except ImportError:
    from urllib2 import urlopen, URLError

log = logging.getLogger('app.logger')

NODEODX_URL   = lambda: os.environ.get("NODEODX_URL", "http://13.63.132.160:3000")
NODEODX_TOKEN = lambda: os.environ.get("NODEODX_TOKEN", "PSKnodeodm2026")


def _ec2_client():
    return boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "eu-north-1"))


def _get_state():
    iid = os.environ.get("EC2_INSTANCE_ID", "")
    if not iid:
        raise ValueError("EC2_INSTANCE_ID environment variable not set")
    r = _ec2_client().describe_instances(InstanceIds=[iid])
    return r["Reservations"][0]["Instances"][0]["State"]["Name"]


def _nodeodx_ready():
    """Returns True if NodeODX /info responds with a valid reply."""
    try:
        url = "{}/info?token={}".format(NODEODX_URL(), NODEODX_TOKEN())
        resp = urlopen(url, timeout=5)
        return resp.status == 200
    except Exception:
        return False


def _dir_size_bytes(path):
    """Fast directory size via du."""
    try:
        r = subprocess.run(
            ["du", "-sb", path],
            capture_output=True, text=True, timeout=30
        )
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _find_orphaned_dirs():
    """
    Scan WebODM media/project/*/task/* and return directories
    whose task UUID is not in the database.
    """
    from app.models import Task as WOTask

    media_root = settings.MEDIA_ROOT
    active_ids = set(str(t.id) for t in WOTask.objects.all())

    orphaned = []
    projects_path = os.path.join(media_root, "project")

    if not os.path.isdir(projects_path):
        return orphaned

    for pid in os.listdir(projects_path):
        task_path = os.path.join(projects_path, pid, "task")
        if not os.path.isdir(task_path):
            continue
        for tid in os.listdir(task_path):
            if tid not in active_ids:
                full_path = os.path.join(task_path, tid)
                orphaned.append({
                    "path": full_path,
                    "task_id": tid,
                    "project_id": pid,
                    "size": _dir_size_bytes(full_path),
                })

    return orphaned


class Plugin(PluginBase):
    def main_menu(self):
        return [Menu("Wake EC2", self.public_url(""), "fa fa-power-off fa-fw")]

    def app_mount_points(self):
        plugin = self

        @login_required
        def index(request):
            return render(request, plugin.template_path("wakeec2.html"), {
                "title": "Wake EC2",
            })

        # ── EC2 endpoints ─────────────────────────────────────────────────────

        @login_required
        @require_POST
        def reboot(request):
            iid = os.environ.get("EC2_INSTANCE_ID", "")
            if not iid:
                return JsonResponse({"error": "EC2_INSTANCE_ID not configured"}, status=500)
            try:
                _ec2_client().reboot_instances(InstanceIds=[iid])
                return JsonResponse({"ec2": "rebooting", "nodeodx": False, "message": "EC2 rebooting — NodeODX ready in ~2 minutes"})
            except Exception as e:
                log.error("Wake EC2 reboot error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        @login_required
        @require_POST
        def start(request):
            iid = os.environ.get("EC2_INSTANCE_ID", "")
            if not iid:
                return JsonResponse({"error": "EC2_INSTANCE_ID not configured"}, status=500)
            try:
                state = _get_state()
                if state == "running":
                    return JsonResponse({"ec2": "running", "nodeodx": _nodeodx_ready(), "message": "EC2 is already running"})
                if state not in ("stopped",):
                    return JsonResponse({"ec2": state, "nodeodx": False, "message": "EC2 is {} — wait a moment".format(state)})
                _ec2_client().start_instances(InstanceIds=[iid])
                return JsonResponse({"ec2": "starting", "nodeodx": False, "message": "EC2 is starting — takes ~2 minutes"})
            except Exception as e:
                log.error("Wake EC2 start error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        @login_required
        def status(request):
            if not os.environ.get("EC2_INSTANCE_ID", ""):
                return JsonResponse({"error": "EC2_INSTANCE_ID not configured"}, status=500)
            try:
                ec2_state = _get_state()
                nodeodx = _nodeodx_ready() if ec2_state == "running" else False
                return JsonResponse({"ec2": ec2_state, "nodeodx": nodeodx})
            except Exception as e:
                log.error("Wake EC2 status error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        # ── Storage endpoints ──────────────────────────────────────────────────

        @login_required
        def storage(request):
            try:
                media_root = settings.MEDIA_ROOT
                total, used, free = shutil.disk_usage(media_root)
                orphaned = _find_orphaned_dirs()
                return JsonResponse({
                    "total": total,
                    "used": used,
                    "free": free,
                    "orphaned": orphaned,
                    "orphaned_size": sum(o["size"] for o in orphaned),
                })
            except Exception as e:
                log.error("Wake EC2 storage error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        @login_required
        @require_POST
        def cleanup(request):
            if not request.user.is_staff:
                return JsonResponse({"error": "Admin only"}, status=403)
            try:
                orphaned = _find_orphaned_dirs()
                deleted = []
                errors = []
                freed = 0
                for o in orphaned:
                    try:
                        shutil.rmtree(o["path"])
                        deleted.append(o["path"])
                        freed += o["size"]
                        log.info("Cleaned orphaned task dir: %s", o["path"])
                    except Exception as e:
                        errors.append({"path": o["path"], "error": str(e)})
                        log.error("Failed to clean %s: %s", o["path"], e)
                return JsonResponse({"deleted": len(deleted), "freed": freed, "errors": errors})
            except Exception as e:
                log.error("Wake EC2 cleanup error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        return [
            MountPoint("$", index),
            MountPoint("start$", start),
            MountPoint("reboot$", reboot),
            MountPoint("status$", status),
            MountPoint("storage$", storage),
            MountPoint("cleanup$", cleanup),
        ]
