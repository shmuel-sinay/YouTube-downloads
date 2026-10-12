"""Adds the YouTube client `visionos_old` to yt-dlp.

The original visionOS app (0.1, RealityDevice14,1) — as in the "YouTube
Premium" userscript: it gets direct stream links (no signature, no n, no
PO token) and, per that script, downloads the whole file where the newer
1.02 that yt-dlp sends can be cut off with 403. Used by agent.py as
`--extractor-args youtube:player_client=visionos_old`.
"""

import copy

from yt_dlp.extractor.youtube import _base

if 'visionos' in _base.INNERTUBE_CLIENTS and 'visionos_old' not in _base.INNERTUBE_CLIENTS:
    _cfg = copy.deepcopy(_base.INNERTUBE_CLIENTS['visionos'])
    _cfg['INNERTUBE_CONTEXT']['client'].update({
        'clientVersion': '0.1',
        'deviceModel': 'RealityDevice14,1',
        'osVersion': '1.3.21O771',
    })
    _base.INNERTUBE_CLIENTS['visionos_old'] = _cfg
    _base.build_innertube_clients()
