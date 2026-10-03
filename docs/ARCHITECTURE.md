# Spotify → Deezer: Arquitetura

> Status: revisão 2, aprovada. PoC local (resolver, matcher, adapter) implementada e validada com download real em 2026-10-02. Próximas etapas e contratos: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
> Pesquisa feita em 2026-10-02 contra `Lunatic16/deezload@0cb3919` e `bambanah/deemix@9e572a4`.

---


> **Revisão 2 (2026-10-02):** o alvo deixou de ser `localhost` e virou um **servidor pequeno para poucos usuários (você e 1 amigo, por enquanto)**, na Oracle Cloud Free Tier. As músicas são entregues **por download no dispositivo** (arquivo ou `.zip`). O **ARL do dono é o padrão**, e cada usuário pode cadastrar o próprio. O frontend é **HTML + JS puro**.

## 0. TL;DR

- **Servidor web pequeno**: FastAPI (Python) + página HTML/JS sem build, atrás do **Caddy** (HTTPS automático + login). Tudo num `docker compose` com 2 containers numa VM ARM da Oracle. Sem Electron, sem fila externa, sem Redis.
- **Login pelo Caddy (basic auth)**: o app não implementa senha, cadastro nem sessão. O Caddy autentica e repassa o nome do usuário ao app num header. Para 2–5 amigos isso basta (detalhes em §7).
- **Isolamento por usuário no backend**: cada job pertence a um usuário; a API só mostra e serve os jobs e arquivos de quem pediu.
- **Entrega**: o servidor baixa para `/data/jobs/{job}/` e o usuário baixa a track avulsa ou o `.zip` (álbum/playlist). Os arquivos são apagados após algumas horas.
- **Spotify só como fonte de metadados**, lido **anonimamente** das páginas `embed` + `oEmbed`. A Web API oficial não serve mais como base (§2–3). Playlists ficam **limitadas a 100 tracks no MVP**; o OAuth do Spotify entra na v2 (já previsto em §11).
- **Consequência central:** na prática **não teremos ISRC**. O matcher precisa ser bom sem ISRC (título + artista + duração, e match de álbum inteiro por posição). O ISRC fica como caminho opcional.
- **Deezload importado como módulo** (submódulo git, sem modificar). Uma instância de `DeezerDownloader` por ARL em uso.
- **Nada do código do Deemix é reaproveitado diretamente.** Só as ideias.

---

## 1. Análise do Deezload

Um único arquivo `deezload.py` (1970 linhas). As dependências são `deezer-py`, `requests`, `pycryptodome` e `mutagen`; `textual` aparece no requirements mas não é usado pelo script.

### 1.1 Arquitetura interna

| Parte | O que faz | Reutilizável? |
|---|---|---|
| `AudioQuality` (enum) + `QUALITY_FALLBACK_ORDER` | FLAC / MP3_320 / MP3_128, com fallback para baixo | Sim |
| `log()` + globais `_VERBOSE/_QUIET/_COLOR_ENABLED/_LOG_FILE_HANDLE` | Logging por `print`, controlado por globais de módulo | Acoplado à CLI. Contornamos (ver §5.3) |
| `DeezerDownloader.__init__(arl, quality, concurrency, min_quality, max_retries, fetch_lyrics)` | Estado: sessão `requests`, cliente `deezer-py`, `stats` com lock | Sim |
| `authenticate()` | `deezer.Deezer().login_via_arl(arl)` | Sim |
| `get_track_info()` | `gw.get_track` → fallback `api.deezer.com/track/{id}` | Sim |
| `get_download_url()` / `_resolve_url_for_quality()` / `_url_is_available()` / `_construct_encrypted_url()` | Token exchange do `deezer-py`, fallback de URL criptografada, verificação com GET ranged e fallback de qualidade | Sim (interno) |
| `download_track(track_id, output_dir, track_num, disc_num, progress_cb)` | **Unidade principal**: info → URL → arquivo `.part` → retry/resume → tags → rename atômico | **Sim, é o ponto de integração** |
| `_download_stream()` | Stream em blocos de 2048 bytes, Blowfish a cada 3º bloco, resume com Range, tratamento de 416, verificação de tamanho | Sim (interno) |
| `_add_tags()` | ID3/Vorbis, capa 1000px, letras USLT/SYLT/.lrc, gênero | Sim (interno) |
| `download_album/playlist/artist()` | Recebem **IDs Deezer** e pegam a lista na API do Deezer | **Não servem**: a lista vem do Spotify |
| `_download_track_list()` | ThreadPool + status via `sys.stdout.write("\r…")` | **Não**: acoplado ao terminal |
| `resolve_deezer_url`, `extract_id_and_type`, `search_and_select` | Utilitários de CLI (`search_and_select` chama `input()`) | Não |
| `load_config/save_config/get_config_path` | INI em `~/.config/deezload/` | Só para **importar o ARL existente** no primeiro uso |
| `main()` | argparse | Não |

### 1.2 Pontos específicos

