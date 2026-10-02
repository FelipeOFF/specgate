---
name: specgate
description: Prepare contexto e interprete decisões tipadas do Jev para verificar claims, avaliar textos externos e selecionar skills em projetos de software. Use ao integrar ou conduzir esses julgamentos com TypeSafe ou OpenRouter.
---

# Specgate

O Dev no harness reúne evidências e conduz o trabalho. Jev responde perguntas
delimitadas; o código aplica a política de decisão. O harness produz a prosa.

Para uma pergunta pontual, formule `question` e opções `{id, text}` e consulte
`jev_decide` com o contexto autorizado, sem exigir uma execução do fluxo Matt.
Declare `question_type`, `requires_authorization` e `missing_personal_fact`
conforme o pedido original; nunca remova uma restrição para obter uma escolha.
`selected_option` é uma candidata do avaliador, não uma resposta do usuário.
`origin=mock` identifica simulação; `origin=jev` identifica avaliação real;
`origin=policy` identifica uma saída definida pelas restrições, sem julgamento.
Preserve `collect`, `review`, `error`, motivos da política e referências à avaliação.
O cliente compartilha até três rodadas entre lacunas locais e semânticas e
interrompe a coleta sem informação nova. Um manifesto de calibração ausente não desliga o Jev. `verified` em
`jev_verify` é um veredito usável depois da confirmação de quem mantém.
`auto_advance` false só impede execução silenciosa.

O piloto local disponibiliza `jev_verify`, `jev_screen` e `jev_find` via MCP
autenticado, com mock heurístico. Confirme que as tools estão conectadas no harness.
Não apresente simulação como chamada real ao Jev. Os adapters de harness aplicam
somente escolhas autorizadas pelos mesmos gates deste fluxo.

No Grok Bot, esta é uma skill privada cooperativa: anexe também o Custom MCP
connector `specgate` e consulte `jev_decide` antes de encaminhar uma pergunta
pontual ao usuário. Aplique a opção somente quando o resultado trouxer
`action=auto`, `origin=automated`, calibração real e nenhuma autorização pendente.
Mock, falha técnica, contexto insuficiente, pergunta aberta ou autorização mantêm
a revisão humana. O produto não publica um contrato para interceptar toda pergunta;
não descreva esta skill como hook obrigatório. Nunca envie conversas, sessões ou
transcripts do Bot ao MCP.

No Cursor, hooks que bloqueiam
ou alteram argumentos não respondem `cursor/ask_question`; quando essa garantia
for necessária, inicie o turno pelo cliente Python ACP controlado.
No Grok Build, hooks também não substituem a resposta anterior à pergunta.
Quando essa garantia for necessária, inicie `grok agent stdio` pelo cliente
Python controlado e responda somente `x.ai/ask_user_question` pelo contrato ACP.

Leia apenas a referência necessária à tarefa:

- Para coletar contexto ou formular perguntas, leia
  [Contexto e perguntas](references/jev-context-and-questions.md).
- Para verificar claims, filtrar material ou selecionar skills, leia
  [Receitas de decisão](references/jev-recipes.md).
- Para integrar Python/OpenRouter, interpretar confiança ou validar resultados,
  leia [Contratos e avaliação](references/jev-contracts-and-evaluation.md).

Preserve IDs públicos e rejeite duplicatas antes de construir mapas. Evidências
ausentes ou contraditórias precisam permanecer visíveis. Confiança alta não
substitui contexto suficiente nem autorização para executar uma ação.

Neste projeto, o avanço automático permanece desabilitado até a calibração dos
gates com casos representativos. Siga o limite de três rodadas de coleta descrito
na referência de contexto; encaminhe lacunas persistentes para revisão humana.
Os 60 casos do piloto rodam exclusivamente com mock; reutilize as respostas reais
gravadas sem fazer novas chamadas pagas em testes rotineiros.

Use apenas dados autorizados da tarefa. Exclua segredos e identificadores,
caminhos ou transcripts de sessões dos harnesses do contexto transmitido.

Ao apresentar uma decisão, informe o veredito, as evidências que o sustentam e
as lacunas relevantes. Distinga resposta real, resultado gravado e simulação
heurística. Políticas e thresholds pertencem ao projeto; exemplos da documentação
não definem os valores de produção.

No checkout do projeto, `scripts/dev-decision-local.sh review <pedido.json>` executa
pedido → fontes explícitas → contexto → MCP → revisão humana. O JSON informa
`objective`, `tool`, `arguments`, `sources` e `required`; `source_rounds` fornece
fontes adicionais para até duas rodadas seguintes. Sem evidência suficiente, o
cliente retorna as lacunas antes de consultar o servidor.

Para roteamento, descubra o catálogo das roots autorizadas, consulte `jev_find` e
leia o `SKILL.md` completo do candidato antes de considerar seu uso. `absent` não
autoriza escolher o primeiro colocado. Resultados do mock exigem revisão.

O cliente Python expõe `specgate.routing.route_skills`: recebe um
`ReviewRequest` de `jev_find`, `authorized_roots` do harness e `disabled_ids`.
`request_context` pode fornecer novas fontes conforme as lacunas. O catálogo é
avaliado em lotes de até 50 candidatos, dentro do orçamento de bytes; cada lote
contribui seu `top` para uma shortlist. Se as instruções de uma candidata
reprovarem, o cliente tenta a próxima. Após confirmar adequação, examina somente
as empatadas na avaliação da descrição. `coverage` identifica o que foi examinado,
desativado, rejeitado ou ficou fora da shortlist; não infira incompatibilidade de
IDs não examinados. Nenhum conteúdo é truncado para caber em uma chamada.
`transport`, `ca_file`, `timeout_seconds` e `progress_callback` são encaminhados
ao mesmo cliente MCP usado pelas demais avaliações.

As instruções integrais da shortlist passam por `jev_screen` e por adequação
em `jev_find`, com o contexto e as regras do projeto. `evaluations` preserva os
resultados e a política de cada etapa. Ausência, empate ou pendência exige
revisão; uma indicação em `candidate` também permanece em revisão, salvo sob
manifesto validado ou, sem manifesto, quando a skill é pública
(`candidate.public`, `gate_basis=confidence_policy`, sem calibração). O catálogo
pessoal ou do projeto exige um escopo separado. O mock não prova compatibilidade
semântica.

O adapter deve carregar `candidate.instructions` e chamar
`confirm_skill_loaded(indication, skill_id, revision, authorized_roots=...,
disabled_ids=...)` com o ID e a revisão efetivamente carregados. A função
reconfere autorização, origem e revisão do arquivo. `loaded=true` registra apenas
essa confirmação explícita; não significa execução nem autorização automática.
Sem `gate_basis=validated_manifest`, `loaded` só é registrado com
`hook=NativeHook(...)`, montado pelo handler do hook nativo do harness quando ele
está instalado e executou naquele carregamento. Uma indicação em revisão
(`origin=review`, que não prova revisão) também carrega com
`review=HumanReview(skill_id, revision)`, montada só quando uma pessoa escolheu
aquela skill e revisão; ela não substitui o hook numa seleção automática. Sem essa
evidência `loaded` não é registrado e `loading` diz o motivo.
