"""Control-M Automation API job definitions: one folder per workflow, one Kubernetes job per task.

The field names follow BMC's published examples for `Job:Kubernetes`; they have not been run against a Control-M server.
"""

from berth import yamlio
from berth.model import Workflow
from berth.render.pod import Context, Doc, task_job

_DAYS = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")


def _expand(field: str, low: int, high: int, what: str) -> list[int]:
    values: list[int] = []
    for part in field.split(","):
        if "/" in part or part == "*":
            msg = f"Control-M cannot express the {what} field {field!r}: list the values instead of using * or steps"
            raise ValueError(msg)
        start, _, end = part.partition("-")
        first, last = int(start), int(end or start)
        if not low <= first <= last <= high:
            msg = f"{what} value {part!r} is outside {low}-{high}"
            raise ValueError(msg)
        values.extend(range(first, last + 1))
    return sorted(set(values))


def controlm_when(cron: str) -> Doc:
    """Translate the part of cron Control-M can express: one fixed time a day, on chosen weekdays or month days."""
    minute, hour, month_day, month, weekday = cron.split()
    if not (minute.isdigit() and hour.isdigit()):
        msg = f"Control-M runs a job at one fixed time a day; {cron!r} needs a single minute and hour"
        raise ValueError(msg)
    if month != "*":
        msg = f"{cron!r}: month restrictions need a Control-M calendar; ask the platform team"
        raise ValueError(msg)
    if month_day != "*" and weekday != "*":
        msg = f"{cron!r}: set month days or weekdays, not both"
        raise ValueError(msg)
    when: Doc = {"FromTime": f"{int(hour):02d}{int(minute):02d}"}
    if month_day != "*":
        when["MonthDays"] = [str(day) for day in _expand(month_day, 1, 31, "month day")]
    else:
        days = range(7) if weekday == "*" else _expand(weekday, 0, 7, "weekday")
        when["WeekDays"] = sorted({_DAYS[day % 7] for day in days}, key=_DAYS.index)
    return when


def folder_name(app: str, workflow: str) -> str:
    return f"{app}-{workflow}"


def folders(ctx: Context, workflows: dict[str, Workflow]) -> Doc:
    instance = ctx.instance
    target = ctx.platform.environments[instance.env].controlm
    result: Doc = {}
    for name in sorted(workflows):
        workflow = workflows[name]
        folder: Doc = {"Type": "Folder", "ControlmServer": target.server, "Application": instance.app, "SubApplication": name, "Description": ctx.app.description}
        if workflow.schedule is None:
            folder["OrderMethod"] = "Manual"
        for task_name in sorted(workflow.tasks):
            task = workflow.tasks[task_name]
            manifest: Doc = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"generateName": f"{instance.app}-{name}-{task_name}-", "namespace": instance.namespace, "labels": ctx.labels(f"{name}-{task_name}")},
                "spec": task_job(ctx, name, task_name, task, retries=task.retries),
            }
            job: Doc = {"Type": "Job:Kubernetes", "ConnectionProfile": target.connection_profile, "Job Spec Yaml": yamlio.dump(manifest), "Get Pod Logs": "Get Logs", "Job Cleanup": "Delete Job"}
            if workflow.schedule is not None:
                job["When"] = controlm_when(workflow.schedule)
            folder[task_name] = job
        for task_name in sorted(workflow.tasks):
            for parent in workflow.tasks[task_name].depends_on:
                folder[f"flow-{parent}-to-{task_name}"] = {"Type": "Flow", "Sequence": [parent, task_name]}
        result[folder_name(instance.app, name)] = folder
    return result