- **Autenticação:** só ARL (cookie). A precedência na CLI é `--arl` > `DEEZLOAD_ARL` > INI. O `deezer-py` mantém a sessão. Não há re-login automático se o ARL expirar no meio de uma execução.
- **Resolução de URLs:** só do Deezer. Irrelevante para nós.
- **Search:** API pública `api.deezer.com/search/*`, interativa. Não serve para matching (não tem score).
- **Download, retries e resume:** é a parte mais sólida. Faz `max_retries` tentativas com backoff 2^n (máximo 30 s) e resume alinhado ao bloco. Um `.part` é mantido em caso de falha. Arquivo já existente → é pulado.
- **Concorrência:** `ThreadPoolExecutor` dentro de `_download_track_list`. `download_track` em si é thread-safe o bastante: `stats` tem lock e a sessão `requests` é compartilhada, como o próprio Deezload já faz no modo concorrente.
- **Progresso:** `progress_cb(pct: float)` por track. **Sem bytes nem velocidade** no callback.
- **Metadata:** vem toda do Deezer (não precisamos taguear a partir do Spotify, o que é bom).
- **Erros:** `download_track` **nunca levanta exceção**: retorna `None` e registra o motivo via `log()`. Qualquer exceção que não seja de rede no loop de retry encerra as tentativas e retorna `None`.
- **Cancelamento:** não existe. Porém uma exceção levantada **dentro de `progress_cb`** sai do loop de retry pelo ramo "non-network error → break", mantém o `.part` e retorna `None`. **Dá para cancelar no meio da track sem tocar no código.**
- **Import:** `import deezload` não tem efeitos colaterais (testado: só imports, mais um `print` de aviso se o `mutagen` faltar). Um `sys.exit(1)` acontece se `deezer-py`/`requests`/`pycryptodome` faltarem; o nosso requirements garante essas dependências.
- **Licença:** o repositório **não tem arquivo LICENSE**. Por isso **não copiamos o arquivo** para dentro do nosso repo: ele entra como **submódulo git** apontando para o upstream. Para uso pessoal não há problema, mas isso impede redistribuir uma cópia.

### 1.3 Conclusão sobre o Deezload

O núcleo (`DeezerDownloader` + `download_track`) é **suficientemente desacoplado para import direto**. O acoplamento com a CLI está em `log()`/`print`, no loop de lista e no `main`, e nenhuma dessas partes é necessária para nós. **Não precisamos de fork nem de refactor.**

---

## 2. Análise do Deemix (integração Spotify)

O código está em `packages/deemix/src/plugins/spotify.ts` (878 linhas).

### 2.1 O que o plugin faz

1. **Parse de URL:** resolve `link.tospotify.com`, corta `?`/`&`/`/` final e usa a regex `[/:](track|album|playlist)[/:](.+)`. Aceita `open.spotify.com/...` e `spotify:track:...`.
2. **Track:** `sp.tracks.get(id)` → `external_ids.isrc` (tira o `-`) → Deezer `track/isrc:{ISRC}`.
3. **Album:** `sp.albums.get(id)` → `external_ids.upc` → Deezer `album/upc:{UPC}`. **Não há fallback**: se o UPC não estiver no Deezer, o álbum falha.
4. **Playlist:** `sp.playlists.getPlaylist` (com retry `market=US` no 404) → paginação. Se falhar, raspa o HTML da página, o `get_access_token` do web player e a página **embed**. Depois converte track a track numa fila `async` (ISRC, e se habilitado "fallbackSearch").
5. **Fallback por metadados** (`deezer-sdk/api.ts:get_track_id_from_metadata`):
   `artist+track+album` → `artist+track` → título sem `(...)` → título antes de ` - `. **Pega sempre `data[0]` sem nenhum score.** Não compara duração.
6. **Cache** em JSON: `spotify_id → {isrc, id, data}`.

### 2.2 O que está quebrado ou é frágil (verificado)

