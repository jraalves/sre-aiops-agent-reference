# Security Policy

## O que este repositório é (e o que isso implica para segurança)

Plataforma de **referência** para agentes de IA em SRE: schemas, ADRs, um
playbook ilustrativo e scripts de análise que rodam sobre CSV fictício. Não há
serviço escutando, não há implantação aqui, não há credencial em nenhum arquivo.

Ainda assim, o **desenho** descrito no README pressupõe um agente que enxerga
telemetria de produção e propõe mudanças. As regras de segurança desse desenho
vivem neste arquivo, porque são parte do contrato — não apêndice de implementação.

## Regras de segurança do desenho (as que valem para qualquer implementação)

1. **Leitura por padrão.** O agente só tem permissão de leitura nos sistemas de
   telemetria e de ticketing. Escrita existe apenas como proposta aprovada por
   identidade humana auditável (ver `schemas/proposta-agente.schema.json`,
   bloco `execucao.aprovacao`).
2. **Nenhuma credencial em payload.** Proposta, contexto e alerta normalizado
   não carregam segredo: o executor injeta credencial em runtime, com escopo
   mínimo e TTL curto. Identidade de serviço nunca pode constar como aprovadora.
3. **Kill switch com propagação medida.** Uma flag desliga a capacidade de
   execução e o tempo de propagação é um número conhecido, não uma promessa.
4. **Limite de taxa de escrita.** O saldo restante da janela aparece **no
   resultado** da proposta, não só no log. Quem aprova precisa ver o freio.
5. **Auditoria append-only.** Decisão de automação não é reescrita: corrige-se
   com evento novo. Retenção mínima declarada no schema (365 dias).
6. **Menor privilégio por ferramenta.** Consultar métrica, ler log, abrir ticket
   e aplicar manifest GitOps são permissões separadas, concedidas separadamente.
7. **Corpus de RAG tem dono e validade.** Um runbook sem `ultimo_teste_em` não
   pode fundamentar uma proposta de escrita. Runbook vencido é vetor de incidente.
8. **Segredo no corpus é falha de projeto.** O índice de runbooks é varrido por
   padrões de token/chave antes de indexar; a ingestão rejeita, não "alerta".

## O que o agente nunca faz (linha defensiva explícita)

- não altera esquema de banco, política de retry de pagamento, chave de
  criptografia, regra de firewall ou credencial de serviço;
- não executa comando arbitrário recebido de texto de alerta ou de ticket —
  entrada de humano é contexto, não instrução;
- não decide sozinho sobre janela de fechamento/liquidação;
- não "empurra" mudança sem diff apresentado ao aprovador;
- não finge que tem evidência: se não há base suficiente, declara
  `nenhuma_fonte_suficiente: true` e escala.

## Reportando uma vulnerabilidade

Não abra issue pública para problema de segurança. Escreva para o endereço
público do mantenedor listado no perfil do repositório, com:

- o arquivo e a linha que você leu;
- o que um atacante ganharia;
- o reproduzo mínimo (uma linha de comando vale mais que três parágrafos).

Resposta-alvo: 3 dias úteis para triagem. Correção sai como commit normal, com
release note; não mantenho canal privado de anúncio.

## Escopo explicitamente fora de segurança

Este repositório **não** trata de: LGPD/PCI-DSS como conformidade (o README
declara onde esses temas aparecem no desenho e onde não aparecem), nem de
hardening de Kubernetes/Terraform — para isso veja os labs do perfil, que são
outros repositórios.
