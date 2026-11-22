"""Testes do `scripts/mttr.py`. Offline, stdlib + pytest, sem fixture binária.

Cada teste planta um CSV mínimo **com a resposta sabida à mão**. Isso é
deliberado: testar script de métrica comparando com ele mesmo não prova nada, e
o que precisa ficar travado aqui é a **definição** (o que entra no numerador, o
que é censurado, qual janela conta), não a aritmética.

Rodar:
    python3 -m pytest -q                        # da raiz do repositório
    python3 -m pytest tests/test_mttr.py -v     # detalhado
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import mttr  # via tests/conftest.py
from conftest import CSV_EXEMPLO

CABECALHO = [
    "incidente_id", "aberto_em", "detectado_em", "diagnosticado_em", "mitigado_em",
    "resolvido_em", "severidade", "componente", "fingerprint", "causa_raiz",
    "origem_deteccao", "status", "impacto_clientes", "aprovacao_humana",
]


def escrever_csv(destino: Path, linhas: list[dict], colunas: list[str] | None = None) -> Path:
    colunas = colunas or CABECALHO
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=colunas, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for linha in linhas:
            writer.writerow({coluna: linha.get(coluna, "") for coluna in colunas})
    return destino


def incidente(ident: str, aberto: str, detectado: str, mitigado: str = "", **extra) -> dict:
    """Linha de incidente com datas montadas a partir de HH:MM:SS em UTC."""
    base = {
        "incidente_id": ident,
        "aberto_em": f"2026-05-01T{aberto}+00:00",
        "detectado_em": f"2026-05-01T{detectado}+00:00",
        "mitigado_em": f"2026-05-01T{mitigado}+00:00" if mitigado else "",
        "severidade": "S2",
        "componente": "svc-a",
        "status": "resolvido" if mitigado else "aberto",
    }
    base.update(extra)
    return base


def rodar_json(tmp_path, capsys, linhas: list[dict], *args: str) -> dict:
    caminho = escrever_csv(tmp_path / "incidentes.csv", linhas)
    codigo = mttr.main(["--csv", str(caminho), "--format", "json", *args])
    assert codigo == 0, f"esperava sucesso, veio {codigo}: {capsys.readouterr().err}"
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# Definições: MTTR, MTTD, TTFD e censura
# --------------------------------------------------------------------------- #

def test_mttr_mediano_por_severidade_usa_a_restauracao_desde_a_origem(tmp_path, capsys):
    # restaurações (desde aberto_em): 30min, 40min, 200min -> mediana 40min
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:40:00"),
        incidente("INC-3", "12:00:00", "12:02:00", "15:20:00"),
    ]
    corpo = rodar_json(tmp_path, capsys, linhas)
    grupo = next(g for g in corpo["por_severidade"] if g["severidade"] == "S2")
    assert grupo["n"] == 3
    assert grupo["mttr_s"]["mediana_s"] == 2400.0
    # mediana e não média: a média (5400s) diria "quase 1h30" por causa de um só
    # incidente de cauda — exatamente o erro que a métrica precisa evitar
    assert grupo["mttr_s"]["media_s"] == 5400.0


def test_mttr_pos_deteccao_exclui_a_janela_de_deteccao(tmp_path, capsys):
    # impacto 10:00, detectado 10:50, mitigado 11:00: a diferença entre as duas
    # leituras do "MTTR" É a detecção, e é ela que o agente encurta
    linhas = [incidente("INC-1", "10:00:00", "10:50:00", "11:00:00")]
    geral = rodar_json(tmp_path, capsys, linhas)["geral"]
    assert geral["mttr_s"]["mediana_s"] == 3600.0
    assert geral["mttr_pos_deteccao_s"]["mediana_s"] == 600.0
    assert geral["mttd_s"]["mediana_s"] == 3000.0


def test_incidente_em_aberto_fora_do_mttr_mas_conta_no_mttd(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00"),
        incidente("INC-2", "12:00:00", "12:20:00", mitigado="", status="aberto"),
    ]
    corpo = rodar_json(tmp_path, capsys, linhas)
    assert corpo["escopo"]["incidentes_analisados"] == 2
    assert corpo["escopo"]["incidentes_restaurados"] == 1
    assert corpo["geral"]["mttr_s"]["n"] == 1          # censura à direita respeitada
    assert corpo["geral"]["mttd_s"]["n"] == 2          # a detecção é conhecida nas duas
    assert corpo["metadados_entrada"]["censurados_sem_restauracao"] == 1


def test_ttfd_so_existe_quando_diagnosticado_em_existe(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00",
                  diagnosticado_em="2026-05-01T10:12:00+00:00"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:30:00"),
    ]
    corpo = rodar_json(tmp_path, capsys, linhas)
    assert corpo["geral"]["tempo_ate_diagnostico_s"]["n"] == 1
    assert corpo["geral"]["tempo_ate_diagnostico_s"]["mediana_s"] == 600.0


def test_tempo_de_fechamento_inclui_o_trabalho_administrativo(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "10:02:00", "10:30:00",
                        resolvido_em="2026-05-01T16:30:00+00:00")]
    geral = rodar_json(tmp_path, capsys, linhas)["geral"]
    assert geral["mttr_s"]["mediana_s"] == 1800.0
    assert geral["mttr_fechamento_s"]["mediana_s"] == 23400.0


# --------------------------------------------------------------------------- #
# Recorrência
# --------------------------------------------------------------------------- #

def test_recorrencia_mesmo_fingerprint_dentro_da_janela(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00", componente="svc-a", fingerprint="fp-1"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:30:00", componente="svc-a", fingerprint="fp-1"),
    ]
    rec = rodar_json(tmp_path, capsys, linhas)["recorrencia_por_componente"]
    svc = next(r for r in rec if r["componente"] == "svc-a")
    assert svc["incidentes"] == 2
    assert svc["problemas_distintos"] == 1
    assert svc["recorrentes"] == 1
    assert svc["taxa_recorrencia"] == 0.5


def test_recorrencia_depende_da_janela_declarada(tmp_path, capsys):
    # mesmo fingerprint, 40 dias de distância: com janela de 30 dias não conta.
    # Sem janela declarada, "recorrente" passaria a ser "tudo que já aconteceu".
    linhas = [
        {"incidente_id": "INC-1", "aberto_em": "2026-04-01T10:00:00+00:00",
         "detectado_em": "2026-04-01T10:02:00+00:00", "mitigado_em": "2026-04-01T10:30:00+00:00",
         "severidade": "S2", "componente": "svc-a", "fingerprint": "fp-1", "status": "resolvido"},
        {"incidente_id": "INC-2", "aberto_em": "2026-05-11T10:00:00+00:00",
         "detectado_em": "2026-05-11T10:02:00+00:00", "mitigado_em": "2026-05-11T10:30:00+00:00",
         "severidade": "S2", "componente": "svc-a", "fingerprint": "fp-1", "status": "resolvido"},
    ]
    caminho = escrever_csv(tmp_path / "incidentes.csv", linhas)
    assert mttr.main(["--csv", str(caminho), "--format", "json", "--janela-recorrencia-dias", "30"]) == 0
    rec = json.loads(capsys.readouterr().out)["recorrencia_por_componente"]
    assert next(r for r in rec if r["componente"] == "svc-a")["recorrentes"] == 0

    assert mttr.main(["--csv", str(caminho), "--format", "json", "--janela-recorrencia-dias", "60"]) == 0
    rec = json.loads(capsys.readouterr().out)["recorrencia_por_componente"]
    assert next(r for r in rec if r["componente"] == "svc-a")["recorrentes"] == 1


def test_recorrencia_usa_cadeia_de_fallback_e_diz_qual_usou(tmp_path, capsys):
    linhas = [
        # sem fingerprint: cai em componente + causa_raiz
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00", componente="svc-b", causa_raiz="fila-saturada"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:30:00", componente="svc-b", causa_raiz="fila-saturada"),
        # sem fingerprint e sem causa: cai em componente (a cadeia mais larga, que
        # superestima recorrência — por isso o relatório imprime qual foi usada)
        incidente("INC-3", "12:00:00", "12:02:00", "12:30:00", componente="svc-c"),
        incidente("INC-4", "13:00:00", "13:02:00", "13:30:00", componente="svc-c"),
    ]
    rec = rodar_json(tmp_path, capsys, linhas)["recorrencia_por_componente"]
    svc_b = next(r for r in rec if r["componente"] == "svc-b")
    svc_c = next(r for r in rec if r["componente"] == "svc-c")
    assert svc_b["cadeias_identidade"] == ["causa"]
    assert svc_b["recorrentes"] == 1
    assert svc_c["cadeias_identidade"] == ["comp"]
    assert svc_c["problema_mais_repetido"] == "comp:svc-c"


def test_recorrencia_nao_cruza_componentes(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00", componente="svc-a", fingerprint="fp-9"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:30:00", componente="svc-d", fingerprint="fp-9"),
    ]
    rec = rodar_json(tmp_path, capsys, linhas)["recorrencia_por_componente"]
    assert sorted(r["recorrentes"] for r in rec) == [0, 0]


def test_gap_mediano_de_repeticao_sai_em_dias(tmp_path, capsys):
    linhas = [
        {"incidente_id": f"INC-{idx}", "aberto_em": f"2026-05-{dia:02d}T10:00:00+00:00",
         "detectado_em": f"2026-05-{dia:02d}T10:02:00+00:00",
         "mitigado_em": f"2026-05-{dia:02d}T10:30:00+00:00",
         "severidade": "S3", "componente": "svc-e", "fingerprint": "fp-5", "status": "resolvido"}
        for idx, dia in enumerate((2, 12, 22), start=1)
    ]
    rec = rodar_json(tmp_path, capsys, linhas)["recorrencia_por_componente"]
    svc = next(r for r in rec if r["componente"] == "svc-e")
    assert svc["recorrentes"] == 2
    assert svc["gap_mediano_repeticao_dias"] == 10.0


# --------------------------------------------------------------------------- #
# Validação de entrada: o script se recusa, e diz por quê
# --------------------------------------------------------------------------- #

def test_coluna_obrigatoria_ausente_falha_com_saida_2(tmp_path, capsys):
    caminho = tmp_path / "ruim.csv"
    caminho.write_text("incidente_id,aberto_em\nINC-1,2026-05-01T10:00:00\n", encoding="utf-8")
    assert mttr.main(["--csv", str(caminho)]) == 2
    assert "colunas obrigatórias ausentes" in capsys.readouterr().err


def test_arquivo_inexistente_falha_com_saida_2(tmp_path, capsys):
    assert mttr.main(["--csv", str(tmp_path / "nao-existe.csv")]) == 2
    assert "não encontrado" in capsys.readouterr().err


def test_erros_acumulados_em_uma_passada_so(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:02:00", "10:30:00", severidade="P1"),
        incidente("INC-2", "11:00:00", "11:02:00", "11:30:00"),
        incidente("INC-2", "12:00:00", "12:02:00", "12:30:00"),
    ]
    assert mttr.main(["--csv", str(escrever_csv(tmp_path / "i.csv", linhas))]) == 2
    erro = capsys.readouterr().err
    assert "severidade 'P1'" in erro
    assert "duplicado" in erro
    assert "2 linha(s) inválida" in erro


def test_timestamp_fora_de_iso8601_e_erro_de_entrada(tmp_path, capsys):
    linhas = [{"incidente_id": "INC-1", "aberto_em": "01/05/2026 10:00", "detectado_em": "10:02",
               "mitigado_em": "10:30", "severidade": "S2", "componente": "svc-a", "status": "resolvido"}]
    assert mttr.main(["--csv", str(escrever_csv(tmp_path / "i.csv", linhas))]) == 2
    assert "não é ISO-8601" in capsys.readouterr().err


def test_detectado_antes_de_aberto_e_erro_sem_tolerancia_de_skew(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "09:59:40", "10:30:00")]
    assert mttr.main(["--csv", str(escrever_csv(tmp_path / "i.csv", linhas))]) == 2
    assert "antes de aberto_em" in capsys.readouterr().err


def test_skew_aceito_dentro_da_tolerancia_e_reportado_a_parte(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "09:59:40", "10:30:00")]
    corpo = rodar_json(tmp_path, capsys, linhas, "--tolerancia-skew-segundos", "30")
    assert corpo["metadados_entrada"]["linhas_com_skew_zerado"] == 1
    assert corpo["geral"]["mttd_s"]["mediana_s"] == 0.0  # zerou, não virou -20s


def test_fuso_misturado_no_arquivo_e_erro(tmp_path, capsys):
    linhas = [
        {"incidente_id": "INC-1", "aberto_em": "2026-05-01T10:00:00+00:00",
         "detectado_em": "2026-05-01T10:02:00+00:00", "mitigado_em": "2026-05-01T10:30:00+00:00",
         "severidade": "S2", "componente": "svc-a", "status": "resolvido"},
        {"incidente_id": "INC-2", "aberto_em": "2026-05-01T11:00:00",
         "detectado_em": "2026-05-01T11:02:00", "mitigado_em": "2026-05-01T11:30:00",
         "severidade": "S2", "componente": "svc-a", "status": "resolvido"},
    ]
    assert mttr.main(["--csv", str(escrever_csv(tmp_path / "i.csv", linhas))]) == 2
    assert "mistura" in capsys.readouterr().err


def test_mitigado_vazio_com_status_resolvido_e_contradicao(tmp_path, capsys):
    linhas = [{"incidente_id": "INC-1", "aberto_em": "2026-05-01T10:00:00+00:00",
               "detectado_em": "2026-05-01T10:02:00+00:00", "mitigado_em": "",
               "severidade": "S2", "componente": "svc-a", "status": "resolvido"}]
    assert mttr.main(["--csv", str(escrever_csv(tmp_path / "i.csv", linhas))]) == 2
    assert "mitigado_em` vazio" in capsys.readouterr().err


def test_filtro_que_nao_deixa_linha_e_erro_nao_tabela_vazia(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "10:02:00", "10:30:00")]
    caminho = str(escrever_csv(tmp_path / "i.csv", linhas))
    assert mttr.main(["--csv", caminho, "--componente", "nao-existe"]) == 2
    assert "sobreviveu aos filtros" in capsys.readouterr().err


def test_coluna_fora_do_contrato_gera_aviso_e_continua(tmp_path, capsys):
    linha = incidente("INC-1", "10:00:00", "10:02:00", "10:30:00")
    linha["origem_do_export"] = "planilha-2026"
    caminho = escrever_csv(tmp_path / "i.csv", [linha], colunas=CABECALHO + ["origem_do_export"])
    codigo = mttr.main(["--csv", str(caminho), "--format", "json"])
    capturado = capsys.readouterr()
    assert codigo == 0
    assert "fora do contrato" in capturado.err
    corpo = json.loads(capturado.out)
    assert corpo["metadados_entrada"]["colunas_desconhecidas_ignoradas"] == ["origem_do_export"]


# --------------------------------------------------------------------------- #
# Portão de política (uso em pipeline) e formatos
# --------------------------------------------------------------------------- #

def test_portao_de_politica_retorna_3(tmp_path, capsys):
    linhas = [
        incidente("INC-1", "10:00:00", "10:20:00", "10:50:00"),
        incidente("INC-2", "11:00:00", "11:25:00", "11:55:00"),
        incidente("INC-3", "12:00:00", "12:30:00", "13:00:00"),
    ]
    caminho = str(escrever_csv(tmp_path / "i.csv", linhas))
    assert mttr.main(["--csv", caminho, "--max-mttd-min", "5"]) == 3
    assert "POLÍTICA VIOLADA" in capsys.readouterr().err
    assert mttr.main(["--csv", caminho, "--max-mttd-min", "60"]) == 0


def test_formato_csv_tem_os_bloco_esperados(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "10:02:00", "10:30:00", fingerprint="fp-1")]
    caminho = str(escrever_csv(tmp_path / "i.csv", linhas))
    assert mttr.main(["--csv", caminho, "--format", "csv"]) == 0
    linhas_saida = [l for l in csv.reader(capsys.readouterr().out.splitlines()) if l]
    blocos = {l[0] for l in linhas_saida[1:]}
    assert {"geral", "por_severidade:mttr_desde_origem", "recorrencia_por_componente"} <= blocos


def test_saida_em_arquivo_replica_o_stdout(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "10:02:00", "10:30:00")]
    caminho = str(escrever_csv(tmp_path / "i.csv", linhas))
    destino = tmp_path / "relatorio.txt"
    assert mttr.main(["--csv", caminho, "--output", str(destino)]) == 0
    conteudo = destino.read_text(encoding="utf-8")
    assert "COMO FOI MEDIDO" in conteudo
    assert "COMO FOI MEDIDO" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Primitivas, rodapé honesto e o dataset de exemplo
# --------------------------------------------------------------------------- #

def test_percentil_usa_interpolacao_linear():
    assert mttr.percentil([10.0], 0.95) == 10.0
    assert mttr.percentil([0.0, 10.0], 0.5) == 5.0
    assert mttr.percentil([0.0, 10.0, 20.0], 0.95) == 19.0


def test_fmt_dur_e_legivel():
    assert mttr.fmt_dur(None) == "—"
    assert mttr.fmt_dur(45) == "45s"
    assert mttr.fmt_dur(420) == "7.0min"
    assert mttr.fmt_dur(5400) == "1h30"
    assert mttr.fmt_dur(3 * 86400) == "3.0d"


def test_rodape_expoe_o_como_foi_medido(tmp_path, capsys):
    linhas = [incidente("INC-1", "10:00:00", "10:02:00", "10:30:00")]
    caminho = str(escrever_csv(tmp_path / "i.csv", linhas))
    assert mttr.main(["--csv", caminho]) == 0
    saida = capsys.readouterr().out
    assert "COMO FOI MEDIDO" in saida
    assert "mitigado_em - aberto_em" in saida
    assert "n<3" in saida  # grupo pequeno continua visível, marcado


def test_csv_de_exemplo_ficticio_carrega_e_e_coerente(capsys):
    assert CSV_EXEMPLO.is_file(), "examples/incidentes_exemplo.csv é parte do repositório"
    assert mttr.main(["--csv", str(CSV_EXEMPLO), "--format", "json"]) == 0
    corpo = json.loads(capsys.readouterr().out)
    assert corpo["escopo"]["incidentes_analisados"] >= 30
    # ficção plausível continua sendo ficção coerente: restauração > detecção
    assert corpo["geral"]["mttr_s"]["mediana_s"] > corpo["geral"]["mttd_s"]["mediana_s"]
    with open(CSV_EXEMPLO, encoding="utf-8") as fh:
        ids = [r["incidente_id"] for r in csv.DictReader(fh)]
    # faixa 9000+ é reservada ao gerador: identifica o dado como sintético à vista
    assert all(i.startswith("INC-2026-9") for i in ids)
    assert len(ids) == len(set(ids))


def test_gerador_e_deterministico(tmp_path):
    """Mesma semente -> mesmos bytes. Sem isso, 'clone e rode para conferir' não cola."""
    script = Path(mttr.__file__).with_name("gera_incidentes_exemplo.py")
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    for destino in (a, b):
        subprocess.run([sys.executable, str(script), "--out", str(destino)],
                       check=True, capture_output=True)
    assert a.read_bytes() == b.read_bytes()
    assert "INC-2026-9" in a.read_text(encoding="utf-8")


def test_cli_help_funciona_por_subprocesso():
    resultado = subprocess.run([sys.executable, str(Path(mttr.__file__)), "--help"],
                               capture_output=True, text=True, check=False)
    assert resultado.returncode == 0
    for flag in ("--csv", "--janela-recorrencia-dias", "--tolerancia-skew-segundos", "--max-mttd-min"):
        assert flag in resultado.stdout
