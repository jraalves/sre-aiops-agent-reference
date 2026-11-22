# Plataforma de agentes de IA para SRE — contratos, decisões e uma ferramenta que conta MTTR

[English](#english)

Todo mundo já viu a demo: um chat que lê o alerta, escre um parágrafo convincente e sugere
`kubectl rollout undo`. Ninguém mostra o resto — o que entra, o que sai, quem aprova, como se mede se
a coisa ajudou, e o que acontece quando o modelo erra às 3h da manhã.

Este repositório é essa outra parte, em forma de contrato executável: três JSON Schemas, dois ADRs, um
playbook e um runbook escritos como se fossem revisados em PR, e scripts de stdlib que rodam offline.
**Não há integração com nenhum sistema real, nenhuma credencial e nenhum dado de produção.** É
referência de desenho, não implantação.

```
alerta (Qualix · Grafana · Zabbix · CloudWatch)
   │  normalização: um formato de entrada, seis origens
   ▼
┌─────────────────────────┐     contexto montado sob demanda
│  contratos (schemas/)   │◀──── (janela = agora - contexto.janela)
└───────────┬─────────────┘
            ▼
┌─────────────────────────┐   proposta  ──────▶  humano aprova  ──▶  execução
│  agente de diagnóstico  │────────┐             (ADR-002)          fora deste repo
└─────────────────────────┘        │
   ▲    guardrails: escopo, dry-run│reversibilidade, limite de risco
   │    janela de validade         ▼
   └──────── auditoria: prompt_hash, modelo, confiança, o que foi recusado
                        │
                        ▼
             mttr.py: MTTR/MTTD mediano, recorrência, quem viu primeiro
```

## O que está aqui

| caminho | o que é | por que existe |
|---|---|---|
| `schemas/alerta-normalizado.schema.json` | entrada única, independentemente de onde o alerta nasceu | seis origens viram um vocabulário: `origem`, `severidade`, `sinal`, `slo`, `correlacao`, `dedupe` |
| `schemas/contexto-servico.schema.json` | pacote de contexto montado **sob demanda** | `mudancas_recentes`, `incidentes_recentes`, `sinais_vizinhos`, `capacidade` e — o campo que quase ninguém põe — `qualidade` |
| `schemas/proposta-agente.schema.json` | **única** saída do agente | o agente não executa: propõe. Daí `acoes[]` + `nao_recomendado` + `guardrails` + `execucao` + `auditoria` com `prompt_hash` |
| `docs/decisoes/ADR-001-…md` | RAG sobre runbooks e telemetria, em vez de fine-tuning | contexto muda semanalmente; peso de modelo não acompanha, e alucinação vira dívida técnica silenciosa |
| `docs/decisoes/ADR-002-…md` | agente propõe, humano aprova | sem auto-execução em produção: a assimetria entre o ganho (minutos) e a perda (um rollout errado em escala) não fecha |
| `examples/playbook-expurgo.yaml` | playbook de expurgo com precondition, dry-run, recuperação | 191 linhas mostrando o formato que eu gostaria de ter herdado: `politicas`, `pre_condicoes`, `passos`, `recuperacao`, `guardrails_globais` |
| `examples/runbook-correlacao.md` | runbook de correlação de jornada entre borda distribuída e sistema transacional | o produto de um runbook não é a query, é a **linha de veredito** (§2.4) e a parte que ninguém quer escrever: a comunicação (§3) |
| `scripts/mttr.py` | MTTR/MTTD mediano, recorrência, origem da detecção | 895 linhas de stdlib, zero dependência, com denominador em todo número |
| `scripts/valida_contratos.py` | valida `schemas/exemplos/*.json` contra os schemas | roda sem rede e sem `pip`; cobre o subconjunto de JSON Schema usado aqui e diz o que **não** cobre |
| `scripts/gera_incidentes_exemplo.csv` → `examples/incidentes_exemplo.csv` | 44 incidentes **ficcionais**, semente fixa | dado plausível e reconstruíável: você pode provar que é sintético rodando um comando |
| `tests/` | 42 casos, todos em stdlib | contrato é contrato só quando alguém verifica — inclusive um teste que exige que **este README** cite os campos estruturais |

## Comece por aqui (90 segundos, sem `pip install`)

```bash
python3 scripts/valida_contratos.py          # 0 violações, e varre padrão proibido
python3 scripts/mttr.py --csv examples/incidentes_exemplo.csv
python3 -m pytest -q                          # 42 passed
make check                                    # os três acima como um portão único
```

Saída real do `mttr.py` sobre o CSV fictício:

```
 entrada ............ examples/incidentes_exemplo.csv
 linhas ............. lidas 44 · válidas 44 · sem restauração 2 · skew zerado 0
 período ............ 2026-04-04T20:48:21  →  2026-06-30T21:15:00
 janelas ............ recorrência 30.0d · amostra mínima 3 · tolerância de skew 0s

1) MTTR por severidade
   a) desde a origem do impacto (mitigado_em - aberto_em) — a dor do cliente:
| S1  |  2 | 2h25 | 2h25 | 3h56 | 44.5min | 4h06 | n<3  |
| S2  | 12 | 2h21 | 3h23 | 9h13 | 43.1min | 13h15| ok   |
| S3  | 17 | 3h59 | 4h12 | 7h45 | 49.7min | 11h26| ok   |

   Diferença entre (a) e (b) = o que a detecção custa. Se (a) é alto e (b) é
   baixo, o problema não é competência de resposta: é telemetria.

2) Quem viu primeiro?
| alerta / monitoramento / agente        | 30 | 5.2min  | 68.2% |
| cliente / suporte / lojista / parceiro | 11 | 63.0min | 25.0% |
```

Aquela última tabela é o argumento inteiro de AIOps em uma linha: quando o cliente vê antes do
monitoramento, o MTTD mediano é **12× maior** (63 min contra 5,2 min). O ganho de um agente bem desenhado
não é "responder mais rápido" — é encurtar o trecho em que ninguém sabia.

## Como MTTR e recorrência são calculados (a definição é o produto)

- **MTTR** = `mitigado_em - aberto_em`, mediana por severidade, com `n=` em toda linha. Mitigação, não
  resolução: o que interessa é quando o cliente voltou a ser atendido. `resolvido_em` continua na série
  para quem quer a versão contábil.
- **MTTD** = `detectado_em - aberto_em`. Separado do MTTR de propósito: somar os dois esconde de quem o
  problema é — o monitoramento ou o plantão.
- **Recorrência** = mesmo `fingerprint` (ou mesmo `componente`, quando o fingerprint falta) repetido em
  janela móvel de `--janela-recorrencia-dias` (padrão 30). Defeito recorrente é sinal de remendo, não de
  azar.
- **Amostra insuficiente** aparece como `n<3`, nunca como número limpo. Duas linhas de S1 geram uma
  mediana; não geram conclusão.
- **Linha inválida é erro com exit code ≠ 0**, não descarte silencioso. Ferramenta de leitura que
  "limpa" dado sozinha fabrica relatório bonito e mentira exata.
- **`--tolerancia-skew-segundos`**: timestamps de sistemas diferentes não confiam uma na outra. Sem
  tolerância, `detectado_em` 2 s antes de `aberto_em` vira MTTD negativo e a mediana vai embora.

Modo portão de CI — falha se a política for violada (exit 3):

```bash
python3 scripts/mttr.py --csv incidentes.csv --severidade S1,S2 --max-mttd-min 15 --format json
```

## Cinco campos que carregam o desenho

O resto é superfície. Estes são os que, se você errar, o sistema inteiro mente — e há um teste que
impede este README de esquecer de citá-los (`test_readme_cita_campos_que_existem_no_contrato`):

| campo | onde | por que é estrutural |
|---|---|---|
| `fingerprint` | `alerta-normalizado` | identidade do **mesmo** problema entre reavaliações e réplicas. Calculado de `(origem, servico, sinal, regra, conjunto estável de labels)`. Proibido entrar timestamp, valor medido, texto formatado ou host efêmero: entra um desses e a supressão vai a zero, o ruído volta inteiro e a recorrência que o `mttr.py` mede vira ficção |
| `correlacao.correlation_id` | `alerta-normalizado` | a chave de negócio da jornada — a única que sobrevive à fronteira sem OpenTelemetry (sistema transacional, fila, lote). Nunca carrega PAN, documento fiscal nem identificador pessoal. No exemplo ela aparece **truncada** de propósito (`redacao_aplicada` registra isso), porque chave de correlação é dado operacional e vazamento operacional também é incidente |
| `slo.burn_rate` | `alerta-normalizado` | o que separa "queimou 2% do budget numa hora" de "queimou 2% em trinta dias". Sem taxa de consumo, threshold absoluto é superstição com gráfico |
| `nao_recomendado` | `proposta-agente` | ações que o agente considerou e **descartou**, com motivo. É o campo que mais gera confiança na revisão humana — e o que impede um operador de propor de novo o que já foi refutado às 3h |
| `guardrails.kill_switch_ativo` | `proposta-agente` | se `true`, nenhuma proposta sai do estado `proposta` **mesmo com aprovação humana**. Um interruptor que depende de alguém lembrar de usar não é interruptor |

Regra transversal aos cinco: `additionalProperties: false`. Contrato permissivo é contrato decorativo.

## Regras que o contrato impõe (e que dão trabalho de propósito)

1. **Uma saída.** O agente não manda mensagem solta; ele produz `proposta-agente.schema.json`. Sem
   contrato, não há auditoria; sem auditoria, não há como discutir o erro depois.
2. **`confianca` não autoriza nada.** O campo existe para ordenar fila de revisão humana, nunca para
   pular a revisão (`ADR-002`).
3. **`nao_recomendado` é obrigatório.** Um agente que só lista o que fazer é metade de um agente: a parte
   que diz "não reinicie isso agora porque o batch de liquidação está no meio" é a que salva a madrugada.
4. **`prompt_hash`, `modelo` e `auditoria`** entram na proposta para que uma decisão possa ser
   reconstruída três semanas depois, quando alguém perguntar por que o expurgo rodou num domingo.
5. **`additionalProperties: false`** em tudo. Contrato permissivo é contrato decorativo.
6. **`qualidade` no contexto**: se a telemetria da janela está incompleta (série sem scrape, log com
   lag), o contexto diz — em vez de o agente descrever uma realidade que não estava lá.
7. **Nada de dado sensível em `labels`, `descricao` nem `correlation_id`**: o `valida_contratos.py` varre 9 classes de
   padrão proibido (CPF, CNPJ, chave de API, conexão com senha, e-mail, IP privado…) em 7 tipos de
   arquivo, e a varredura faz parte do `make check`.

## O que este repositório deliberadamente **não** é

- Não é plataforma implantável: não há orquestrador, provedor de modelo, nem cliente de Jira/GitLab/Argo.
- Não é código de empregador ou cliente. Nomes de serviço, incidentes, fingerprints e durações são
  ficcionais e gerados com semente fixa.
- Não é promessa de resultado: nenhum número aqui veio de produção. O `mttr.py` é uma calculadora honesta,
  e é por isso que ele insiste em denominador e em rótulo de amostra.
- Não substitui `jsonschema`: o validador próprio cobre o subconjunto usado nos contratos e declara o
  que falta (`oneOf`, `anyOf`, `if/then`, `$ref` remoto).

## Requisitos

Python 3.11+ e `pytest`. Nem `jsonschema` nem nenhuma outra lib é necessária: os contratos são verificados
pelo validador próprio, porque ambiente de revisão enxuto é ambiente real — e dependência que só existe
na minha máquina é teste que alguém vai pular.

## English

Reference design for AI agents in SRE operations, expressed as executable contracts rather than a
demo. Three JSON Schemas define the only shapes the system accepts and emits — a normalized alert
regardless of origin (Prometheus, Zabbix, CloudWatch, vendor APM), an on-demand service context that
carries an explicit `qualidade` field so incomplete telemetry cannot be described as truth, and a single
agent output: a *proposal* with `acoes`, `nao_recomendado`, `guardrails`, expiry window and an audit
block (`prompt_hash`, model, confidence). Two ADRs justify RAG over runbooks instead of fine-tuning, and
human approval instead of self-healing execution — the asymmetry between minutes saved and one wrong
rollout at scale does not close. An expunge playbook and a cross-boundary correlation runbook are written
as reviewable artifacts, including the sections people skip: communication and known failure modes.
`scripts/mttr.py` (895 lines, stdlib only, offline) computes median MTTR/MTTD per severity, recurrence in
a rolling window and detection origin, always printing denominators and marking `n<3` as insufficient;
invalid rows exit non-zero rather than being silently dropped, because a reader that "cleans" data
manufactures polished fiction. Its CI gate mode fails the pipeline when policy is breached.
`scripts/valida_contratos.py` validates the example payloads without network or pip and sweeps nine
forbidden data classes across seven file types — the same sweep that keeps this repository free of
credentials. The 44-row incident CSV is generated from a fixed seed and is entirely fictional, so the
tools can be exercised reproducibly while the security posture stays unambiguous.

---

<sub>Se você encontrou uma forma deste contrato deixar um agente executar algo sem registro auditável,
abra issue — é exatamente o tipo de falha que este repositório quer discutir. Ver [`SECURITY.md`](SECURITY.md).</sub>
