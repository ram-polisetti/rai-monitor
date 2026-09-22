"""Static HTML dashboard report.

Renders metric time-series (disparate impact ratio, decision rate, volume),
per-group breakdowns for the latest window, and the incident list into one
self-contained HTML file. Charts are matplotlib PNGs embedded as base64, so
the report has no external dependencies and can be archived or shared as a
single file.

Requires the ``report`` extra (``pip install raimonitor[report]``).
"""

from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path


def _charts(windows: list[dict], group_attrs: list[str]) -> dict[str, str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    images: dict[str, str] = {}
    nan = float("nan")

    def _fig(caption: str, series: dict[str, list], ylabel: str,
             ylim: tuple | None = None) -> None:
        fig, ax = plt.subplots(figsize=(9, 3.2))
        # x axis: union of window starts in chronological order
        starts = sorted({w["window_start"] for w in windows})
        xs = list(range(len(starts)))
        index = {s: i for i, s in enumerate(starts)}
        labels = [s[:10] for s in starts]
        for name, points in sorted(series.items()):
            # points: list of (window_start, value)
            aligned = [nan] * len(starts)
            for s, v in points:
                aligned[index[s]] = v if v is not None else nan
            ax.plot(xs, aligned, marker="o", label=name)
        ax.set_title(caption)
        ax.set_ylabel(ylabel)
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        if ylim:
            ax.set_ylim(*ylim)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110)
        plt.close(fig)
        images[caption] = base64.b64encode(buf.getvalue()).decode()

    systems = sorted({w["system"] for w in windows})
    by_system = {s: [w for w in windows if w["system"] == s] for s in systems}

    # Per-system volume and decision-rate series (kept from session 1).
    for system in systems:
        sw = by_system[system]
        _fig(f"Volume — {system}",
             {system: [(w["window_start"], w["n"]) for w in sw]}, "events")
        _fig(f"Positive-decision rate — {system}",
             {system: [(w["window_start"], w["decision_rate"]) for w in sw]},
             "rate", ylim=(0, 1))

    # Cross-system comparison: one chart per attribute, one line per system.
    for attr in group_attrs:
        _fig(f"Disparate impact ratio ({attr}) — all systems",
             {s: [(w["window_start"],
                   (w["groups"].get(attr) or {}).get("disparate_impact_ratio"))
                  for w in by_system[s]]
              for s in systems},
             "DIR", ylim=(0, 1.05))
        _fig(f"TPR gap ({attr}) — all systems",
             {s: [(w["window_start"], (w.get("tpr_gap") or {}).get(attr))
                  for w in by_system[s]]
              for s in systems},
             "gap", ylim=(0, 1.05))
        _fig(f"FPR gap ({attr}) — all systems",
             {s: [(w["window_start"], (w.get("fpr_gap") or {}).get(attr))
                  for w in by_system[s]]
              for s in systems},
             "gap", ylim=(0, 1.05))

    # Per-group decision-rate time series: one chart per (attr, value),
    # one line per system — shows which group's rate moved, not just DIR.
    attr_values: dict[str, set[str]] = {}
    for w in windows:
        for attr, ginfo in (w.get("groups") or {}).items():
            for value in (ginfo.get("values") or {}):
                attr_values.setdefault(attr, set()).add(value)
    for attr in sorted(attr_values):
        for value in sorted(attr_values[attr]):
            _fig(f"Decision rate ({attr}={value}) — all systems",
                 {s: [(w["window_start"],
                       ((w["groups"].get(attr) or {}).get("values") or {})
                       .get(value, {}).get("decision_rate"))
                      for w in by_system[s]]
                  for s in systems},
                 "rate", ylim=(0, 1))
    return images


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_ci(value, ci, digits: int = 3) -> str:
    """Format a point estimate with its bootstrap CI, e.g. ``0.250 (0.18-0.33)``."""
    if value is None:
        return "n/a"
    base = _fmt(value, digits)
    if ci:
        return f"{base} ({_fmt(ci[0], 2)}-{_fmt(ci[1], 2)})"
    return base


def _latest_per_system(windows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for w in windows:
        latest[w["system"]] = w
    return latest


def build_report(metrics_doc: dict, incidents: list[dict],
                 title: str = "Responsible AI monitoring dashboard",
                 refresh_seconds: int | None = None) -> str:
    """Build the dashboard HTML from a metrics document and incident list.

    ``refresh_seconds`` adds a meta-refresh tag so the page live-updates
    when served by ``raimonitor serve``.
    """
    windows = metrics_doc.get("windows", [])
    group_attrs = sorted({
        attr
        for w in windows
        for attr in (w.get("groups") or {})
    })
    images = _charts(windows, group_attrs) if windows else {}
    open_incidents = [i for i in incidents if i.get("status") == "open"]
    refresh_tag = (f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
                   if refresh_seconds else "")
    guards_note = ""
    if metrics_doc.get("min_group_n", 1) > 1:
        guards_note = (f" Groups below n={metrics_doc['min_group_n']} are "
                       f"flagged as insufficient evidence.")
    if metrics_doc.get("bootstrap_reps", 0):
        guards_note += (f" Rates show 95% bootstrap CIs "
                        f"({metrics_doc['bootstrap_reps']} reps, "
                        f"seed {metrics_doc.get('bootstrap_seed', 0)}).")

    parts = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh_tag}
