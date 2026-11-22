#!/usr/bin/env python3
"""mttr.py — MTTR mediano, recorrência e janela de detecção a partir de um CSV de incidentes.

Ferramenta de leitura, não de coleta: ela presume que **alguém** já decidiu o que
conta como incidente e registrou as timestamps. A definição é a parte que
importa (README, seção "Como MTTR e recorrência são calculados") — o número é
consequência da definição.

Regras deste script:
  * stdlib apenas (roda em qualquer Python 3.11+, offline, sem `pip install`);
  * nunca inventa dado: linha inválida é erro com saída != 0, não linha
    silenciosamente descartada;
  * todo número sai com denominador (`n=`) e com a fórmula impressa no rodapé;
  * amostra abaixo de `--min-amostra` aparece marcada como insuficiente em vez de
    sumir — esconder o `n` pequeno é como se fabrica conclusão.

Uso:
    python3 scripts/mttr.py --csv examples/incidentes_exemplo.csv
    python3 scripts/mttr.py --csv examples/incidentes_exemplo.csv --format json
    python3 scripts/mttr.py --csv incidents.csv --desde 2026-05-01 --severidade S1,S2
    python3 scripts/mttr.py --csv incidents.csv --max-mttd-min 15   # portão de CI

Códigos de saída:
    0  ok
    2  entrada inválida (CSV ausente, coluna faltando, timestamp impossível)
    3  política violada (só com --max-mttr-min / --max-mttd-min)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "1.0.0"

REQUIRED_COLUMNS = (
    "incidente_id",
    "aberto_em",
    "detectado_em",
    "severidade",
    "componente",
)
OPTIONAL_COLUMNS = (
    "mitigado_em",
    "diagnosticado_em",
    "resolvido_em",
    "status",
    "fingerprint",
    "causa_raiz",
    "origem_deteccao",
    "impacto_clientes",
    "aprovacao_humana",
)
SEVERIDADES_PADRAO = ("S1", "S2", "S3", "S4")
# Status sem restauração: entram na contagem e na detecção, ficam fora do MTTR.
STATUS_NAO_RESTAURADO = {"aberto", "em_andamento", "investigando"}
# Origens de detecção que significam "o cliente viu antes de nós".
ORIGENS_REATIVAS = {"cliente", "suporte", "usuario_final", "parceiro", "lojista"}
MAX_RELATORIO_ERROS = 20


class ErroEntrada(Exception):
    """Entrada inválida: o script se recusa a produzir número a partir dela."""


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #

@dataclass
class Incidente:
    linha: int
    id: str
    severidade: str
    componente: str
    aberto_em: datetime
    detectado_em: datetime
    mitigado_em: datetime | None = None
    diagnosticado_em: datetime | None = None
    resolvido_em: datetime | None = None
    status: str = "resolvido"
    fingerprint: str = ""
    causa_raiz: str = ""
    origem_deteccao: str = ""
    impacto_clientes: int | None = None
    aprovacao_humana: str = ""
    skew_clampado: bool = False

    @property
    def restaurado(self) -> bool:
        """Incidente em aberto não tem duração de restauração: é censura à direita.

        Tratar "ainda não acabou" como "muito lento" envia a mediana para cima no
        exato mês em que a fila cresce — o pior lugar para errar a leitura. Essas
        linhas ficam fora das durações de restauração e são contadas em separado.
        """
        return self.mitigado_em is not None and self.status not in STATUS_NAO_RESTAURADO

    # ---- durações derivadas (segundos; None quando não há o que medir) ------
    @property
    def mttd_s(self) -> float:
        """Janela de detecção: do impacto ao sinal que alguém (ou algo) percebeu."""
        return max(0.0, (self.detectado_em - self.aberto_em).total_seconds())

    @property
    def mttr_s(self) -> float | None:
        """Restauração desde a origem do impacto — a que dói no cliente."""
        if not self.restaurado:
            return None
        return (self.mitigado_em - self.aberto_em).total_seconds()

    @property
    def mttr_pos_deteccao_s(self) -> float | None:
        """Restauração desde a detecção — a que o time de plantão controla."""
        if not self.restaurado:
            return None
        return (self.mitigado_em - self.detectado_em).total_seconds()

    @property
    def ttf_diag_s(self) -> float | None:
        """Tempo até o primeiro diagnóstico — o que um agente de diagnóstico move."""
        if self.diagnosticado_em is None or not self.restaurado:
            return None
        return (self.diagnosticado_em - self.detectado_em).total_seconds()

    @property
    def mttr_fechamento_s(self) -> float | None:
        if self.resolvido_em is None or not self.restaurado:
            return None
        return (self.resolvido_em - self.aberto_em).total_seconds()

    @property
    def chave_recorrencia(self) -> str:
        """Identidade do "mesmo problema", com cadeia de fallback explícita.

        1. `fingerprint` — se o sistema de alertas já calcula um, use-o;
        2. `componente + causa_raiz` — causa declarada no post-mortem;
        3. `componente` — último caso: mede "o mesmo lugar pegando fogo", não o
           mesmo defeito. O relatório imprime qual cadeia foi usada, porque as
           três dão números diferentes e a terceira é sempre a maior.
        """
        fp = self.fingerprint.strip().lower()
        if fp:
            return f"fp:{fp}"
        causa = self.causa_raiz.strip().lower()
        comp = self.componente.strip().lower() or "(sem componente)"
        if causa:
            return f"causa:{comp}|{causa}"
        return f"comp:{comp}"

    @property
    def cadeia_recorrencia(self) -> str:
        return self.chave_recorrencia.split(":", 1)[0]


# --------------------------------------------------------------------------- #
# Primitivas estatísticas (convenções declaradas — mudam o número)
# --------------------------------------------------------------------------- #

def _round(valor: float | None, nd: int = 1) -> float | None:
    return None if valor is None else round(valor, nd)


def percentil(valores_ordenados: list[float], q: float) -> float:
    """Percentil por interpolação linear (mesma convenção de `numpy.percentile`
    com `method='linear'`).

    Documentado porque p95 de 4 amostras muda de valor conforme o método
    escolhido — e "p95 do S2" é exatamente o tipo de número que vira prioridade
    de backlog.
    """
    if not valores_ordenados:
        raise ValueError("percentil exige ao menos uma amostra")
    if len(valores_ordenados) == 1:
        return float(valores_ordenados[0])
    idx = (len(valores_ordenados) - 1) * q
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return float(valores_ordenados[int(idx)])
    return float(valores_ordenados[lo] + (valores_ordenados[hi] - valores_ordenados[lo]) * (idx - lo))


@dataclass
class Estatisticas:
    n: int = 0
    mediana: float | None = None
    media: float | None = None
    p95: float | None = None
    minimo: float | None = None
    maximo: float | None = None

    @classmethod
    def de(cls, valores: list[float | None]) -> "Estatisticas":
        limpos = sorted(v for v in valores if v is not None)
        if not limpos:
            return cls()
        return cls(
            n=len(limpos),
            mediana=float(statistics.median(limpos)),
            media=float(statistics.fmean(limpos)),
            p95=percentil(limpos, 0.95),
            minimo=float(limpos[0]),
            maximo=float(limpos[-1]),
        )

    def dict(self, min_amostra: int) -> dict:
        return {
            "n": self.n,
            "amostra_suficiente": self.n >= min_amostra,
            "mediana_s": _round(self.mediana),
            "media_s": _round(self.media),
            "p95_s": _round(self.p95),
            "min_s": _round(self.minimo),
            "max_s": _round(self.maximo),
        }


# --------------------------------------------------------------------------- #
# Leitura e validação
# --------------------------------------------------------------------------- #

def parse_timestamp(valor: str, campo: str, linha: int, id_hint: str = "?") -> datetime | None:
    """ISO-8601 com ou sem fuso; aceita `Z`. Não aceita texto livre nem data ambígua."""
    texto = (valor or "").strip()
    if not texto:
        return None
    normalizado = texto[:-1] + "+00:00" if texto.endswith(("Z", "z")) else texto
    try:
        return datetime.fromisoformat(normalizado)
    except ValueError as exc:
        raise ErroEntrada(f"linha {linha} ({id_hint}): coluna `{campo}` não é ISO-8601 ({texto!r}): {exc}") from exc


def parse_data_arg(valor: str, nome_flag: str) -> datetime:
    ts = parse_timestamp(valor, nome_flag, 0, f"--{nome_flag}")
    if ts is None:
        raise ErroEntrada(f"--{nome_flag} informado sem valor")
    return ts


def _inteiro(valor: str, campo: str, linha: int, id_hint: str) -> int | None:
    texto = (valor or "").strip()
    if not texto:
        return None
    try:
        return int(float(texto))
    except ValueError as exc:
        raise ErroEntrada(f"linha {linha} ({id_hint}): coluna `{campo}` não é número ({texto!r})") from exc


def ler_incidentes(caminho: str, severidades_validas: tuple[str, ...], tolerancia_skew_s: float,
                   avisos: list[str]) -> tuple[list[Incidente], dict]:
    if not os.path.isfile(caminho):
        raise ErroEntrada(f"arquivo não encontrado: {caminho}")
    if os.path.getsize(caminho) == 0:
        raise ErroEntrada(f"arquivo vazio: {caminho}")

    with open(caminho, newline="", encoding="utf-8-sig") as fh:
        leitor = csv.DictReader(fh)
        cabecalhos = [c for c in (leitor.fieldnames or []) if c]
        faltando = [c for c in REQUIRED_COLUMNS if c not in cabecalhos]
        if faltando:
            raise ErroEntrada(
                "colunas obrigatórias ausentes no CSV: " + ", ".join(faltando)
                + f" | cabeçalho lido: {', '.join(cabecalhos) or '(vazio)'}"
            )
        desconhecidas = [
            c for c in cabecalhos
            if c not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS and not c.startswith("_")
        ]
        if desconhecidas:
            avisos.append("colunas fora do contrato, ignoradas no cálculo: " + ", ".join(desconhecidas))

        incidentes: list[Incidente] = []
        vistos: dict[str, int] = {}
        erros: list[str] = []
        censurados = 0
        com_skew = 0
        n_linhas = 0

        for linha, row in enumerate(leitor, start=2):
            n_linhas += 1
            ident = (row.get("incidente_id") or "").strip()
            if not ident:
                erros.append(f"linha {linha}: `incidente_id` vazio — incidente sem id não entra na conta")
                continue
            if ident in vistos:
                erros.append(f"linha {linha}: `incidente_id` duplicado ({ident}, já visto na linha {vistos[ident]})")
                continue
            vistos[ident] = linha

            sev = (row.get("severidade") or "").strip().upper()
            if sev not in severidades_validas:
                erros.append(f"linha {linha} ({ident}): severidade {sev!r} fora de {'/'.join(severidades_validas)}")
                continue

            componente = (row.get("componente") or "").strip()
            if not componente:
                erros.append(f"linha {linha} ({ident}): `componente` vazio — recorrência precisa de componente")
                continue

            try:
                aberto = parse_timestamp(row.get("aberto_em", ""), "aberto_em", linha, ident)
                detectado = parse_timestamp(row.get("detectado_em", ""), "detectado_em", linha, ident)
                mitigado = parse_timestamp(row.get("mitigado_em", ""), "mitigado_em", linha, ident)
                diagnostico = parse_timestamp(row.get("diagnosticado_em", ""), "diagnosticado_em", linha, ident)
                resolvido = parse_timestamp(row.get("resolvido_em", ""), "resolvido_em", linha, ident)
            except ErroEntrada as exc:
                # erro de formato é acumulado junto com o resto: quem está limpando
                # uma planilha de incidente quer a lista inteira de problemas, não a
                # primeira, ou passa a tarde rodando o script uma vez por linha ruim.
                erros.append(str(exc))
                continue

            if aberto is None:
                erros.append(f"linha {linha} ({ident}): `aberto_em` vazio — sem origem não há janela de nada")
                continue
            if detectado is None:
                erros.append(f"linha {linha} ({ident}): `detectado_em` vazio — se ninguém sabe quando foi detectado, "
                             f"registre o status como aberto e deixe explícito, não em branco")
                continue

            status = (row.get("status") or "resolvido").strip().lower()
            if mitigado is None and status not in STATUS_NAO_RESTAURADO:
                erros.append(f"linha {linha} ({ident}): `mitigado_em` vazio com status {status!r} — "
                             f"use um dos status em aberto {sorted(STATUS_NAO_RESTAURADO)} ou informe a restauração")
                continue

            # Detecção antes da origem do impacto é fisicamente impossível, mas é o
            # achado mais comum entre camadas com relógios independentes (Linux e
            # legado). Dentro da tolerância declarada, trata-se como skew: a janela
            # vira 0 e a linha é contada. Número negativo polui a mediana; descartar
            # a linha esconde o problema de relógio que é, ele mesmo, o achado.
            lidos = [t for t in (aberto, detectado, mitigado, diagnostico, resolvido) if t is not None]
            if len({t.tzinfo is None for t in lidos}) > 1:
                erros.append(
                    f"linha {linha} ({ident}): mistura timestamp com fuso e sem fuso — subtração entre os "
                    f"dois é indefinida; padronize a exportação em UTC com offset"
                )
                continue

            delta_deteccao = (detectado - aberto).total_seconds()
            skew_clampado = False
            if delta_deteccao < 0:
                if abs(delta_deteccao) <= tolerancia_skew_s:
                    skew_clampado = True
                    com_skew += 1
                else:
                    erros.append(
                        f"linha {linha} ({ident}): detectado_em está {-delta_deteccao:.0f}s antes de aberto_em "
                        f"(tolerância de skew = {tolerancia_skew_s:.0f}s; aumente --tolerancia-skew-segundos "
                        f"se as camadas têm relógios independentes)"
                    )
                    continue
            if mitigado is not None and (mitigado - aberto).total_seconds() < -tolerancia_skew_s:
                erros.append(f"linha {linha} ({ident}): mitigado_em antes de aberto_em — restauração antes do impacto")
                continue
            if diagnostico is not None and detectado is not None:
                if diagnostico < detectado:
                    avisos.append(f"linha {linha} ({ident}): diagnosticado_em antes de detectado_em")
                elif mitigado is not None and diagnostico > mitigado:
                    avisos.append(f"linha {linha} ({ident}): diagnosticado_em depois de mitigado_em (diagnóstico a posteriori)")
            if resolvido is not None and mitigado is not None and resolvido < mitigado:
                avisos.append(f"linha {linha} ({ident}): resolvido_em antes de mitigado_em")

            if status in STATUS_NAO_RESTAURADO:
                censurados += 1

            try:
                incidentes.append(Incidente(
                    linha=linha,
                    id=ident,
                    severidade=sev,
                    componente=componente,
                    aberto_em=aberto,
                    detectado_em=detectado,
                    mitigado_em=mitigado,
                    diagnosticado_em=diagnostico,
                    resolvido_em=resolvido,
                    status=status,
                    fingerprint=(row.get("fingerprint") or "").strip(),
                    causa_raiz=(row.get("causa_raiz") or "").strip(),
                    origem_deteccao=(row.get("origem_deteccao") or "").strip().lower(),
                    impacto_clientes=_inteiro(row.get("impacto_clientes", ""), "impacto_clientes", linha, ident),
                    aprovacao_humana=(row.get("aprovacao_humana") or "").strip().lower(),
                    skew_clampado=skew_clampado,
                ))
            except TypeError as exc:  # último rede: comparação impossível entre datetimes
                erros.append(f"linha {linha} ({ident}): {exc} — revista os fusos da exportação")
            except ErroEntrada as exc:
                erros.append(str(exc))

        # consistência de fuso no arquivo inteiro: a recorrência subtrai aberto_em
        # entre linhas de um mesmo componente, então misturar UTC com horário local
        # não erra uma conta — erra a ordem cronológica dos episódios.
        if any(i.aberto_em.tzinfo is not None for i in incidentes) and \
           any(i.aberto_em.tzinfo is None for i in incidentes):
            erros.append("o CSV mistura linhas com fuso e linhas sem fuso em `aberto_em` — "
                         "exporte tudo em UTC (ex.: 2026-05-01T10:00:00+00:00) e rode de novo")

        if erros:
            bloco = "\n".join(f"  - {e}" for e in erros[:MAX_RELATORIO_ERROS])
            mais = f"\n  - ... e mais {len(erros) - MAX_RELATORIO_ERROS} erros" if len(erros) > MAX_RELATORIO_ERROS else ""
            raise ErroEntrada(f"{len(erros)} linha(s) inválida(s):\n{bloco}{mais}")

    if not incidentes:
        raise ErroEntrada("CSV cabeçalho-ok, mas sem nenhuma linha de incidente")

    metadados = {
        "linhas_lidas": n_linhas,
        "incidentes_validos": len(incidentes),
        "censurados_sem_restauracao": censurados,
        "linhas_com_skew_zerado": com_skew,
        "colunas_desconhecidas_ignoradas": desconhecidas,
    }
    return incidentes, metadados


def filtrar(incidentes: list[Incidente], args: argparse.Namespace) -> list[Incidente]:
    resultado = incidentes
    if args.severidade:
        alvos = {s.upper() for s in args.severidade}
        resultado = [i for i in resultado if i.severidade in alvos]
    if args.componente:
        alvos = {c.lower() for c in args.componente}
        resultado = [i for i in resultado if i.componente.lower() in alvos]
    if args.desde:
        limite = parse_data_arg(args.desde, "desde")
        resultado = [i for i in resultado if _comparavel(i.aberto_em, limite)]
    if args.ate:
        limite = parse_data_arg(args.ate, "ate")
        resultado = [i for i in resultado if _comparavel(i.aberto_em, limite, antes=True)]
    if not resultado:
        raise ErroEntrada("nenhum incidente sobreviveu aos filtros — revise --severidade/--componente/--desde/--ate")
    return resultado


def _comparavel(ts: datetime, limite: datetime, antes: bool = False) -> bool:
    a, b = ts, limite
    if (a.tzinfo is None) != (b.tzinfo is None):
        if a.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=timezone.utc)
    return a <= b if antes else a >= b


# --------------------------------------------------------------------------- #
# Agregações
# --------------------------------------------------------------------------- #

def por_severidade(incidentes: list[Incidente], ordem: tuple[str, ...]) -> list[tuple[str, list[Incidente]]]:
    grupos: dict[str, list[Incidente]] = {}
    for i in incidentes:
        grupos.setdefault(i.severidade, []).append(i)
    chaves = [s for s in ordem if s in grupos] + sorted(s for s in grupos if s not in ordem)
    return [(chave, grupos[chave]) for chave in chaves]


def calcular_recorrencia(incidentes: list[Incidente], janela_dias: float) -> list[dict]:
    """Recorrência por componente.

    Um incidente B é **recorrente** se existe um incidente A, no mesmo
    componente, com a mesma chave de identidade do problema, cuja origem está a
    no máximo `janela_dias` antes da origem de B. A janela faz parte do número:
    recorrência sem janela declarada é adjetivo, não métrica.
    """
    por_componente: dict[str, list[Incidente]] = {}
    for i in incidentes:
        por_componente.setdefault(i.componente, []).append(i)

    janela = timedelta(days=janela_dias)
    linhas: list[dict] = []
    for componente, grupo in por_componente.items():
        grupo = sorted(grupo, key=lambda i: (i.aberto_em, i.id))
        por_chave: dict[str, list[Incidente]] = {}
        for i in grupo:
            por_chave.setdefault(i.chave_recorrencia, []).append(i)

        recorrentes = 0
        intervalos: list[float] = []
        mais_repetida, mais_repetida_n = "", 0
        for chave, itens in sorted(por_chave.items()):
            if len(itens) > mais_repetida_n:
                mais_repetida, mais_repetida_n = chave, len(itens)
            anterior = None
            for item in itens:
                if anterior is not None and (item.aberto_em - anterior.aberto_em) <= janela:
                    recorrentes += 1
                    intervalos.append((item.aberto_em - anterior.aberto_em).total_seconds() / 86400.0)
                anterior = item

        total = len(grupo)
        linhas.append({
            "componente": componente,
            "incidentes": total,
            "problemas_distintos": len(por_chave),
            "recorrentes": recorrentes,
            "taxa_recorrencia": (recorrentes / total) if total else None,
            "unicidade": (len(por_chave) / total) if total else None,
            "gap_mediano_repeticao_dias": float(statistics.median(intervalos)) if intervalos else None,
            "problema_mais_repetido": mais_repetida,
            "repeticoes": mais_repetida_n,
            "cadeias_identidade": sorted({i.cadeia_recorrencia for i in grupo}),
        })
    linhas.sort(key=lambda l: (-l["recorrentes"], -l["incidentes"], l["componente"]))
    return linhas


DEFINICOES = {
    "mttd": "MTTD = detectado_em - aberto_em (janela de detecção)",
    "mttr": "MTTR (restauração) = mitigado_em - aberto_em",
    "mttr_pos_deteccao": "MTTR pós-detecção = mitigado_em - detectado_em",
    "ttfd": "TTFD = diagnosticado_em - detectado_em (tempo até o primeiro diagnóstico)",
    "fechamento": "tempo até fechamento = resolvido_em - aberto_em (inclui trabalho administrativo)",
    "recorrencia": "incidente com a mesma chave de problema a menos de N dias do anterior, no mesmo componente",
    "centro_de_massa": "mediana, não média: com n pequeno e cauda longa, a média é narrativa, não medida",
    "percentil": "p95 por interpolação linear (numpy-compatible)",
    "censura": "linha sem mitigado_em ou com status em aberto fica fora do MTTR e é contada em separado",
    "skew": "detectado_em < aberto_em dentro da tolerância vira MTTD=0 e entra em 'skew zerado'",
}


def resumo(incidentes: list[Incidente], metadados: dict, args: argparse.Namespace) -> dict:
    grupos = por_severidade(incidentes, args.severidades)
    fim_periodo = max((i.mitigado_em or i.resolvido_em or i.detectado_em) for i in incidentes)

    alertas = [i for i in incidentes if i.origem_deteccao and i.origem_deteccao not in ORIGENS_REATIVAS]
    reativas = [i for i in incidentes if i.origem_deteccao in ORIGENS_REATIVAS]
    sem_origem = [i for i in incidentes if not i.origem_deteccao]

    return {
        "ferramenta": "mttr.py",
        "versao": SCRIPT_VERSION,
        "csv": os.path.abspath(args.csv),
        "janela_recorrencia_dias": args.janela_recorrencia_dias,
        "min_amostra": args.min_amostra,
        "tolerancia_skew_s": args.tolerancia_skew_segundos,
        "filtros_aplicados": {
            "severidade": args.severidade or None,
            "componente": args.componente or None,
            "desde": args.desde or None,
            "ate": args.ate or None,
        },
        "metadados_entrada": metadados,
        "escopo": {
            "incidentes_analisados": len(incidentes),
            "incidentes_restaurados": sum(1 for i in incidentes if i.restaurado),
            "periodo": {"inicio": min(i.aberto_em for i in incidentes).isoformat(), "fim": fim_periodo.isoformat()},
        },
        "geral": {
            "mttr_s": Estatisticas.de([i.mttr_s for i in incidentes]).dict(args.min_amostra),
            "mttr_pos_deteccao_s": Estatisticas.de([i.mttr_pos_deteccao_s for i in incidentes]).dict(args.min_amostra),
            "mttd_s": Estatisticas.de([i.mttd_s for i in incidentes]).dict(args.min_amostra),
            "tempo_ate_diagnostico_s": Estatisticas.de([i.ttf_diag_s for i in incidentes]).dict(args.min_amostra),
            "mttr_fechamento_s": Estatisticas.de([i.mttr_fechamento_s for i in incidentes]).dict(args.min_amostra),
        },
        "por_severidade": [
            {
                "severidade": sev,
                "n": len(grupo),
                "mttr_s": Estatisticas.de([i.mttr_s for i in grupo]).dict(args.min_amostra),
                "mttr_pos_deteccao_s": Estatisticas.de([i.mttr_pos_deteccao_s for i in grupo]).dict(args.min_amostra),
                "mttd_s": Estatisticas.de([i.mttd_s for i in grupo]).dict(args.min_amostra),
                "tempo_ate_diagnostico_s": Estatisticas.de([i.ttf_diag_s for i in grupo]).dict(args.min_amostra),
            }
            for sev, grupo in grupos
        ],
        "deteccao": {
            "por_alerta_ou_agente": {"n": len(alertas), "mttd_s": Estatisticas.de([i.mttd_s for i in alertas]).dict(args.min_amostra)},
            "por_cliente_ou_suporte": {"n": len(reativas), "mttd_s": Estatisticas.de([i.mttd_s for i in reativas]).dict(args.min_amostra)},
            "sem_origem_declarada": len(sem_origem),
            "cobertura_propria_pct": (100.0 * len(alertas) / len(incidentes)) if incidentes else None,
            "vocabulario_origens": {
                "reativas": sorted(ORIGENS_REATIVAS),
                "nao_reativas_observadas": sorted({i.origem_deteccao for i in alertas}),
            },
        },
        "recorrencia_por_componente": calcular_recorrencia(incidentes, args.janela_recorrencia_dias),
        "definisoes": DEFINICOES,
        "aviso_dados": "Rode sobre dado real somente com as definições desta seção acordadas com quem abre e fecha incidente.",
    }


# --------------------------------------------------------------------------- #
# Renderização
# --------------------------------------------------------------------------- #

def fmt_dur(segundos: float | None) -> str:
    if segundos is None:
        return "—"
    sinal = "-" if segundos < 0 else ""
    s = abs(segundos)
    if s < 90:
        return f"{sinal}{s:.0f}s"
    minutos = s / 60.0
    if minutos < 90:
        return f"{sinal}{minutos:.1f}min"
    horas = minutos / 60.0
    if horas < 48:
        hh = int(horas)
        mm = int(round((horas - hh) * 60))
        if mm == 60:
            hh, mm = hh + 1, 0
        return f"{sinal}{hh}h{mm:02d}"
    return f"{sinal}{horas / 24.0:.1f}d"


def tabela(cabecalhos: list[str], linhas: list[list[str]], alinhamentos: list[str] | None = None) -> str:
    alinhamentos = alinhamentos or ["l"] * len(cabecalhos)
    larguras = [len(str(c)) for c in cabecalhos]
    for linha in linhas:
        for idx, celula in enumerate(linha):
            larguras[idx] = max(larguras[idx], len(str(celula)))
    separador = "+" + "+".join("-" * (w + 2) for w in larguras) + "+"

    def formatar(celulas: list[str]) -> str:
        partes = []
        for celula, largura, alinhamento in zip(celulas, larguras, alinhamentos):
            texto = str(celula)
            partes.append(f" {texto.rjust(largura) if alinhamento == 'r' else texto.ljust(largura)} ")
        return "|" + "|".join(partes) + "|"

    saida = [separador, formatar(cabecalhos), separador]
    if not linhas:
        saida.append(formatar(["(sem linhas)"] + [""] * (len(cabecalhos) - 1)))
    for linha in linhas:
        saida.append(formatar(linha))
    saida.append(separador)
    return "\n".join(saida)


def _bloco(titulo: str, linhas: list[dict], chave: str, args: argparse.Namespace) -> list[str]:
    cab = ["agrupamento", "n", "mediana", "média", "p95", "mín", "máx", "amostra"]
    rows = []
    for item in linhas:
        est: Estatisticas = Estatisticas(**{k: v for k, v in {
            "n": item[chave]["n"],
            "mediana": item[chave]["mediana_s"],
            "media": item[chave]["media_s"],
            "p95": item[chave]["p95_s"],
            "minimo": item[chave]["min_s"],
            "maximo": item[chave]["max_s"],
        }.items()})
        rotulo = item.get("severidade") or "todos"
        rows.append([
            rotulo, str(est.n), fmt_dur(est.mediana), fmt_dur(est.media), fmt_dur(est.p95),
            fmt_dur(est.minimo), fmt_dur(est.maximo),
            "ok" if est.n >= args.min_amostra else f"n<{args.min_amostra}",
        ])
    return ([titulo] if titulo else []) + [tabela(cab, rows, ["l", "r", "r", "r", "r", "r", "r", "l"])]


def render_texto(corpo: dict, args: argparse.Namespace) -> str:
    esc, met = corpo["escopo"], corpo["metadados_entrada"]
    out: list[str] = []
    push = out.append

    push("=" * 78)
    push(f" MTTR, recorrência e janela de detecção  —  mttr.py v{corpo['versao']}")
    push("=" * 78)
    push(f" entrada ............ {corpo['csv']}")
    push(f" linhas ............. lidas {met['linhas_lidas']} · válidas {met['incidentes_validos']}"
         f" · sem restauração {met['censurados_sem_restauracao']} · skew zerado {met['linhas_com_skew_zerado']}")
    push(f" período ............ {esc['periodo']['inicio']}  →  {esc['periodo']['fim']}")
    push(f" janelas ............ recorrência {corpo['janela_recorrencia_dias']}d · amostra mínima "
         f"{corpo['min_amostra']} · tolerância de skew {corpo['tolerancia_skew_s']:.0f}s")
    filtros = {k: v for k, v in corpo["filtros_aplicados"].items() if v}
    if filtros:
        push(f" filtros ............ {filtros}")
    push("")

    grupos = corpo["por_severidade"]
    push("1) MTTR por severidade")
    out.extend(_bloco("   a) desde a origem do impacto (mitigado_em - aberto_em) — a dor do cliente:", grupos, "mttr_s", args))
    out.extend(_bloco("   b) desde a detecção (mitigado_em - detectado_em) — o que o plantão controla:", grupos, "mttr_pos_deteccao_s", args))
    push("   Diferença entre (a) e (b) = o que a detecção custa. Se (a) é alto e (b) é")
    push("   baixo, o problema não é competência de resposta: é telemetria.")
    push("")

    push("2) Janela de detecção (MTTD = detectado_em - aberto_em)")
    out.extend(_bloco("   ", grupos, "mttd_s", args))
    det = corpo["deteccao"]
    total = esc["incidentes_analisados"]
    push("   Quem viu primeiro?")
    push(tabela(
        ["origem da detecção", "n", "MTTD mediano", "% dos incidentes"],
        [
            ["alerta / monitoramento / agente", str(det["por_alerta_ou_agente"]["n"]),
             fmt_dur(det["por_alerta_ou_agente"]["mttd_s"]["mediana_s"]), f"{det['cobertura_propria_pct']:.1f}%"],
            ["cliente / suporte / lojista / parceiro", str(det["por_cliente_ou_suporte"]["n"]),
             fmt_dur(det["por_cliente_ou_suporte"]["mttd_s"]["mediana_s"]),
             f"{100.0 * det['por_cliente_ou_suporte']['n'] / total:.1f}%"],
            ["origem não declarada (dado fraco)", str(det["sem_origem_declarada"]), "—",
             f"{100.0 * det['sem_origem_declarada'] / total:.1f}%"],
        ],
        ["l", "r", "r", "r"],
    ))
    push(f"   vocabulário reativo tratado: {', '.join(det['vocabulario_origens']['reativas'])}")
    push("")

    com_diag = [g for g in grupos if g["tempo_ate_diagnostico_s"]["n"] > 0]
    if com_diag:
        push("3) Tempo até o primeiro diagnóstico (TTFD = diagnosticado_em - detectado_em)")
        push("   É o indicador de um agente de diagnóstico: não mede resolução, mede a")
        push("   distância entre 'sabemos que dói' e 'sabemos onde dói'.")
        out.extend(_bloco("   ", com_diag, "tempo_ate_diagnostico_s", args))
    else:
        push("3) Tempo até o primeiro diagnóstico: coluna `diagnosticado_em` ausente — nada a medir.")
    push("")

    push(f"4) Recorrência por componente (janela de {corpo['janela_recorrencia_dias']} dias)")
    rows = []
    for item in corpo["recorrencia_por_componente"]:
        taxa = item["taxa_recorrencia"]
        rows.append([
            item["componente"], str(item["incidentes"]), str(item["problemas_distintos"]),
            str(item["recorrentes"]), f"{taxa * 100:.1f}%" if taxa is not None else "—",
            f"{item['gap_mediano_repeticao_dias']:.1f}d" if item["gap_mediano_repeticao_dias"] is not None else "—",
            f"{item['problema_mais_repetido']} x{item['repeticoes']}",
            "+".join(item["cadeias_identidade"]),
        ])
    push(tabela(
        ["componente", "incid.", "problemas distintos", "recorrentes", "taxa = rec/incid.",
         "gap mediano", "problema mais repetido", "chave"],
        rows, ["l", "r", "r", "r", "r", "r", "l", "l"],
    ))
    push("   leitura: taxa alta com poucos problemas distintos = um defeito só voltando")
    push("   (é trabalho de causa raiz, não de alerta). Taxa alta com muitos problemas")
    push("   distintos = componente estruturalmente frágil, e abrir ticket de")
    push("   recorrência não muda nada ali.")
    push("")

    g = corpo["geral"]
    push("5) Consolidado do período (somente incidentes restaurados, exceto MTTD)")
    push(tabela(
        ["indicador", "n", "mediana", "média", "p95"],
        [
            ["MTTR (desde a origem)", str(g["mttr_s"]["n"]), fmt_dur(g["mttr_s"]["mediana_s"]), fmt_dur(g["mttr_s"]["media_s"]), fmt_dur(g["mttr_s"]["p95_s"])],
            ["MTTR (desde a detecção)", str(g["mttr_pos_deteccao_s"]["n"]), fmt_dur(g["mttr_pos_deteccao_s"]["mediana_s"]), fmt_dur(g["mttr_pos_deteccao_s"]["media_s"]), fmt_dur(g["mttr_pos_deteccao_s"]["p95_s"])],
            ["MTTD (todos os incidentes)", str(g["mttd_s"]["n"]), fmt_dur(g["mttd_s"]["mediana_s"]), fmt_dur(g["mttd_s"]["media_s"]), fmt_dur(g["mttd_s"]["p95_s"])],
            ["Tempo até 1º diagnóstico", str(g["tempo_ate_diagnostico_s"]["n"]), fmt_dur(g["tempo_ate_diagnostico_s"]["mediana_s"]), fmt_dur(g["tempo_ate_diagnostico_s"]["media_s"]), fmt_dur(g["tempo_ate_diagnostico_s"]["p95_s"])],
            ["Tempo até fechamento", str(g["mttr_fechamento_s"]["n"]), fmt_dur(g["mttr_fechamento_s"]["mediana_s"]), fmt_dur(g["mttr_fechamento_s"]["media_s"]), fmt_dur(g["mttr_fechamento_s"]["p95_s"])],
        ],
        ["l", "r", "r", "r", "r"],
    ))
    push("")
    push("-" * 78)
    push(" COMO FOI MEDIDO (via junto do número, sempre)")
    for chave, texto in DEFINICOES.items():
        push(f"   [{chave}] {texto}")
    push(f"   [agregação] mediana por grupo; grupo com n<{args.min_amostra} marcado 'n<{args.min_amostra}'.")
    push("   [denominador] taxa de recorrência = recorrentes / incidentes do componente;")
    push("                   cobertura própria = incidentes com origem não-reativa / todos.")
    if met["colunas_desconhecidas_ignoradas"]:
        push(f"   [atenção] colunas fora do contrato ignoradas: {', '.join(met['colunas_desconhecidas_ignoradas'])}")
    push("-" * 78)
    return "\n".join(out)


def render_csv(corpo: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["bloco", "agrupamento", "n", "mediana_s", "media_s", "p95_s", "extra"])
    nomes = {
        "mttr_desde_origem": "mttr_s",
        "mttr_desde_deteccao": "mttr_pos_deteccao_s",
        "mttd": "mttd_s",
        "tempo_ate_diagnostico": "tempo_ate_diagnostico_s",
        "tempo_ate_fechamento": "mttr_fechamento_s",
    }
    for rotulo, chave in nomes.items():
        est = corpo["geral"][chave]
        writer.writerow(["geral", rotulo, est["n"], est["mediana_s"], est["media_s"], est["p95_s"], ""])
    for item in corpo["por_severidade"]:
        for rotulo, chave in nomes.items():
            est = item.get(chave)
            if not est:
                continue
            writer.writerow([f"por_severidade:{rotulo}", item["severidade"], est["n"], est["mediana_s"],
                             est["media_s"], est["p95_s"], f"n_grupo={item['n']}"])
    for item in corpo["recorrencia_por_componente"]:
        writer.writerow(["recorrencia_por_componente", item["componente"], item["incidentes"],
                         item["taxa_recorrencia"], item["problemas_distintos"], item["recorrentes"],
                         f"gap_mediano_dias={_round(item['gap_mediano_repeticao_dias'], 2)};"
                         f"chave={''.join(item['cadeias_identidade'])}"])
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mttr.py",
        description="Calcula MTTR mediano por severidade, recorrência por componente e "
                    "janela de detecção a partir de um CSV de incidentes (stdlib apenas).",
        epilog="Saída: 0 ok · 2 entrada inválida · 3 política violada.\n"
               "O CSV de exemplo (examples/incidentes_exemplo.csv) é 100% fictício.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv", required=True, metavar="ARQUIVO",
                        help="CSV de incidentes; colunas obrigatórias: " + ", ".join(REQUIRED_COLUMNS))
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table",
                        help="formato da saída (padrão: table)")
    parser.add_argument("--output", metavar="ARQUIVO", help="além do stdout, escreve a saída em arquivo")
    parser.add_argument("--severidade", action="append", default=[], metavar="S1[,S2]",
                        help="filtra por severidade (repetível ou separado por vírgula)")
    parser.add_argument("--componente", action="append", default=[], metavar="NOME",
                        help="filtra por componente (repetível ou separado por vírgula)")
    parser.add_argument("--desde", metavar="DATA", help="só incidentes com aberto_em >= DATA (ISO-8601)")
    parser.add_argument("--ate", metavar="DATA", help="só incidentes com aberto_em <= DATA (ISO-8601)")
    parser.add_argument("--severidades", default=",".join(SEVERIDADES_PADRAO), metavar="LISTA",
                        help="vocabulário válido de severidade (padrão: S1,S2,S3,S4)")
    parser.add_argument("--janela-recorrencia-dias", type=float, default=30.0, metavar="DIAS",
                        help="janela de recorrência (padrão: 30 dias)")
    parser.add_argument("--min-amostra", type=int, default=3, metavar="N",
                        help="abaixo disto o grupo é marcado como amostra insuficiente (padrão: 3)")
    parser.add_argument("--tolerancia-skew-segundos", type=float, default=0.0, metavar="SEG",
                        help="skew de relógio tolerado entre camadas; dentro dele a janela de "
                             "detecção vira 0 e a linha é contada em 'skew zerado' (padrão: 0)")
    parser.add_argument("--max-mttr-min", type=float, metavar="MIN",
                        help="política: código de saída 3 se a mediana global de MTTR passar de MIN")
    parser.add_argument("--max-mttd-min", type=float, metavar="MIN",
                        help="política: código de saída 3 se a mediana global de MTTD passar de MIN")
    parser.add_argument("--version", action="version", version=f"mttr.py {SCRIPT_VERSION}")
    return parser


def normalizar_lista(valores: list[str]) -> list[str]:
    saida: list[str] = []
    for valor in valores:
        saida.extend(p for p in (x.strip() for x in valor.split(",")) if p)
    return saida


def main(argv: list[str] | None = None) -> int:
    parser = construir_parser()
    args = parser.parse_args(argv)
    args.severidade = normalizar_lista(args.severidade)
    args.componente = normalizar_lista(args.componente)
    args.severidades = tuple(s.strip().upper() for s in str(args.severidades).split(",") if s.strip())

    avisos: list[str] = []
    try:
        incidentes, metadados = ler_incidentes(args.csv, args.severidades, args.tolerancia_skew_segundos, avisos)
        incidentes = filtrar(incidentes, args)
        corpo = resumo(incidentes, metadados, args)
    except ErroEntrada as exc:
        print(f"ERRO DE ENTRADA: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        texto = json.dumps(corpo, indent=2, ensure_ascii=False)
    elif args.format == "csv":
        texto = render_csv(corpo)
    else:
        texto = render_texto(corpo, args)

    print(texto)
    for aviso in avisos[:MAX_RELATORIO_ERROS]:
        print(f"AVISO: {aviso}", file=sys.stderr)
    if len(avisos) > MAX_RELATORIO_ERROS:
        print(f"AVISO: ... e mais {len(avisos) - MAX_RELATORIO_ERROS} avisos", file=sys.stderr)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(texto + "\n")
        print(f"[mttr] saída escrita em {args.output}", file=sys.stderr)

    violacoes: list[str] = []
    mttr_med = corpo["geral"]["mttr_s"]["mediana_s"]
    mttd_med = corpo["geral"]["mttd_s"]["mediana_s"]
    if args.max_mttr_min is not None and mttr_med is not None and mttr_med / 60.0 > args.max_mttr_min:
        violacoes.append(f"MTTR mediano {fmt_dur(mttr_med)} acima da política de {args.max_mttr_min:.0f}min")
    if args.max_mttd_min is not None and mttd_med is not None and mttd_med / 60.0 > args.max_mttd_min:
        violacoes.append(f"MTTD mediano {fmt_dur(mttd_med)} acima da política de {args.max_mttd_min:.0f}min")
    if violacoes:
        for violacao in violacoes:
            print(f"POLÍTICA VIOLADA: {violacao}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
