"""Turn review results into text for a terminal and Markdown for a pipeline summary."""

from collections.abc import Mapping
from collections.abc import Set as AbstractSet

from berth.diff import show
from berth.review import Decision, Requirement

_WIDTH = 48


def _show(value: object) -> str:
    if value is None:
        return "-"
    text = ", ".join(str(item) for item in value) if isinstance(value, tuple) else str(value)
    if "\n" in text:  # a file's contents: say how big, and leave the content to the rendered diff
        count = len(text.splitlines())
        return f"({count} line{'' if count == 1 else 's'})"
    return text if len(text) <= _WIDTH else text[: _WIDTH - 3] + "..."


def text(reqs: list[Requirement], affected: Mapping[str, AbstractSet[str]], decision: Decision, notes: tuple[str, ...] = ()) -> str:
    lines: list[str] = []
    for req in reqs:
        lines.append(f"{req.app or 'platform files'}: {req.tier.name.lower()} review")
        rows = [(f.change.env or "*", show(f.change.path), f"{_show(f.change.before)} -> {_show(f.change.after)}", f.tier.name.lower(), f.reason) for f in req.findings]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        lines.extend("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:4])) + f"  {row[4]}" for row in rows)
    unexplained = {app: envs for app, envs in affected.items() if app not in {req.app for req in reqs}}
    if unexplained:
        lines.append("rendered output changes without a spec change (owners are notified, not asked):")
        lines.extend(f"  {app}: {', '.join(sorted(envs))}" for app, envs in sorted(unexplained.items()))
    lines.extend(f"note: {note}" for note in notes)
    lines.append(f"gate: {decision.state}")
    lines.extend(f"  {line}" for line in decision.lines)
    return "\n".join(lines) + "\n"


def markdown(reqs: list[Requirement], affected: Mapping[str, AbstractSet[str]], decision: Decision, notes: tuple[str, ...] = ()) -> str:
    out = [f"## berth review: {decision.state}", ""]
    out.extend(f"- {line}" for line in decision.lines)
    out.extend(f"- Note: {note}" for note in notes)
    for req in reqs:
        out += ["", f"### {req.app or 'platform files'} ({req.tier.name.lower()})", "", "| env | path | change | tier | why |", "|---|---|---|---|---|"]
        out.extend(f"| {f.change.env or '*'} | `{show(f.change.path)}` | {_show(f.change.before)} → {_show(f.change.after)} | {f.tier.name.lower()} | {f.reason} |" for f in req.findings)
    unexplained = sorted(app for app in affected if app not in {req.app for req in reqs})
    if unexplained:
        out += ["", "### Rendered output changed without a spec change", "", *(f"- {app}: {', '.join(sorted(affected[app]))}" for app in unexplained)]
    return "\n".join(out) + "\n"