<title>{_esc(title)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
.cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.card {{ border: 1px solid #ddd; border-radius: 8px; padding: 0.8rem 1.2rem; min-width: 140px; }}
.card b {{ font-size: 1.6rem; display: block; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }}
th {{ background: #f4f4f4; }}
.critical {{ color: #b00020; font-weight: bold; }} .warning {{ color: #9a6a00; font-weight: bold; }}
.insufficient {{ color: #666; font-style: italic; }}
img {{ max-width: 100%; }}
footer {{ color: #666; font-size: 0.8rem; margin-top: 2rem; }}
</style></head><body>
<h1>{_esc(title)}</h1>
<div class="cards">
<div class="card"><b>{len({w['system'] for w in windows})}</b>systems monitored</div>
<div class="card"><b>{len(windows)}</b>windows</div>
<div class="card"><b>{len(open_incidents)}</b>open incidents</div>
<div class="card"><b>{_esc(metrics_doc.get('window', ''))}</b>window size</div>
</div>"""]

    latest = _latest_per_system(windows)
    if latest:
        parts.append("<h2>Cross-system comparison — latest window</h2>")
        parts.append("<table><tr><th>system</th><th>window</th><th>n</th>"
                     "<th>decision rate (95% CI)</th>"
                     + "".join(f"<th>DIR ({_esc(a)})</th>" for a in group_attrs)
                     + "<th>open incidents</th></tr>")
        for system in sorted(latest):
            w = latest[system]
            open_n = sum(1 for i in open_incidents if i.get("system") == system)
            dirs = "".join(
                f"<td>{_fmt((w['groups'].get(a) or {}).get('disparate_impact_ratio'))}</td>"
                for a in group_attrs)
            parts.append(
                f"<tr><td>{_esc(system)}</td><td>{_esc(w['window_start'][:10])}</td>"
                f"<td>{w['n']}</td>"
                f"<td>{_esc(_fmt_ci(w['decision_rate'], w.get('decision_rate_ci')))}</td>"
                f"{dirs}<td>{open_n}</td></tr>")
        parts.append("</table>")

    for caption, png in sorted(images.items()):
        parts.append(f"<h2>{_esc(caption)}</h2>"
                     f'<img alt="{_esc(caption)}" src="data:image/png;base64,{png}">')

    for system in sorted(latest):
        w = latest[system]
        parts.append(f"<h2>Latest window — {_esc(system)} "
                     f"({_esc(w['window_start'][:10])})</h2>")
        parts.append("<table><tr><th>metric</th><th>value</th></tr>")
        for label, value in [
            ("events (n)", _fmt(w["n"])),
            ("positive-decision rate",
             _fmt_ci(w["decision_rate"], w.get("decision_rate_ci"))),
            ("accuracy", _fmt(w["accuracy"])),
            ("precision", _fmt(w["precision"])),
        ]:
            parts.append(f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>")
        parts.append("</table>")
        for attr in group_attrs:
            ginfo = (w.get("groups") or {}).get(attr) or {}
            parts.append(f"<h3>Group breakdown — {_esc(system)} / {_esc(attr)} "
                         f"(DIR {_fmt(ginfo.get('disparate_impact_ratio'))})</h3>")
            parts.append("<table><tr><th>value</th><th>n</th>"
                         "<th>decision rate (95% CI)</th>"
                         "<th>TPR (95% CI)</th><th>FPR (95% CI)</th></tr>")
            for value, info in (ginfo.get("values") or {}).items():
                if info.get("insufficient_evidence"):
                    row = (f"<tr><td>{_esc(value)}</td><td>{info['n']}</td>"
                           f"<td colspan=\"3\" class=\"insufficient\">"
                           f"insufficient evidence (n &lt; "
                           f"{metrics_doc.get('min_group_n', 1)})</td></tr>")
                else:
                    row = (
                        f"<tr><td>{_esc(value)}</td><td>{info['n']}</td>"
                        f"<td>{_esc(_fmt_ci(info['decision_rate'], info.get('decision_rate_ci')))}</td>"
                        f"<td>{_esc(_fmt_ci(info['tpr'], info.get('tpr_ci')))}</td>"
                        f"<td>{_esc(_fmt_ci(info['fpr'], info.get('fpr_ci')))}</td></tr>")
                parts.append(row)
            parts.append("</table>")

    parts.append("<h2>Incidents</h2>")
    if incidents:
        parts.append("<table><tr><th>id</th><th>rule</th><th>system</th><th>window</th>"
                     "<th>observed</th><th>severity</th><th>status</th></tr>")
        for inc in sorted(incidents, key=lambda i: i["window_start"]):
            sev = f"<span class=\"{_esc(inc['severity'])}\">{_esc(inc['severity'])}</span>"
            parts.append(
                f"<tr><td><code>{_esc(inc['id'])}</code></td><td>{_esc(inc['rule'])}</td>"
                f"<td>{_esc(inc['system'])}</td><td>{_esc(inc['window_start'][:10])}</td>"
                f"<td>{_fmt(inc['observed'])} {html.escape(str(inc['op']))} {_fmt(inc['threshold'])}</td>"
                f"<td>{sev}</td><td>{_esc(inc['status'])}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p>No incidents raised.</p>")

    parts.append("<footer>Generated by raimonitor. Metrics are signals for "
                 "human review, not verdicts — see docs/ARCHITECTURE.md."
                 + _esc(guards_note) + "</footer></body></html>")
    return "\n".join(parts)



def write_report(metrics_doc: dict, incidents: list[dict], path: str | Path,
                 title: str = "Responsible AI monitoring dashboard",
                 refresh_seconds: int | None = None) -> Path:
    """Write the dashboard HTML to ``path`` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = build_report(metrics_doc, incidents, title,
                               refresh_seconds=refresh_seconds)
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required for reports: pip install 'raimonitor[report]'"
        ) from exc
    path.write_text(content, encoding="utf-8")
    return path


def load_json(path: str | Path) -> dict | list:
    """Load a JSON document (metrics doc, rules list, ...)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
