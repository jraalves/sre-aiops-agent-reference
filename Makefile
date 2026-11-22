# Portões deste repositório. Tudo offline, tudo stdlib — o `check` tem que rodar
# num runner enxuto e no laptop de quem revisa, sem `pip install` prévio.
PY ?= python3

.PHONY: help check contratos metrica smoke testes limpeza

help:
	@printf '%s\n' \
	"check      contratos + varredura de padrão proibido + testes + métricas de exemplo" \
	"contratos  valida schemas/exemplos contra schemas/*.json" \
	"metrica    roda mttr.py sobre o CSV ficcional e imprime a tabela" \
	"gate       mttr.py em modo portão de CI (falha se violar a política)" \
	"testes     pytest -q" \
	"limpeza    remove __pycache__/.pytest_cache"

check: contratos testes metrica
	@echo "OK  contratos, testes e métricas passaram"

contratos:
	$(PY) scripts/valida_contratos.py

testes:
	$(PY) -m pytest -q

metrica:
	$(PY) scripts/mttr.py --csv examples/incidentes_exemplo.csv | tail -24

# Saída 3 = política violada. Útil em pipeline: MTTR/MTTD deixa de ser dashboard
# decorativo e vira critério de merge.
gate:
	$(PY) scripts/mttr.py --csv examples/incidentes_exemplo.csv \
	  --severidade S1,S2 --max-mttd-min 90 --format json > /dev/null

limpeza:
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -exec rm -rf {} +
	@echo "limpo"
