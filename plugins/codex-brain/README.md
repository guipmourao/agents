# codex-brain

Um plugin de memoria persistente para o Codex: vault local com escrita
sempre revisada por transacao, ledgers de proveniencia de fonte/claim, e
skills de onboarding/assistente que propoem conteudo em vez de escrever
sozinhas.

Repositorio: https://github.com/guipmourao/codex-brain

Veja `AGENTS.md` para as regras de uso. Todas as skills seguem o formato
Agent Skills (compativel com Codex CLI, OpenCode, Gemini CLI, Cursor,
Windsurf).

## Skills

| Skill | Faz o quê |
|---|---|
| `brain-init` | Inicializar um vault novo ou adotar um diretorio existente |
| `brain-save` | Persistir um resultado especifico de uma conversa |
| `brain-ingest` | Transformar uma fonte fornecida em paginas conectadas e citadas |
| `brain-query` | Responder usando so o que ja esta no vault (read-only) |
| `brain-onboarding` | Ponto de entrada: inicializa o vault se necessario, entende o workspace, propoe `wiki/people/`/`wiki/projects/` |
| `brain-assistant` | Apoio continuo apos o onboarding: contexto, rascunhos, proximos passos |
| `brain-new-person` | Criar ou atualizar uma nota em `wiki/people/` |
| `brain-new-project` | Bootstrap de um projeto ou experimento novo |
| `brain-write-like-me` | Extrair um perfil de estilo de escrita a partir de amostras aprovadas |
| `brain-loop` | Desenhar uma checagem recorrente orquestrada por um agendador externo |

## Testar o motor sem instalar nada

```bash
python3 skills/brain-init/scripts/vault.py --help
```

## Instalacao

**Importante antes de instalar:** se voce vai usar o Codex Desktop no
Windows, saiba que ele so serve pro lado de leitura (`brain-query`,
dry-runs, o planejamento do `brain-onboarding`). Qualquer escrita real
(`brain-init --apply`, `brain-save`, `brain-ingest`) precisa rodar de
dentro de uma sessao Codex CLI aberta num terminal WSL de verdade — nao
importa onde o vault esteja, o processo do motor precisa estar rodando em
WSL/Linux/macOS. Detalhes e o porque em
`skills/brain-init/docs/windows-wsl.md`.

Achado real de uso: em testes no Codex Desktop, nao apareceu uma
ferramenta nativa de "instalar plugin de marketplace a partir de caminho
local" — o que existe la e uma skill de sistema `skill-installer` que
instala skills individualmente a partir de um repo GitHub. Os dois metodos
abaixo cobrem as duas situacoes.

### Como plugin registrado (recomendado, documentado)

`codex-brain` tem um manifesto valido em `.codex-plugin/plugin.json` e e
publico no GitHub, entao o caminho oficial e registrar direto pelo
`owner/repo`, sem precisar clonar antes:

```bash
codex plugin marketplace add guipmourao/codex-brain
```

Para desenvolver/editar localmente antes de instalar, clone primeiro e
aponte pro caminho local:

```bash
git clone https://github.com/guipmourao/codex-brain.git
codex plugin marketplace add ./codex-brain
```

Alternativa manual, sem CLI: criar/editar
`~/.agents/plugins/marketplace.json` (pessoal) ou
`$REPO_ROOT/.agents/plugins/marketplace.json` (por repositorio) com uma
entrada `source.path` apontando para este diretorio. No Codex desktop, a
instalacao/teste local acontece pela propria interface do app depois que a
marketplace esta configurada.

### Skills soltas, sem plugin (metodo antigo, ainda funciona)

O padrao documentado pelo Agent Skills para descoberta sem manifesto e
`~/.agents/skills`. Em uma instalacao real de Codex desktop no Windows, o
caminho que de fato continha skills pre-existentes era
`%USERPROFILE%\.codex\skills` — os dois sao cobertos abaixo. Isso ainda
funciona, mas nao registra o plugin como unidade (sem versao, sem
`codex plugin marketplace upgrade`).

#### Linux, WSL, macOS

Symlink — mudancas neste repo refletem automaticamente:

```bash
mkdir -p "$HOME/.agents/skills"
for skill in skills/*/; do
  name="$(basename "$skill")"
  ln -s "$(pwd)/$skill" "$HOME/.agents/skills/$name"
done
```

#### Windows — Codex desktop (caminho observado, nao documentado)

Rode do lado do Windows (PowerShell), nao de dentro do WSL — um app Windows
observando (`watch`) um caminho `\\wsl.localhost\...` pode falhar
(`EISDIR`, ver `skills/brain-init/docs/windows-wsl.md`). Use **copia**, nao
symlink: um symlink do Windows apontando para dentro do WSL tem o mesmo
tipo de fragilidade entre filesystems que ja vimos quebrar com o Obsidian
neste projeto.

Mais simples: clonar direto no filesystem do Windows (elimina o problema
de fronteira WSL/Windows por completo, ja que o repo e publico):

```powershell
git clone https://github.com/guipmourao/codex-brain.git "$env:USERPROFILE\codex-brain"
$dest = "$env:USERPROFILE\.codex\skills"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Get-ChildItem -Directory "$env:USERPROFILE\codex-brain\skills" |
  ForEach-Object { Copy-Item -Recurse -Force $_.FullName -Destination $dest }
```

Se preferir copiar a partir do checkout no WSL em vez de clonar de novo no
Windows:

```powershell
$dest = "$env:USERPROFILE\.codex\skills"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Get-ChildItem -Directory "\\wsl.localhost\Ubuntu\home\<usuario>\workspace\projects\codex-brain\skills" |
  ForEach-Object { Copy-Item -Recurse -Force $_.FullName -Destination $dest }
```

Copia significa que o destino **nao** atualiza sozinho quando este repo
muda — repita o comando acima depois de qualquer alteracao nas skills.

## Publicando uma versao nova

Toda vez que `.codex-plugin/plugin.json` mudar de versao, atualize
`.agents/plugins/marketplace.json` junto: o campo `plugins[].version`
(e, se relevante, `description`/`author`/`homepage`) precisa bater com o
`plugin.json`. O Codex Desktop parece ler versao/descricao do catalogo do
marketplace, nao reabrir o `plugin.json` de cada plugin a cada vez — um
`marketplace.json` desatualizado deixa a versao "presa" mesmo depois de
corrigir e commitar o `plugin.json`. Esse padrao (duas copias sincronizadas
dos metadados) foi confirmado observando o marketplace real do
[`wshobson/agents`](https://github.com/wshobson/agents).

## Status

- Motor Python (`skills/brain-init/scripts/`): roda standalone e foi
  verificado (`--help`, mais um dry-run real de `init`, apos a renomeacao
  completa para `codex-brain`).
- `.codex-plugin/plugin.json`: JSON valido, segue a estrutura documentada
  (`name`/`version`/`description`/`author`/`skills`), mas `codex plugin
  marketplace add` nunca foi executado de verdade neste ambiente — nao ha
  Codex CLI instalado aqui para confirmar.
- Instalacao em `~/.agents/skills`: segue o formato `SKILL.md` documentado
  publicamente pelo padrao Agent Skills, mas nao foi testada contra uma
  instalacao real do Codex CLI neste ambiente.
- Instalacao em `%USERPROFILE%\.codex\skills`: os arquivos foram copiados
  e confirmados presentes no destino, mas isso nao prova que o Codex
  desktop os carrega — carregamento real ainda nao foi verificado
  invocando uma skill (por exemplo `brain-onboarding`) numa sessao nova.
