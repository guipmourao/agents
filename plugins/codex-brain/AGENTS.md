# codex-brain

Manifesto do plugin: `.codex-plugin/plugin.json`. Instalacao: veja
`README.md`.

Plugin de memoria persistente para o Codex: um vault local, com escrita
sempre revisada por transacao (plano -> hash aprovado -> apply), ledgers de
proveniencia de fonte/claim, e uma skill de onboarding que propoe conteudo
em vez de escrever sozinha.

## Regra central

Nenhuma escrita no vault do usuario acontece fora de uma transacao revisada:
plano (dry-run) -> hash aprovado -> apply. Isso vale para toda skill deste
plugin. Nunca editar arquivos do vault diretamente com ferramentas de
escrita genericas.

## Motor

`skills/brain-init/scripts/vault.py` e o ponto de entrada do motor. Resolva
o vault do usuario por `--vault` explicito, `CODEX_BRAIN`, ou
`.codex-brain.json` mais proximo — nunca use a raiz deste plugin como
vault.

```bash
python3 skills/brain-init/scripts/vault.py --help
```

As demais skills (`brain-save`, `brain-ingest`, `brain-query`,
`brain-onboarding`, `brain-new-person`, `brain-new-project`,
`brain-write-like-me`, `brain-assistant`, `brain-loop`) resolvem o mesmo
motor por caminho relativo, assumindo que todas as skills deste plugin
ficam instaladas como irmas (mesmo diretorio pai em `~/.agents/skills/`).

## Estrutura de um vault deste plugin

- `wiki/` — conhecimento gerado (paginas, index, log, hot cache, ledgers).
  Gerenciado exclusivamente via transacao.
- `.raw/` — payloads de fonte imutaveis.
- `wiki/people/` — pessoas e colaboradores.
- `wiki/projects/` — trabalho ativo de longa duracao.
- `wiki/experiments/` — investigacoes curtas, descartaveis.
- `inbox/` — staging visivel para fontes a processar.

## Skills

- `skills/brain-init/` — inicializar ou adotar um vault existente.
- `skills/brain-save/` — persistir um resultado especifico de conversa.
- `skills/brain-ingest/` — transformar uma fonte fornecida em paginas
  conectadas e citadas.
- `skills/brain-query/` — responder usando so o que ja esta no vault
  (read-only).
- `skills/brain-onboarding/` — primeira configuracao: entender o workspace,
  perguntar sobre projetos e pessoas relevantes, e propor ativamente notas
  em `wiki/people/` e `wiki/projects/` a partir do que for encontrado, sempre pedindo
  aprovacao antes de escrever.
- `skills/brain-new-person/` — criar ou atualizar uma nota em `wiki/people/`.
- `skills/brain-new-project/` — criar um projeto ou experimento novo com
  README (e opcionalmente `AGENTS.md` local).
- `skills/brain-write-like-me/` — extrair um perfil de estilo de escrita a
  partir de amostras aprovadas pelo usuario.
- `skills/brain-assistant/` — apoio continuo apos o onboarding: contexto,
  rascunhos, proximos passos.
- `skills/brain-loop/` — desenhar uma checagem recorrente orquestrada por
  um agendador externo (`codex exec` via cron); nunca aplica escrita sem
  supervisao.
