"""Expõe `scripts/` para o pytest sem instalar o pacote como dependencia de runtime.

O `mttr.py` é stdlib puro e roda com `python3 scripts/mttr.py`. O teste só precisa
do caminho no `sys.path` para chamar `main(argv)` diretamente e ler o retorno —
sem subprocess, sem venv obrigatório, sem rede.
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
for subpasta in ("scripts",):
    caminho = str(RAIZ / subpasta)
    if caminho not in sys.path:
        sys.path.insert(0, caminho)

CSV_EXEMPLO = RAIZ / "examples" / "incidentes_exemplo.csv"
