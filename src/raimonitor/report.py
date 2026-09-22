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

    def _fig(caption: str, series: dict[str, list], ylabel: str,
             ylim: tuple | None = None) -> None:
        fig, ax = plt.subplots(figsize=(9, 3.2))
        xs = list(range(len(windows)))
        labels = [w["window_start"][:10] for w in windows]
        for name, values in sorted(series.items()):
            ax.plot(xs, values, marker="o", label=name)
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
    for system in systems:
        sw = [w for w in windows if w["system"] == system]
        _fig(f"Volume — {system}",
             {system: [w["n"] for w in sw]}, "events")
        _fig(f"Positive-decision rate — {system}",
             {system: [w["decision_rate"] for w in sw]}, "rate", ylim=(0, 1))
        for attr in group_attrs:
            series = {
                system: [(w["groups"].get(attr) or {}).get("disparate_impact_ratio")
                         for w in sw]
            }
            _fig(f"Disparate impact ratio ({attr}) — {system}",
                 series, "DIR", ylim=(0, 1.05))
    return images


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def build_report(metrics_doc: dict, incidents: list[dict],
                 title: str = "Responsible AI monitoring dashboard") -> str:
    """Build the dashboard HTML from a metrics document and incident list."""
    windows = metrics_doc.get("windows", [])
    group_attrs = sorted({
        attr
        for w in windows
        for attr in (w.get("groups") or {})
    })
    images = _charts(windows, group_attrs) if windows else {}
    open_incidents = [i for i in incidents if i.get("status") == "open"]

    parts = [f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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

    for caption, png in sorted(images.items()):
        parts.append(f"<h2>{_esc(caption)}</h2>"
                     f'<img alt="{_esc(caption)}" src="data:image/png;base64,{png}">')

    if windows:
        latest = windows[-1]
        parts.append(f"<h2>Latest window — {_esc(latest['window_start'][:10])} "
                     f"({_esc(latest['system'])})</h2>")
        parts.append("<table><tr><th>metric</th><th>value</th></tr>")
        for label, value in [
            ("events (n)", latest["n"]),
            ("positive-decision rate", _fmt(latest["decision_rate"])),
            ("accuracy", _fmt(latest["accuracy"])),
            ("precision", _fmt(latest["precision"])),
        ]:
            parts.append(f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>")
        parts.append("</table>")
        for attr in group_attrs:
            ginfo = (latest.get("groups") or {}).get(attr) or {}
            parts.append(f"<h3>Group breakdown — {_esc(attr)} "
                         f"(DIR {_fmt(ginfo.get('disparate_impact_ratio'))})</h3>")
            parts.append("<table><tr><th>value</th><th>n</th><th>decision rate</th>"
                         "<th>TPR</th><th>FPR</th></tr>")
            for value, info in (ginfo.get("values") or {}).items():
                parts.append(
                    f"<tr><td>{_esc(value)}</td><td>{info['n']}</td>"
                    f"<td>{_fmt(info['decision_rate'])}</td>"
                    f"<td>{_fmt(info['tpr'])}</td><td>{_fmt(info['fpr'])}</td></tr>")
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
                 "human review, not verdicts — see docs/ARCHITECTURE.md.</footer>"
                 "</body></html>")
    return "\n".join(parts)


def write_report(metrics_doc: dict, incidents: list[dict], path: str | Path,
                 title: str = "Responsible AI monitoring dashboard") -> Path:
    """Write the dashboard HTML to ``path`` and return it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = build_report(metrics_doc, incidents, title)
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required for reports: pip install 'raimonitor[report]'"
        ) from exc
    path.write_text(content, encoding="utf-8")
    return path


def load_json(path: str | Path) -> dict | list:
    """Load a JSON document (metrics doc, rules list, ...)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
