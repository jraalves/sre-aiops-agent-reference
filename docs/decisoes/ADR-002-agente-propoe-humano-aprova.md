# ADR-002 — Agente propõe, humano aprova mudança em produção (e por que não auto-execução)

- **Status:** Aceito
- **Data:** 2026-03-15
- **Decisão de:** SRE / observabilidade. Implementação de referência própria, reconstruída de prática — não é artefato de nenhum empregador ou cliente.

## Contexto

Existe uma pressão legítima: se o agente diagnosticou com confiança alta e a ação
é a mesma que o runbook manda fazer há três anos, por que esperar um humano
clicar? Auto-execução promete MTTR menor, menos toil de madrugada, e é o argumento
comercial padrão de plataforma AIOps.

No domínio em que trabalho, o custo de um erro de ação não é simétrico com o
benefício de acertar:

- um diagnóstico errado custa minutos de atenção;
- uma **ação** errada em caminho de pagamento pode custar movimentação financeira
  duplicada, efeito em janela de fechamento, e um incidente regulatório — não um
  incidente de disponibilidade.

Some a isso o que a telemetria deste ambiente realmente permite saber: a fronteira
com o sistema transacional tem cobertura parcial de log e nenhuma de trace
(tema do repositório irmão `otel-correlation-design`). Ou seja: o agente decide
com uma visão **incompleta e declaradamente incompleta** do estado. Executar com
confiança 0,8 numa visão que cobre 71% do que acontece do outro lado é uma aposta,
não um autômato.

Há ainda um fator organizacional, não técnico: quem responde pelo plantão precisa
saber que a barreira entre "máquina sugere" e "máquina faz" é uma decisão **humana
registrada**, não um flag que alguém ligou num sprint de automação.

## Decisão

**Nenhuma ação de escrita em produção sem aprovação humana explícita, por
identidade auditável, com o diff apresentado antes do clique.**

O contrato implementa isso estruturalmente, não por configuração
(`schemas/proposta-agente.schema.json`):

1. `acoes[].modo` tem vocabulário fechado: `somente-leitura`,
   `proposta-com-aprovacao`, `auto-execucao-pos-aprovacao`. **Não existe**
   `auto-execucao-sem-aprovacao` no enum. Não é "existe e está desligado": não
   haver a opção é uma defesa mais forte que uma flag, porque flag muda em PR,
   vocabulário muda em decisão.
2. `execucao.aprovacao` é `null` até existir decisão humana, e quando existe traz
   `aprovador` (username humano — identidade de serviço é recusada), `quando`,
   `meio`, `diff_apresentada: true` e `validade_segundos` (aprovação expira:
   "aprovei de manhã" não autoriza execução às 23h).
3. `idempotencia.efeito_colateral_financeiro: true` **bloqueia** qualquer modo
   que não seja humano-executa. Dupla autorização de pagamento não é caso de
   automação assistida; é caso de revisão de arquitetura.
4. `blast_radius` e `rollback.testado_em` são obrigatórios. Ação com rollback que
   ninguém testou não chega ao aprovador como "baixo risco".
5. `guardrails.kill_switch_ativo` e `limite_de_taxa_restante` aparecem **no
   resultado**, não só no log: quem aprova vê o estado do freio.
6. `nao_recomendado[]` registra o que o agente considerou e descartou, com a
   regra aplicada. É a parte que dá ao revisor condição de discordar com
   conhecimento, não de confiar.

Aprovado, a execução preferida é **por PR no repositório GitOps**
(`acoes[].pr_em_vez_de_comando`), não por comando num console: mudança em produção
entra por revisão, com histórico, dono e rollback declarado. Se a única forma de
executar uma ação é por console, isso é um achado do desenho, não um detalhe.

## Consequências

**Ganha-se:**

- MTTR cai onde é seguro cair: o tempo até o **primeiro diagnóstico** é
  automatizado (e é essa a métrica do README), enquanto o tempo de decisão é
  assistido, não substituído;
- a barreira de segurança não depende de disciplina humana contínua, depende de
  um enum e de um schema que falham em review de código;
- o registro de quem aprovou o quê, com qual diff, é o material de post-mortem
  que ninguém precisa reconstruir de memória;
- o aprovador tem incentivo para ler, porque a proposta é curta, tem alternativas
  descartadas e tem custo de leitura baixo.

**Paga-se (e não vou fingir que é grátis):**

1. **Latência de aprovação é o novo MTTR.** Se a escala demora 12 min para
   aparecer, o agente melhorou o diagnóstico e piorou a resolução. Mitigação
   medida: `[medir: tempo entre "proposta emitida" e "aprovada", por turno e por
   gravidade]` — este número precisa aparecer junto do MTTR, ou a plataforma
   mente.
2. **Aprovação vira carimbo.** O risco real não é o agente executar; é o humano
   aprovar sem ler porque "sempre deu certo". Mitigação: `diff_apresentada`
   obrigatório, proposta com no máximo 3 ações por agregado, e `[medir: taxa de
   aprovação em menos de 20 segundos por aprovador]` — aprovação relâmpago é
   sinal de carimbo, não de eficiência.
3. **Plantão precisa de contexto para aprovar.** Isso custa documentação viva:
   runbook com `ultimo_teste_em` é o preço de manter o aprovador capaz.
4. **Nem todo caso aceita esse modelo.** Mitigação de capacidade com escala
   horizontal auto executada por HPA é auto-execução, e está bem. A linha não é
   "automação proibida": é **automação com escopo e reversibilidade conhecida**.
   Ação com efeito financeiro ou com mudança de configuração de negócio entra por
   humano. Ação elástica, idempotente e com rollback testado pode ir por política
   declarada em outro lugar — e este repositório não é esse lugar.

## Quando eu trocaria esta decisão

- se `tempo_de_aprovacao_p50` medido ficar consistentemente acima do
  `tempo_ate_diagnostico` que o agente economiza, o modelo é um pesadelo
  operacional e precisa ser refeito (aí a discussão é qual classe de ação pode
  virar auto-execução com policy, e não se o agente é bom);
- se existir um tipo de ação com as quatro propriedades a seguir, **por ADR nova e
  não por flag**, ela pode sair da aprovação síncrona: (a) idempotência provada,
  (b) rollback testado nos últimos 90 dias, (c) blast radius no nível
  recurso/pod, (d) efeito_colateral_financeiro false;
- se a plataforma um dia precisar agir mais rápido que a humana (proteção de
  cascata em segundos), isso não é agente propondo: é mecanismo de controle com
  política e limites rígidos. São sistemas diferentes, e chamar o segundo de
  "agente" é como se perde a aprovação de quem deveria confiar no primeiro.
