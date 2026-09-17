# J.A.R.V.I.S.

**Just A Rather Very Intelligent System** — a modular workshop AI that runs locally on
Ollama, (speaks with a British neural voice), watches your hardware without being asked,
writes and executes code in every language your machine can compile, and answers in six
languages including Hindi, Bengali, Telugu, Marathi and Tamil.

```
     ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
     ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
     ██║███████║██████╔╝██║   ██║██║███████╗
██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
 ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
```

---

## What it does

| | |
|---|---|
| **Desktop app** | `jarvis-desktop` opens a real window with two surfaces — a conversation and a full editor — switched with `Ctrl`+`Shift`+`C`. Runs on the standard library alone: no Electron, no bundle, no extra install. |
| **A separate Code surface** | Not a panel: a second window inside the window. Activity bar, explorer, workspace search, outline, problems, a bottom dock, a mission panel, and a status bar where every field is a button. |
| **An editor that edits** | Open, change and save, sandboxed to the workspace. Auto-indent, bracket pairs, comment toggling, move and duplicate lines, find and replace, go to line, fuzzy open, a minimap, change bars in the gutter, and native undo because it is built on a real text area. |
| **54 languages** | One tokeniser, fifty-four grammars, and your colours rather than a palette of its own. The round trip is checked against every file in this repository: strip the spans, unescape, get the source back character for character. |
| **Glass** | Translucent panels with lit edges and a specular highlight that follows the pointer, over three washes of your accent drifting on mutually prime periods. Every bit of it stops under `prefers-reduced-motion`. |
| **A lock on the window** | Set a password in **Appearance → Security** and the window is locked; remove it and it is not. No file to edit, no command to run, no restart. Google sign-in too, set up in the same panel. |
| **Logs** | The session as it happens — timestamp, level, message — filterable by level, fed by the same event stream as everything else. |
| **A real terminal** | PowerShell on Windows, your login shell elsewhere, running as one long-lived process beside the conversation. `cd` sticks, variables persist, history on the arrow keys, exit codes in red. |
| **The agentic HUD, in the app** | Its own grammar — `⏺` for what J.A.R.V.I.S. did, `⎿` for what came back — streaming live in its own tab. The terminal front end is not replaced by the window; it is reproduced in it, and still runs on its own. |
| **Quiet by default** | Grey on near-black. The accent reaches the caret, the selected row and a status dot, and nothing else. An instrument you keep open all day should not be shouting a hue at you — the colour is there, but you have to ask for it. |
| **Any colour you like** | Not four palettes: *any* colour. Drag a wheel or type `#ff8c42`, and the entire interface — backgrounds, surfaces, borders, gauges, the reactor — is re-derived from that one seed, stays readable by WCAG AA, and is remembered. `/theme violet` works in the terminal too. |
| **Full-screen HUD** | The whole terminal, in Claude Code's grammar: a scrollable transcript, replies that arrive word by word, instrument cards that spin while they run and resolve in place, and a working line that says what it is doing and how to stop it. It opens on a greeting that has read the machine first. `--classic` restores the old pinned strip. |
| **Live keywords** | `talk` and `quiet` act the moment you send them, and are coloured as you type them so you can see it before you press enter. |
| **Built to answer fast** | The model is loaded before you ask; the reply streams to the screen as it is generated; independent instruments run side by side; Ctrl-C tears the request down rather than asking it to stop when convenient. |
| **Wake-word voice** | Say **"Hello J.A.R.V.I.S."**, **"Namaste, J.A.R.V.I.S."** or **"Pi lagu, J.A.R.V.I.S."** Background listening, a rising chime, and he never answers his own echo — and in the window, the whole room turns blue while he listens. |
| **Knows your voice** | Enrol once and the call word is checked against your voiceprint. |
| **Background listener** | `--daemon` waits for the call word with no window open, greets you, and opens a terminal. |
| **Speaks as it thinks** | Sentences go to the speaker as they stream, so he starts answering in about a second instead of eight. |
| **Polyglot engineering** | Writes and *runs* code in 32 languages. Detects which toolchains actually exist on the host and never proposes one that does not. |
| **Six languages** | Answers in English, हिन्दी, বাংলা, తెలుగు, मराठी and தமிழ் — detected automatically from what you type or say, spoken back in a native neural voice. |
| **Stark Protocols** | Named macros — House Party, Veronica, Clean Slate — triggered by voice or command. |
| **Ambient monitoring** | A background thread that notices a dying battery or a memory spike and mentions it before it becomes your problem. |
| **Reach beyond the workspace** | Opens applications, files and web pages, closes running programs, and works anywhere on disk — asking your permission every time. |
| **ReAct tool calling** | Ten instruments wired into a native Ollama function-calling loop with streaming. |
| **Form of address** | Asks once how you wish to be addressed — Sir, Ma'am, Boss, or anything you type — then remembers it forever. |

---

## Quick start

### Teacher school portal (initial institutional slice)

This is a draft implementation for local development, not a production school
deployment. Streaming, document extraction, the full teaching toolkit, rate limits,
and deployment security hardening remain unfinished.

The repository now includes a separate teacher-only web portal. It keeps the existing
desktop assistant and its local shell/editor boundary intact while adding SQLite-backed
school accounts, teacher/admin roles, private workspace items, approved school knowledge,
document validation and a lesson-plan workflow powered by the configured Ollama model.

```bash
cp .env.example .env
python main.py portal
```

Open `http://127.0.0.1:8787`, create the first administrator, then use Administration to
create teacher accounts and add approved school sources. The first administrator setup is
only allowed when the database has no users. Sessions are HttpOnly, SameSite cookies with
an environment-configurable expiry. Teachers can only read and delete their own workspace
items; administrators can view health and usage summaries and curate school knowledge.

Ollama remains the initial AI provider. If it is stopped, the portal reports that it is
unavailable and does not fabricate a lesson plan. Configure `JARVIS_PORTAL_DB`,
`JARVIS_PORTAL_STORAGE`, `JARVIS_SCHOOL_NAME`, and the other portal values in the
process environment. `.env.example` documents those values; portal-specific values
are not yet loaded automatically from `.env`.
See [docs/ARCHITECTURE_AUDIT.md](docs/ARCHITECTURE_AUDIT.md) for the migration decisions,
security boundary and the next implementation phases.

```powershell
cd C:\Users\Shiva\J.A.R.V.I.S._AI

# 1. Make sure the Ollama API server is actually running (see the note below)
ollama serve

# 2. Launch — the terminal HUD
.venv\Scripts\python.exe main.py

# ...or the desktop window
jarvis-desktop
```

That is it — the virtual environment is already built and every dependency, including
PyAudio, is installed.

### Building it elsewhere

```powershell
uv venv .venv --python 3.11 --seed
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
```

Or with stock tooling:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **On this machine specifically:** the `python` on `PATH` is a broken Hermes venv with no
> pip. The working interpreter is the uv-managed CPython at
> `%APPDATA%\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe`, and `uv` 0.11.32
> is on `PATH`. The commands above use it.

### Ollama

The model is **`gemma4:31b-cloud`** — already pulled, and advertising `completion`,
`thinking`, `tools` and `vision`.

> **The Ollama desktop app does not start the API server on this machine.** If nothing
> answers on `127.0.0.1:11434`, start the daemon directly:
>
> ```powershell
> & "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
> ```

Verify with `curl http://127.0.0.1:11434/api/version`, or just run
`main.py --check`, which tells you precisely what is wrong and how to fix it.

---

## First run

On first launch J.A.R.V.I.S. asks one question:

```
╭──────────────────────────────────────────╮
│  How shall I address you?                │
│                                          │
│      1   Sir                             │
│      2   Ma'am                           │
│      3   Boss                            │
│                                          │
│  Enter 1, 2 or 3 — or simply type the    │
│  title you prefer.                       │
╰──────────────────────────────────────────╯
```

Answer `1`, `2`, `3`, or type anything you like — your name, `Captain`, `Doctor`. The
choice is saved to `.jarvis_profile.json` and never asked again. Change it later with
`/title <whatever>`, or reset it with `--reset-profile`.

Every honorific in the system — spoken lines, HUD copy, ambient alerts, the system
prompt — follows this setting, and it is translated per language (`सर`, `স্যার`, `சார்`).

---

## Waking him

The call words are three:

| Say | |
|---|---|
| **"Hello J.A.R.V.I.S."** | English |
| **"Namaste, J.A.R.V.I.S."** | English |
| **"Pi lagu, J.A.R.V.I.S."** | पाय लागू जार्विस — also heard as *pay lagu*, *paay laagu*, *pai lagu* |

Speech recognition never returns the dots, so `Hello J.A.R.V.I.S.`, `hello jarvis` and
even `hello j a r v i s` all normalise to the same phrase. Common mishearings of the name
(`jervis`, `javis`, `jarwis`) are accepted too, and each greeting is configured under
several spellings — a recogniser hands back what it heard rather than what was meant, and
a greeting that only works when the microphone spells it your way is not a greeting.

### The window turns blue

Say it with the desktop window open and the whole thing lights up:

| | |
|---|---|
| **The moment it hears you** | One brighter beat, and a ring out from the middle. |
| **While it is listening** | A blue wash over the window and a lit border that breathes with the level of your voice. |
| **While it answers** | The same field, calmer and green, so the two halves of a conversation do not look identical. |

Blue whatever accent you chose — the one deliberate exception to deriving every
colour in the interface from your seed. A signal that means *he is listening to the
room* has to read the same way every time, at a glance, from across a room, and it
cannot if it is whatever colour somebody picked last week. Your accent is mixed in
at a fifth so it still belongs to the theme.

The microphone button in the composer starts and stops listening, and says what it
is doing. It used to send the word "talk" as a chat message, which is not what a
microphone button is. Under `prefers-reduced-motion` nothing moves; the colour still
says it.

A window opened on its own — `jarvis-desktop`, with no terminal behind it — now
builds its own voice, so it can be spoken to. A window opened onto a running
terminal session is looking at *that* session's microphone, does not own it, and
says so rather than pretending to switch it on and off underneath it.

A bare **"Jarvis"** deliberately does **not** wake him — the call word is the whole
greeting, so mentioning him in conversation is safe. Set `WAKE_ALLOW_BARE_NAME=true` if
you would rather it did.

Say the call word alone and he acknowledges and listens for your command; say it followed
by a command and he acts immediately.

---

## Knowing your voice

Record a sample once:

```powershell
.venv\Scripts\python.exe main.py --enroll-voice
```

He asks for five short clips — two of them the call words themselves, because the print
is compared against wake-word audio and enrolling on the phrase it will actually hear
matches far better than enrolling on unrelated speech. The result lands in
`.jarvis_voiceprint.json`: a list of numbers, no recording, nothing from which speech
could be reconstructed. `/voiceprint` shows it, `/voiceprint forget` deletes it.

### How well it works — measured, not claimed

The voiceprint is a scale-equalised MFCC embedding compared by cosine similarity.
Benchmarked twice on this machine against seven synthetic voices (male and female;
British, Indian, American and Hindi), with the enrolment taken from five clips:

| | owner | closest impostor (same accent, same gender) |
|---|---|---|
| Full sentences | 0.870 – 0.917 | 0.783 |
| **Short wake phrases** | 0.696 – 0.827 | 0.673 – 0.707 |

At the shipped threshold of **0.65** the operator was never falsely rejected at either
length, and roughly five impostor clips in six were turned away. The failures are
concentrated in one place: a voice of the same sex and accent scores almost exactly where
you do on a phrase as short as "Hello Jarvis". There is no threshold that separates those
two — 0.70 turns that impostor away but also rejects *you* on one wake phrase in three,
which is the worse trade for a wake word.

So: **this is a convenience gate, not security.** It stops the television and a passer-by.
It will not stop somebody who sounds like you, and it is not identity verification.

It also fails *open* by design — no print enrolled, numpy missing, audio undecodable, a
voiceprint whose shape no longer matches, any error at all, and the call word simply works
for anyone. Being locked out of your own assistant is the worse failure, every time.

Two things were tried and measured and did **not** work, recorded here so nobody repeats
them: plain cosine over raw MFCC means and standard deviations put the owner at 0.963 and
a completely different woman at 0.940 — unthresholdable; and adding pitch (F0) features
changed nothing measurable, because the per-dimension scaling absorbs them.

