from app.plugins import PluginBase, Menu, MountPoint
from django.shortcuts import render
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
import boto3
import os
import logging

log = logging.getLogger('app.logger')


def _ec2_client():
    return boto3.client("ec2", region_name=os.environ.get("AWS_REGION", "eu-north-1"))


def _get_state():
    iid = os.environ.get("EC2_INSTANCE_ID", "")
    if not iid:
        raise ValueError("EC2_INSTANCE_ID environment variable not set")
    r = _ec2_client().describe_instances(InstanceIds=[iid])
    return r["Reservations"][0]["Instances"][0]["State"]["Name"]


class Plugin(PluginBase):
    def main_menu(self):
        return [Menu("Wake EC2", self.public_url(""), "fa fa-power-off fa-fw")]

    def app_mount_points(self):
        plugin = self

        @login_required
        def index(request):
            return render(request, plugin.template_path("wakeec2.html"), {
                'title': 'Wake EC2',
            })

        @login_required
        @require_POST
        def start(request):
            iid = os.environ.get("EC2_INSTANCE_ID", "")
            if not iid:
                return JsonResponse({"error": "EC2_INSTANCE_ID not configured"}, status=500)
            try:
                state = _get_state()
                if state == "running":
                    return JsonResponse({"state": "running", "message": "EC2 is already running"})
                if state not in ("stopped",):
                    return JsonResponse({"state": state, "message": "EC2 is {}  — wait a moment".format(state)})
                _ec2_client().start_instances(InstanceIds=[iid])
                return JsonResponse({"state": "starting", "message": "EC2 is starting — takes ~2 minutes"})
            except Exception as e:
                log.error("Wake EC2 start error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        @login_required
        def status(request):
            if not os.environ.get("EC2_INSTANCE_ID", ""):
                return JsonResponse({"error": "EC2_INSTANCE_ID not configured"}, status=500)
            try:
                return JsonResponse({"state": _get_state()})
            except Exception as e:
                log.error("Wake EC2 status error: %s", e)
                return JsonResponse({"error": str(e)}, status=500)

        return [
            MountPoint('$', index),
            MountPoint('start$', start),
            MountPoint('status$', status),
        ]
