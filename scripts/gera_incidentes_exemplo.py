#!/usr/bin/env python3
"""gera_incidentes_exemplo.py — produz `examples/incidentes_exemplo.csv`.

**Tudo aqui é ficção.** Os ids (`INC-2026-9001`), os componentes
(`svc-pagamento-instantaneo`), os fingerprints (`fp-7a13`) e as durações foram
gerados por este script com semente fixa para exercitar o `mttr.py`. Nenhuma
linha representa incidente real, de nenhum ambiente, de nenhum cliente.

Por que um gerador em vez de um CSV "na mão"? Porque o CSV precisa ser
**plausível** (recorrência concentrada em poucos defeitos, detecção reativa mais
lenta que detecção por sinal próprio, cauda longa no MTTR) e precisa ser
**reconstruível**. Com semente fixa o arquivo é determinístico: quem revisar
consegue provar que o dado é sintético rodando um comando, sem depender da minha
palavra.

Dois vícios foram plantados de propósito, porque dataset sem vício ensina a
responder errado:

* alguns incidentes com `origem_deteccao` vazio — dado fraco; o relatório mostra
  "origem não declarada" em vez de sumir com a linha;
* dois incidentes com `status=aberto` e `mitigado_em` vazio — censura à direita;
  sem fim não há duração de restauração, e tratá-los como "lentos" distorce a
  mediana.

Uso:
    python3 scripts/gera_incidentes_exemplo.py --out examples/incidentes_exemplo.csv
    python3 scripts/gera_incidentes_exemplo.py --semente 7 --out /tmp/outro.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import datetime, timedelta

SEMENTE_PADRAO = 20260522
INICIO = datetime(2026, 4, 1, 0, 0)
FIM = datetime(2026, 6, 30, 23, 0)

COLUNAS = [
    "incidente_id", "aberto_em", "detectado_em", "diagnosticado_em", "mitigado_em",
    "resolvido_em", "severidade", "componente", "fingerprint", "causa_raiz",
    "origem_deteccao", "status", "impacto_clientes", "aprovacao_humana",
]

# Componentes fictícios: papéis de arquitetura, não nomes de sistema de ninguém.
COMPONENTES = [
    "svc-pagamento-instantaneo",
    "svc-autorizacao-cartao",
    "svc-liquidacao-lote",
    "svc-tokenizacao",
    "gw-adquirencia-a",
    "adaptador-legado",
    "portal-lojista",
    "batch-arquivo-liquidacao",
]

# (componente, fingerprint, causa, quantas repetições, severidade)
# É aqui que a métrica de recorrência ganha sentido: mesmo componente, mesma
# identidade de problema, voltando dentro da janela de 30 dias.
EPISODIOS_RECORRENTES = [
    ("svc-pagamento-instantaneo", "fp-7a13", "fila-saturada", 5, "S2"),
    ("adaptador-legado", "fp-4d02", "id-perdido-na-fronteira", 4, "S3"),
    ("batch-arquivo-liquidacao", "fp-3c91", "janela-de-batch-estourada", 3, "S2"),
    ("svc-tokenizacao", "fp-8be5", "rotacao-de-chave-falhou", 2, "S1"),
    ("gw-adquirencia-a", "fp-1f77", "timeout-downstream", 2, "S3"),
]

# (causa, componente) — uma aparição cada, fingerprint único.
CAUSAS_UNICAS = [
    ("deploy-regressivo", "svc-autorizacao-cartao"),
    ("regua-retencao-nao-aplicada", "svc-liquidacao-lote"),
    ("certificado-expirado", "portal-lojista"),
    ("limite-de-conexao-esgotado", "gw-adquirencia-a"),
    ("retry-tempestade", "svc-pagamento-instantaneo"),
    ("manutencao-planejada-de-database", "svc-tokenizacao"),
    ("regra-de-negocio-atualizada", "svc-autorizacao-cartao"),
    ("disco-cheio-em-volume-de-log", "adaptador-legado"),
    ("fila-morto-sem-dead-letter", "svc-liquidacao-lote"),
    ("timeout-herdado-de-config", "portal-lojista"),
]

ORIGENS = ["alerta", "synthetic", "cliente", "suporte", "reconciliacao-lote", ""]
PESOS_ORIGEM = [56, 10, 13, 6, 7, 8]


def horario_com_pico(rng: random.Random) -> datetime:
    """Pagamento tem pico: expediente do comércio e janela de lote de madrugada.

    Sortear instante uniforme daria um dataset com MTTR de formato irreal, e o
    objetivo aqui é treinar leitura, não só aritmética.
    """
    base = INICIO + timedelta(seconds=rng.randint(0, int((FIM - INICIO).total_seconds())))
    sorteio = rng.random()
    if sorteio < 0.12:
        base = base.replace(hour=rng.randint(1, 4))          # janela de lote
    elif sorteio < 0.60:
        base = base.replace(hour=rng.randint(10, 21))        # expediente
    return base.replace(microsecond=0, second=rng.randint(0, 59))


def janelas(rng: random.Random, severidade: str, origem: str) -> tuple[int, int, int, int]:
    """Retorna (mttd_s, tempo_ate_diagnostico_s, mitigacao_apos_diag_s, fechamento_s).

    Distribuições por intenção declarada:
      * detecção por alerta é ordens de grandeza mais rápida que por cliente;
      * S1 tem plantão dedicado: diagnóstico rápido, mitigação ainda lenta;
      * origem não declarada é deliberadamente lenta (é o custo do dado fraco).
    """
    if origem in ("cliente", "suporte", "reconciliacao-lote"):
        mttd = rng.randint(600, 5400)
    elif origem == "alerta":
        mttd = rng.randint(35, 420)
    elif origem == "synthetic":
        mttd = rng.randint(90, 1200)
    else:
        mttd = rng.randint(1800, 14000)

    fator = {"S1": 0.55, "S2": 0.85, "S3": 1.25, "S4": 1.9}[severidade]
    diagnostico = max(45, int(rng.lognormvariate(6.6, 0.85) * fator))
    mitigacao = max(90, int(rng.lognormvariate(8.9, 1.0) * fator))
    fechamento = rng.randint(1800, 5 * 86400)
    return mttd, diagnostico, mitigacao, fechamento


def linha(rng: random.Random, seq: int, componente: str, fingerprint: str, causa: str,
          severidade: str, aberto: datetime, status: str = "resolvido") -> dict:
    origem = rng.choices(ORIGENS, weights=PESOS_ORIGEM)[0]
    mttd, tempo_diag, mitigacao, fechamento = janelas(rng, severidade, origem)
    if status == "aberto":
        # sem restauração registrada: o incidente continua correndo
        mttd = min(mttd, 900)
        detectado = aberto + timedelta(seconds=mttd)
        return {
            "incidente_id": f"INC-2026-{9000 + seq}",
            "aberto_em": aberto.isoformat(timespec="seconds"),
            "detectado_em": detectado.isoformat(timespec="seconds"),
            "diagnosticado_em": "",
            "mitigado_em": "",
            "resolvido_em": "",
            "severidade": severidade,
            "componente": componente,
            "fingerprint": fingerprint,
            "causa_raiz": causa,
            "origem_deteccao": origem,
            "status": status,
            "impacto_clientes": rng.randint(1, 400),
            "aprovacao_humana": "",
        }

    detectado = aberto + timedelta(seconds=mttd)
    diagnosticado = detectado + timedelta(seconds=tempo_diag)
    mitigado = diagnosticado + timedelta(seconds=mitigacao)
    resolvido = mitigado + timedelta(seconds=fechamento)
    impacto = 0 if severidade == "S4" else rng.randint(1, 9000)
    if componente in ("batch-arquivo-liquidacao", "svc-liquidacao-lote"):
        impacto *= 12  # lote atinge muitos por uma vez só
    return {
        "incidente_id": f"INC-2026-{9000 + seq}",
        "aberto_em": aberto.isoformat(timespec="seconds"),
        "detectado_em": detectado.isoformat(timespec="seconds"),
        "diagnosticado_em": diagnosticado.isoformat(timespec="seconds"),
        "mitigado_em": mitigado.isoformat(timespec="seconds"),
        "resolvido_em": resolvido.isoformat(timespec="seconds"),
        "severidade": severidade,
        "componente": componente,
        "fingerprint": fingerprint,
        "causa_raiz": causa,
        "origem_deteccao": origem,
        "status": status,
        "impacto_clientes": impacto,
        "aprovacao_humana": rng.choices(["sim", "nao", ""], weights=[46, 12, 42])[0],
    }


def gerar(semente: int) -> list[dict]:
    rng = random.Random(semente)
    linhas: list[dict] = []
    seq = 1

    # 1) episódios recorrentes, espaçados 3-9 dias para cair dentro da janela
    for componente, fp, causa, vezes, severidade in EPISODIOS_RECORRENTES:
        base = INICIO + timedelta(days=rng.randint(2, 60))
        base = base.replace(hour=rng.randint(2, 22), minute=rng.randint(0, 59), microsecond=0)
        acumulado = timedelta()
        for _ in range(vezes):
            aberto = base + acumulado + timedelta(hours=rng.randint(-2, 2), minutes=rng.randint(0, 59))
            linhas.append(linha(rng, seq, componente, fp, causa, severidade, aberto))
            seq += 1
            acumulado += timedelta(days=rng.randint(3, 9))

    # 2) pontuais: uma aparição, fingerprint único
    for causa, componente in CAUSAS_UNICAS:
        linhas.append(linha(rng, seq, componente, f"fp-{rng.randrange(16 ** 4):04x}", causa,
                            rng.choices(["S1", "S2", "S3", "S4"], weights=[8, 22, 40, 30])[0],
                            horario_com_pico(rng)))
        seq += 1

    # 3) ruído de plantão: S3/S4 sem causa declarada (o caso em que a recorrência
    #    cai na cadeia de fallback `componente` e o relatório precisa dizer isso)
    for _ in range(16):
        linhas.append(linha(rng, seq, rng.choice(COMPONENTES), "", "",
                            rng.choices(["S2", "S3", "S4"], weights=[15, 50, 35])[0],
                            horario_com_pico(rng)))
        seq += 1

    # 4) censura à direita
    linhas.append(linha(rng, seq, "svc-pagamento-instantaneo", "fp-7a13", "fila-saturada", "S2",
                        FIM - timedelta(hours=5), status="aberto"))
    seq += 1
    linhas.append(linha(rng, seq, "portal-lojista", "fp-9d44", "cache-quebrado", "S3",
                        FIM - timedelta(hours=2), status="aberto"))

    linhas.sort(key=lambda l: l["aberto_em"])
    return linhas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gera o CSV de incidentes FICTÍCIOS consumido por scripts/mttr.py.")
    parser.add_argument("--out", default=os.path.join("examples", "incidentes_exemplo.csv"),
                        help="arquivo de saída (padrão: examples/incidentes_exemplo.csv)")
    parser.add_argument("--semente", type=int, default=SEMENTE_PADRAO,
                        help=f"seed do gerador (padrão: {SEMENTE_PADRAO})")
    args = parser.parse_args()

    linhas = gerar(args.semente)
    destino = os.path.normpath(args.out)
    if os.path.dirname(destino):
        os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUNAS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(linhas)
    print(f"[gera_incidentes_exemplo] {len(linhas)} incidentes FICTÍCIOS -> {destino} (semente {args.semente})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