For real accuracy the module uses [Resemblyzer](https://github.com/resemble-ai/Resemblyzer)
automatically if it is importable. It could not be installed here: its `webrtcvad`
dependency needs the Visual C++ Build Tools to compile. Install those and
`pip install resemblyzer`, then re-enrol, and verification becomes genuinely reliable.

---

## Hands-free: the background listener

```powershell
.venv\Scripts\python.exe main.py --daemon
```

No window, no HUD — just a process waiting for the call word. Say **"Namaste,
J.A.R.V.I.S."** and he:

1. plays a rising chime,
2. greets you aloud — *"नमस्ते सर, कैसे हैं आप? मैं हूँ जार्विस, आपका असिस्टेंट।"* — in
   the Hindi voice, with the honorific matching how you asked to be addressed,
3. opens a new Windows Terminal running the full assistant,
4. and **stops listening until you close it**, because two processes fighting over one
   microphone produce nothing but garbage.

Say the call word with a command attached — "Namaste Jarvis, open notepad" — and the
command is handed to the new session, which acts on it immediately.

Customise the greeting with `WAKE_GREETING` in `.env`; `{honorific}` becomes Sir, Ma'am,
सर or मैडम as appropriate.

---

## Talking, and not talking

**He boots silent.** An assistant that starts speaking the moment you open a terminal is
a nuisance, so speech waits to be invited.

| Type this, no slash | |
|---|---|
| **`talk`** | Speaking mode on — he answers aloud and opens the microphone if there is one |
| **`quiet`** | Shuts him up immediately, mid-word if necessary |
| `stop` | Cuts off whatever he is saying, but only while he is saying it |

`shut up`, `chup`, `bas`, `mute`, `silence` all do what `quiet` does; `bolo`, `speak`,
`unmute` all do what `talk` does. `/talk` and `/quiet` work too if you prefer a slash.

Matching is on the **whole line**, so "talk to me about python decorators" is a question,
not a command — only a bare `talk` on its own switches speech on.

Start him talking from the outset with `--talk`, or set `START_MUTED=false` in `.env`.

---

## Speaking like an assistant

| | |
|---|---|
| **Streaming speech** | Each sentence goes to the speaker the moment it completes, rather than after the whole reply lands. Only the opening summary is spoken — tables and code stay on screen where they belong. |
| **Follow-up window** | For seven seconds after a reply the microphone stays warm, so "and close it" needs no second call word. |
| **Barge-in** | Start talking and he stops. He compares what he hears against what he is currently saying, so his own voice coming back through the speakers is not mistaken for you. Best with headphones — there is no acoustic echo cancellation. |
| **Chime** | A rising two-tone on wake, generated at runtime rather than shipped as an asset. |

Sentence splitting understands the Devanagari danda (`।`), so Hindi replies stream
sentence by sentence exactly as English ones do — and a short sentence merges into the
next rather than being dropped, which is what happens to `नमस्ते सर।` under a naive
minimum-length filter.

---

## Speaking in tongues

Six languages are enabled by default, and he switches between them without being asked
twice.

| Code | Language | Native | Voice |
|---|---|---|---|
| `en` | English | English | `en-GB-RyanNeural` |
| `hi` | Hindi | हिन्दी | `hi-IN-MadhurNeural` |
| `bn` | Bengali | বাংলা | `bn-IN-BashkarNeural` |
| `te` | Telugu | తెలుగు | `te-IN-MohanNeural` |
| `mr` | Marathi | मराठी | `mr-IN-ManoharNeural` |
| `ta` | Tamil | தமிழ் | `ta-IN-ValluvarNeural` |

Five more are registered and one line of config away — Kannada, Malayalam, Gujarati,
Urdu and Punjabi:

```ini
ENABLED_LOCALES=en,hi,kn,ml,ta
```

**How detection works.** Script first and decisively: Devanagari, Bengali, Telugu or
Tamil characters identify the language outright. Hindi and Marathi share Devanagari, so
Marathi-specific words (`आहे`, `नाही`, `मला`) break that tie. For romanised input
("bhai mujhe batao kya hai ye") a marker-word heuristic applies, requiring **two**
distinct markers — so an English sentence containing one loan word is never
misclassified.

Code, identifiers, file paths, commands and error messages always stay in English,
whatever the conversation language.

```
/lang hindi        switch and pin
/lang auto         follow my lead again
/locales           show the full table
```

> **Indic speech needs edge-tts.** A stock Windows install has only `Microsoft David`
> and `Microsoft Zira`, both US English — there is no offline British voice and no Indic
> voice at all. So `TTS_ENGINE=auto` prefers **edge-tts** (neural, networked) and falls
> back to pyttsx3 only when edge-tts or an audio player is missing. `ffplay` is present
> on this machine, so playback works out of the box.

---

## Polyglot engineering

J.A.R.V.I.S. writes complete programs and then *runs* them. Compiled languages are built
first, and on failure the compiler diagnostics come back verbatim — the single most
useful thing a model can be handed.

**Currently online here** (11 of 32):

| Language | Toolchain | Version |
|---|---|---|
| Python | project interpreter | 3.11.15 |
| JavaScript | `node` | 24.19.0 |
| TypeScript | `node` native type-stripping | 24.19.0 |
| C# | `dotnet run` file-based app | 10.0.400 |
| F# | `dotnet fsi` | 10.0.400 |
| PowerShell | `pwsh` | 7.6.5 |
| Bash | `bash` | 5.2.37 |
| Batch, Perl, VBScript, AWK | | |

**Registered but not installed here:** C, C++, Java, Kotlin, Go, Rust, Ruby, PHP, Lua, R,
Julia, Dart, Swift, Zig, Nim, Haskell, Scala, Elixir, Groovy, OCaml, SQL. Install any one
of them and it becomes available immediately — `/toolchains refresh`, no code change and
no restart.

Detection is version-aware, which matters more than it sounds. Java 8 is installed here,
but single-file `java Foo.java` needs Java 11+ and there is no `javac`, so Java correctly
reports as **absent** rather than failing at runtime. Node is checked for ≥ 22 before
being trusted with TypeScript.

HTML, CSS, JSON, YAML and Markdown are written and validated rather than executed — JSON
genuinely goes through `json.loads`.

```
/toolchains            what can run here
/toolchains refresh    re-probe every binary
python main.py --toolchains
```

---

## The instruments

| Tool | What it does |
|---|---|
| `system_diagnostics` | CPU, per-core load, memory, disks, battery, GPU/VRAM, temperatures, heaviest processes. Scopes: `summary`, `full`, `cpu`, `memory`, `disk`, `battery`, `gpu`, `processes`. |
| `code_executor` | Runs source in any installed language. Compiles first where needed; returns stdout, stderr and exit code. |
| `run_command` | Builds and test suites — `npm test`, `dotnet build`, `pytest -q`, `git diff`. Workspace-confined, with a destructive-command refusal list. |
| `code_toolchains` | Which languages this machine can compile and run, with install hints for the rest. |
| `web_search` | DuckDuckGo, parsed with the standard library. No scraping dependencies. |
| `file_ops` | `read`, `write`, `append`, `list`, `info`, `exists`, `mkdir`, `delete`. Reads anywhere; writes only inside the workspace. |
| `execute_protocol` | Fires a Stark Protocol. |
| `open_app` | Opens an application, file, folder or URL — by friendly name, path or address. **Asks first.** |
| `app_control` | `list` running applications, `close` one, or `focus` its window. Closing **asks first.** |
| `set_language` | Switches the conversation language mid-flight. |

---

## Reaching outside the workspace

J.A.R.V.I.S. treats `WORKSPACE_ROOT` as his own desk — he reads, writes and runs things
there without interrupting you. Everything beyond it goes through a consent layer.

**What requires your approval**

| | |
|---|---|
| Opening an application, file or URL | `open_app` |
| Closing a running application | `app_control close` |
| Reading or listing files outside the workspace | `file_ops` |
| Writing, deleting or creating outside the workspace | `file_ops` |
| Running a command with a working directory outside the workspace | `run_command` |

**What the prompt looks like**

```
╭─ Permission required ──────────────────────────────────────────╮
│ Sir, may I open Visual Studio Code?                            │
│                                                                │
│   Scope  Launch an application                                 │
│  Action  open                                                  │
│  Target  Visual Studio Code                                    │
│ Context  binary: C:\...\Microsoft VS Code\Code.exe             │
│                                                                │
│  [y] allow once   [a] always allow   [n] deny   [!] stop asking │
╰────────────────────────────────────────────────────────────────╯
```

- **`y`** — this one action, this once.
- **`a`** — remember it. For an application that means that app; for a file that means
  its whole directory, so you are not re-prompted for every file in a folder you just
  approved.
- **`n`** — refuse. He is told to drop it, not to look for another way.
- **`!`** — refuse and stop asking about that whole category this session.

**Rules that are not negotiable**

- Grants live **for the session only** and are never written to disk. A permission you
  forgot you granted last Tuesday is a trap.
- **Veronica lockdown refuses everything outside the workspace without even prompting.**
  A security protocol that can be talked out of its own rules is decoration.
- The destructive-command refusal list (`rm -rf /`, `format`, `diskpart`, `mkfs`,
  piping curl into a shell…) is absolute. There is no prompt that will approve those.
- **Permission is granted by typing, never by speaking.** He will read the question
  aloud, but the answer must come from the keyboard — otherwise anything that can make
  a noise near your microphone could authorise filesystem access.
- A refusal also answers the *next* identical request for 90 seconds. Models retry when
  told no, and re-prompting for that is precisely how consent dialogs get trained into
  reflexive approval.
- With no console attached to ask, every request **fails closed**.

**What this is not**

`code_executor` and `run_command` can reach the whole filesystem — that is what running
code means. J.A.R.V.I.S. is instructed to use `file_ops` and `open_app` for anything
outside the workspace, and code naming an absolute path outside it triggers the same
prompt. Both are speed bumps on an honest path, not a sandbox wall: a determined model,
or code you paste in yourself, can still get around them. Treat this as a guard against
overreach and accident, which is what it is — not as a security boundary, which it is
not. If you need a real one, run the whole thing in a container or a VM.

**Managing it**

```
/permissions        what he has been allowed, and what he has asked for
/allow chrome       pre-approve an application so he stops asking
/revoke             forget every grant made this session
/apps               what is currently running
```

Or set it once in `.env`:

```ini
PERMISSION_MODE=ask     # ask (default) | allow | deny
ALLOWED_APPS=code,chrome,spotify
```

`--allow-all` on the command line approves everything for one run. It exists for
scripting; do not make it your habit.

---

## Stark Protocols

Trigger by voice (*"J.A.R.V.I.S., initiate House Party Protocol"*) or by command
(`/protocol house_party`).

### `PROTOCOL HOUSE PARTY` — gold palette
Everything on. Full diagnostics, un-mutes audio, launches every command in
`PROTOCOL_DEV_COMMANDS`, verifies the Ollama endpoint, and scans your workspace for git
repositories, reporting branch and dirty state for each (depth ≤ 3, capped at 25 repos).

### `PROTOCOL VERONICA` — crimson palette
Security lockdown. Enumerates processes above `VERONICA_KILL_THRESHOLD_MB` and — when
`VERONICA_DRY_RUN=false` — terminates the non-essential ones. While engaged it refuses
file deletions and overwrites, blocks the shell runner entirely, and refuses obviously
destructive source code.

**It will never touch** PID 0 or 4, this process or its parents, `ollama`, your terminal,
or any of `System`, `Registry`, `csrss`, `wininit`, `services`, `lsass`, `dwm`,
`explorer`. It ships in dry-run mode: it reports what it *would* stop and stops nothing.

### `PROTOCOL CLEAN SLATE` — steel palette
Wipes conversation context, clears the HUD, purges `__pycache__` and `.jarvis_cache`,
truncates oversized logs, resets the palette and stands down any lockdown. Every deletion
path is asserted to resolve under the project root before anything is unlinked.

---

## Commands

| Command | Effect |
|---|---|
| `/help` | The command reference |
| `/diag` | Full hardware telemetry |
| `/protocol <name>` · `/protocols` | Fire a protocol · list them |
| `/lang <name>` · `/locales` | Switch reply language · list all six |
| `/toolchains [refresh]` | Programming languages available here |
| `talk` · `quiet` | Speech on / off — bare words, no slash needed |
| `/tools` | The instrument rack |
| `/voiceprint` | Show the enrolled voiceprint (`/voiceprint forget` deletes it) |
| `/apps` | Applications currently running |
| `/permissions` · `/allow <app>` · `/revoke` | What he may do outside the workspace |
| `/voice on\|off` · `/mute` · `/unmute` | Audio control |
| `/mics` | Input devices |
| `/theme <colour>` | **Any colour**: `#ff8c42`, `violet`, `veronica`, `light`, `surprise` |
| `/desktop` | Open the desktop window onto this session (`/desktop close` shuts it) |
| `/title <value>` | Change how he addresses you |
| `/model` · `/history` · `/clear` | Model info · transcript · reset context |
| `/metrics` | Where the last turn's time went: first token, throughput, instrument time |
| `/quit` · `/exit` | Shut down cleanly |

### Command line

| Flag | |
|---|---|
| `--text` | Text only; no microphone, no speech |
| `--ask "..."` | One-shot question, then exit |
| `--check` | Preflight diagnostics and exit |
| `--locale <code>` | Pin the reply language |
| `--locales` · `--toolchains` · `--list-mics` | Print a table and exit |
| `--protocol <name>` | Fire a protocol at boot |
| `--model <name>` · `--host <url>` | Override Ollama settings |
| `--classic` | The pinned status strip instead of the full-screen HUD |
| `desktop` · `--desktop` | Open the desktop window instead of the terminal front end |
| `--theme <colour>` | Start in a colour: a hex code, a name, a preset or `surprise` |
| `--port <n>` · `--no-window` | Pin the desktop app's port · serve it without opening a window |
| `--no-turbo` | Drive the model synchronously: no warm-up, no parallel instruments |
| `--no-hud` · `--no-monitor` | Plain output · no ambient thread |
| `--talk` | Start in speaking mode instead of silent |
| `--allow-all` | Approve every out-of-workspace action without prompting (scripting only) |
| `--enroll-voice` | Record your voice sample, then exit |
| `--forget-voice` | Delete the stored voiceprint |
| `--daemon` / `--listen` | Background listener; opens a terminal on the call word |
| `--reset-profile` | Forget the form of address and ask again |
| `--debug` | Log at DEBUG |

---

## J.A.R.V.I.S. Desktop

A third front end. The two terminal HUDs are excellent at what they do and completely
unable to render a colour wheel, so this one is a window.

```bash
jarvis-desktop                    # the launcher script
jarvis-desktop --theme violet     # ...opened in a colour
python main.py desktop            # the same thing, spelled out
python main.py --desktop          # or as a flag
```

Inside a session already running in the terminal, `/desktop` opens a window onto
**that** session — same memory, same instruments, same permissions, one queue. Anything
typed in the window arrives exactly as if it had been typed in the terminal, and
everything J.A.R.V.I.S. says appears in both. `/desktop close` puts it away.

The window has **two surfaces**, switched from the title bar or with
`Ctrl`+`Shift`+`C`:

| | |
|---|---|
| **Chat** | The conversation. Quiet, grey, restrained — you read it for hours. |
| **Code** | A full editor. Not quiet at all. |

They share the title bar, the theme and the event stream, and nothing else. Each has its
own layout and its own keyboard map, because a conversation and an editor want opposite
things from the same pixels.

### Chat

Three panes, two draggable seams, a status bar. The shape is Hermes Desktop's, because
Hermes got it right: chat first, with a preview rail on the right so reading a file never
costs you your place in the conversation.

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ▤ ⇄  session name                              ▤ ◍ ⌨ ☀ ☰  │  ─  □  ✕       │
├──────────────┬───────────────────────────────┬──────────────────────────┬──┤
│ New session  │                               │ TERMINAL AGENT LOGS SYS  │>_│
│ Capabilities │                               ├──────────────────────────┤>_│
│ Messaging    │         J.A.R.V.I.S.          │ ~/J.A.R.V.I.S $ pwd      │◎ │
│ Code       2 │                               │ /home/you/J.A.R.V.I.S    │≡ │
│ ⌕ Search…    │   Ask a question, paste an    │ ~/J.A.R.V.I.S $ cd jarvis│▤ │
│              │   error, or point me at a     │ ~/…/jarvis $ ls          │  │
│ PINNED       │   repository.                 │ core.py  theme.py  …     │  │
│ SESSIONS  3  │                               │ ~/…/jarvis $ ▊           │  │
│  TODAY       │                               │                          │  │
│  • current   │                               │                          │  │
│ WORKSPACE    │  ┌─────────────────────────┐  │                          │  │
│  ▾ jarvis    │  │ + What's on your mind?  │  │                          │  │
│    theme.py  │  │       gemma4 ● ⌄  🎤  → │  │ bash · ready  Interrupt  │  │
├──────────────┴──┴─────────────────────────┴──┴──────────────────────────┴──┤
│ ● Ready │ gemma4:31b-cloud │ ~/J.A.R.V.I.S   theme.py · 713 lines  cpu 34% │
└────────────────────────────────────────────────────────────────────────────┘
```

| | |
|---|---|
| **Sidebar** | Navigation, session search, pinned sessions, sessions grouped by day, and a lazily-loaded workspace tree with file sizes. Click a file and it opens on the Code surface. |
| **Transcript** | Replies stream in word by word behind a caret, then settle into rendered Markdown — headings, lists, tables, quotes, and code blocks with a Copy button. |
| **Instrument cards** | Every tool call appears as a card that spins while it runs and resolves in place with its reading. |
| **Terminal** | A real shell — PowerShell, bash, zsh. The next section. |
| **Agent** | The agentic HUD, live, in the app. Same events as the transcript, rendered in the terminal's grammar. |
| **Logs** | Timestamp, level, message — the three columns you actually scan a log by — filterable, and carrying the instrument calls as well as the system lines. |
| **System** | Live CPU, memory, disk and battery, a per-core strip, the model, the instruments and the protocols. |
| **Permission cards** | The consent layer, as a card with *Allow once* / *Always allow* / *Deny*. Nothing reaches outside the workspace without one. |
| **Status bar** | State, model, workspace root, open file, last turn's latency, live CPU, and whether the event stream is connected. |
| **Command palette** | `Ctrl`+`K` for every command, every protocol and every rail view. |
| **Colour studio** | `Ctrl`+`,`. Further down. |

Keys: `Enter` sends, `Shift`+`Enter` newlines, `/` opens command completion, `Ctrl`+`K`
the palette, `Ctrl`+`,` appearance, `Ctrl`+`B` the rail, `Ctrl`+`\` the sidebar,
`Ctrl`+`1`–`4` the rail views, `Ctrl`+`N` a new session, `Ctrl`+`.` interrupts, `?` the
full sheet. Start typing anywhere and the composer takes it — including the character you
typed, which the earlier version swallowed. Both seams drag and remember their width.

`Esc` closes what is open and nothing more. It used to interrupt the running turn when no
panel was showing, which made a reflex into a way to lose work.

### The terminal

A real one. PowerShell on Windows — PowerShell 7 if you have it, Windows PowerShell if
not — and your login shell everywhere else. It runs as a **single long-lived process**,
which is the whole point: `cd` sticks, an activated virtualenv stays activated, and a
variable you set on one line is still set on the next.

```
PS C:\Users\Shiva> cd .\projects\reactor
PS ~\projects\reactor> python -c "print(2**32)"
4294967296
PS ~\projects\reactor> nosuchcommand
nosuchcommand : The term 'nosuchcommand' is not recognized…
exit 1
```

Command history on ↑/↓, `Ctrl`+`C` to interrupt, `Ctrl`+`L` to clear, and a prompt that
tracks the working directory and shortens it the way a shell prompt does.

There is no pseudo-terminal behind it — the standard library has no portable one, and
pulling in a PTY layer for a single pane is not a trade worth making — so it is
line-oriented. Ordinary commands work; a full-screen program like `vim` or `top` has
nothing to draw into. Since the shell therefore never prints a prompt of its own, each
command is followed by a sentinel carrying the exit status and the working directory,
and that marker carries a per-session random suffix so output cannot forge one.

It is exactly as dangerous as a terminal, which is what makes it useful. It sits behind
the same loopback-only, token-checked, `Host`-verified boundary as everything else the
window can reach — the same boundary the `run_command` instrument already sits behind, so
this adds a pane rather than a privilege. Every line is written to the log file before it
runs. Set `DESKTOP_SHELL_ENABLED=false` in your `.env` to remove the pane entirely and
have the route refuse outright.

## The Code surface

Not a tab in the conversation. A second window inside the same window, with its own
layout, its own keyboard map and its own visual register.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ▤ ⇄  session name        ┃ ꒨ Chat ┃ <> Code ┃          │  ─  □  ✕           │
├──┬───────────────┬────────────────────────────────┬────────────────┤
│▤ │ EXPLORER    ↻ < │ desktop.py ✕  theme.py ✕          │ MISSION      ● ›│
│⌕ │ › jarvis       │ J.A.R.V.I.S › jarvis › desktop.py     ├────────────────┤
│≡ │ › tests        │ 596 def language_for(path):    ▐█▖ │ ● Idle        │
│⚠ │   config.py    │ 597     """The language…"""     ▐██ │ ┌──────────────┐ │
│◎ │   main.py      │ 598     by_name = FILENAMES…    ▐█▌ │ │ Ask about   │ │
│  │   README.md    │ 599     if by_name:            ▐█  │ │ this file…  │ │
│  │                │█600         return by_name     ▐██▖│ └──────────────┘ │
│  │                │ 601     by_suffix = LANGUAGES…  ▐█▌ │ whole file  Send│
│  │                ├──────────────────────────────────┤ ACTIVITY        │
│  │                │ PROBLEMS  OUTPUT  TERMINAL   ⌗ ⌄ │ ● read theme.py │
│☀ │                │ PS ~\reactor> python -c "…"     │ ● edited …      │
│⌨ │                │ 4294967296                       │                │
├──┴───────────────┴────────────────────────────────┴────────────────┤
│ ⎇ J.A.R.V.I.S  ⚠ 0          Ln 600, Col 1  Spaces: 4  LF  Python  No wrap │
└──────────────────────────────────────────────────────────────────┘
```

| | |
|---|---|
| **Activity bar** | Explorer, Search, Outline, Problems, Agent. Click the one already showing to collapse the panel. |
| **Explorer** | The workspace tree, lazily loaded, rows revealing in sequence. |
| **Search** | Literal text across every file, grouped by file, the match picked out in each line. Click a hit to land on it. |
| **Outline** | The symbols in the open file — functions, classes, structs, traits, headings — derived per language. |
| **Problems** | Unbalanced brackets, merge-conflict markers, mixed tabs and spaces, trailing whitespace, `TODO`/`FIXME`. Marked in the gutter and listed in the dock. |
| **Agent** | Six things worth asking about a file — Explain, Review, Simplify, Tests, Document, Fix — and the files touched this session. |
| **Editor** | Tabs, breadcrumbs, line numbers, active-line band, change bars, a minimap you can drag, soft wrap. |
| **Dock** | Problems, Output, and the terminal. |
| **Mission** | The conversation, in the editor. Ask about the open file or the current selection and the reply streams in right here — instrument calls in place, code blocks with *Copy* and *Insert*, and a *Stop* while it is working. One agent, one conversation, two views of it. |
| **Status bar** | Workspace, problem count, line and column, indent width, line endings, language, wrap. Every one of them is a button. |

### The editor

It edits. A transparent `<textarea>` sits exactly on top of a highlighted copy of the
same text, which is a deliberate choice and not a shortcut: every edit goes through the
browser's own editing machinery, so undo, redo, autorepeat, IME composition and the caret
all behave the way the platform says they should, for nothing. The price is that the two
layers must agree to the character — which is why every operation below is made with
`insertText` rather than by assigning to `value`, and why `tests/test_ui.py` checks the
painted layer against the buffer after opening a docstring and undoing it.

| | |
|---|---|
| `Ctrl`+`S` | Save |
| `Ctrl`+`P` | Open a file by name, fuzzy |
| `Ctrl`+`F` / `Ctrl`+`H` | Find / find and replace |
| `Ctrl`+`G` | Go to line |
| `Ctrl`+`/` | Toggle comment, in that language's comment style |
| `Ctrl`+`D` | Duplicate the line or the selection |
| `Ctrl`+`Shift`+`K` | Delete the line |
| `Alt`+`↑` / `Alt`+`↓` | Move the line |
| `Tab` / `Shift`+`Tab` | Indent / outdent, selection-aware |
| `Alt`+`Z` | Soft wrap |
| `Alt`+`W` | Close the file |
| `Ctrl`+`B` / `Ctrl`+`Shift`+`B` / `Ctrl`+`J` | Side panel / mission panel / dock |
| `Ctrl`+``` | Terminal |
| `Ctrl`+`Shift`+`E`/`F`/`O`/`M`/`A` | Explorer / Search / Outline / Problems / Agent |

Closing a bracket or a quote closes it for you, and typing one around a selection wraps
it. `Enter` keeps your indentation and adds a level after anything that opens a block;
pressed between a bracket and its partner it opens the pair out into three lines with the
caret on the middle one. `Backspace` in leading whitespace takes a whole indent, and
between a pair takes both. `Home` goes to the first non-space, then to column one.

It is **`Alt`+`W`, not `Ctrl`+`W`**, to close a file. `Ctrl`+`W` closes the window in
every browser and most desktop webviews, and an editor that can lose the session to a
typo is not one you would leave a file open in.

### Saving

`Ctrl`+`S` writes the file. It is bounded by `WORKSPACE_ROOT` — the same root the
`file_ops` tool is bounded by, through the same resolve-then-compare boundary the reader
uses, so there is exactly one place where the edge is decided. The write goes to a
temporary file beside the target and is moved into place, so a failure halfway through
leaves the original intact rather than a truncated one.

Each read hands back a short digest of the file, and each save sends it back. If the file
changed on disk in between — which is the ordinary case here, not an exotic one, because
an agent turn rewriting the file you have open is the whole point of the window — the
save is refused and you are asked, rather than one of you silently losing the work. The
other direction is covered too: when a tool call names a file you have open and you have
no unsaved changes, the editor quietly reloads it; when you do have changes, it says so
and leaves your version alone.

`DESKTOP_EDIT_ENABLED=false` makes the editor read-only and the route refuse.
`DESKTOP_EDIT_MAX_BYTES` caps one save.

### Thirty languages, one tokeniser

`jarvis/web/lang.js` carries **54 language definitions** — Python, JavaScript,
TypeScript, Rust, Go, C, C++, C#, Java, Kotlin, Swift, Ruby, PHP, Perl, shell,
PowerShell, batch, SQL, HTML, CSS, JSON, YAML, TOML, INI, XML, Markdown, Lua, R, Julia,
Elixir, Erlang, Haskell, Scala, Dart, Zig, Nim, V, Clojure, Lisp, OCaml, F#, Groovy,
Solidity, Terraform, Protobuf, GraphQL, Vim script, assembly, LaTeX, diff, Dockerfile,
Makefile, CMake and plain text — each with its keywords, its comment style, its string
rules, its indent and dedent shapes, and how a symbol is recognised in it.

Markup, stylesheets, prose, patches and key/value data get their own line handlers,
because pushing them through a tokeniser built for words and brackets produces something
worse than no colour at all.

The tokeniser works on the **raw** line and escapes each piece as it emits it. The
earlier one did the opposite — escaped first, then matched — and any rule that consumed
a byte on its own could cut an entity in half, which reached the screen as a literal
`&quot;`. Escaping last makes that whole class of bug unreachable. The round trip is
checked against every file in this repository: strip the spans, unescape, and you must
get the source back, character for character. Nearly thirty thousand lines, no drift.

Colour comes from your seed. Six anchor hues — keyword, string, number, function, type,
constant — are each blended into your accent through `color-mix`, so a violet theme gets
violet-leaning strings rather than the same green every editor ships, and there is not one
literal hue in the stylesheet that is not mixed into your colour first.

### Glass

The conversation is grey and flat on purpose. This surface is not.

Every panel is real glass: a translucent fill, a blur of what is behind it, a lit top lip
and a dark bottom one, and a specular highlight that follows the pointer. Behind them,
three very large washes of your accent drift on periods of 47, 61 and 83 seconds —
mutually prime, so the field never visibly repeats — and an SVG turbulence field
displaces what shows through the edges by a couple of pixels, which is what makes a border
read as thick glass rather than as a blurred rectangle.

Panels settle on a spring rather than a line. Tabs arrive with a small blur. Tree rows
reveal in sequence. The line you are typing into flares for a third of a second. A file
the agent has just written to gets one sweep of light in the tree and then stops.

All of it is listed under a single `prefers-reduced-motion` query and all of it stops
there — which the test suite checks by counting running animations, not by reading the
stylesheet.

### The terminal, still

You asked for the agentic terminal to stay, and it does — in both senses.

The full-screen Textual HUD is untouched: `python main.py` still opens it, `/desktop`
opens a window *onto that same session*, and closing the window hands the terminal its
HUD back. One agent core, several surfaces, exactly as before.

There is also exactly **one** shell, whichever surface you are on. The Code surface does
not build a second terminal in its dock; it moves the pane there and hands it back when
you return to the conversation, so the scrollback, the history and the running command are
the same object wherever you happen to be looking at it.

And the window has its own Agent tab that renders the same event stream in the HUD's
grammar: `⏺` opening anything J.A.R.V.I.S. did or said, `⎿` hanging the result under it,
`›` marking what you typed. Markdown is flattened to prose there — fenced code keeps its
body verbatim, so `a * b * c` does not quietly lose its operators to an italics rule.

### How it is built

A local web application, and deliberately so. The interface has to animate at sixty
frames a second and recolour itself completely from one operator-chosen seed — the first
is CSS's home ground and the second is four lines of custom properties in CSS and a
rewrite in every GUI toolkit. It also must not add a hundred megabytes to a project that
installs from a short requirements file.

So the server is **standard library only**: `http.server`, one long-lived `GET` for
Server-Sent Events, and five static files — two stylesheets and three scripts, none of
them minified, none of them built. No Flask, no websockets package, no bundler, no
Electron, nothing new in `requirements.txt`. The window itself is whichever of these
the machine has, best first: `pywebview` if it happens to be installed, otherwise a
Chromium-family browser in `--app` mode (no tabs, no address bar, its own profile), and
failing both, an ordinary tab.

It binds to `127.0.0.1` only, mints a random token at startup, and refuses any request
that does not carry it or that claims a `Host` other than localhost. That last check is
not decoration: J.A.R.V.I.S. runs shell commands, so an unguarded local port would be a
remote code execution hole for any page in the browser.

## The line it opens on

Every time the window opens it says something different.

```
                            J.A.R.V.I.S.

   Evening, Sir. The workshop is warm and nothing is on fire.
```

The line is written by the model — not chosen from a list — and it knows the hour,
the weekday and the name of the workspace it is looking at. If the battery is low or
the processor is pinned, it may mention that instead of saying all is quiet.

The catch is that a local model asked a question takes anywhere between two hundred
milliseconds and never, and a window that waits for it is a window that hangs. So it
does not wait. It keeps a **bank** of sixteen lines in `.jarvis_greetings.json`: opening
the window takes one out and paints it immediately, then asks the model for a new one in
the background and puts it in. When the model is quick the fresh line arrives while the
welcome is still on screen and fades in over the old one — genuinely of this moment. When
the model is slow, missing or mid-download you get a good line anyway and never notice.

Lines are drawn at random rather than in turn, and never the one shown last, so two
launches a minute apart do not read like a rota. The first launch after installing is the
only one with an empty bank; it falls back to three shipped lines, and once the bank has
filled you never see them again.

Whatever the model returns is trimmed before it is shown — fenced blocks, "Here is a
greeting:", quotation marks, markdown, and anything longer than two sentences. And it
is escaped twice over, once for the markup and once for the inline script it is also
written into, because a language model will put a `</script>` in the middle of a
sentence if the conversation happened to wander that way.

Set `GREETING_FROM_MODEL=false` to keep the shipped lines and never ask the model.

## Signing in

Nothing is locked when you install it, and nothing has to be configured to lock
it. Open **Appearance → Security** and set a password. That is the whole thing.

```
SECURITY
● Locked. It asks for your password.
[ Change password ]  [ Remove it ]  [ Lock now ]
› Sign in with Google instead
```

Set a password and the window is locked. Remove it and it is not. There is no
mode to choose, no file to edit, no command to run and no restart — the model is
one sentence long because most people who use this will never open a terminal.

Four characters is enough. This is a lock on a laptop, not a login to a service:
six wrong guesses in a minute and the door stops answering for a minute, which
makes even a four-digit PIN about a day's work to walk. A person who will not
set a PIN sets nothing at all, and nothing is worse.

**Lock now** signs every browser out and shows the lock screen. **Keep me signed
in** on that screen lasts a week rather than a working day — and only until
J.A.R.V.I.S. restarts, because sessions live in memory and none of them is
written down.

### What happens behind it

With a password set, `/` serves a lock screen instead of the application, and
that screen is given the theme and nothing else: no token, no workspace path, no
model name, nothing about the session behind it. Signing in mints an `HttpOnly`,
`SameSite=Strict` cookie, and only then does the browser get the page — which is
what hands out the token. Every route then needs both. The `Host` and `Origin`
checks are untouched underneath: a third layer, not a replacement for the two
below it.

### Google

Google needs a client ID of your own. There is no way around that — every copy
of every application that signs in with Google has one — but the setup is in the
window rather than in a dotfile. Open **Appearance → Security → Sign in with
Google instead**, and it walks you through it:

1. Open [Google Cloud › Credentials](https://console.cloud.google.com/apis/credentials)
2. Create credentials › OAuth client ID › **Desktop app**
3. Paste the client ID into the box

It is OAuth 2.0 with PKCE on a loopback redirect, the flow Google documents for
native applications. This process exchanges the code with Google's token
endpoint directly over TLS, so the ID token arrives over an authenticated
channel rather than through the browser — Google's own documented exception to
verifying its signature locally, and what keeps this inside the standard library
instead of pulling in a JWT stack for one check. Audience, issuer, expiry and
`email_verified` are all still checked.

The first account to sign in claims the window and is remembered; nobody else
gets in after that. `GOOGLE_ALLOWED_ACCOUNTS` names a list instead.

### The password is not in the code

You asked for it saved in the code. It is saved — as a **salted scrypt hash** in
`.jarvis_credentials.json` beside the profile, mode `0600`, ignored by git — and
not as a literal in a Python file.

That is a deliberate departure, and the reason is short: this repository is
pushed to GitHub, so a password written into a source file is a password
published to the internet, along with every other place the same one is used.
Nothing in this project ever writes a password down; the panel reads it, hashes
it and forgets it.

### If you would rather use a terminal

Everything above has a command-line equivalent, and a setting that overrides the
panel for anyone deploying this somewhere shared:

```bash
python main.py set-password        # typed in, never written down
python main.py clear-password
```

| Key | Default | |
|---|---|---|
| `GREETING_FROM_MODEL` | `true` | Whether the model writes the welcome line |
| `DESKTOP_AUTH_MODE` | *(follows what is set up)* | `off` · `password` · `google` · `any` |
| `DESKTOP_AUTH_TTL_HOURS` | `12` | A session that was not asked to be remembered |
| `GOOGLE_CLIENT_ID` | *(empty)* | Overrides whatever was pasted into the panel |
| `GOOGLE_ALLOWED_ACCOUNTS` | *(empty)* | Addresses allowed in |

Write `DESKTOP_AUTH_MODE` down and it wins: the panel says so and stops offering
a switch that would not take. Somebody who spelled it out meant it.

### What it is not

A defence against a hostile process on the same machine. Something running as
you can read the token out of the page or the cookie out of the browser, and
could before any of this existed. What a lock screen defends against is the
laptop left open, which is the threat that actually happens.

---

## Colour

The shipped default is **grey on near-black**, and deliberately so. The accent reaches
the caret, the selected row, a focus ring and a status dot — and nothing else. An
instrument you keep open all day should not be shouting a hue at you, and an interface
where every surface is tinted and every panel glows reads as a demo rather than a tool.

The colour is still there. It is one slider away, and it goes all the way.

```bash
/theme #ff8c42          # a hex code
/theme violet           # a name — 40-odd of them, plus every CSS name
/theme veronica         # a preset, fourteen of them
/theme light            # change the ground, keep the colour
/theme surprise         # random, but never ugly
/theme                  # what am I wearing?
```

Or open the studio (`Ctrl`+`,` in the window) and drag the wheel. Every control is live:
the interface recolours as you move, and the choice is written to `.jarvis_theme.json`
the moment you let go, so it is there next time — **in the terminal HUDs as well**. A
colour is not a desktop-app setting; it is how you want J.A.R.V.I.S. to look.

| Control | |
|---|---|
| **Wheel + brightness** | Hue around, saturation outward, lightness on the slider beneath. |
| **Ground** | `Dark`, `Midnight` (true black, for OLED) or `Light`. |
| **Colour in the background** | How much of your hue bleeds into the surfaces. Ships at **0.10** — near-neutral. Turn it up and it floods the whole room. |
| **Panel separation** | How far the cards stand off the ground. |
| **Glow** | The accent bloom behind live elements and the ambient light in the room. |
| **Corner rounding** | 0 (severe) to 28 (soft). |
| **Type** | System, Grotesk, Rounded, Serif or Monospace. |
| **Secondary accent** | Tool cards and the second bloom. Derived 42° off your accent unless you set it. |

### The two rules

**Your seed is honoured exactly.** `--accent` is the colour you chose, byte for byte.
A colour picker that quietly improves your choice is a colour picker nobody trusts.

**Text is never allowed to be unreadable.** A seed can be any lightness, including ones
that vanish against their background, so every value used for *text* is walked towards
or away from the ground — hue and saturation intact — until it clears WCAG AA (4.5:1).
That variant is published separately as `--accent-text`; the raw seed is left alone.

This is tested rather than asserted. `tests/test_theme.py` derives a full interface from
the eight most awkward seeds — pure black, pure white, yellow, pure blue, mid grey, full
magenta — on all three grounds, and checks every text colour against its background. It
does the same for all fourteen presets and for sixty random `surprise` themes.

```
python main.py --theme "#b6ff3d"      # lime, and everything follows
python main.py --theme surprise
```

The terminal front ends get the same treatment: the derived colours are registered as a
`custom` palette with Rich and with Textual, both of which take `#rrggbb` wherever they
take a colour name. One derivation, three front ends. A fresh install with no theme file
keeps the hand-tuned palettes it shipped with.

---

## The HUD

The default front end takes the whole terminal and follows Claude Code's grammar:
a bullet for anything J.A.R.V.I.S. did or said, its reading hanging underneath,
and a rounded composer at the bottom. Nothing said yet, and the window belongs to
a greeting that has read the machine first — the Claude app's opening, with
something true underneath it.

```
  ✻ J.A.R.V.I.S.   gemma4:31b-cloud
 ────────────────────────────────────────────────────────────────────────────────


                                        ✻

                              Good afternoon, Shiva.
              A filesystem is at 94%. Say the word and I will clear the caches.

                           /help for commands   ·   ? for shortcuts


  ╭──────────────────────────────────────────────────────────────────────────────╮
  │ › ask me anything                                                            │
  ╰──────────────────────────────────────────────────────────────────────────────╯
    ? for shortcuts   ·   / for commands   ·   talk / quiet for speech
```

The greeting is not decoration. It greets by the clock and by name, then reports
the most worth saying: a dying battery, a filesystem at ninety percent, memory
under pressure, a daemon it cannot reach. Only when there is genuinely nothing to
report does it fall back to pleasantry. Ask anything and it steps aside for the
transcript; `/clear` brings it back, which is what a cleared session should look
like.

```
  ✻ J.A.R.V.I.S.   gemma4:31b-cloud · warm
 ────────────────────────────────────────────────────────────────────────────────
  › check the workshop and search for arc reactor efficiency

  ⏺ system_diagnostics(scope=summary)  0.31s
    ⎿  CPU 38%, RAM 71.5%, root disk 94%

  ⠸ web_search(query=arc reactor efficiency)  2s

  ⏺ The workshop is healthy, Sir, with one caveat:

     • Root disk at 94%
     • Memory at 71.5%

    Shall I clear the build caches?▍

   ✹ Cogitating… (4s · ↓ 1.4k tokens · esc to interrupt)
  ╭──────────────────────────────────────────────────────────────────────────────╮
  │ ›                                                                            │
  ╰──────────────────────────────────────────────────────────────────────────────╯
    ? for shortcuts   ·   / for commands   ·   talk / quiet for speech
```

An instrument card appears the moment the call is issued and resolves in place,
so a slow build or a network search shows as running rather than as a gap. The
working line names what it is doing, how long it has been at it, how much has
come back, and how to stop it.

### Live keywords

`talk` and `quiet`, typed on their own, act the moment you send them rather than
going to the model. Nothing in the interface said so, which made them folklore —
so they are now **coloured as you type them**, the way Claude Code lights
`ultrathink`: green for the word that opens the speaker, blue for the one that
closes it. Every alias is lit, `shut up` and `chup kar` included, and the colour
survives into the transcript, so a reply that arrives in silence explains itself
three lines further up. Whole words only: `talkative` and `basement` stay
ordinary prose.

### Keys

| Key | |
|---|---|
| `enter` | Send |
| `esc` | Interrupt the turn in progress |
| `ctrl+c` | Interrupt; at an idle prompt, leave |
| `ctrl+d` | Leave |
| `ctrl+l` | Clear the transcript |
| `ctrl+s` | Speech on / off |
| `ctrl+b` | The instrument panel — vitals, latency, systems |
| `ctrl+r` | Show or hide the model's reasoning |
| `ctrl+p` | Command palette |
| `↑` / `↓` | Walk back through what you have asked |
| `f1` / `?` | Keys and commands |

The mouse works: scroll the transcript, click to focus, select text.

Claude Code has no sidebar, so neither has this one until `ctrl+b` asks for it.
The vitals, the latency meter and the arc reactor are all live behind it, and
`/metrics` reports the same figures inline.

Permission requests arrive as a modal dialog rather than a line of prose, so an
irreversible action cannot be approved by a keystroke aimed at something else.
The transcript keeps four hundred entries mounted and the log keeps all of them.

It steps aside when it should. `--classic` gives you the pinned status strip;
`--no-hud`, `--ask`, `--check` and any non-terminal stdout give you plain lines
that pipe cleanly. Without `textual` installed, the classic HUD is used and the
rest of the system is unaffected.

---

## Why it answers quickly

Four measured changes, all in `jarvis/engine.py`. `--no-turbo` disables the lot
and falls back to the synchronous core in `jarvis/core.py`, which behaves
identically and simply waits more.

**The model is warm before you ask.** At boot, an empty-prompt request loads the
weights into the daemon and `OLLAMA_KEEP_ALIVE` pins them there. The first
question of a session otherwise pays several seconds for something that has
nothing to do with the question.

**The reply is on screen while it is still being written.** Tokens go to the
display as they arrive rather than being buffered and rendered once at the end.
The perceived latency of an answer is when its first word appears, not its last.

**Independent instruments run at the same time.** Two web searches in one turn
cost one web search. Anything marked dangerous — the tools that route through
the permission broker — is still run one at a time, because two consent dialogs
at one terminal is not a race the operator should have to win. Results are
written back in the order the model asked for them regardless of the order they
finish in.

**Ctrl-C actually stops it.** The turn runs as an asyncio task; interrupting
cancels it, which tears down the HTTP read immediately instead of waiting for
the next chunk to arrive before noticing a flag.

Two smaller things: one pooled, keep-alive connection is held open for the
session rather than dialled per turn, and the display coalesces — a model
emitting three hundred tokens a second costs thirty redraws a second, not three
hundred.

`/metrics` reports the last turn:

```
 Measure               Value
 First token           310 ms
 Throughput            68.4 tokens/second
 Model time            3.41 s
 Instrument time       0.62 s
 Instruments           2
 Run in parallel       yes
 Saved by parallelism  0.58 s
 Total                 4.10 s
```

Throughput comes from the daemon's own `eval_count` and `eval_duration` where it
reports them, so it is measured rather than inferred.

---

## Tests

```
pip install pytest pytest-asyncio
pytest -q
```

Four hundred and fifty-nine tests, no Ollama daemon and no microphone, a
little over two minutes. Most need no browser either; the handful in
`tests/test_ui.py` drive the real front end through Playwright and skip
themselves cleanly when it is not installed. The model is a scripted fake and the HUD is driven
through Textual's pilot, so the suite covers streaming, tool parallelism,
cancellation, the transcript, the modal dialogs and the whole boot sequence.

The desktop app is driven over its own HTTP API with `urllib`, exactly as the
front end drives it, so a passing suite means the front end has something real
to talk to. `tests/test_ui.py` goes one further and drives the rendered
interface itself. Every guard there names a bug that was found by using the
interface rather than by reading it:

· the conversation never collapses at any of eight widths, and neither does the
  editor stage;
· nothing spills out of the window, and the page never scrolls sideways;
· a closed pane stays closed across a reload;
· the editor's painted layer is the buffer, character for character, even after
  opening a docstring and undoing it;
· an editor shortcut does not also fire the conversation's — `Ctrl`+`Shift`+`K`
  deleted a line *and* opened the command palette on top of it;
· the terminal is moved between the two surfaces, never duplicated;
· typing into the page keeps the character that moved the focus;
· `Esc` does not interrupt a running turn;
· a collapsed pane paints nothing over the pane beside it — measured on what is
  actually painted, since `getBoundingClientRect` ignores `overflow: hidden`;
· a turn asked from the Code surface appears on the Code surface;
· the welcome line differs from one window to the next, and a greeting carrying
  `</script><script>` cannot run code in the page.

That includes the security properties — no token, wrong token,
a forged `Host`, a cross-origin request and `../` in a static path are each
asserted to be refused, and so are the workspace escapes the editor could
otherwise become. Reads and writes go through one boundary and both are held to
it: `../`, nested traversal, out-of-tree symlinks, an oversized save, a save
with editing switched off, and a save over a file that changed underneath you.

The lock has its own file. `tests/test_auth.py` holds the credential to its
promises — the password never appears in the file that stores it, the same
password hashes differently every time, a corrupt record refuses rather than
crashes — and drives the Google exchange against a local stub that answers the
way Google's token endpoint does, so a token for another client, from another
issuer, past its expiry, on an unverified address, or replayed on a used state
is each refused. `tests/test_desktop.py` then checks the door itself: a valid
token alone opens nothing, the lock screen carries no token, both the cookie and
the token are required, signing out puts the lock back, and the `Host` and
`Origin` checks still apply to a signed-in request. The colour engine is checked by arithmetic rather than against fixed
hex values: every derived text colour must clear WCAG AA against its own
background, for the eight most awkward seeds on all three grounds.

---

## Configuration

Copy `.env.example` to `.env`. Every key is optional; the defaults are already sensible.

| Key | Default | |
|---|---|---|
| `MODEL_NAME` | `gemma4:31b-cloud` | The Ollama model |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Daemon address |
| `MODEL_THINKING` | `false` | Surface the model's reasoning in the HUD |
| `USER_TITLE` / `USER_GENDER` | asked on first run | Form of address |
| `WAKE_WORDS` | `hello jarvis,namaste jarvis,…` | The call phrases |
| `WAKE_ALLOW_BARE_NAME` | `false` | Whether bare "Jarvis" wakes him |
| `TTS_ENGINE` | `auto` | `auto` · `edge-tts` · `pyttsx3` · `none` |
| `TTS_VOICE_GENDER` | `male` | Which voice of each language pair |
| `ENABLED_LOCALES` | `en,hi,bn,te,mr,ta` | Active languages |
| `RESPONSE_LOCALE` | `auto` | Pin a language, or auto-detect |
| `RAM_ALERT_THRESHOLD` | `90` | Ambient alert threshold (%) |
| `SHELL_TOOL_ENABLED` | `true` | Whether `run_command` exists |
| `DESKTOP_SHELL_ENABLED` | `true` | Whether the window has a terminal at all |
| `DESKTOP_EDIT_ENABLED` | `true` | Whether the editor may write to disk |
| `DESKTOP_EDIT_MAX_BYTES` | `2000000` | Ceiling on one save |
| `DESKTOP_AUTH_MODE` | *(follows the Security panel)* | See [Signing in](#signing-in) |
| `WORKSPACE_ROOT` | project directory | The boundary he may act inside freely |
| `PERMISSION_MODE` | `ask` | `ask` · `allow` · `deny` for everything beyond it |
| `ALLOWED_APPS` | *(empty)* | Applications that never need approval |
| `VERONICA_DRY_RUN` | `true` | Report rather than terminate |

---

## Architecture

```
config.py            Pydantic settings, palettes, states, the operator profile
main.py              Entry point: CLI, onboarding, wiring, REPL, shutdown
jarvis/
  core.py            ReAct loop over Ollama, streaming, conversation memory
  engine.py          The fast path: async client, warm-up, parallel instruments
  tools.py           Tool registry, JSON schemas, the eight instruments
  protocols.py       Stark Protocol engine and the three macros
  voice.py           Wake words, background listening, non-blocking TTS
  monitor.py         Telemetry and the ambient alert thread
  languages.py       Programming-language toolchain registry and detection
  locales.py         Human languages: detection, voices, native phrases
  permissions.py     The consent layer for anything outside the workspace
  apps.py            Resolving, launching and closing applications
  speaker.py         Voiceprint enrolment and verification (pure numpy)
  daemon.py          The background wake listener
  ui.py              The classic HUD: pinned status strip above a scrolling shell
  tui.py             The full-screen HUD: transcript, instruments, modals
  desktop.py         The desktop HUD: local server, event stream, window
  auth.py            The lock on the window: scrypt passwords, Google, sessions
  shell.py           The Terminal pane's shell: PowerShell, bash, zsh
  theme.py           The colour engine: one seed in, a whole interface out
  web/
    index.html       The desktop front end: one page, two surfaces
    signin.html      The lock screen: self-contained, and given no token
    app.css          The conversation
    app.js           The conversation: transcript, composer, rail, shell, theme
    ide.css          The Code surface: glass, the editor, the panels
    ide.js           The Code surface: editor, tabs, search, outline, problems
    lang.js          54 language grammars and the tokeniser they drive
  prompts.py         System prompt and personality
jarvis-desktop       Launcher script (jarvis-desktop.bat on Windows)
tests/               Engine, HUD, colour, desktop and boot tests; no daemon, no
                     microphone, no browser
```

The import graph is strictly acyclic. `config` sits at the bottom; `monitor`, `ui`, `tui`,
`voice`, `languages` and `locales` import nothing from the project but `config`; `ui` and
`voice` objects reach the rest of the system only as constructor arguments, always
duck-typed and always optional. `tui.py` presents exactly the interface `ui.py` presents,
method for method, so the agent, the monitor, the voice system and the permission broker
bind to whichever display was chosen without knowing which one they got — and `engine.py`
subclasses `core.py` rather than replacing it, so a turn behaves the same on either path.

`desktop.py` is the third front end and joins on the same terms: `DesktopHUD` presents
that identical interface, so the agent has no idea it is not talking to a terminal, and
`theme.py` sits beside `config` at the bottom of the graph importing nothing but the
standard library — which is why one derivation can serve Rich, Textual and CSS at once.

Every module degrades rather than raising: no microphone
means a text TUI, no Ollama means diagnostics and protocols still work, no compiler for a
language means a clear refusal with an install hint, and a desktop app with no browser to
open prints its address and waits.

---

## Troubleshooting

**"I cannot reach the Ollama daemon."** The desktop app does not start the API server on
this machine. Run `& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve`, then
`main.py --check`.

**No microphone.** He drops to text mode automatically and says so. `--list-mics` shows
the devices; set `MIC_INDEX` to pick one.

**PyAudio will not install.** The usual Windows failure, and genuinely optional — voice
input simply switches off. On Debian/Ubuntu: `sudo apt install portaudio19-dev`. On
macOS: `brew install portaudio`.

**No speech.** Check `/voice`. edge-tts needs a network connection and a player; `ffplay`
is present here. Without either, it falls back to pyttsx3 (US English only). On Linux,
pyttsx3 needs `espeak`.

**Boxes and question marks instead of Devanagari.** The console is not UTF-8. `main.py`
reconfigures stdout automatically; if your terminal still cannot cope, use Windows
Terminal or run `chcp 65001`. The HUD falls back to ASCII glyphs on its own.

**The HUD disappears while I type / my typing gets mangled by an alert.** It should
not any more: `prompt_toolkit` keeps the status strip pinned as a bottom toolbar during
input and routes background output above the prompt. If it is missing
(`pip install prompt-toolkit`) the HUD falls back to suspending the live region, which
is the older, blinkier behaviour. Arrow keys give you history and Tab completes slash
commands.

**He does not answer to the call word.** Wake detection is pinned to `en-IN`, which was
measured to be the only recogniser language that transcribes both English and spoken
Hindi well enough to match every call phrase — `hi-IN` renders "Hello Jarvis" as
`हेलो जार्विस` and misses it. If he still does not answer, check `/voiceprint`: a print
enrolled in a noisy room can be over-tight. `/voiceprint forget` removes the gate.

**A language reports absent that you know is installed.** The probe result is cached for
24 hours. `/toolchains refresh` re-probes everything.

---

## A note on `code_executor`

It runs code in a throwaway directory, in a subprocess, with a timeout. That is **process
isolation, not a security sandbox** — the code executes with this application's own
privileges and can reach your filesystem and network. Veronica lockdown blocks obviously
destructive source, and `run_command` keeps a refusal list, but both are seatbelts rather
than airbags. Treat it as you would treat running a script somebody sent you.

---

*"Sometimes you gotta run before you can walk."*
