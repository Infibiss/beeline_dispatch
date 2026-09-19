"""Bundle client assets alongside a validated comparison result."""
from pathlib import Path
import shutil
from urllib.parse import quote
from .dispatcher import render_dispatcher


def write_dispatcher(payload,output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    assets=Path(__file__).with_name('static')/'vendor'
    shutil.copytree(assets,output/'assets',dirs_exist_ok=True)
    payload=dict(payload)
    if 'datasets' not in payload:
        siblings=sorted(output.parent.glob('*/comparison.json'))
        if len(siblings)>1:
            payload['datasets']=[{'region':p.parent.name,'url':'../'+quote(p.parent.name)+'/dispatcher.html'} for p in siblings]
    (output/'dispatcher.html').write_text(render_dispatcher(payload),encoding='utf-8')
