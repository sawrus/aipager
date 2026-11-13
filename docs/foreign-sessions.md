# Claude sessions started without aipager (fork `sawrus/aipager`)

aipager drives the Claude Code sessions it runs under `dtach`: it types into
their terminal and reads their hooks. A `claude` you start in a plain
terminal is out of its reach. This fork closes the gap two ways.

## Pick up a conversation: `/resume` and `aipager resume --claude`

`/resume` shows **🔎 Other Claude sessions** to the chat's owner and admins.
It lists the Claude Code conversations with activity in the last 14 days that
aipager does not track, newest first, ten per page: the title as
`<folder> # <task>` (the task is the name you gave it, else the short title
Claude keeps current as the work goes on, else Claude's summary, else the
first words you typed; `claude -p` runs, such as scripted code reviews,
are not listed), the folder, how long ago, and whether a terminal still has it open (waiting, or
working right now). Tap one and aipager continues it under `dtach` with
`claude --resume <id>` in its folder; questions, answers and permission
prompts come to Telegram from then on. The session is named after the task
(`fix_login_bug`), else after the folder (`my_proj`), then `_2`, `_3`, ...
when the name is taken.

`aipager resume --claude` is the same list in the terminal (`n`, `p`, `q`,
or a number).

**Still open in a terminal.** aipager cannot type into a terminal it did not
start, so the terminal's claude must close first. aipager asks (`Take over`
/ `Cancel`, or `[y/N]` in the CLI), then sends that claude SIGTERM, waits up
to 10 seconds and continues the conversation here. Claude Code saves the
conversation as it exits, so nothing is lost. A terminal claude that is
working right now is never taken over: try again when it waits. One that
does not exit within 10 seconds is left alone, and nothing changes. The
signal goes only to the process you confirmed: if another process holds the
conversation by then, aipager asks again.

A conversation aipager already runs is never picked up a second time.

**Hooks must run there.** aipager sees a session only through its hooks. So
before it closes anything or starts anything, aipager reads the Claude Code
settings files that can turn hooks off for the conversation's folder:
managed settings (`/etc/claude-code/managed-settings.json` and
`managed-settings.d/`), `~/.claude/settings.json` (or the one
`CLAUDE_CONFIG_DIR` points to), `<folder>/.claude/settings.json` and
`<folder>/.claude/settings.local.json`. Precedence is Claude Code's: managed,
then local, then project, then user. When the file that decides says
`"disableAllHooks": true`, aipager refuses and names that file; the terminal
claude stays open and nothing is started. `aipager resume --claude` does the
same check before its `[y/N]`.

A session can still come up with no hooks for another reason (a hook command
that fails, a settings layer aipager does not read). So after the start
aipager waits up to 30 seconds (`ADOPT_HOOK_WAIT_SECONDS`) for the first hook
(Claude Code's `SessionStart`). If none comes, aipager stops its copy, drops
the session and says so. The terminal claude is already closed at that
point: continue the conversation in a terminal with `claude --resume <id>`.
A plain `/resume` of a session aipager already ran does not do these checks.

## One owner per conversation

`claude --resume <id>` starts a new process; it does not join the one that
has the conversation open. Two processes on one conversation both write to
its transcript. So every resume closes the other copies first:

- `/resume <name>`, the resume picker, `aipager resume`, `aipager resume
  --claude`, `aipager session <name> -- --resume <id>`, and
  `aipager-claude --resume <id>` (also when the daemon is not running);
- each live interactive claude in Claude Code's session registry
  (`~/.claude/sessions/<pid>.json`) with that conversation id gets SIGTERM,
  then SIGKILL if it is still there after 5 seconds; its own process line
  and a pid whose start time no longer matches are left alone;
- when that claude ran in a tmux pane (the registry's `tmux` field) and only
  an idle shell is left in it, the pane is closed too;
- when that claude ran in a herdr terminal (its
  `HC_TERMINAL_ID`, and the terminal's program is the claude or one of its
  ancestors), the terminal is closed too with `herdr-core kill` and `rm`:
  when it finished, when only an idle shell is left in it, or when herdr's
  `keep` put a fresh shell in place of the claude. A terminal on another
  machine (`vps/t7`) or one busy with another program stays;
- a claude that outlives both signals stops the resume: nothing is launched.

The other direction works the same: a resume of the conversation in a tmux
window closes aipager's copy, and aipager marks that session gone. A
relaunch of a session aipager runs (a mode change) keeps its own claude: the
relaunch closes it itself.

## Start every terminal session under aipager: `aipager wrap`

```
aipager wrap install     # adds `alias claude=aipager-claude` to ~/.bashrc (interactive shells only)
aipager wrap status
aipager wrap uninstall
```

With it, `claude` in a project folder runs `aipager session <folder>_N --
<your args>`: the terminal works as before (detach with Ctrl-\), and the
session is in Telegram from the start. `aipager-claude` runs the real claude
directly when that is what you meant: inside an aipager session, when input
or output is not a terminal, with `-p`/`--print`, `-v`/`--version`,
`-h`/`--help`, for Claude Code's own subcommands (`mcp`, `config`, `doctor`,
`update`, `install`, `agents`, `stop`, `attach`, `daemon`, `auth`, `plugin`,
...), and when the aipager daemon is not running. It takes the real claude
from `claude_path` in `aipager.yaml` when that is set (instant), else from
aipager's resolver. aipager itself always starts claude by its full path, so
the alias never loops.