| Problema | Evidência |
|---|---|
| **Spotify Web API, mudanças de fev/2026:** em apps Development Mode foram **removidos `external_ids` (ISRC/UPC)** de tracks e álbuns, o batch `GET /tracks` e `GET /playlists/{id}/tracks` (virou `/items`). Os itens de playlist **só aparecem para playlists do próprio usuário ou colaborativas**. Dev Mode exige Premium; search limitado a 10 resultados. | [Migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide), [TechCrunch](https://techcrunch.com/2026/02/06/spotify-changes-developer-mode-api-to-require-premium-accounts-limits-test-users/) |
| Por isso o caminho principal do Deemix (ISRC/UPC via Web API) **não funciona em apps novos**. O PR de correção [#340](https://github.com/bambanah/deemix/pull/340) (OAuth + `/items`) **continua aberto** em set/2026. | [#340](https://github.com/bambanah/deemix/pull/340), [#293](https://github.com/bambanah/deemix/issues/293), [#177](https://github.com/bambanah/deemix/issues/177) |
| `open.spotify.com/get_access_token` (token anônimo do web player) **não funciona mais** (exige TOTP). O Deemix ainda chama esse endpoint. | [librespot #1475](https://github.com/librespot-org/librespot/issues/1475) |
| **O filtro `artist:"..."` da busca avançada do Deezer retorna 0 resultados hoje.** `track:"..."` e a busca em texto livre funcionam. Testado ao vivo em 2026-10-02. Isso quebra os 2 primeiros passos do `get_track_id_from_metadata`. | Teste manual. Relacionado: [#364 "ISRC Fallback doesn't work"](https://github.com/bambanah/deemix/issues/364) |
| `data[0]` sem score → baixa **a música errada** em silêncio. | [#276](https://github.com/bambanah/deemix/issues/276) |
| Bug: `try { return generateTrackItem(...) } catch {}` sem `await`, então a rejeição da promise não é capturada e o fallback nunca roda para tracks avulsas. | `spotify.ts:120-126` |
| `isrc.replace("-", "")` só remove o primeiro `-`. | `spotify.ts:547` |
| Track não encontrada vira um objeto falso com `id: "0"`. Daí vem o erro `Cannot read properties of undefined (reading 'mp3_320')`. | [#295](https://github.com/bambanah/deemix/issues/295) |

### 2.3 O que ainda é válido no Deemix

- A **ordem de prioridade** ISRC → metadados (com score, que lá não existe).
- A normalização (`–`→`-`, `’`→`'`) e a ideia de **tentar o título sem a parte de versão**.
- **Raspar a página embed** como fallback anônimo. É o que funciona hoje, e virou o nosso caminho principal.
- O **cache** `spotify_id → deezer_id`.
- Track "não encontrada" vira um **estado explícito**, não um objeto falso.

---

## 3. Estado atual do Spotify como fonte (testado em 2026-10-02)

| Fonte | Auth | Dá ISRC? | Track | Álbum | Playlist | Observação |
|---|---|---|---|---|---|---|
| `open.spotify.com/embed/{type}/{id}` (`__NEXT_DATA__`) | nenhuma | **não** | título, artistas[], duração ms | título, lista de tracks (título, "artistas" como string, duração), ordem | nome, capa, lista | **Playlist limitada a 100 tracks** (observado). É scraping, então pode quebrar. |
| `open.spotify.com/oembed?url=…` | nenhuma | não | título, thumbnail | idem | idem | Endpoint público e estável. Usado para nome e capa. |
| Web API, client credentials (Dev Mode) | client id/secret | **não** em apps novos | sim | sim | **não** (itens só de playlist própria) | Pode ainda trazer `external_ids` em apps antigos (as mudanças de endpoint foram adiadas para integrações existentes) |
| Web API + OAuth do usuário (Dev Mode) | Premium + app | não | sim | sim | só playlists próprias/colaborativas | Sem limite de 100 tracks |
| Web API, Extended Quota | — | sim | — | — | — | Inacessível para uso pessoal |

Decisão: o **Embed** é o resolver padrão. A Web API fica como **enriquecimento opcional na v2** (ver §9, decisão D3, e §11).

Pelo embed, a lista de um álbum não traz o nome do álbum por track (está no nível da entidade) nem o disco. A posição vem da ordem. A lista de uma playlist **não traz o álbum** de cada track.

---


## 4. Arquitetura recomendada

```
Browser (amigo / você)
   │  HTTPS, basic auth
   ▼
Caddy  ── TLS automático (Let's Encrypt), basic_auth com 1 usuário por pessoa,
   │       repassa X-Remote-User: <nome>, remove qualquer X-Remote-User vindo de fora
   ▼  rede interna do docker (o app NÃO publica porta)
FastAPI  ── app/backend/api.py  (rotas finas; usuário = header X-Remote-User)
   │
   ├── spotify/resolver.py   URL → SpotifyCollection{meta, tracks: [TrackCandidate]}
   ├── matching/matcher.py   TrackCandidate → MatchResult{status, deezer_track, score, alternatives}
   │        └── deezer/api.py   API pública do Deezer + rate limit global
   ├── users.py              preferências por usuário + ARL próprio (criptografado)
   ├── jobs.py               jobs em memória por usuário; fila global de downloads; zip; limpeza
   └── downloads/adapter.py  DeezloadAdapter (1 DeezerDownloader por ARL)
            └── vendor/deezload/deezload.py   (submódulo git, sem modificar)

Volume /data:
  users/<nome>.json        preferências + ARL criptografado (AES-GCM)
  match_cache.json         spotify_id → deezer_id (compartilhado)
  jobs/<job_id>/...        arquivos temporários + .zip
Segredos (env / .env, fora do git): APP_SECRET_KEY, DEFAULT_ARL
```

Um único processo Python. Os jobs ficam em memória (um restart perde os jobs em andamento; para esse uso isso é aceitável). **Não há banco**: os dados persistentes são só os JSONs de usuário e o cache.

### 4.1 Componentes

**`spotify/resolver.py`, `SpotifyResolver`**
- `parse_url(url) -> (kind, id)`: aceita `open.spotify.com/[intl-xx/]{track|album|playlist}/{id}`, `spotify:{kind}:{id}` e `spotify.link`/`link.tospotify.com` (segue redirect só para hosts do Spotify, para evitar SSRF). Ignora query string.
- `resolve(url) -> SpotifyCollection`, onde `SpotifyCollection{kind, id, title, subtitle, cover_url, tracks: list[TrackCandidate], truncated: bool}`.
- Separa artistas da string `subtitle` (separador `, `).
- `truncated = True` quando a playlist chega com 100 itens.

```python
@dataclass(frozen=True)
class TrackCandidate:
    spotify_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None        # conhecido p/ track e album; None em playlist (embed)
    duration_ms: int
    isrc: str | None         # quase sempre None (ver §3)
    track_number: int | None # posição no álbum (1..N), só p/ album
    disc_number: int | None  # None via embed
```

**`deezer/api.py`, `DeezerPublicApi`**: `track_by_isrc`, `search_tracks`, `search_albums`, `album`, `album_tracks`. Um **rate limiter global** (o IP do servidor é um só para todos os usuários) e retry em 429/5xx/quota. Não usa ARL.

**`matching/matcher.py`, `DeezerMatcher`.** Detalhado em §6.

**`downloads/adapter.py`, `DeezloadAdapter`.** Detalhado em §5.

**`users.py`**
- `get_prefs(user)` / `set_prefs(user, …)`: `{quality, concurrency, arl_enc?}` em `/data/users/<user>.json`.
- `arl_for(user)`: **o caminho padrão é sempre `DEFAULT_ARL`**. Se o usuário tiver salvo um ARL próprio, ele sobrescreve o default só para aquele usuário. Não há onboarding, rotação, administração de contas nem políticas: é um campo opcional na UI e um `if` no código.
- O ARL salvo em disco é criptografado com **AES-GCM** (`pycryptodome`, que já é dependência do Deezload), com a chave derivada de `APP_SECRET_KEY` por SHA-256. São duas funções, `encrypt(str)->str` e `decrypt(str)->str`, com umas 15 linhas. Se a descriptografia falhar (chave trocada), o ARL próprio é ignorado com um aviso no log e o usuário volta para o default.

**`jobs.py`, `JobManager`**
- `jobs: dict[job_id, Job]`; cada `Job` tem `owner`. **Toda leitura filtra por `owner == usuário da requisição`**; um job de outro usuário responde 404, não 403.
- Um **job ativo por usuário**. Um novo analyze substitui o anterior se ele não estiver baixando.
- **Limite global de downloads simultâneos** (`MAX_PARALLEL_DOWNLOADS`, padrão 4) via semáforo compartilhado. A concorrência do usuário é limitada por ele.
- No fim do download, monta a entrega: 1 arquivo → serve direto; vários → `.zip` com `ZIP_STORED` (FLAC/MP3 já são comprimidos, então zip "sem compressão" é rápido e quase do mesmo tamanho).
- **Limpeza:** uma thread apaga `jobs/<id>` com mais de `JOB_TTL_HOURS` (padrão 6); no startup apaga tudo. Recusa novos downloads se o disco livre ficar abaixo de `MIN_FREE_GB`.

**`frontend/`**: `index.html` + `app.js` + `style.css`, servidos como estáticos pelo FastAPI. Sem framework e sem build. Não há tela de login (o navegador mostra o prompt do basic auth).

### 4.2 API HTTP

Resumo abaixo; **o contrato detalhado (corpos, erros, schema do snapshot) está em [IMPLEMENTATION_PLAN.md §4](IMPLEMENTATION_PLAN.md) e prevalece**.

Todas as rotas exigem o header `X-Remote-User` (injetado pelo Caddy). Todo POST/PUT exige `X-Requested-With: spot2deez` (§7).

| Método | Rota | Corpo / Resposta |
|---|---|---|
| GET | `/api/me` | `{user, quality, concurrency, own_arl: bool}`; **nunca devolve ARL** |
| PUT | `/api/me` | `{quality?, concurrency?, arl? \| arl: null}`; o ARL é só escrita, e `null` remove o ARL próprio |
| POST | `/api/analyze` | `{url}` → `{job_id}` |
| GET | `/api/job` | snapshot do job atual do usuário: meta, itens (`status`, `score`, `deezer`, `alternatives`), progresso, `download_url` quando estiver pronto |
| POST | `/api/job/items/{i}/choose` | `{deezer_id \| null}` |
| POST | `/api/job/download` | inicia |
| POST | `/api/job/cancel` | — |
| GET | `/api/job/file` | o arquivo ou `.zip` (`Content-Disposition: attachment`), só para o dono |

Progresso por **polling de 1 s** do snapshot. Para 2–5 usuários, isso é desprezível.

### 4.3 Fluxo completo

1. O usuário abre `https://seu-dominio`; o Caddy pede usuário e senha; o navegador lembra.
2. Cola a URL e clica em **Analyze** → resolve no Spotify + match no Deezer (como na v1).
3. A UI mostra ✓ / ! / ✕; o usuário resolve os ambíguos.
4. Escolhe a qualidade e clica em **Download** → `JobManager` monta `DownloadItem`s com destino `/data/jobs/<id>/<Nome>/…` (mesma organização de pastas da v1: álbum com número de track, playlist com `.m3u8`).
5. O adapter pega o `DeezerDownloader` do ARL desse usuário, faz login se for preciso, e baixa respeitando a concorrência do usuário **e** o semáforo global.
6. Ao terminar: zip → `download_url` aparece no snapshot → o navegador baixa.
7. Cancel funciona como na v1. Depois de `JOB_TTL_HOURS` os arquivos somem.

## 5. Integração com o Deezload

### 5.1 Import direto ou subprocess

| | Import (A) | Subprocess (B) |
|---|---|---|
| Progresso por track | `progress_cb` nativo | Parse de texto colorido de `\r` (frágil) |
| Cancelamento | `progress_cb` levanta exceção, aborta limpo | `kill` no processo; pode deixar `.part`, que já é ok |
| Erro por track | Captura de `log()` (ver 5.3) | Parse de stdout |
| Lista de tracks arbitrária | `download_track(id)` em loop | 1 processo por track (relogin a cada vez) |
| Isolamento de falha | Mesmo processo | Melhor |

**Escolhemos (A), import direto.** O contrato de `download_track` é simples e estável, e o subprocess forçaria um login por track ou passar a lista por `--track-id` uma de cada vez.

### 5.2 Interface do adapter

```python
class DeezloadAdapter:
    def __init__(self, global_slots: threading.Semaphore): ...
    def run(self, arl: str, quality: AudioQuality, concurrency: int,
            items: list[DownloadItem], on_update, cancel: threading.Event) -> None
    # levanta AuthError se o ARL não logar
```

Por dentro, o adapter mantém um **pool de `DeezerDownloader` com a chave `(sha256(arl), quality)`**. A qualidade fica num atributo da instância (`self.quality`, lido em `get_download_url`), então dois usuários com o mesmo ARL padrão e qualidades diferentes **não podem** compartilhar uma instância sem condição de corrida. Cada instância faz login uma vez e é descartada após 1 h sem uso. Cada `download_track` ocupa uma vaga do semáforo global.

Não há `downloadAlbum` nem `downloadPlaylist` no adapter. Ao contrário do sugerido, **álbum e playlist do Spotify são só listas de tracks já resolvidas**, e a organização de pastas é responsabilidade do `JobManager`. Reaproveitar `download_album` do Deezload só faria sentido com o álbum do Deezer inteiro, o que daria um resultado diferente do álbum do Spotify (edições deluxe etc.). `setOutputDirectory` vira um parâmetro de cada `DownloadItem`.

### 5.3 Como contornar o acoplamento sem editar o arquivo

- `deezload._QUIET = True` desliga os `print` de info e as barras de progresso.
- `deezload.log` é substituído por uma função nossa que manda para `logging` e guarda a **última mensagem warn/error por thread** (`threading.local`). Assim o item que falhou mostra na UI o motivo real ("No available quality…", "Could not get track info…"). É um *monkeypatch* de um nome de módulo, que é aceitável e fica documentado como ponto de quebra em updates.
- Fallback de qualidade é detectado pela extensão do arquivo retornado (`.mp3` quando foi pedido FLAC).
- O ARL nunca é logado. O Deezload também não o loga, só o nome e o ID do usuário.

### 5.4 Vendoring

`git submodule add https://github.com/Lunatic16/deezload vendor/deezload`, fixado num commit. O adapter faz `sys.path.insert(0, vendor/deezload)`. Para atualizar basta `git submodule update --remote`, rodar os testes do adapter e fazer commit do novo ponteiro.

---

## 6. Algoritmo de matching

### 6.1 Normalização

`norm(s)`: NFKC → casefold → remove acentos → `–—`→`-`, `’`→`'` → remove `feat./ft./with …` → colapsa espaços e pontuação.
`split_version(title) -> (base, tags)`: extrai `(…)`, `[…]` e ` - …`. `tags` é um conjunto normalizado entre {remix, live, acoustic, instrumental, remaster, radio edit, extended, demo, version, edit, mono, stereo, sped up, slowed, …}. `remaster`/`remastered YYYY` conta como **tag neutra**, porque não muda a música.

### 6.2 Match de uma track

1. **ISRC** (se houver): `track/isrc:X` → MATCHED, score 1.0. Sanity check: |Δduração| ≤ 10 s; se falhar, vira AMBIGUOUS.
2. **Candidatos** (união, dedupe por id, cerca de 25 itens):
   - `search?q={artist1} {base_title}` (texto livre, que **funciona**)
   - `search?q=track:"{base_title}"`, que só é útil quando o artista é muito genérico
   - **não** usar `artist:"..."` (quebrado hoje; ver §2.2)
3. **Score** de cada candidato, de 0 a 1:
   - título `0.45`: `rapidfuzz.token_set_ratio(base_sp, base_dz)/100`
   - artista `0.30`: o melhor `ratio` entre qualquer artista do Spotify e o artista principal do Deezer (+ `contributors` quando disponível)
   - duração `0.20`: Δ≤2 s → 1.0; ≤5 s → 0.7; ≤10 s → 0.3; senão 0
   - álbum `0.05`: `token_set_ratio` se o álbum for conhecido; senão o peso é redistribuído ao título
   - **penalidade −0.25** se os conjuntos de *tags não-neutras* forem diferentes (por exemplo, "Live" de um lado só). É isso que evita trocar o original pelo remix.
4. **Decisão:**
   - agrupa candidatos **equivalentes** (mesmo título base, mesmo artista e Δduração ≤ 2 s; é a mesma gravação no single e no álbum) e escolhe o representante pelo álbum mais parecido, com o menor id como desempate
   - `top ≥ 0.85` **e** (só um grupo ≥ 0.70 **ou** margem para o 2º grupo ≥ 0.10) → **MATCHED**
   - `top ≥ 0.60` → **AMBIGUOUS**, com os 3 melhores grupos como `alternatives`
   - senão → **NOT_FOUND**
5. O resultado vai para o cache por `spotify_id`. Escolhas manuais **também** vão para o cache e têm prioridade.

Os limiares são calibrados com um conjunto de testes fixo ([IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), etapa 8b), não no chute.

### 6.3 Match de álbum inteiro (só para URLs de álbum)

É mais robusto que track a track, porque a posição e a duração de todas as tracks se confirmam mutuamente.

1. `search/album?q={artist} {album}` → candidatos.
2. Para cada candidato: `album/{id}/tracks` e o score do álbum é `0.4·sim(título) + 0.2·sim(artista) + 0.4·(fração de posições i com |Δdur_i| ≤ 3 s e sim(título_i) ≥ 0.8)`.
3. Se o melhor score for ≥ 0.85, cada track recebe MATCHED com o id na mesma posição, desde que o par individual (título e duração) também passe. Se o nº de tracks for diferente (por exemplo, edição deluxe), só as posições que batem são aceitas.
4. Tracks que não casaram por posição (deluxe com extras, por exemplo) passam pelo §6.2 individualmente.

---


## 7. Autenticação, segredos e segurança

### 7.1 Login via Caddy

```caddyfile
musica.seudominio.duckdns.org {
    basic_auth {
        voce  $2a$14$...hash...     # gerado com: docker compose exec caddy caddy hash-password
        amigo $2a$14$...hash...
    }
    request_header -X-Remote-User                 # descarta qualquer valor vindo do cliente
    reverse_proxy app:8000 {
        header_up X-Remote-User {http.auth.user.id}
    }
}
```

- O app **só é acessível pela rede interna do Docker**: o compose não publica a porta 8000. Por isso o header `X-Remote-User` é confiável.
- Para adicionar um amigo: gerar o hash, acrescentar 1 linha e rodar `docker compose exec caddy caddy reload`.
- Limitações conhecidas: o prompt do navegador é feio e não há "logout" de verdade. Para 2–5 pessoas é aceitável. Se incomodar, troca-se por uma tela de login própria (contas em JSON + cookie de sessão) sem mexer no resto, porque o app só depende de "quem é o usuário".

### 7.2 CSRF

O navegador reenvia as credenciais do basic auth automaticamente, inclusive em requisições disparadas por outros sites. Por isso **todo POST/PUT exige o header `X-Requested-With: spot2deez`**. Um site de terceiros não consegue enviar esse header sem preflight CORS, e o app não responde ao preflight. `GET /api/job/file` só lê dados do próprio usuário, então não precisa disso.

### 7.3 Segredos

| Segredo | Onde fica | Nunca vai para |
|---|---|---|
| `DEFAULT_ARL` (o seu) | `.env` no servidor (`chmod 600`), fora do git | frontend, log, `/data` |
| ARL próprio de cada usuário | `/data/users/<user>.json`, criptografado com AES-GCM | frontend (só `own_arl: bool`), log |
| `APP_SECRET_KEY` (32 bytes aleatórios) | `.env` | git |
| Senhas | só o hash bcrypt no `Caddyfile` | — |

Um `logging.Filter` mascara qualquer ARL conhecido que apareça numa mensagem de log.

### 7.4 Outros cuidados

- **Path traversal:** os nomes de arquivo vêm de metadados (Spotify/Deezer). O Deezload já usa `sanitise_filename`. Além disso, o app valida que todo caminho servido está dentro de `/data/jobs/<job_id>/`.
- **SSRF:** o resolver só faz requisições para `open.spotify.com`, `spotify.link` e `link.tospotify.com`, e só segue redirect para esses hosts. Requisições para o Deezer só vão para `api.deezer.com`.
- **Abuso de disco:** TTL, `MIN_FREE_GB` e um job ativo por usuário.

---

## 8. Deploy (Oracle Cloud Free Tier)

**Máquina:** VM **Ampere A1 (ARM64)**, Ubuntu, no plano *Always Free* (até 4 OCPU / 24 GB de RAM; 1 OCPU / 6 GB já sobra). Disco: os 200 GB de block storage grátis são suficientes, porque os arquivos são temporários. Tráfego: 10 TB/mês de saída grátis.

**Compatível com ARM:** `python:3.13-slim` e `caddy:2` são multi-arch, e `pycryptodome`, `rapidfuzz` e `mutagen` têm wheels `aarch64`. O build roda **na própria VM** (`docker compose up -d --build`), então não há cross-compile.

```yaml
# docker-compose.yml (esboço)
services:
  app:
    build: .
    env_file: .env
    volumes: ["./data:/data"]
    restart: unless-stopped        # sem "ports:", só acessível pelo caddy
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes: ["./Caddyfile:/etc/caddy/Caddyfile", "caddy_data:/data"]
    restart: unless-stopped
volumes: { caddy_data: {} }
```

**Passo a passo resumido** (vai detalhado no README):
1. Criar a conta Oracle e escolher a **home region** (não dá para mudar depois). Escolha um país onde o Deezer funciona e, de preferência, o mesmo país das contas Deezer (ex.: São Paulo).
2. Criar a VM A1. Se aparecer "Out of host capacity", tentar outro *availability domain* ou outro horário.
3. Liberar as portas 80 e 443 **em dois lugares**: na *Security List* da VCN **e** no `iptables` da imagem Ubuntu da Oracle, que vem bloqueando tudo exceto SSH. Esse é o erro mais comum.
4. Domínio grátis: um subdomínio **DuckDNS** apontando para o IP público, para o Caddy emitir o certificado.
5. Instalar Docker; `git clone --recursive`; criar `.env` e `Caddyfile`; `docker compose up -d --build`.
6. **Smoke test antes de qualquer outra coisa:** baixar 1 track com o seu ARL **a partir da VM**. Isso valida o risco do IP de datacenter (§10).
7. Atualizar: `git pull --recurse-submodules && docker compose up -d --build`.

**Recuperação de VMs ociosas:** a Oracle pode recuperar instâncias Always Free ociosas (CPU/rede/memória muito baixas por 7 dias). Para evitar, converta a conta para *Pay As You Go*: os recursos Always Free continuam gratuitos, e com isso a recuperação por ociosidade não se aplica. Configure um alerta de orçamento de US$ 1 por segurança.

---

## 9. Decisões arquiteturais

| # | Decisão | Por quê | Alternativa descartada |
|---|---|---|---|
| D1 | FastAPI + HTML/JS puro | Mínimo de peças; o Deezload é Python. O isolamento por usuário é do backend, não exige framework no front | Electron/Tauri (não há desktop); Node/TS no backend (perderia o reaproveitamento do Deezload); SPA com framework |
| D2 | Polling de snapshot | 2–5 usuários, robusto a reload | SSE/WebSocket |
| D3 | Spotify via embed/oEmbed anônimo | A Web API não dá ISRC para Dev Mode e não lê playlists de terceiros | Web API (enriquecimento/OAuth na v2) |
| D4 | Matcher próprio com score e 3 estados | O fallback do Deemix escolhe `data[0]` às cegas | Portar `get_track_id_from_metadata` |
| D5 | Deezload via import + monkeypatch de `log`; 1 `DeezerDownloader` por ARL | Progresso, cancelamento e um login por conta | Subprocess; fork |
| D6 | Deezload como submódulo | Repo sem LICENSE; update fácil | Copiar o arquivo |
| D7 | **Auth no Caddy (basic auth) + header** | Zero código de senha, sessão ou cadastro no app; o HTTPS já vem junto | Login próprio (fica para quando houver mais usuários) |
| D8 | **Sem banco**: JSON por usuário + jobs em memória | 2–5 usuários, sem histórico | SQLite (entra se surgir histórico ou login próprio) |
| D9 | **Entrega por download (arquivo/zip) + TTL** | Pouco disco; cada um guarda no próprio dispositivo | Biblioteca compartilhada + Navidrome |
| D10 | **ARL padrão do dono + ARL próprio opcional** | Prático para começar; cada amigo pode usar a própria conta | Só ARL próprio; só o ARL do dono |
| D11 | Docker Compose (app + Caddy) | Agora há servidor: deploy e update viram 1 comando | systemd + venv (dá, mas o Caddy e o update ficam manuais) |
| D12 | `rapidfuzz` | `token_set_ratio` rápido | `difflib` |

### Dependências

`fastapi`, `uvicorn`, `requests`, `rapidfuzz`, mais as do Deezload: `deezer-py`, `pycryptodome` (também usado no AES-GCM do ARL), `mutagen`. Testes: `pytest`. Infra: Docker, Caddy, DuckDNS.

---

## 10. Riscos

| Risco | Prob. | Impacto | Mitigação |
|---|---|---|---|
| **O Deezer bloqueia ou limita o IP de datacenter da Oracle**, ou o catálogo do país da VM é diferente | Média | **Alto** | Smoke test na VM **antes** de construir o resto (etapa 0b do plano). Região no mesmo país das contas. Plano B: rodar o app em casa (mesmo compose) com Tailscale/Cloudflare Tunnel |
| **Conta do dono concentra o tráfego** (ARL padrão) → bloqueio da conta Deezer | Média | Alto | Semáforo global baixo (4), concorrência por usuário ≤ 3, ARL próprio opcional. Lembrete: o ARL padrão também significa distribuir arquivos a terceiros com a sua assinatura; a responsabilidade é sua |
| O Spotify muda o embed | Média | Alto | Parser isolado + fixtures; erro claro; Web API como plano B |
| Playlists com mais de 100 tracks são truncadas | Certa | Médio | Aviso na UI; OAuth na v2 (§11) |
| Sem ISRC → falso positivo | Média | Médio | Score + penalidade de versão + AMBIGUOUS + match de álbum + escolha manual |
| O filtro `artist:` do Deezer continua quebrado | Média | Médio | Texto livre; testes `@live` |
| Rate limit do Deezer (IP compartilhado) | Baixa | Baixo | Limiter global + cache compartilhado |
| ARL expira | Média | Médio | Login no início do download; mensagem clara ("seu ARL expirou" / "o ARL padrão expirou, avise o admin") |
| A Oracle recupera ou encerra a VM gratuita | Baixa–Média | Médio | Conta PAYG; `/data` e configs reproduzíveis pelo README; nada insubstituível no servidor |
| Disco cheio | Baixa | Médio | TTL + `MIN_FREE_GB` + limpeza no startup |
| Mudanças no upstream do Deezload | Baixa | Médio | Submódulo fixado + testes do adapter + teste de assinatura |

---

## 11. v2 já prevista (não fazer no MVP, mas sem bloquear)

**OAuth do Spotify para playlists com mais de 100 tracks** (você confirmou que quer):
- No servidor isso fica viável: redirect `https://seu-dominio/api/spotify/callback` (o Spotify exige HTTPS fora do loopback, e o Caddy já fornece).
- Cada usuário conecta o próprio Spotify. O refresh token é guardado criptografado em `/data/users/<user>.json`, como o ARL.
- Limites do Dev Mode: o dono do app precisa ter **Premium**; até **5 usuários** cadastrados manualmente no dashboard do Spotify; só retorna itens de playlists **próprias ou colaborativas**. Para uma playlist de terceiros, o usuário a copia para a conta dele.
- **Impacto no MVP:** nenhum. O `SpotifyResolver` já é uma interface com `resolve(url)`; a v2 acrescenta uma segunda implementação (`WebApiSource`) usada quando o usuário tem o Spotify conectado e o embed veio truncado. `TrackCandidate` não muda (de quebra, `album` passa a vir preenchido em playlists).

Outros itens de v2: velocidade em bytes/s, PR upstream no Deezload (`logger`), login próprio no lugar do basic auth, opção "salvar numa biblioteca do servidor".

---

## 12. Revisão crítica da própria proposta

1. **"ISRC exato como prioridade 1" quase nunca vai acontecer** com a fonte embed. O investimento real vai para o matcher sem ISRC e o golden set de testes.

2. **`downloadAlbum()`/`downloadPlaylist()` no adapter seria a abstração errada** (o álbum do Spotify ≠ o álbum do Deezer). O adapter só baixa tracks.

3. **Polling e não SSE/WebSocket.** Continua valendo no servidor: 2–5 usuários fazendo poll a cada 1 s é nada.

4. **Monkeypatch de `deezload.log`.** Começa assim (isolado e testado); depois, um PR upstream adicionando `logger`.

5. **Match de álbum inteiro** fica no MVP; é removido se o golden set mostrar que não agrega.

6. **Basic auth no Caddy parece "gambiarra"?** Não para 2–5 pessoas conhecidas: é HTTPS + bcrypt mantidos pelo Caddy, com zero código de autenticação nosso, que é justamente onde se costuma errar. O custo é a UX (prompt do navegador, sem logout). O app depende só de `X-Remote-User`, então trocar depois é barato. **O ponto crítico é o app nunca ficar exposto sem o Caddy na frente.** O compose não publica a porta 8000, e há um teste que garante que a API rejeita requisições sem o header.

7. **Por que não SQLite agora?** Os dados persistentes são "preferências de 2 pessoas" e um cache. JSON + lock resolve. SQLite entra se aparecer histórico de downloads ou login próprio.

8. **O maior risco deixou de ser técnico e virou operacional:** o IP da Oracle e a conta Deezer do dono. Por isso o plano tem um **smoke test do Deezload na VM logo na etapa 0**, antes de investir no resto. Se o Deezer bloquear o IP, o plano B (mesmo compose em casa + túnel) já fica descrito.

9. **Docker passou de "evitar" para "usar"**, porque agora existe servidor. Ele continua não sendo obrigatório para desenvolver: localmente, `uvicorn` + um header `X-Remote-User` fixo via variável `DEV_USER` (que só funciona com `APP_ENV=dev`).

10. **O que fica de fora de propósito:** artistas/discografias, URLs do Deezer, busca livre, histórico, múltiplos jobs por usuário, painel de admin e edição de tags.
