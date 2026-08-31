# J.A.R.V.I.S.

**Just A Rather Very Intelligent System** — a modular workshop AI that runs locally on
Ollama, speaks with a British neural voice, watches your hardware without being asked,
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
| **Stark HUD** | A live terminal display: animated Unicode soundwave, colour-coded state, real-time CPU/RAM/disk/battery/GPU gauges pinned above the prompt. Four palettes that swap when a protocol engages. |
| **Wake-word voice** | Say **"Hello J.A.R.V.I.S."** or **"Namaste, J.A.R.V.I.S."** Background listening, a rising chime, and he never answers his own echo. |
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

```powershell
cd C:\Users\Shiva\J.A.R.V.I.S._AI

# 1. Make sure the Ollama API server is actually running (see the note below)
ollama serve

# 2. Launch
.venv\Scripts\python.exe main.py
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

The call words are exactly two:

| Say | |
|---|---|
| **"Hello J.A.R.V.I.S."** | English |
| **"Namaste, J.A.R.V.I.S."** | and its native forms — नमस्ते जार्विस, নমস্তে জার্ভিস, வணக்கம் ஜார்விஸ் |

Speech recognition never returns the dots, so `Hello J.A.R.V.I.S.`, `hello jarvis` and
even `hello j a r v i s` all normalise to the same phrase. Common mishearings of the name
(`jervis`, `javis`, `jarwis`) are accepted too.

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
| `/theme <palette>` | `standard`, `house_party`, `veronica`, `clean_slate` |
| `/title <value>` | Change how he addresses you |
| `/model` · `/history` · `/clear` | Model info · transcript · reset context |
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
| `--no-hud` · `--no-monitor` | Plain output · no ambient thread |
| `--talk` | Start in speaking mode instead of silent |
| `--allow-all` | Approve every out-of-workspace action without prompting (scripting only) |
| `--enroll-voice` | Record your voice sample, then exit |
| `--forget-voice` | Delete the stored voiceprint |
| `--daemon` / `--listen` | Background listener; opens a terminal on the call word |
| `--reset-profile` | Forget the form of address and ask again |
| `--debug` | Log at DEBUG |

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
  ui.py              The HUD: palettes, waveform, gauges, panels
  prompts.py         System prompt and personality
```

The import graph is strictly acyclic. `config` sits at the bottom; `monitor`, `ui`,
`voice`, `languages` and `locales` import nothing from the project but `config`; `ui` and
`voice` objects reach the rest of the system only as constructor arguments, always
duck-typed and always optional. Every module degrades rather than raising: no microphone
means a text TUI, no Ollama means diagnostics and protocols still work, no compiler for a
language means a clear refusal with an install hint.

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
